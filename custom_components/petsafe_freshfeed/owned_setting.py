"""Owned-only setting entities, backed exclusively by guarded writes."""
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import EntityCategory

from .const import DOMAIN, OWNED_SETTING_ENTITIES
from .entity import FreshFeedEntity
from .services import friendly_reason


class OwnedSetting(FreshFeedEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, entry_id, key):
        super().__init__(hub, entry_id, key)
        self.dp = OWNED_SETTING_ENTITIES[key][1]
        self._attr_icon = {
            "stainless_inserts": "mdi:silverware", "meal_counter": "mdi:counter",
            "brightness_setting": "mdi:brightness-6",
        }[key]

    @property
    def observed(self):
        return self.hub._observed.get(int(self.dp))

    async def async_set(self, value):
        adapter = self.hub.adapter
        if adapter is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="controls_disabled")
        expected = "on" if value is True else "off" if value is False else str(value)
        result = await adapter.async_submit(
            "settings", {self.dp: value}, unchanged=lambda: self.observed == expected)
        if not result["admitted"]:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="setting_refused",
                                     translation_placeholders={"reason": friendly_reason(result["reason"])})
        if result.get("unchanged"):
            return
        if result["state"] != "verified":
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="setting_uncertain")
