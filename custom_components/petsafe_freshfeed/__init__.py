"""PetSafe FreshFeed integration: read-only observation plus opt-in durable controls.

Home Assistant is imported lazily inside the setup functions so the stdlib backend tests can
import the pure sibling modules (``observed``, ``const``, ``sqlite_witness`` ...) through this
package without Home Assistant installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import CONF_MIGRATION_STAGED_CONNECTION, DOMAIN, PLATFORMS, TRANSITIONS_KEY

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

    from . import hub as hub_module
    from .hub import FreshFeedHub
    from .migration import in_migration

    # Migration admission precedes any hub or source construction: a migration
    # call in progress, or an unfinished journal, fences startup. Existing
    # entity registry rows remain (states unavailable) meanwhile.
    if in_migration(hass, entry) or not await hub_module.async_migration_admits(hass, entry):
        raise ConfigEntryNotReady(hub_module.MIGRATION_PENDING)

    # A hub whose failed-setup shutdown could not drain still owns its parked
    # coordinator: no replacement starts until that drain succeeds.
    retiring = hass.data.setdefault(DOMAIN, {}).setdefault(_RETIRING, {})
    previous = retiring.get(entry.entry_id)
    if previous is not None:
        if not await previous.async_stop():
            raise ConfigEntryNotReady("previous_coordinator_draining")
        retiring.pop(entry.entry_id, None)

    from .repairs import FreshFeedRepairs, async_clear_offline_issue, async_reconcile_protocol_issue
    # A persisted offline observation cannot survive a process restart.
    async_clear_offline_issue(hass, entry)
    async_reconcile_protocol_issue(hass, entry)
    hub = FreshFeedHub(hass, entry)
    entry.runtime_data = hub
    try:
        # Admission was decided above for this setup: not re-derived by the hub.
        await hub.async_start(migration_admitted=True)
        await hass.config_entries.async_forward_entry_setups(entry, list(PLATFORMS))
    except BaseException:
        # Retained until a successful drain, including if this cleanup is cancelled.
        retiring[entry.entry_id] = hub
        if await hub.async_stop():
            retiring.pop(entry.entry_id, None)
        raise
    repairs = FreshFeedRepairs(hass, entry)
    def update_repairs():
        repairs.update(connected=hub.snapshot.available, write_view=hub.write_view)
    unsubscribe = hub.async_add_listener(update_repairs)
    hub._repairs_stop = lambda: (unsubscribe(), repairs.stop())
    update_repairs()
    from .services import async_register
    async_register(hass, entry)
    # Enabling controls in the options flow persists the store IDs; reload to apply them.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # An explicit transition (host reconfigure) unloads and sets up the entry
    # exactly once itself: never a second, listener-driven reload.
    if entry.entry_id in hass.data.get(DOMAIN, {}).get(TRANSITIONS_KEY, ()):
        return
    # An open migration (staged connection) reloads only when its coordinator
    # requests it after committing owned fields; never from this listener.
    if CONF_MIGRATION_STAGED_CONNECTION in entry.data:
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the persisted event history and migration journal of a deleted entry.

    The retained-intent store is deliberately left in place: it is never cleared.
    A journal that cannot be removed safely is left behind (it is keyed by this
    entry's unique ID, so no later entry can adopt it).
    """
    from homeassistant.helpers.storage import Store

    from .migration_journal import MigrationJournal
    from .migration_state import MigrationFault

    from .repairs import async_remove_entry_issues
    async_remove_entry_issues(hass, entry.entry_id)
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    try:
        await MigrationJournal(hass.config.config_dir, entry.entry_id).async_remove()
    except MigrationFault:
        pass


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
        if hasattr(hub, "_repairs_stop"):
            hub._repairs_stop()
        from .services import async_unregister
        async_unregister(hass, entry.entry_id)
    else:
        hub.resume_after_failed_unload()
    return unloaded
