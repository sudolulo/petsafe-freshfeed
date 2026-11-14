"""Read-only FreshFeed binary sensors."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import FreshFeedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(FreshFeedBinarySensor(entry.runtime_data, entry.entry_id, key) for key in ("connected", "fault", "out_of_food", "defrosting"))


class FreshFeedBinarySensor(FreshFeedEntity, BinarySensorEntity):
    def __init__(self, hub, entry_id, key):
        super().__init__(hub, entry_id, key)
        self._always_available = key == "connected"
        self._attr_device_class = {"connected": BinarySensorDeviceClass.CONNECTIVITY, "fault": BinarySensorDeviceClass.PROBLEM, "out_of_food": BinarySensorDeviceClass.PROBLEM, "defrosting": BinarySensorDeviceClass.RUNNING}[key]
        if key == "connected":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self):
        snapshot = self.hub.snapshot
        if self.key == "connected":
            return snapshot.available
        if self.key == "out_of_food":
            return None if snapshot.meals_left is None else snapshot.meals_left == 0
        return getattr(snapshot, self.key)
