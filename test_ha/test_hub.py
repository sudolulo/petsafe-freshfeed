"""Tests for the read-only FreshFeed hub."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.petsafe_freshfeed.hub import FreshFeedHub


def make_hub(hass, source):
    entry = MockConfigEntry(domain="petsafe_freshfeed", data={"source_entry_id": source.entry_id})
    entry.add_to_hass(hass)
    return FreshFeedHub(hass, entry)


async def change(hass, entity_id, state):
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()


async def test_resolves_raw_entities_and_device(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    assert hub.raw_entity_ids == raw_entities
    assert hub.device_identifiers == {("tuya_local", "fakeid")}
    assert hub.snapshot.status == "no_food"
    assert hub.snapshot.meals_left == 0
    assert hub.last_fed is None
    await hub.async_stop()


async def test_missing_required_raw_not_ready(hass, source_entry, raw_entities):
    er.async_get(hass).async_remove(raw_entities[145])
    hub = make_hub(hass, source_entry)
    with pytest.raises(ConfigEntryNotReady):
        await hub.async_start()


async def test_setup_without_optional_dp14(hass, source_entry, raw_entities):
    from homeassistant.config_entries import ConfigEntryState
    er.async_get(hass).async_remove(raw_entities[14])
    entry = await _setup_entry(hass, source_entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.snapshot.available
    assert entry.runtime_data.snapshot.feed_code is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_dispense_refill_skip_sequence(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    seen = []
    hub.async_add_event_listener(seen.append)
    await change(hass, raw_entities[145], "1")
    await change(hass, raw_entities[145], "2")
    dispense_at = hass.states.get(raw_entities[145]).last_changed
    await change(hass, raw_entities[14], "1")
    await change(hass, raw_entities[14], "10")
    assert [e.kind for e in seen] == ["refilled", "dispensed", "skipped_fault"]
    assert hub.last_fed == dispense_at
    assert hub.last_event == seen[-1]
    await hub.async_stop()


async def test_unavailable_raw_makes_entities_unavailable(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    await change(hass, raw_entities[145], "unavailable")
    assert not hub.snapshot.available
    await change(hass, raw_entities[145], "3")
    assert hub.snapshot.available
    assert not hub.events
    await hub.async_stop()


async def test_store_persists_and_restart_does_not_create_event(hass, source_entry, raw_entities, hass_storage):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    await change(hass, raw_entities[145], "1")
    await change(hass, raw_entities[145], "2")
    saved = hub.last_fed
    await hub.async_stop()
    assert len(hass_storage[f"petsafe_freshfeed.{hub.entry.entry_id}"]["data"]["events"]) == 2
    await change(hass, raw_entities[145], "unknown")
    restarted = FreshFeedHub(hass, hub.entry)
    await restarted.async_start()
    await change(hass, raw_entities[145], "6")
    assert [e.kind for e in restarted.events] == ["refilled", "dispensed"]
    assert restarted.last_fed == saved
    await restarted.async_stop()


async def test_cached_startup_dp1_does_not_establish_schedule(hass, source_entry, raw_entities):
    registry = er.async_get(hass)
    device_id = registry.async_get(raw_entities[145]).device_id
    dp1 = registry.async_get_or_create(
        "text", "tuya_local", "fakeid-text_dp_1_raw",
        suggested_object_id="petsafe_freshfeed_dp_1_raw",
        config_entry=source_entry, device_id=device_id,
    )
    hass.states.async_set(dp1.entity_id, "fwgAAQE=")
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    assert hub.schedule.entries is None
    assert hub.schedule.source_state == "unknown"
    assert hub.schedule.observed_at is None
    assert hub.schedule.revision is None
    await hub.async_stop()


async def test_schedule_only_restoration_does_not_create_feed_history(hass, source_entry, raw_entities, hass_storage):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState

    hub = make_hub(hass, source_entry)
    key = f"petsafe_freshfeed.{hub.entry.entry_id}"
    cached = ScheduleState().with_report("fwgAAQE=", dt_util.utcnow())
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {
        "version": 2, "events": [], "schedule": cached.to_dict(),
    }}
    await hub.async_start()
    assert hub.schedule.entries == cached.entries
    assert hub.schedule.source_state == "last_known"
    assert hub.schedule.revision == cached.revision
    assert hub.events == []
    assert hub.last_fed is None
    await hub.async_stop()


async def test_store_migration_and_schedule_restart(hass, source_entry, raw_entities, hass_storage):
    hub = make_hub(hass, source_entry)
    key = f"petsafe_freshfeed.{hub.entry.entry_id}"
    event = {"kind": "dispensed", "compartment": 2, "at": (dt_util.utcnow() - timedelta(days=2)).isoformat()}
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": [event]}
    await hub.async_start()
    assert [item.to_dict() for item in hub.events] == [event]
    assert hub.schedule.source_state == "unknown"
    hub.schedule = hub.schedule.with_report('/w==', dt_util.utcnow())
    await hub.async_stop()
    assert hass_storage[key]["data"]["events"] == [event]
    restarted = FreshFeedHub(hass, hub.entry)
    await restarted.async_start()
    assert restarted.schedule.entries == ()
    assert restarted.schedule.source_state == "last_known"
    assert [item.to_dict() for item in restarted.events] == [event]
    await restarted.async_stop()
    hass_storage[key]["data"]["schedule"] = {"value": "broken"}
    again = FreshFeedHub(hass, hub.entry)
    await again.async_start()
    assert again.schedule.source_state == "unknown"
    assert [item.to_dict() for item in again.events] == [event]
    await again.async_stop()


async def test_legacy_history_migration_preserves_multiple_events_with_missing_cache(
    hass, source_entry, raw_entities, hass_storage, freezer
):
    freezer.move_to("2026-11-10T12:00:00+00:00")
    hub = make_hub(hass, source_entry)
    key = f"petsafe_freshfeed.{hub.entry.entry_id}"
    events = [
        {"kind": "refilled", "compartment": 1, "at": "2026-11-08T12:00:00+00:00"},
        {"kind": "dispensed", "compartment": 2, "at": "2026-11-09T12:00:00+00:00"},
    ]
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": events}
    await hub.async_start()
    assert [event.to_dict() for event in hub.events] == events
    assert hub.schedule.entries is None
    await hub.async_stop()
    del hass_storage[key]["data"]["schedule"]
    restarted = FreshFeedHub(hass, hub.entry)
    await restarted.async_start()
    assert [event.to_dict() for event in restarted.events] == events
    assert restarted.schedule.entries is None
    assert restarted.last_fed == datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    await restarted.async_stop()


async def test_tick_updates_next_meal(hass, source_entry, raw_entities, hass_storage, freezer):
    freezer.move_to("2026-11-10T11:59:30+00:00")
    hub = make_hub(hass, source_entry)
    hass_storage[f"petsafe_freshfeed.{hub.entry.entry_id}"] = {"version": 1, "minor_version": 1, "key": f"petsafe_freshfeed.{hub.entry.entry_id}", "data": [
        {"kind": "dispensed", "compartment": 2, "at": "2026-11-08T12:00:00+00:00"},
        {"kind": "dispensed", "compartment": 3, "at": "2026-11-09T12:00:00+00:00"},
    ]}
    await hub.async_start()
    seen = []
    hub.async_add_listener(lambda: seen.append(True))
    assert hub.snapshot.meals_left == 0
    assert hub.next_meal is None
    await change(hass, raw_entities[145], "1")
    assert hub.next_meal is None
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hub.next_meal is None
    assert seen
    await hub.async_stop()


async def test_unload_cleans_up_and_flushes(hass, source_entry, raw_entities, hass_storage):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    seen = []
    hub.async_add_listener(lambda: seen.append(True))
    await change(hass, raw_entities[145], "1")
    await hub.async_stop()
    assert hass_storage[f"petsafe_freshfeed.{hub.entry.entry_id}"]["data"]
    count = len(seen)
    await change(hass, raw_entities[145], "2")
    assert len(seen) == count


async def test_no_service_calls(hass, source_entry, raw_entities):
    with patch.object(type(hass.services), "async_call") as call:
        hub = make_hub(hass, source_entry)
        await hub.async_start()
        await change(hass, raw_entities[145], "1")
        await hub.async_stop()
        call.assert_not_called()


async def test_restore_last_fed_keeps_newest(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    newer = datetime(2026, 11, 10, tzinfo=timezone.utc)
    hub.restore_last_fed(newer)
    hub.restore_last_fed(newer - timedelta(days=1))
    assert hub.last_fed == newer
    await hub.async_stop()


async def _setup_entry(hass, source):
    entry = MockConfigEntry(domain="petsafe_freshfeed", data={"source_entry_id": source.entry_id}, unique_id=source.entry_id)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entry_setup_retry_when_required_raw_missing(hass, source_entry, raw_entities):
    from homeassistant.config_entries import ConfigEntryState
    er.async_get(hass).async_remove(raw_entities[145])
    entry = await _setup_entry(hass, source_entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_forward_failure_stops_hub(hass, source_entry, raw_entities, hass_storage, freezer):
    from custom_components.petsafe_freshfeed import async_setup_entry
    hub = make_hub(hass, source_entry)
    seen = []
    hub.async_add_listener(lambda: seen.append(True))
    with patch("custom_components.petsafe_freshfeed.hub.FreshFeedHub", return_value=hub), \
         patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=RuntimeError("forward failed")):
        with pytest.raises(RuntimeError, match="forward failed"):
            await async_setup_entry(hass, hub.entry)
    await change(hass, raw_entities[145], "1")
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not seen
    assert not hub._unsubscribers


async def test_remove_entry_deletes_storage(hass, source_entry, raw_entities, hass_storage):
    from custom_components.petsafe_freshfeed import async_remove_entry
    hub = make_hub(hass, source_entry)
    key = f"petsafe_freshfeed.{hub.entry.entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": []}
    await async_remove_entry(hass, hub.entry)
    assert key not in hass_storage


async def test_entry_reload_keeps_events_and_unload_flushes(hass, source_entry, raw_entities, hass_storage):
    from homeassistant.config_entries import ConfigEntryState
    entry = await _setup_entry(hass, source_entry)
    await change(hass, raw_entities[145], "1")
    await change(hass, raw_entities[145], "2")
    fed = entry.runtime_data.last_fed
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert [e.kind for e in entry.runtime_data.events] == ["refilled", "dispensed"]
    assert entry.runtime_data.last_fed == fed
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert len(hass_storage[f"petsafe_freshfeed.{entry.entry_id}"]["data"]["events"]) == 2
