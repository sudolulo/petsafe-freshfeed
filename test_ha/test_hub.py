"""Owned observation, persistence and lifecycle contracts."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.petsafe_freshfeed.hub import FreshFeedHub
from owned_test_support import until
from test_owned_hub import DEVICE, make_entry, owned_data, setup


def owned_hub(owned):
    owned.device.dps.pop('1', None)
    owned.device.dps.pop('140', None)
    owned.device.dps['145'] = 6
    return FreshFeedHub(owned.hass, make_entry(owned, owned_data()))


async def report(owned, hub, dps):
    before = hub.last_report
    owned.device.push_status(dps)
    await until(lambda: hub.last_report != before)
    await owned.hass.async_block_till_done()
    return hub.last_report


async def test_resolves_owned_device_without_raw_entities(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    assert not hasattr(hub, 'raw_entity_ids')
    assert hub.device_identifiers == {('petsafe_freshfeed', DEVICE)}
    assert hub.snapshot.status == 'no_food'
    assert hub.snapshot.meals_left == 0
    assert hub.last_fed is None
    await hub.async_stop()


async def test_setup_without_optional_dp14(owned):
    owned.device.dps.pop('14')
    entry = await setup(owned, owned_data())
    assert entry.runtime_data.snapshot.available
    assert entry.runtime_data.snapshot.feed_code is None


async def test_dispense_refill_skip_sequence(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    seen = []
    hub.async_add_event_listener(seen.append)
    await report(owned, hub, {'145': 1})
    dispense_at = await report(owned, hub, {'145': 2})
    await report(owned, hub, {'14': 1})
    await report(owned, hub, {'14': 10})
    assert [e.kind for e in seen] == ['refilled', 'dispensed', 'meal_report', 'skipped_fault']
    assert hub.last_fed == dispense_at
    assert hub.last_event == seen[-1]
    await hub.async_stop()


async def test_disconnect_reconnect_does_not_invent_events(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    owned.device.refuse = True
    owned.device.sockets[-1].drop()
    await until(lambda: not hub.snapshot.available)
    owned.device.dps['145'] = 3
    owned.device.refuse = False
    await until(lambda: hub.snapshot.available)
    assert hub.snapshot.compartment == 3
    assert not hub.events
    await hub.async_stop()


async def test_store_persists_and_restart_does_not_create_event(owned, hass_storage):
    hub = owned_hub(owned)
    await hub.async_start()
    await report(owned, hub, {'145': 1})
    await report(owned, hub, {'145': 2})
    saved = hub.last_fed
    await hub.async_stop()
    assert len(hass_storage[f'petsafe_freshfeed.{hub.entry.entry_id}']['data']['events']) == 2
    restarted = FreshFeedHub(owned.hass, hub.entry)
    await restarted.async_start()
    assert [e.kind for e in restarted.events] == ['refilled', 'dispensed']
    assert restarted.last_fed == saved
    await restarted.async_stop()


async def test_unreported_dp1_does_not_establish_schedule(owned):
    hub = owned_hub(owned)
    owned.hass.states.async_set('text.optimistic', 'fwgAAQE=')
    await hub.async_start()
    assert hub.schedule.entries is None
    assert hub.schedule.source_state == 'unknown'
    assert hub.schedule.observed_at is None
    assert hub.schedule.revision is None
    await hub.async_stop()


async def test_schedule_only_restoration_does_not_create_feed_history(owned, hass_storage):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    hub = owned_hub(owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    cached = ScheduleState().with_report('fwgAAQE=', dt_util.utcnow())
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': {
        'version': 2, 'events': [], 'schedule': cached.to_dict()}}
    await hub.async_start()
    assert hub.schedule.entries == cached.entries
    assert hub.schedule.source_state == 'last_known'
    assert hub.schedule.revision == cached.revision
    assert hub.events == []
    assert hub.last_fed is None
    await hub.async_stop()


async def test_store_migration_and_schedule_restart(owned, hass_storage):
    hub = owned_hub(owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    event = {'kind': 'dispensed', 'compartment': 2, 'at': (dt_util.utcnow() - timedelta(days=2)).isoformat()}
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': [event]}
    await hub.async_start()
    assert [item.to_dict() for item in hub.events] == [event]
    assert hub.schedule.source_state == 'unknown'
    hub.schedule = hub.schedule.with_report('/w==', dt_util.utcnow())
    await hub.async_stop()
    assert hass_storage[key]['data']['events'] == [event]
    restarted = FreshFeedHub(owned.hass, hub.entry)
    await restarted.async_start()
    assert restarted.schedule.entries == ()
    assert restarted.schedule.source_state == 'last_known'
    assert [item.to_dict() for item in restarted.events] == [event]
    await restarted.async_stop()
    hass_storage[key]['data']['schedule'] = {'value': 'broken'}
    again = FreshFeedHub(owned.hass, hub.entry)
    await again.async_start()
    assert again.schedule.source_state == 'unknown'
    assert [item.to_dict() for item in again.events] == [event]
    await again.async_stop()


async def test_legacy_history_migration_preserves_multiple_events_with_missing_cache(owned, hass_storage, freezer):
    freezer.move_to('2026-11-10T12:00:00+00:00')
    hub = owned_hub(owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    events = [
        {'kind': 'refilled', 'compartment': 1, 'at': '2026-11-08T12:00:00+00:00'},
        {'kind': 'dispensed', 'compartment': 2, 'at': '2026-11-09T12:00:00+00:00'}]
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': events}
    await hub.async_start()
    assert [event.to_dict() for event in hub.events] == events
    assert hub.schedule.entries is None
    await hub.async_stop()
    del hass_storage[key]['data']['schedule']
    restarted = FreshFeedHub(owned.hass, hub.entry)
    await restarted.async_start()
    assert [event.to_dict() for event in restarted.events] == events
    assert restarted.schedule.entries is None
    assert restarted.last_fed == datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    await restarted.async_stop()


async def test_tick_updates_next_meal(owned, hass_storage, freezer):
    freezer.move_to('2026-11-10T11:59:30+00:00')
    hub = owned_hub(owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': [
        {'kind': 'dispensed', 'compartment': 2, 'at': '2026-11-08T12:00:00+00:00'},
        {'kind': 'dispensed', 'compartment': 3, 'at': '2026-11-09T12:00:00+00:00'}]}
    await hub.async_start()
    seen = []
    hub.async_add_listener(lambda: seen.append(True))
    assert hub.snapshot.meals_left == 0
    assert hub.next_meal is None
    # Frozen wall time cannot be used as a receive barrier here.
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    hub._device_report(DeviceReport(
        hub.bridge.epoch, 100, dt_util.utcnow(), {'145': 1}))
    assert hub.next_meal is None
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(owned.hass, dt_util.utcnow())
    await owned.hass.async_block_till_done()
    assert hub.next_meal is None
    assert seen
    await hub.async_stop()


async def test_unload_cleans_up_and_flushes(owned, hass_storage):
    hub = owned_hub(owned)
    await hub.async_start()
    seen = []
    hub.async_add_listener(lambda: seen.append(True))
    await report(owned, hub, {'145': 1})
    await hub.async_stop()
    assert hass_storage[f'petsafe_freshfeed.{hub.entry.entry_id}']['data']
    count = len(seen)
    owned.device.push_status({'145': 2})
    await owned.hass.async_block_till_done()
    assert len(seen) == count


async def test_no_service_calls(owned):
    with patch.object(type(owned.hass.services), 'async_call') as call:
        hub = owned_hub(owned)
        await hub.async_start()
        await report(owned, hub, {'145': 1})
        await hub.async_stop()
        call.assert_not_called()
    assert owned.device.controls == []


async def test_restore_last_fed_keeps_newest(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    newer = datetime(2026, 11, 10, tzinfo=timezone.utc)
    hub.restore_last_fed(newer)
    hub.restore_last_fed(newer - timedelta(days=1))
    assert hub.last_fed == newer
    await hub.async_stop()


async def test_forward_failure_stops_hub(owned, freezer):
    from custom_components.petsafe_freshfeed import async_setup_entry
    hub = owned_hub(owned)
    seen = []
    hub.async_add_listener(lambda: seen.append(True))
    with patch('custom_components.petsafe_freshfeed.hub.FreshFeedHub', return_value=hub), \
         patch.object(owned.hass.config_entries, 'async_forward_entry_setups', side_effect=RuntimeError('forward failed')):
        with pytest.raises(RuntimeError, match='forward failed'):
            await async_setup_entry(owned.hass, hub.entry)
    count = len(seen)
    owned.device.push_status({'145': 1})
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(owned.hass, dt_util.utcnow())
    await owned.hass.async_block_till_done()
    assert len(seen) == count
    assert not hub._unsubscribers
    assert all(sock.closed for sock in owned.device.sockets)


async def test_remove_entry_deletes_storage(owned, hass_storage):
    from custom_components.petsafe_freshfeed import async_remove_entry
    hub = owned_hub(owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': []}
    await async_remove_entry(owned.hass, hub.entry)
    assert key not in hass_storage


async def test_entry_reload_keeps_events_and_unload_flushes(owned, hass_storage):
    from homeassistant.config_entries import ConfigEntryState
    owned.device.dps['145'] = 6
    entry = await setup(owned, owned_data())
    await report(owned, entry.runtime_data, {'145': 1})
    await report(owned, entry.runtime_data, {'145': 2})
    fed = entry.runtime_data.last_fed
    assert await owned.hass.config_entries.async_reload(entry.entry_id)
    await owned.hass.async_block_till_done()
    assert [e.kind for e in entry.runtime_data.events] == ['refilled', 'dispensed']
    assert entry.runtime_data.last_fed == fed
    assert await owned.hass.config_entries.async_unload(entry.entry_id)
    await owned.hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert len(hass_storage[f'petsafe_freshfeed.{entry.entry_id}']['data']['events']) == 2
