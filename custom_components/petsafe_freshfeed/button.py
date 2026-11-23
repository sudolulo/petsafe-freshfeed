"""Owned "Reset after refill" button: the existing guarded `refilled` command.

The press is the confirmation. The caller must be an administrator, exactly as
for the `petsafe_freshfeed.command` service, and refusals use the same public
reasons. An admitted request is not raised on: the write status reports its
outcome, as for the service.
"""
from homeassistant.components.button import ButtonEntity

from .const import REFILLED_VALUE, WRITE_OPERATION_DPS
from .entity import FreshFeedEntity
from .services import service_error


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.runtime_data._owned:
        async_add_entities([RefillResetButton(entry.runtime_data, entry.entry_id)])


class RefillResetButton(FreshFeedEntity, ButtonEntity):
    _attr_icon = "mdi:tray-arrow-down"

    def __init__(self, hub, entry_id):
        super().__init__(hub, entry_id, "refill_reset")

    async def async_press(self) -> None:
        caller_id = self._context.user_id if self._context is not None else None
        if not caller_id:
            raise service_error("admin_required")
        caller = await self.hass.auth.async_get_user(caller_id)
        if caller is None or not caller.is_admin:
            raise service_error("admin_required")
        adapter = self.hub.adapter
        if adapter is None:
            raise service_error("source_unavailable")
        result = await adapter.async_submit(
            "refilled", {WRITE_OPERATION_DPS["refilled"]: REFILLED_VALUE}, confirmed=True)
        if not result["admitted"]:
            raise service_error(result["reason"])
