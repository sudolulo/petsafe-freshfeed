"""Home Assistant-observed feeding events."""

from homeassistant.components.event import EventEntity

from .const import EVENT_KINDS
from .entity import FreshFeedEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([FreshFeedEvent(entry.runtime_data, entry.entry_id)])


class FreshFeedEvent(FreshFeedEntity, EventEntity):
    _always_available = True
    _attr_event_types = list(EVENT_KINDS)

    def __init__(self, hub, entry_id):
        super().__init__(hub, entry_id, "feeding", "ha_observed")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_add_event_listener(self._handle_event))

    def _handle_event(self, event):
        self._trigger_event(event.kind, {"compartment": event.compartment, "source_dp": event.source_dp, "report_value": event.report_value})
        self.async_write_ha_state()
