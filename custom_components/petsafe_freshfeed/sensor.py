"""Read-only FreshFeed sensors."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass, RestoreSensor
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.util import dt as dt_util

from .entity import FreshFeedEntity


SENSORS = (
    "status", "meals_left", "compartment", "food_level", "last_fed",
    "last_feed_result", "meals_today", "next_meal", "defrost_remaining",
    "brightness", "firmware_a", "firmware_b", "last_report", "schedule", "quick_feed", "write_status", "history",
)


async def async_setup_entry(hass, entry, async_add_entities):
    entities = [FreshFeedSensor(entry.runtime_data, entry.entry_id, key) for key in SENSORS]
    if entry.runtime_data._owned:
        entities.append(OwnedQuickFeedState(entry.runtime_data, entry.entry_id, "quick_feed_state"))
    async_add_entities(entities)


class OwnedQuickFeedState(FreshFeedEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Quick feed state"

    @property
    def native_value(self):
        return self.hub._observed.get(140)


class FreshFeedSensor(FreshFeedEntity, RestoreSensor):
    def __init__(self, hub, entry_id, key):
        basis = "ha_observed" if key in {"last_fed", "last_feed_result", "meals_today"} else "inferred" if key in {"next_meal", "brightness"} else "observed"
        super().__init__(hub, entry_id, key, basis)
        self._always_available = key in {"last_fed", "last_report", "schedule", "quick_feed", "write_status", "history", "next_meal"}
        if key in {"last_fed", "next_meal", "last_report"}:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif key in {"food_level", "last_feed_result"}:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["high", "low", "empty"] if key == "food_level" else ["dispensed", "refilled", "skipped_empty", "skipped_fault"]
        elif key == "defrost_remaining":
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
        if key == "meals_left":
            self._attr_native_unit_of_measurement = "meals"
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key in {"brightness", "firmware_a", "firmware_b", "last_report"}:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.key == "last_fed":
            state = await self.async_get_last_state()
            restored = dt_util.parse_datetime(state.state) if state else None
            if restored is not None and restored.tzinfo is not None:
                self.hub.restore_last_fed(restored)

    @property
    def native_value(self):
        if self.key == 'history':
            return len(self.hub.events)
        if self.key == 'quick_feed':
            return self.hub.quick_feed.status
        if self.key == 'write_status':
            return self.hub.write_view['state']
        if self.key == "schedule":
            entries = self.hub.schedule.entries
            return len(entries) if entries is not None else None
        if self.key == "last_feed_result":
            return self.hub.last_event.kind if self.hub.last_event else None
        if self.key in {"last_fed", "meals_today", "next_meal", "last_report"}:
            return getattr(self.hub, self.key)
        key = "defrost_minutes" if self.key == "defrost_remaining" else self.key
        return getattr(self.hub.snapshot, key)

    @property
    def extra_state_attributes(self):
        if self.key == 'history':
            return {'basis': 'ha_observed', 'events': [dict(source_dp=None, report_value=None, **{k: v for k, v in event.to_dict().items() if k not in {'source_dp', 'report_value'}}) if event.source_dp is None else event.to_dict() for event in self.hub.events], 'dp151_supported': False}
        if self.key == 'next_meal':
            schedule = self.hub.schedule
            return {'basis': 'schedule', 'source_state': schedule.source_state,
                    'observed_at': schedule.observed_at.isoformat() if schedule.observed_at else None,
                    'reason': 'empty_tray' if self.hub.snapshot.meals_left == 0 else None}
        if self.key == 'quick_feed':
            return {'command': self.hub.quick_feed.command, 'minutes': self.hub.quick_feed.minutes}
        if self.key == 'write_status':
            return self.hub.write_view
        if self.key == "schedule":
            from dataclasses import asdict
            schedule = self.hub.schedule
            return {
                "entries": [asdict(row) for row in schedule.entries] if schedule.entries is not None else None,
                "source_state": schedule.source_state,
                "observed_at": schedule.observed_at.isoformat() if schedule.observed_at else None,
                "revision": schedule.revision,
                "source_entity_id": self.hub.schedule_entity_id,
                "error": schedule.error,
            }
        attrs = super().extra_state_attributes
        if self.key == "last_feed_result":
            attrs["at"] = self.hub.last_event.at.isoformat() if self.hub.last_event else None
        return attrs
