"""Owned FreshFeed setting switches."""
from homeassistant.components.switch import SwitchEntity

from .owned_setting import OwnedSetting


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.runtime_data._owned:
        async_add_entities(OwnedSwitch(entry.runtime_data, entry.entry_id, key)
                           for key in ("stainless_inserts", "meal_counter"))


class OwnedSwitch(OwnedSetting, SwitchEntity):
    @property
    def is_on(self):
        return {"on": True, "off": False}.get(self.observed)

    async def async_turn_on(self, **kwargs):
        await self.async_set(True)

    async def async_turn_off(self, **kwargs):
        await self.async_set(False)
