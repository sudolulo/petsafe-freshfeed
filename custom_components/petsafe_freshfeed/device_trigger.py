"""Device automations for newly observed feeding events."""
from datetime import datetime

import probatio
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import state
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN, EVENT_KINDS

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({
    probatio.Required("domain"): DOMAIN,
    probatio.Required("entity_id"): cv.entity_id_or_uuid,
    probatio.Required("type"): probatio.In(EVENT_KINDS),
})


def _valid_entry(hass, entity, device_id):
    if not entity or entity.domain != "event" or entity.platform != DOMAIN or entity.device_id != device_id or entity.disabled_by:
        return False
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    return bool(entry and entry.domain == DOMAIN and entry.state == ConfigEntryState.LOADED and entity.unique_id == f"{entry.entry_id}_feeding")


async def async_get_triggers(hass, device_id):
    registry = er.async_get(hass)
    return [
        {"platform": "device", "domain": DOMAIN, "device_id": device_id, "entity_id": entity.id, "type": kind}
        for entity in er.async_entries_for_device(registry, device_id)
        if _valid_entry(hass, entity, device_id)
        for kind in EVENT_KINDS
    ]


async def async_validate_trigger_config(hass, config):
    config = TRIGGER_SCHEMA(config)
    registry = er.async_get(hass)
    entity_id = er.async_resolve_entity_id(registry, config["entity_id"])
    entity = registry.async_get(entity_id) if entity_id else None
    if not _valid_entry(hass, entity, config["device_id"]):
        raise probatio.Invalid("Invalid feeding entity")
    return {**config, "entity_id": entity.id}


def _timestamp(state_obj):
    if state_obj is None or state_obj.attributes.get("restored"):
        return None
    try:
        result = datetime.fromisoformat(state_obj.state)
        return result if result.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


async def async_attach_trigger(hass, config, action, trigger_info):
    config = await async_validate_trigger_config(hass, config)
    state_config = await state.async_validate_trigger_config(hass, {"platform": "state", "entity_id": config["entity_id"]})

    async def filtered_action(variables, context):
        transition = variables["trigger"]
        old, new = transition["from_state"], transition["to_state"]
        old_time, new_time = _timestamp(old), _timestamp(new)
        if old_time is not None and new_time is not None and old_time != new_time and new.attributes.get("event_type") == config["type"]:
            await action(variables, context)

    return await state.async_attach_trigger(hass, state_config, filtered_action, trigger_info, platform_type="device")
