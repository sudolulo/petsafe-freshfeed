"""Lookup of the tuya_local source entry's DP145 raw binding.

Shared by the config flow and the Tuya Local assist so neither imports the
other. Moved unchanged from ``config_flow.py``; reads registry state only.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import SOURCE_DOMAIN, SOURCE_PROFILE

DP145_RE = re.compile(r"(?:^|_)dp_145_raw$")
LEAF_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)


def profile_type(entry: ConfigEntry) -> Any:
    """Return the tuya_local profile type; options win over data. Reads only the type key."""
    if "type" in entry.options:
        return entry.options["type"]
    return entry.data.get("type")


def binding_id(hass, source_entry_id: str) -> str | None:
    """Source-owned registry leaf of the selected DP145 raw entity, or None."""
    source = hass.config_entries.async_get_entry(source_entry_id)
    if source is None or source.domain != SOURCE_DOMAIN or profile_type(source) != SOURCE_PROFILE:
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
               if entity.platform == SOURCE_DOMAIN and DP145_RE.search(entity.unique_id)}
    if len(devices) != 1 or None in devices:
        return None
    device = dr.async_get(hass).async_get(next(iter(devices)))
    if (device is None or device.config_entry_id != source_entry_id
            or device.config_subentry_id is not None
            or device.is_composite_device is not False
            or type(device.id) is not str or LEAF_RE.fullmatch(device.id) is None
            or type(device.identifiers) is not set
            or any(type(member) is not tuple or len(member) != 2
                   or type(member[0]) is not str or type(member[1]) is not str
                   for member in device.identifiers)
            or (SOURCE_DOMAIN, device_id) not in device.identifiers):
        return None
    return device.id
