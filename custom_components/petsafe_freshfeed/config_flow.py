"""Config flow for PetSafe FreshFeed: pick the tuya_local raw FreshFeed entry to observe.

Controls are opt-in. Choosing ``enable_controls`` (in the user step for a new
entry, or in the options flow for an existing read-only entry) is the ONLY path
that provisions the retained-intent store. It provisions exactly once with a
fresh random store ID under the source-owned registry leaf, and persists the IDs
only after provisioning succeeded. Any failure or cancellation persists nothing
and is never retried, adopted or repaired here; ordinary startup only opens.

"Sign in with PetSafe" (offered only when the app profile is bundled): one
account sign-in per submit, at most one submit per 60 s per Home Assistant
instance, then the Tuya Local source entry is created through Tuya Local's own
flow and this entry is created exactly as on the existing path. The password is
used only to build a single-use ``SignIn`` and is never kept on the flow; the
feeder material lives on the flow only until it finishes. Nothing about the
account, the key, the device ID or the host is logged or stored here.
"""

from __future__ import annotations

import functools
import ipaddress
import time
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    SOURCE_IGNORE,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_PASSWORD
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

from . import account, lan_locate, store_bootstrap, tuya_local_assist
from .tuya_local_assist import AssistError, AssistStatus
from .source_lookup import DP145_RE, LEAF_RE, binding_id, profile_type
from .const import (
    CONF_ENABLE_CONTROLS,
    CONF_INTENT_BINDING_ID,
    CONF_INTENT_STORE_ID,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
    INTENT_STORE_ROOT,
    SOURCE_DOMAIN,
    SOURCE_PROFILE,
)

_CONTROLS = vol.Optional(CONF_ENABLE_CONTROLS, default=False)
# Moved to source_lookup (shared with the Tuya Local assist); names kept for callers.
_DP145_RE = DP145_RE
_LEAF_RE = LEAF_RE
_profile = profile_type
_binding_id = binding_id

# --- account sign-in path -------------------------------------------------------

CONF_COUNTRY = "country"
CONF_FEEDER = "feeder"
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
_PREFLIGHT_ABORTS = {
    AssistStatus.TUYA_LOCAL_MISSING: "tuya_local_missing",
    AssistStatus.TUYA_LOCAL_UNSUPPORTED: "tuya_local_unsupported",
    AssistStatus.PROFILE_CONFLICT: "profile_conflict",
}
# After the assist created a new Tuya Local entry, every abort of this flow says
# that entry was kept (it is never deleted here).
_KEPT_REASONS = {
    "already_configured": "tl_entry_kept_already_configured",
    "already_in_progress": "tl_entry_kept_already_in_progress",
    "binding_unavailable": "tl_entry_kept_binding_unavailable",
    "provision_failed": "tl_entry_kept_provision_failed",
    "entry_not_loaded": "tl_entry_kept_not_loaded",
}
_KEPT_OTHER = "tl_entry_kept_aborted"
_LAN_NETWORKS = tuple(ipaddress.IPv4Network(net)
                      for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_PROFILE_NOTE = "The PetSafe FreshFeed profile will be installed into Tuya Local."
# Confirm-step text: a new Tuya Local entry, or the existing one that is reused.
_SETUP_CREATE = ("A Tuya Local entry for this feeder will be created through Tuya Local's own "
                 "setup, which checks the local connection.")
_SETUP_REUSE = ("The existing Tuya Local entry for this feeder will be used; no new Tuya Local "
                "entry is created.")


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


async def _async_provision(hass, source_entry_id: str) -> dict[str, str] | str:
    """Provision once; return the IDs to persist, or a fixed abort reason."""
    binding_id = _binding_id(hass, source_entry_id)
    if binding_id is None:
        return "binding_unavailable"
    store_id = uuid4().hex
    try:
        await store_bootstrap.async_provision_store(
            hass.config.path(INTENT_STORE_ROOT), binding_id=binding_id, store_id=store_id)
    except (store_bootstrap.StoreBootstrapError, ValueError):
        return "provision_failed"
    return {CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: binding_id}


class FreshFeedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single user step that binds to one tuya_local petsafe_freshfeed_raw entry."""

    VERSION = 1

    # Account-path state; lives only until the flow finishes or is removed.
    _feeders: tuple = ()
    _feeder = None
    _located = None
    _host: str | None = None
    _reuse_entry_id: str | None = None
    _install_profile = False
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

    async def _async_create(
        self, source_entry_id: str, title: str, enable_controls: bool, *,
        require_binding: bool = False, kept: bool = False,
    ) -> ConfigFlowResult:
        """Create this entry for a tuya_local source entry (both paths).

        ``require_binding`` also requires the DP145 raw binding without controls;
        ``kept`` marks a Tuya Local entry the assist just created, so every abort
        reports that it was kept.
        """
        try:
            await self.async_set_unique_id(source_entry_id)
            self._abort_if_unique_id_configured()
        except AbortFlow as err:
            if not kept:
                raise
            return self._abort(err.reason, kept=True)
        if require_binding and _binding_id(self.hass, source_entry_id) is None:
            return self._abort("binding_unavailable", kept=kept)
        data = {CONF_SOURCE_ENTRY_ID: source_entry_id}
        if enable_controls:
            provisioned = await _async_provision(self.hass, source_entry_id)
            if isinstance(provisioned, str):
                return self._abort(provisioned, kept=kept)
            data.update(provisioned)
        self._clear()
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
            return await self._async_create(
                source.entry_id, source.title, user_input.get(CONF_ENABLE_CONTROLS) is True)

        if len(candidates) == 1:
            await self.async_set_unique_id(candidates[0].entry_id)
            self._abort_if_unique_id_configured()
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
        self._reuse_entry_id = None
        self._install_profile = False

    @callback
    def async_remove(self) -> None:
        self._clear()

    @callback
    def _abort(self, reason: str, *, kept: bool = False) -> ConfigFlowResult:
        self._clear()
        if kept:
            reason = _KEPT_REASONS.get(reason, _KEPT_OTHER)
        return self.async_abort(reason=reason)

    async def _async_preflight_refusal(self) -> str | None:
        """Fixed abort reason when Tuya Local cannot be used; records a needed install."""
        try:
            status = await tuya_local_assist.async_preflight(self.hass)
        except AssistError as err:
            return err.reason
        except Exception:  # noqa: BLE001 - never surface exception text or args
            return "tuya_local_unsupported"
        if status in _PREFLIGHT_ABORTS:
            return _PREFLIGHT_ABORTS[status]
        self._install_profile = status is AssistStatus.PROFILE_NEEDED
        return None

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
        # Checked on every submit, before the cooldown stamp: no password is sent
        # when Tuya Local became unusable since the form was shown.
        refusal = await self._async_preflight_refusal()
        if refusal is not None:
            del password
            return self._abort(refusal)
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
        refusal = await self._async_preflight_refusal()
        if refusal is not None:
            return self._abort(refusal)
        device_id = self._feeder.device_id
        try:
            reuse = await tuya_local_assist.async_existing_source(self.hass, device_id)
        except AssistError as err:
            return self._abort(err.reason)
        if reuse is not None:
            ours = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, reuse)
            if ours is not None and ours.source != SOURCE_IGNORE:
                return self._abort("already_configured")
            self._reuse_entry_id = reuse
            return await self.async_step_confirm()
        located = await self.hass.async_add_executor_job(_locate, device_id)
        del device_id
        if located is None:
            return await self.async_step_host()
        self._located = located
        return await self.async_step_confirm()

    async def async_step_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._feeder is None:
            return self._abort("aborted")
        errors: dict[str, str] = {}
        if user_input is not None:
            host = _lan_ipv4(user_input.get(CONF_HOST))
            if host is not None:
                self._host = host
                return await self.async_step_confirm()
            errors[CONF_HOST] = "invalid_host"
        return self.async_show_form(
            step_id="host",
            data_schema=vol.Schema({vol.Required(CONF_HOST): TextSelector()}),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        feeder = self._feeder
        if feeder is None:
            return self._abort("aborted")
        # A second submit while the first is still working changes nothing.
        if user_input is None or self._confirming:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({_CONTROLS: bool}),
                description_placeholders={
                    "name": feeder.name,
                    "setup": _SETUP_REUSE if self._reuse_entry_id is not None
                    else _SETUP_CREATE,
                    "profile": _PROFILE_NOTE if self._install_profile else "",
                },
            )
        enable_controls = user_input.get(CONF_ENABLE_CONTROLS) is True
        self._confirming = True
        kept = False
        try:
            # Re-check right before changing anything: the install may have changed since.
            refusal = await self._async_preflight_refusal()
            if refusal is not None:
                return self._abort(refusal)
            if self._install_profile:
                try:
                    await tuya_local_assist.async_install_profile(self.hass)
                except AssistError as err:
                    return self._abort(err.reason)
            if self._reuse_entry_id is not None:
                source_entry_id = self._reuse_entry_id
            else:
                # If this flow is removed while the assist runs, a Tuya Local entry
                # created in that window is kept and goes unreported.
                try:
                    source_entry_id = await tuya_local_assist.async_create_source_entry(
                        self.hass, feeder, self._located, self._host, name=feeder.name)
                except AssistError as err:
                    return self._abort(err.reason, kept=err.reason == "entry_not_loaded")
                kept = True
            source = self.hass.config_entries.async_get_entry(source_entry_id)
            title = source.title if source is not None else feeder.name
            del feeder
            return await self._async_create(
                source_entry_id, title, enable_controls, require_binding=True, kept=kept)
        except AbortFlow:
            self._clear()
            raise
        except Exception:  # noqa: BLE001 - never surface exception text or args
            return self._abort("aborted", kept=kept)
        finally:
            self._confirming = False


class FreshFeedOptionsFlow(OptionsFlow):
    """Explicitly enable controls on an existing read-only entry; never disables/clears."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if CONF_INTENT_STORE_ID in entry.data or CONF_INTENT_BINDING_ID in entry.data:
            return self.async_abort(reason="controls_already_enabled")
        if user_input is None:
            return self.async_show_form(
                step_id="init", data_schema=vol.Schema({_CONTROLS: bool}))
        if user_input.get(CONF_ENABLE_CONTROLS) is not True:
            return self.async_create_entry(data={})
        provisioned = await _async_provision(self.hass, entry.data[CONF_SOURCE_ENTRY_ID])
        if isinstance(provisioned, str):
            return self.async_abort(reason=provisioned)
        # Persist only after durable success; the update listener reloads the entry.
        self.hass.config_entries.async_update_entry(
            entry, data={CONF_SOURCE_ENTRY_ID: entry.data[CONF_SOURCE_ENTRY_ID], **provisioned})
        return self.async_create_entry(data={})
