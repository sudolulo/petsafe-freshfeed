"""PetSafe FreshFeed integration: read-only observation plus opt-in durable controls.

Home Assistant is imported lazily inside the setup functions so the stdlib backend tests can
import the pure sibling modules (``observed``, ``const``, ``sqlite_witness`` ...) through this
package without Home Assistant installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN, PLATFORMS

# hass.data[DOMAIN] key: hubs whose failed-setup shutdown has not drained yet.
_RETIRING = "retiring"

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the hub (and, with persisted store IDs, its coordinator), then forward platforms.

    The hub is runtime data before it starts: the write adapter reads the opened witness
    from it. Any setup failure performs the same fenced shutdown as an unload.
    """
    from homeassistant.exceptions import ConfigEntryNotReady

    from .hub import FreshFeedHub

    # A hub whose failed-setup shutdown could not drain still owns its parked
    # coordinator: no replacement starts until that drain succeeds.
    retiring = hass.data.setdefault(DOMAIN, {}).setdefault(_RETIRING, {})
    previous = retiring.get(entry.entry_id)
    if previous is not None:
        if not await previous.async_stop():
            raise ConfigEntryNotReady("previous_coordinator_draining")
        retiring.pop(entry.entry_id, None)

    hub = FreshFeedHub(hass, entry)
    entry.runtime_data = hub
    try:
        await hub.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, list(PLATFORMS))
    except BaseException:
        # Retained until a successful drain, including if this cleanup is cancelled.
        retiring[entry.entry_id] = hub
        if await hub.async_stop():
            retiring.pop(entry.entry_id, None)
        raise
    from .services import async_register
    async_register(hass, entry)
    # Enabling controls in the options flow persists the store IDs; reload to apply them.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the persisted event history when the entry is deleted.

    The retained-intent store is deliberately left in place: it is never cleared.
    """
    from homeassistant.helpers.storage import Store

    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drain the coordinator first; a failed drain refuses the unload and retains everything.

    Then unload platforms and finish the hub (cancels listeners and flushes the store).
    """
    hub = entry.runtime_data
    if not await hub.async_drain_controls():
        hub.resume_after_failed_unload()
        return False
    unloaded = await hass.config_entries.async_unload_platforms(entry, list(PLATFORMS))
    if unloaded:
        unloaded = await hub.async_stop()
    if unloaded:
        from .services import async_unregister
        async_unregister(hass, entry.entry_id)
    else:
        hub.resume_after_failed_unload()
    return unloaded
