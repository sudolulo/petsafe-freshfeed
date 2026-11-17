"""Owned FreshFeed brightness setting."""
import math

from homeassistant.components.number import NumberEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .owned_setting import OwnedSetting


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.runtime_data._owned:
        async_add_entities([OwnedBrightness(entry.runtime_data, entry.entry_id, "brightness_setting")])


class OwnedBrightness(OwnedSetting, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 1

    @property
    def native_value(self):
        try:
            return int(self.observed)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not 0 <= value <= 10 or int(value) != value):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="invalid_setting")
        await self.async_set(int(value))
