"""Config flow for PetSafe FreshFeed.

"Sign in with PetSafe" (offered only when the app profile is bundled): one
  account sign-in per submit, at most one submit per 60 s per Home Assistant
  instance. The selected feeder's device ID and local key, plus a located or
  typed RFC 1918 address, become a PetSafe-owned local connection only after a
  transient, authenticated, read-only protocol 3.5 status admission that is
  fully drained before the entry is created. Tuya Local is not used. The entry
  (unique ID: the physical device ID) starts read-only; its setup creates the
  PetSafe-owned registry leaf, and controls are enabled later, explicitly, in
  the options flow against that leaf. The password is used only to build a
  single-use ``SignIn`` and is never kept; no email, password or session is
  persisted. Nothing about the account, the key, the device ID or the host is
  logged here.
Controls are opt-in. Choosing ``enable_controls`` (in
the options flow for an existing read-only entry) is the ONLY path that
provisions the retained-intent store. It provisions exactly once with a fresh
random store ID under the entry's registry leaf, and persists the IDs only after
provisioning succeeded, merged into the existing entry data. Any failure or
cancellation persists nothing and is never retried, adopted or repaired here;
ordinary startup only opens.

Host reconfigure (owned entries): under a per-entry transition gate (which the
update listener honours) the entry is unloaded (refusal aborts), the new host is
admitted by the same transient probe, only the host changes, and the entry is
set up exactly once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import functools
import ipaddress
import time
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import account, lan_locate, store_bootstrap
from .owned_source import (
    SUPPORTED_PROTOCOLS, UNSUPPORTED_CONFIGURATION, LocalConnection, LocalConnectionError,
    OwnedSource, OwnedSourceError,
)
from .source_lookup import owned_binding_id
from .const import (
    BACKEND_OWNED,
    CONF_CONNECTION_BACKEND,
    CONF_DEVICE_ID,
    CONF_ENABLE_CONTROLS,
    CONF_HOST,
    CONF_INTENT_BINDING_ID,
    CONF_INTENT_STORE_ID,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
    INTENT_STORE_ROOT,
    SOURCE_DOMAIN,
    TRANSITIONS_KEY,
)

_CONTROLS = vol.Optional(CONF_ENABLE_CONTROLS, default=False)

# --- account sign-in path -------------------------------------------------------

CONF_COUNTRY = "country"
CONF_FEEDER = "feeder"
# The admitted local protocol (owned_source.SUPPORTED_PROTOCOLS); never downgraded.
OWNED_PROTOCOL = "3.5"
# Completion text (config.create_entry) explaining the separate controls step.
CREATED_READ_ONLY = "owned_read_only"
ADMISSION_FAILED = "local_admission_failed"
# hass.data[DOMAIN] keys: the cached app-profile check and the last sign-in submit.
_PROFILE_KEY = "account_app_profile"
_SUBMIT_AT_KEY = "account_submit_at"
# Account-lock protection: at most one password-bearing submit per window per hass.
_CLOUD_DISABLE_GENERATIONS = "_cloud_disable_generations"
SUBMIT_COOLDOWN_S = 60.0
DISCOVER_DEADLINE_S = 30.0
# Indirection so tests can drive the cooldown with a fake clock.
_monotonic = time.monotonic
_NO_PROFILE = object()

_COUNTRY_CODES = frozenset(code for code, _label in account.SUPPORTED_COUNTRIES)
_OUTCOME_ERRORS = {
    account.Outcome.REJECTED: "invalid_auth",
    account.Outcome.CHALLENGE: "challenge_required",
    account.Outcome.UNSUPPORTED_ROUTE: "unsupported_region",
    account.Outcome.UNSUPPORTED_PROFILE: "unsupported_region",
    account.Outcome.TIMEOUT: "timeout",
}
_LAN_NETWORKS = tuple(ipaddress.IPv4Network(net)
                      for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


async def _async_app_profile(hass):
    """The bundled app profile, or None. Read once per hass in the executor."""
    data = hass.data.setdefault(DOMAIN, {})
    if _PROFILE_KEY not in data:
        try:
            profile = await hass.async_add_executor_job(account.load_app_profile)
        except Exception:  # noqa: BLE001 - any failure hides the account option
            profile = _NO_PROFILE
        data[_PROFILE_KEY] = profile
    profile = data[_PROFILE_KEY]
    return None if profile is _NO_PROFILE else profile


def _lan_ipv4(value: Any) -> str | None:
    """The address when it is an RFC 1918 IPv4 address, else None."""
    if type(value) is not str:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if type(address) is not ipaddress.IPv4Address:
        return None
    if not any(address in network for network in _LAN_NETWORKS):
        return None
    return str(address)


def _locate(device_id: str):
    """Blocking LAN locate; any failure means the user types the address."""
    try:
        return lan_locate.locate(device_id)
    except Exception:  # noqa: BLE001
        return None


async def _async_probe(hass, connection: LocalConnection) -> bool:
    """Transient authenticated read-only admission; True only once fully drained.

    One owned source: its admission is the authenticated 3.5 session and one
    decoded status (a decoded device identity must match). It is stopped at
    once; a stop that cannot drain actual I/O keeps its claim and fails here.
    """
    source = OwnedSource(hass, connection)
    try:
        await source.async_start()
    except OwnedSourceError:
        return False
    return await source.async_stop() is True


async def _async_provision(hass, leaf_id: str | None) -> dict[str, str] | str:
    """Provision once under an existing leaf; return the IDs to persist, or a fixed reason."""
    if leaf_id is None:
        return "binding_unavailable"
    store_id = uuid4().hex
    try:
        await store_bootstrap.async_provision_store(
            hass.config.path(INTENT_STORE_ROOT), binding_id=leaf_id, store_id=store_id)
    except (store_bootstrap.StoreBootstrapError, ValueError):
        return "provision_failed"
    return {CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: leaf_id}


def _physical_ids(entry: ConfigEntry) -> set:
    """Physical device IDs named by active config data or options."""
    found = set()
    for container in (entry.data, entry.options):
        if isinstance(container, Mapping):
            value = container.get(CONF_DEVICE_ID)
            if type(value) is str and value:
                found.add(value)
    return found


def _duplicate_reason(hass, device_id: str) -> str | None:
    """Explicit duplicate scan for one physical feeder, before any connection.

    Owned and migrated PetSafe entries name the device directly; legacy entries
    are resolved through their Tuya Local source entry (disabled or ignored
    included), since the unique ID schemes differ. Any other enabled Tuya Local
    entry for the device already holds its one local connection. An ignored
    PetSafe entry does not block a manual setup.
    """
    for ours in hass.config_entries.async_entries(DOMAIN, include_ignore=False):
        if ours.unique_id == device_id or device_id in _physical_ids(ours):
            return "already_configured"
        source_id = ours.data.get(CONF_SOURCE_ENTRY_ID)
        source = (hass.config_entries.async_get_entry(source_id)
                  if type(source_id) is str else None)
        if source is not None and source.domain == SOURCE_DOMAIN and device_id in _physical_ids(source):
            return "already_configured"
    for other in hass.config_entries.async_entries(
            SOURCE_DOMAIN, include_ignore=False, include_disabled=False):
        if device_id in _physical_ids(other):
            return "tuya_local_conflict"
    return None


class FreshFeedConfigFlow(ConfigFlow, domain=DOMAIN):
    """PetSafe account sign-in that creates an owned entry."""

    VERSION = 1
    MINOR_VERSION = 2
    _cloud_session = None
    _reauth_entry = None

    async def async_step_reauth(self, entry_data):
        self._reauth_entry = self._get_reauth_entry()
        generations = self.hass.data.setdefault(DOMAIN, {}).setdefault(_CLOUD_DISABLE_GENERATIONS, {})
        self._reauth_generation = generations.get(self._reauth_entry.entry_id, 0)
        if self._reauth_entry.data.get(CONF_CONNECTION_BACKEND) != BACKEND_OWNED:
            return self._abort("legacy_backend_removed")
        return await self.async_step_account()

    # Account-path state; lives only until the flow finishes or is removed.
    _feeders: tuple = ()
    _feeder = None
    _located = None
    _host: str | None = None
    _country: str | None = None
    _confirming = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FreshFeedOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_account(user_input)

    # --- account path -------------------------------------------------------------

    @callback
    def _clear(self) -> None:
        """Drop the feeder material and every other account-path value."""
        self._feeders = ()
        self._feeder = None
        self._located = None
        self._host = None
        self._cloud_session = None

    @callback
    def async_remove(self) -> None:
        self._clear()

    @callback
    def _abort(self, reason: str) -> ConfigFlowResult:
        self._clear()
        return self.async_abort(reason=reason)

    def _account_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        countries = [SelectOptionDict(value=code, label=label)
                     for code, label in account.SUPPORTED_COUNTRIES]
        country = vol.Required(CONF_COUNTRY) if self._country is None else vol.Required(
            CONF_COUNTRY, default=self._country)
        # Neither the email nor the password is ever filled back in.
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({
                country: SelectSelector(SelectSelectorConfig(
                    options=countries, mode=SelectSelectorMode.DROPDOWN)),
                vol.Required(CONF_EMAIL): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD,
                                       autocomplete="current-password")),
            }),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Remove credentials before the first cancellable profile lookup.
        submitted_password = user_input.pop(CONF_PASSWORD, None) if user_input is not None else None
        profile = await _async_app_profile(self.hass)
        if self._reauth_entry is not None:
            generations = self.hass.data.setdefault(DOMAIN, {}).setdefault(_CLOUD_DISABLE_GENERATIONS, {})
            if generations.get(self._reauth_entry.entry_id, 0) != self._reauth_generation:
                if user_input is not None:
                    user_input.pop(CONF_PASSWORD, None)
                return self._abort("aborted")
        if profile is None:
            if user_input is not None:
                user_input.pop(CONF_PASSWORD, None)
            return self._abort("account_unavailable")
        # Take the password out of the submitted mapping at once; it is used only to
        # build the single-use SignIn below.
        password = None if user_input is None else user_input.pop(CONF_PASSWORD, None)
        password = submitted_password
        del submitted_password
        if user_input is None:
            return self._account_form({})
        email = user_input.get(CONF_EMAIL)
        country = user_input.get(CONF_COUNTRY)
        if country in _COUNTRY_CODES:
            self._country = country
        if (country not in _COUNTRY_CODES or type(email) is not str or not email.strip()
                or type(password) is not str or not password):
            return self._account_form({"base": "invalid_auth"})
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        now = _monotonic()
        last = domain_data.get(_SUBMIT_AT_KEY)
        if last is not None and now - last < SUBMIT_COOLDOWN_S:
            return self._account_form({"base": "rate_limited"})
        domain_data[_SUBMIT_AT_KEY] = now
        request = account.SignIn(email.strip(), password, country)
        del password, email
        try:
            discovery = await self.hass.async_add_executor_job(functools.partial(
                account.discover, request, profile=profile, deadline_s=DISCOVER_DEADLINE_S))
        except Exception:  # noqa: BLE001 - discover does not raise; never surface text
            discovery = None
        del request
        if discovery is None:
            return self._account_form({"base": "cannot_connect"})
        if discovery.outcome is not account.Outcome.OK:
            return self._account_form(
                {"base": _OUTCOME_ERRORS.get(discovery.outcome, "cannot_connect")})
        feeders = tuple(discovery.feeders)
        self._cloud_session = discovery.session
        del discovery
        if self._reauth_entry is not None:
            entry = self._reauth_entry
            generations = self.hass.data.setdefault(DOMAIN, {}).setdefault(_CLOUD_DISABLE_GENERATIONS, {})
            if generations.get(entry.entry_id, 0) != self._reauth_generation:
                return self._abort("aborted")
            if not any(f.device_id == entry.data.get(CONF_DEVICE_ID) for f in feeders):
                self._cloud_session = None
                return self._account_form({"base": "wrong_account"})
            if self._cloud_session is None:
                return self._account_form({"base": "invalid_auth"})
            data = {**entry.data, "cloud_session": self._cloud_session.storage_data(),
                    "cloud_route": self._cloud_session.storage_route()}
            from .repairs import async_cloud_auth_issue
            async_cloud_auth_issue(self.hass, entry, False)
            self._clear()
            return self.async_update_reload_and_abort(
                entry, data_updates=data,
                options={**entry.options, "cloud_read_enabled": True})
        if not feeders:
            return self._account_form({"base": "no_feeders"})
        if len(feeders) == 1:
            self._feeder = feeders[0]
            del feeders
            return await self._async_after_feeder()
        self._feeders = feeders
        del feeders
        return await self.async_step_feeder()

    async def async_step_feeder(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._feeders:
            return self._abort("aborted")
        indexes = {str(index): feeder for index, feeder in enumerate(self._feeders)}
        if user_input is not None:
            feeder = indexes.get(user_input.get(CONF_FEEDER))
            if feeder is None:
                return self._abort("aborted")
            self._feeder = feeder
            # Only the chosen feeder's material is kept from here on.
            self._feeders = ()
            del indexes
            return await self._async_after_feeder()
        options = [SelectOptionDict(value=index, label=feeder.name)
                   for index, feeder in indexes.items()]
        return self.async_show_form(
            step_id="feeder",
            data_schema=vol.Schema({vol.Required(CONF_FEEDER): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))}),
        )

    async def _async_after_feeder(self) -> ConfigFlowResult:
        device_id = self._feeder.device_id
        # Before any connection: one owner per physical feeder.
        duplicate = _duplicate_reason(self.hass, device_id)
        if duplicate is not None:
            return self._abort(duplicate)
        # Advisory only (broadcasts can be forged): admission authenticates later.
        located = await self.hass.async_add_executor_job(_locate, device_id)
        del device_id
        if located is None:
            return await self.async_step_host()
        if located.version not in SUPPORTED_PROTOCOLS:
            # Explicitly unsupported protocol: never downgraded, never probed.
            return self._abort(UNSUPPORTED_CONFIGURATION)
        self._located = located
        self._host = None
        return await self.async_step_confirm()

    def _host_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="host",
            data_schema=vol.Schema({vol.Required(CONF_HOST): TextSelector()}),
            errors=errors,
        )

    async def async_step_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._feeder is None:
            return self._abort("aborted")
        if user_input is not None:
            host = _lan_ipv4(user_input.get(CONF_HOST))
            if host is not None:
                self._host = host
                self._located = None
                return await self.async_step_confirm()
            return self._host_form({CONF_HOST: "invalid_host"})
        return self._host_form({})

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        feeder = self._feeder
        if feeder is None:
            return self._abort("aborted")
        # A second submit while the first is still working changes nothing.
        if user_input is None or self._confirming:
            return self.async_show_form(
                step_id="confirm", data_schema=vol.Schema({}),
                description_placeholders={"name": feeder.name})
        self._confirming = True
        try:
            host = self._located.host if self._located is not None else self._host
            if host is None:
                return self._abort("aborted")
            await self.async_set_unique_id(feeder.device_id)
            self._abort_if_unique_id_configured()
            duplicate = _duplicate_reason(self.hass, feeder.device_id)
            if duplicate is not None:
                return self._abort(duplicate)
            try:
                connection = LocalConnection(feeder.device_id, host, feeder.local_key,
                                             OWNED_PROTOCOL)
            except LocalConnectionError as err:
                code = str(err)  # fixed code, never the rejected value
                return self._abort(code)
            admitted = await _async_probe(self.hass, connection)
            del connection
            if not admitted:
                # Stale host, forged discovery, wrong key or another device: nothing
                # is created; the user may correct the address (no new sign-in).
                self._located = None
                self._host = None
                return self._host_form({"base": ADMISSION_FAILED})
            # Re-checked after the probe: another flow may have finished meanwhile.
            self._abort_if_unique_id_configured()
            duplicate = _duplicate_reason(self.hass, feeder.device_id)
            if duplicate is not None:
                return self._abort(duplicate)
            data = {
                CONF_CONNECTION_BACKEND: BACKEND_OWNED,
                CONF_DEVICE_ID: feeder.device_id,
                CONF_LOCAL_KEY: feeder.local_key,
                CONF_HOST: host,
                CONF_PROTOCOL_VERSION: OWNED_PROTOCOL,
            }
            if self._cloud_session is not None:
                data["cloud_session"] = self._cloud_session.storage_data()
                data["cloud_route"] = self._cloud_session.storage_route()
            title = feeder.name
            del feeder
            self._clear()
            return self.async_create_entry(
                title=title, data=data, options={"cloud_read_enabled": True},
                description=CREATED_READ_ONLY,
                description_placeholders={"name": title})
        except AbortFlow:
            self._clear()
            raise
        except Exception:  # noqa: BLE001 - never surface exception text or args
            return self._abort("aborted")
        finally:
            self._confirming = False

    # --- owned host reconfigure -----------------------------------------------------

    def _reconfigure_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        # The current address is not filled in or shown.
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_HOST): TextSelector()}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        data = entry.data
        if data.get(CONF_CONNECTION_BACKEND) != BACKEND_OWNED:
            return self.async_abort(reason="legacy_backend_removed")
        if data.get(CONF_PROTOCOL_VERSION) not in SUPPORTED_PROTOCOLS:
            return self.async_abort(reason=UNSUPPORTED_CONFIGURATION)
        if user_input is None:
            return self._reconfigure_form({})
        host = _lan_ipv4(user_input.get(CONF_HOST))
        if host is None:
            return self._reconfigure_form({CONF_HOST: "invalid_host"})
        try:
            connection = LocalConnection(data.get(CONF_DEVICE_ID), host, data.get(CONF_LOCAL_KEY),
                                         data.get(CONF_PROTOCOL_VERSION))
        except LocalConnectionError as err:
            code = str(err)  # fixed code, never the rejected value
            return self.async_abort(reason=code)
        transitions = self.hass.data.setdefault(DOMAIN, {}).setdefault(TRANSITIONS_KEY, set())
        if entry.entry_id in transitions:
            return self.async_abort(reason="reconfigure_in_progress")
        transitions.add(entry.entry_id)
        try:
            enabled = entry.disabled_by is None
            if entry.state is not ConfigEntryState.NOT_LOADED:
                try:
                    unloaded = await self.hass.config_entries.async_unload(entry.entry_id)
                except Exception:  # noqa: BLE001 - not unloadable now: refuse
                    unloaded = False
                if not unloaded or entry.state is not ConfigEntryState.NOT_LOADED:
                    return self.async_abort(reason="unload_failed")
            # The old connection is drained; one transient probe of the new host.
            admitted = await _async_probe(self.hass, connection)
            del connection
            if admitted:
                # Same entry, leaf and store/binding IDs: only the host changes.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_HOST: host})
            if enabled:
                await self.hass.config_entries.async_setup(entry.entry_id)
        finally:
            transitions.discard(entry.entry_id)
        if not admitted:
            return self._reconfigure_form({"base": ADMISSION_FAILED})
        return self.async_abort(reason="reconfigure_successful")


class FreshFeedOptionsFlow(OptionsFlow):
    """Enable owned controls explicitly; never clear or replace retained storage."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if entry.data.get(CONF_CONNECTION_BACKEND) != BACKEND_OWNED:
            return self.async_abort(reason="legacy_backend_removed")
        if user_input is not None and "cloud_read_enabled" in user_input:
            if user_input["cloud_read_enabled"] is False:
                generations = self.hass.data.setdefault(DOMAIN, {}).setdefault(_CLOUD_DISABLE_GENERATIONS, {})
                generations[entry.entry_id] = generations.get(entry.entry_id, 0) + 1
                # A pending sign-in must not send a password after Fully local,
                # nor block a fresh flow when cloud reads are enabled again.
                for flow in self.hass.config_entries.flow.async_progress_by_handler(DOMAIN):
                    context = flow.get("context", {})
                    if (context.get("source") == SOURCE_REAUTH
                            and context.get("entry_id") == entry.entry_id):
                        self.hass.config_entries.flow.async_abort(flow["flow_id"])
            if user_input["cloud_read_enabled"] is False and (
                entry.options.get("cloud_read_enabled", False)
                or "cloud_session" in entry.data or "cloud_route" in entry.data
            ):
                helper = getattr(getattr(entry, "runtime_data", None), "cloud_read", None)
                if helper is not None and hasattr(helper, "invalidate"):
                    # Fence a read completing before close() runs.
                    helper.invalidate()
                adapter = getattr(getattr(entry, "runtime_data", None), "adapter", None)
                if adapter is not None:
                    adapter.clear_cloud_evidence()
                data = dict(entry.data)
                data.pop("cloud_session", None)
                data.pop("cloud_route", None)
                options = {**entry.options, "cloud_read_enabled": False}
                self.hass.config_entries.async_update_entry(entry, data=data, options=options)
                from .repairs import async_cloud_auth_issue
                async_cloud_auth_issue(self.hass, entry, False)
                if helper is not None:
                    await asyncio.shield(asyncio.create_task(helper.close()))
                if (user_input.get(CONF_ENABLE_CONTROLS) is True
                        and await self._controls_refusal() != "controls_already_enabled"):
                    return await self._async_controls(user_input)
                return self.async_create_entry(data=options)
            if user_input["cloud_read_enabled"] is True and not entry.options.get("cloud_read_enabled", False):
                if (user_input.get(CONF_ENABLE_CONTROLS) is True
                        and await self._controls_refusal() != "controls_already_enabled"):
                    result = await self._async_controls(user_input)
                    if result["type"] != "create_entry":
                        return result
                entry.async_start_reauth(self.hass)
                return self.async_abort(reason="cloud_sign_in")
        if user_input is not None:
            return await self._async_controls(user_input)
        return self._controls_form("init")

    async def _controls_refusal(self) -> str | None:
        entry = self.config_entry
        if entry.data.get(CONF_CONNECTION_BACKEND) != BACKEND_OWNED:
            return "legacy_backend_removed"
        if CONF_INTENT_STORE_ID in entry.data or CONF_INTENT_BINDING_ID in entry.data:
            return "controls_already_enabled"
        return None

    def _controls_form(self, step_id: str) -> ConfigFlowResult:
        schema = {_CONTROLS: bool}
        if step_id == "init":
            schema[vol.Optional("cloud_read_enabled", default=self.config_entry.options.get(
                "cloud_read_enabled", False))] = bool
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(schema))

    async def async_step_controls(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            refusal = await self._controls_refusal()
            if refusal is not None:
                return self.async_abort(reason=refusal)
            return self._controls_form("controls")
        return await self._async_controls(user_input)

    async def _async_controls(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input.get(CONF_ENABLE_CONTROLS) is not True:
            return self.async_create_entry(data=dict(entry.options))
        refusal = await self._controls_refusal()
        if refusal is not None:
            return self.async_abort(reason=refusal)
        leaf_id = owned_binding_id(self.hass, entry)
        provisioned = await _async_provision(self.hass, leaf_id)
        if isinstance(provisioned, str):
            return self.async_abort(reason=provisioned)
        # Persist only after durable success, never dropping connection/backend or
        # migration fields; the update listener reloads the entry.
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, **provisioned})
        return self.async_create_entry(data=dict(entry.options))
