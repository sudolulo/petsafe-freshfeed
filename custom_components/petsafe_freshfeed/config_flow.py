"""Config flow for PetSafe FreshFeed: pick the tuya_local raw FreshFeed entry to observe.

Controls are opt-in. Choosing ``enable_controls`` (in the user step for a new
entry, or in the options flow for an existing read-only entry) is the ONLY path
that provisions the retained-intent store. It provisions exactly once with a
fresh random store ID under the source-owned registry leaf, and persists the IDs
only after provisioning succeeded. Any failure or cancellation persists nothing
and is never retried, adopted or repaired here; ordinary startup only opens.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from . import store_bootstrap
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

_DP145_RE = re.compile(r"(?:^|_)dp_145_raw$")
_LEAF_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_CONTROLS = vol.Optional(CONF_ENABLE_CONTROLS, default=False)


def _profile(entry: ConfigEntry) -> Any:
    """Return the tuya_local profile type; options win over data. Reads only the type key."""
    if "type" in entry.options:
        return entry.options["type"]
    return entry.data.get("type")


def _binding_id(hass, source_entry_id: str) -> str | None:
    """Source-owned registry leaf of the selected DP145 raw entity, or None."""
    source = hass.config_entries.async_get_entry(source_entry_id)
    if source is None or source.domain != SOURCE_DOMAIN or _profile(source) != SOURCE_PROFILE:
        return None
    device_id = source.options.get("device_id", source.data.get("device_id"))
    cid = source.options.get("device_cid", source.data.get("device_cid"))
    if (type(device_id) is not str or not 1 <= len(device_id) <= 128
            or "\x00" in device_id
            or (cid is not None and (type(cid) is not str or cid != ""))):
        return None
    registry = er.async_get(hass)
    devices = {entity.device_id
               for entity in er.async_entries_for_config_entry(registry, source_entry_id)
               if entity.platform == SOURCE_DOMAIN and _DP145_RE.search(entity.unique_id)}
    if len(devices) != 1 or None in devices:
        return None
    device = dr.async_get(hass).async_get(next(iter(devices)))
    if (device is None or device.config_entry_id != source_entry_id
            or device.config_subentry_id is not None
            or device.is_composite_device is not False
            or type(device.id) is not str or _LEAF_RE.fullmatch(device.id) is None
            or type(device.identifiers) is not set
            or any(type(member) is not tuple or len(member) != 2
                   or type(member[0]) is not str or type(member[1]) is not str
                   for member in device.identifiers)
            or (SOURCE_DOMAIN, device_id) not in device.identifiers):
        return None
    return device.id


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
            await self.async_set_unique_id(source.entry_id)
            self._abort_if_unique_id_configured()
            data = {CONF_SOURCE_ENTRY_ID: source.entry_id}
            if user_input.get(CONF_ENABLE_CONTROLS) is True:
                provisioned = await _async_provision(self.hass, source.entry_id)
                if isinstance(provisioned, str):
                    return self.async_abort(reason=provisioned)
                data.update(provisioned)
            return self.async_create_entry(title=source.title, data=data)

        if len(candidates) == 1:
            await self.async_set_unique_id(candidates[0].entry_id)
            self._abort_if_unique_id_configured()
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({_CONTROLS: bool}),
                description_placeholders={"title": candidates[0].title},
            )

        options = [
            SelectOptionDict(value=entry.entry_id, label=entry.title) for entry in candidates
        ]
        return self.async_show_form(
            step_id="user",
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
