"""Shared read-only FreshFeed entity behavior."""

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity


class FreshFeedEntity(Entity):
    _attr_has_entity_name = True
    _always_available = False
    _attr_should_poll = False

    def __init__(self, hub, entry_id, key, basis="observed"):
        self.hub = hub
        self.key = key
        self.basis = basis
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        # HA 2026.10 scopes device identifiers to each config entry; passing
        # device_info would create a second device instead of reusing the source.
        self.device_entry = dr.async_get(hub.hass).async_get(hub.device_id)

    @property
    def available(self):
        return self._always_available or self.hub.snapshot.available

    @property
    def extra_state_attributes(self):
        return {"basis": self.basis}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_add_listener(self.async_write_ha_state))
