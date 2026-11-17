"""Config flow for PetSafe FreshFeed.

Two ways to add a feeder:

* "Sign in with PetSafe" (offered only when the app profile is bundled): one
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
* "Use an existing Tuya Local entry" (legacy): pick a tuya_local
  petsafe_freshfeed_raw entry, unchanged for unmigrated users.

Controls are opt-in. Choosing ``enable_controls`` (on the legacy pick, or in
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

from collections.abc import Mapping
import functools
import ipaddress
import time
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_IGNORE,
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

from . import account, lan_locate, migration, store_bootstrap
from .owned_source import (
    SUPPORTED_PROTOCOLS, UNSUPPORTED_CONFIGURATION, LocalConnection, LocalConnectionError,
    OwnedSource, OwnedSourceError,
)
from .source_lookup import DP145_RE, LEAF_RE, binding_id, owned_binding_id, profile_type
from .const import (
    BACKEND_LEGACY,
    BACKEND_OWNED,
    CONF_CONNECTION_BACKEND,
    CONF_DEVICE_ID,
    CONF_ENABLE_CONTROLS,
    CONF_HOST,
    CONF_INTENT_BINDING_ID,
    CONF_INTENT_STORE_ID,
    CONF_LOCAL_KEY,
    CONF_MIGRATION_STAGED_CONNECTION,
    CONF_PROTOCOL_VERSION,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
    INTENT_STORE_ROOT,
    SOURCE_DOMAIN,
    SOURCE_PROFILE,
    TRANSITIONS_KEY,
)

_CONTROLS = vol.Optional(CONF_ENABLE_CONTROLS, default=False)
# Moved to source_lookup (shared with the hub and migration); names kept for callers.
_DP145_RE = DP145_RE
_LEAF_RE = LEAF_RE
_profile = profile_type
_binding_id = binding_id

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
    """Physical device IDs a config entry names (data, options, staged connection)."""
    found = set()
    for container in (entry.data, entry.options, entry.data.get(CONF_MIGRATION_STAGED_CONNECTION)):
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
    """Account sign-in to an owned entry, or the legacy tuya_local pick."""

    VERSION = 1

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

    def _candidates(self) -> list[ConfigEntry]:
        return [
            entry
            for entry in self.hass.config_entries.async_entries(SOURCE_DOMAIN)
            if _profile(entry) == SOURCE_PROFILE
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if await _async_app_profile(self.hass) is None:
            return await self._async_step_pick("user", user_input)
        return self.async_show_menu(
            step_id="user", menu_options=["account", "existing"],
            description_placeholders={"title": "your FreshFeed"})

    async def async_step_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_pick("existing", user_input)

    # --- legacy path ------------------------------------------------------------

    def _owned_duplicate_of_source(self, source_entry_id: str) -> bool:
        """The legacy source's feeder already has an owned/migrated PetSafe entry."""
        source = self.hass.config_entries.async_get_entry(source_entry_id)
        if source is None:
            return False
        devices = _physical_ids(source)
        return any(devices & _physical_ids(ours)
                   for ours in self.hass.config_entries.async_entries(DOMAIN, include_ignore=False)
                   if ours.data.get(CONF_CONNECTION_BACKEND) == BACKEND_OWNED
                   or CONF_DEVICE_ID in ours.data)

    async def _async_create_legacy(
        self, source_entry_id: str, title: str, enable_controls: bool
    ) -> ConfigFlowResult:
        """Create a legacy entry for a tuya_local source entry."""
        await self.async_set_unique_id(source_entry_id)
        self._abort_if_unique_id_configured()
        if self._owned_duplicate_of_source(source_entry_id):
            return self.async_abort(reason="already_configured")
        data = {CONF_SOURCE_ENTRY_ID: source_entry_id}
        if enable_controls:
            provisioned = await _async_provision(self.hass, _binding_id(self.hass, source_entry_id))
            if isinstance(provisioned, str):
                return self.async_abort(reason=provisioned)
            data.update(provisioned)
        return self.async_create_entry(title=title, data=data)

    async def _async_step_pick(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """The existing path: pick a tuya_local petsafe_freshfeed_raw entry."""
        candidates = self._candidates()
        if not candidates:
            return self.async_abort(reason="no_source")

        if user_input is not None:
            if len(candidates) == 1:
                source = candidates[0]
            else:
                by_id = {entry.entry_id: entry for entry in candidates}
                source = by_id.get(user_input.get(CONF_SOURCE_ENTRY_ID))
                if source is None:
                    return self.async_abort(reason="no_source")
            return await self._async_create_legacy(
                source.entry_id, source.title, user_input.get(CONF_ENABLE_CONTROLS) is True)

        if len(candidates) == 1:
            await self.async_set_unique_id(candidates[0].entry_id)
            self._abort_if_unique_id_configured()
            if self._owned_duplicate_of_source(candidates[0].entry_id):
                return self.async_abort(reason="already_configured")
            return self.async_show_form(
                step_id=step_id,
                data_schema=vol.Schema({_CONTROLS: bool}),
                description_placeholders={"title": candidates[0].title},
            )

        options = [
            SelectOptionDict(value=entry.entry_id, label=entry.title) for entry in candidates
        ]
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCE_ENTRY_ID): SelectSelector(
                        SelectSelectorConfig(options=options)
                    ),
                    _CONTROLS: bool,
                }
            ),
            description_placeholders={"title": "the selected tuya_local FreshFeed device"},
        )

    # --- account path -------------------------------------------------------------

    @callback
    def _clear(self) -> None:
        """Drop the feeder material and every other account-path value."""
        self._feeders = ()
        self._feeder = None
        self._located = None
        self._host = None

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
        profile = await _async_app_profile(self.hass)
        if profile is None:
            return self._abort("account_unavailable")
        # Take the password out of the submitted mapping at once; it is used only to
        # build the single-use SignIn below.
        password = None if user_input is None else user_input.pop(CONF_PASSWORD, None)
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
        del discovery
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
            title = feeder.name
            del feeder
            self._clear()
            return self.async_create_entry(
                title=title, data=data, description=CREATED_READ_ONLY,
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
        if data.get(CONF_CONNECTION_BACKEND, BACKEND_LEGACY) != BACKEND_OWNED:
            return self.async_abort(reason="reconfigure_unsupported")
        if CONF_MIGRATION_STAGED_CONNECTION in data:
            return self.async_abort(reason="migration_pending")
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


_MIGRATION_REASONS = {
    migration.MigrationStatus.RESTART_REQUIRED: "migration_restart_required",
    migration.MigrationStatus.READY_TO_RESUME: "migration_ready_to_resume",
    migration.MigrationStatus.FORWARD_REPAIR_REQUIRED: "migration_forward_repair_required",
    migration.MigrationStatus.COMPLETE: "migration_complete",
    migration.MigrationStatus.CANCELLED: "migration_cancelled",
    migration.MigrationStatus.REFUSED: "migration_refused",
}


class FreshFeedOptionsFlow(OptionsFlow):
    """Enable controls explicitly (never disables/clears), and drive an explicit migration.

    Without an applicable migration choice the init step is the existing
    enable-controls form. Otherwise it is a menu of the applicable choices:
    ``controls`` (same form), ``migrate`` (legacy entries; preview, optional
    host/protocol correction, then the staged prepare), ``resume`` / ``cancel``
    for an unfinished journal, and ``remove_source`` only once a later process
    re-observed the verified handoff. Every outcome is a fixed abort reason.
    """

    _host: str | None = None
    _protocol: str | None = None
    _acknowledged: tuple | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return await self._async_controls(user_input)
        choices = await migration.async_options_choices(self.hass, entry)
        refusal = await self._controls_refusal()
        if not choices:
            if refusal is not None:
                return self.async_abort(reason=refusal)
            return self._controls_form("init")
        menu = (["controls"] if refusal is None else []) + list(choices)
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def _controls_refusal(self) -> str | None:
        entry = self.config_entry
        if CONF_INTENT_STORE_ID in entry.data or CONF_INTENT_BINDING_ID in entry.data:
            return "controls_already_enabled"
        if await migration.async_controls_fenced(self.hass, entry):
            return "migration_pending"
        return None

    def _controls_form(self, step_id: str) -> ConfigFlowResult:
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema({_CONTROLS: bool}))

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
        refusal = await self._controls_refusal()
        if refusal is not None:
            return self.async_abort(reason=refusal)
        if user_input.get(CONF_ENABLE_CONTROLS) is not True:
            return self.async_create_entry(data={})
        backend = entry.data.get(CONF_CONNECTION_BACKEND, BACKEND_LEGACY)
        if backend == BACKEND_OWNED:
            # The leaf this entry's own setup created (or its migrated leaf).
            leaf_id = owned_binding_id(self.hass, entry)
        elif backend == BACKEND_LEGACY and type(entry.data.get(CONF_SOURCE_ENTRY_ID)) is str:
            leaf_id = _binding_id(self.hass, entry.data[CONF_SOURCE_ENTRY_ID])
        else:
            leaf_id = None
        provisioned = await _async_provision(self.hass, leaf_id)
        if isinstance(provisioned, str):
            return self.async_abort(reason=provisioned)
        # Persist only after durable success, never dropping connection/backend or
        # migration fields; the update listener reloads the entry.
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, **provisioned})
        return self.async_create_entry(data={})

    # --- explicit migration -----------------------------------------------------

    def _migration_abort(self, status) -> ConfigFlowResult:
        return self.async_abort(reason=_MIGRATION_REASONS.get(status, "migration_refused"))

    async def async_step_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Preview (retiring raw entity IDs only), then the staged prepare."""
        entry = self.config_entry
        preview = await migration.async_preflight_migration(
            self.hass, entry, host=self._host, protocol=self._protocol)
        if not preview.eligible:
            if (preview.reason is migration.PreviewReason.UNSUPPORTED_SHAPE and self._host is None
                    and await migration.async_migration_correctable(self.hass, entry)):
                return await self.async_step_migrate_host()
            return self.async_abort(reason=f"migration_{preview.reason.value}")
        if user_input is None or self._acknowledged is None:
            self._acknowledged = preview.retiring_entity_ids
            return self.async_show_form(
                step_id="migrate", data_schema=vol.Schema({}),
                description_placeholders={
                    "count": str(len(preview.retiring_entity_ids)),
                    "retiring": ", ".join(preview.retiring_entity_ids) or "-"})
        status = await migration.async_prepare_migration(
            self.hass, entry, host=self._host, protocol=self._protocol,
            acknowledged=self._acknowledged)
        return self._migration_abort(status)

    async def async_step_migrate_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Correction form: an explicit RFC 1918 address and protocol 3.5."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = _lan_ipv4(user_input.get(CONF_HOST))
            protocol = user_input.get(CONF_PROTOCOL_VERSION)
            if host is None:
                errors[CONF_HOST] = "invalid_host"
            elif protocol not in SUPPORTED_PROTOCOLS:
                errors["base"] = UNSUPPORTED_CONFIGURATION
            else:
                preview = await migration.async_preflight_migration(
                    self.hass, self.config_entry, host=host, protocol=protocol)
                if preview.eligible:
                    self._host, self._protocol = host, protocol
                    return await self.async_step_migrate()
                if preview.reason is not migration.PreviewReason.UNSUPPORTED_SHAPE:
                    return self.async_abort(reason=f"migration_{preview.reason.value}")
                errors["base"] = UNSUPPORTED_CONFIGURATION
        return self.async_show_form(
            step_id="migrate_host",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): TextSelector(),
                vol.Required(CONF_PROTOCOL_VERSION, default=OWNED_PROTOCOL): SelectSelector(
                    SelectSelectorConfig(options=sorted(SUPPORTED_PROTOCOLS))),
            }),
            errors=errors,
        )

    async def async_step_resume(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Continue (or forward-repair) the migration; optional host/key corrections.

        The local key is only for forward repair after the move when the Tuya
        Local entry was deleted or changed; it is never shown or filled back in.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get(CONF_HOST)
            host = None if raw in (None, "") else _lan_ipv4(raw)
            key = user_input.get(CONF_LOCAL_KEY)
            key = None if key in (None, "") else key
            if raw not in (None, "") and host is None:
                errors[CONF_HOST] = "invalid_host"
            else:
                status = await migration.async_resume_migration(
                    self.hass, self.config_entry, host=host, local_key=key)
                del key
                return self._migration_abort(status)
        return self.async_show_form(
            step_id="resume",
            data_schema=vol.Schema({
                vol.Optional(CONF_HOST): TextSelector(),
                vol.Optional(CONF_LOCAL_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")),
            }),
            errors=errors,
        )

    async def async_step_cancel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="cancel", data_schema=vol.Schema({}))
        return self._migration_abort(
            await migration.async_cancel_migration(self.hass, self.config_entry))

    async def async_step_remove_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="remove_source", data_schema=vol.Schema({}))
        return self._migration_abort(
            await migration.async_remove_source(self.hass, self.config_entry))
