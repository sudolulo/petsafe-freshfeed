"""Read-only owned registry leaf validation shared by setup and options."""

from __future__ import annotations

import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_DEVICE_ID, CONF_REGISTRY_DEVICE_ID, DOMAIN, SOURCE_DOMAIN,
)

LEAF_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)


def owned_leaf_valid(device, entry_id, physical_id) -> bool:
    """An owned entry's leaf: single owner, no subentry/composite/via, own identifiers.

    A migrated leaf may keep its (tuya_local, physical) identifier. Shared by
    owned setup and the options flow, which provisions controls against it.
    """
    allowed = {(DOMAIN, physical_id), (SOURCE_DOMAIN, physical_id)}
    try:
        return (device is not None and device.config_entry_id == entry_id
                and device.config_subentry_id is None
                and getattr(device, "is_composite_device", False) is False
                and getattr(device, "has_composite_identifiers", False) is False
                and device.via_device_id is None
                and bool(device.identifiers) and set(device.identifiers) <= allowed)
    except Exception:  # noqa: BLE001 - unknown shape is invalid
        return False


def owned_binding_id(hass, entry: ConfigEntry) -> str | None:
    """The owned entry's existing registry leaf ID, or None. Never creates one.

    A persisted ``registry_device_id`` (migration) must name the leaf; otherwise
    the entry must own exactly one leaf, which its own setup created.
    """
    physical = entry.data.get(CONF_DEVICE_ID)
    if type(physical) is not str or not physical:
        return None
    registry = dr.async_get(hass)
    if CONF_REGISTRY_DEVICE_ID in entry.data:
        persisted = entry.data[CONF_REGISTRY_DEVICE_ID]
        device = registry.async_get(persisted) if type(persisted) is str else None
        if device is None or device.id != persisted:
            return None
    else:
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        if len(devices) != 1:
            return None
        device = devices[0]
    if (not owned_leaf_valid(device, entry.entry_id, physical)
            or type(device.id) is not str or LEAF_RE.fullmatch(device.id) is None):
        return None
    return device.id
