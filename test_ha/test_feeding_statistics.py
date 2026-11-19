"""Persistent observed meal counter contracts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest

from custom_components.petsafe_freshfeed.observed import FeedEvent
from custom_components.petsafe_freshfeed.sensor import FreshFeedSensor
from test_owned_hub import make_entry, owned_data
from custom_components.petsafe_freshfeed.hub import FreshFeedHub


def make_hub(hass, owned):
    owned.device.dps.pop('1', None)
    return FreshFeedHub(hass, make_entry(owned, owned_data()))


@pytest.fixture
def mock_recorder_before_hass(request, monkeypatch):
    # The recorder database fixture must run before HA's autouse fixture.
    if request.node.name == 'test_total_increasing_statistics_compile':
        request.getfixturevalue('recorder_db_url')
        # Python 3.14 autospec evaluates HA's TYPE_CHECKING-only annotation.
        from homeassistant.components.recorder import Recorder, migration
        monkeypatch.setattr(migration, 'Recorder', Recorder, raising=False)
        from homeassistant.helpers import recorder as recorder_helper
        from sqlalchemy.orm import Session
        monkeypatch.setattr(recorder_helper, 'Session', Session, raising=False)


@pytest.mark.parametrize('payload', [[], {'version': 2, 'events': [], 'schedule': None}])
async def test_v2_and_list_upgrade_zero_no_backfill(hass, owned, hass_storage, payload):
    hub = make_hub(hass, owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    now = datetime.now(timezone.utc)
    old_event = FeedEvent('dispensed', 2, now).to_dict()
    payload = deepcopy(payload)
    if isinstance(payload, list):
        payload.append(old_event)
    else:
        payload['events'] = [old_event]
        payload['schedule'] = ScheduleState().with_report('fwgAAQE=', now).to_dict()
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': payload}
    await hub.async_start()
    try:
        assert hub.observed_meals_dispensed_total == 0
        assert hub.counting_since.tzinfo is not None
        assert [event.to_dict() for event in hub.events] == [old_event]
        if isinstance(payload, dict):
            assert hub.schedule.to_dict()['value'] == 'fwgAAQE='
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'observed_meals_dispensed_total')
        assert sensor.native_value == 0
        assert sensor.native_unit_of_measurement == 'meals'
        assert sensor.state_class == 'total_increasing'
    finally:
        await hub.async_stop()


@pytest.mark.parametrize('counter', [
    {'observed_meals_dispensed_total': -1, 'counting_since': '2026-01-01T00:00:00+00:00'},
    {'observed_meals_dispensed_total': True, 'counting_since': '2026-01-01T00:00:00+00:00'},
    {'observed_meals_dispensed_total': {'bad': [1]}, 'counting_since': 'bad'},
    {'observed_meals_dispensed_total': 1, 'counting_since': '2026-01-01T00:00:00'},
    {'observed_meals_dispensed_total': 1, 'counting_since': 'bad'},
    {'observed_meals_dispensed_total': 1, 'counting_since': '2026-02-30T00:00:00+00:00'},
    {'observed_meals_dispensed_total': 1.5, 'counting_since': '2026-01-01T00:00:00+00:00'},
    {'observed_meals_dispensed_total': 1, 'counting_since': {'bad': [1]}},
    {'observed_meals_dispensed_total': 1},
    {'counting_since': '2026-01-01T00:00:00+00:00'},
])
@pytest.mark.parametrize('save_path', ['event', 'dispensed', 'schedule', 'disconnect', 'stop'])
async def test_corrupt_v3_survives_each_unrelated_save_and_reload(hass, owned, hass_storage, counter, save_path):
    hub = make_hub(hass, owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    original = deepcopy(counter)
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': {'version': 3, 'events': [], **counter}}
    await hub.async_start()
    assert hub.observed_meals_dispensed_total is None
    assert hub.statistics_reason == 'invalid_statistics_payload'
    assert hub.counting_since is None
    sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'observed_meals_dispensed_total')
    assert not sensor.available and sensor.native_value is None
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    now = datetime.now(timezone.utc)
    if save_path in {'event', 'dispensed', 'stop'}:
        hub._record_event(FeedEvent('dispensed' if save_path == 'dispensed' else 'meal_report', 2, now))
    elif save_path == 'schedule':
        hub._device_report(DeviceReport(1, 1, now, {'1': 'fwgAAQE='}))
    else:
        hub._device_report(DeviceReport(1, 1, now, {'1': 'fwgAAQE='}))
        hub._connection_changed(1, False)
    if save_path != 'stop':
        async_fire_time_changed(hass, now + timedelta(seconds=11))
        await hass.async_block_till_done()
        data = hass_storage[key]['data']
        assert {k: data[k] for k in original} == original
    assert await hub.async_stop()
    data = hass_storage[key]['data']
    assert {k: data[k] for k in ('observed_meals_dispensed_total', 'counting_since') if k in data} == original
    if save_path in {'event', 'dispensed', 'stop'}:
        assert len(data['events']) == 1
    else:
        assert data['schedule']['value'] == 'fwgAAQE='
    assert hub.observed_meals_dispensed_total is None
    restarted = type(hub)(hass, hub.entry)
    await restarted.async_start()
    try:
        assert restarted.observed_meals_dispensed_total is None
        assert restarted.statistics_reason == 'invalid_statistics_payload'
    finally:
        await restarted.async_stop()


@pytest.mark.parametrize('payload', [
    {'version': 4, 'events': [], 'future': {'counter': [4]}},
    {'version': 1, 'events': [], 'observed_meals_dispensed_total': 9},
    {'version': True, 'events': []},
    {'version': 3.0, 'events': []},
    {'events': [], 'counting_since': 'unknown'},
    'unrecognized payload',
])
async def test_unsupported_store_is_preserved_on_save_and_reload(hass, owned, hass_storage, payload):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    hub = make_hub(hass, owned)
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    original = deepcopy(payload)
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': deepcopy(payload)}
    await hub.async_start()
    assert hub.observed_meals_dispensed_total is None
    assert hub.counting_since is None
    assert hub.statistics_reason == 'invalid_statistics_payload'
    now = datetime.now(timezone.utc)
    hub._record_event(FeedEvent('dispensed', 2, now))
    async_fire_time_changed(hass, now + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert hass_storage[key]['data'] == original
    assert await hub.async_stop()
    assert hass_storage[key]['data'] == original
    restarted = type(hub)(hass, hub.entry)
    await restarted.async_start()
    try:
        assert restarted.observed_meals_dispensed_total is None
        assert restarted.statistics_reason == 'invalid_statistics_payload'
    finally:
        await restarted.async_stop()
    assert hass_storage[key]['data'] == original


async def test_total_increasing_statistics_compile(recorder_mock, hass, owned, freezer):
    """Real recorder: refill is flat; backup rollback starts a new meter cycle."""
    from homeassistant.components.recorder import get_instance
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done, do_adhoc_statistics, statistics_during_period,
    )
    from test_entities import state
    from test_owned_hub import setup
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    freezer.move_to(start)
    entry = await setup(owned, owned_data(), title='PetSafe FreshFeed')
    hub = entry.runtime_data
    entity_id = state(hass, 'sensor', 'observed_meals_dispensed_total').entity_id
    try:
        for minute, kind in [(1, 'dispensed'), (2, 'dispensed'), (3, 'refilled')]:
            freezer.move_to(start + timedelta(minutes=minute))
            hub._record_event(FeedEvent(kind, 2, start + timedelta(minutes=minute)))
            await hass.async_block_till_done()
        current = hass.states.get(entity_id)
        assert current.state == '2'
        assert current.attributes['unit_of_measurement'] == 'meals'
        assert current.attributes['state_class'] == 'total_increasing'
        assert 'device_class' not in current.attributes
        await async_wait_recording_done(hass)
        # Compile all twelve five-minute periods, including the hourly rollup.
        for minute in range(0, 60, 5):
            do_adhoc_statistics(hass, start=start + timedelta(minutes=minute))
        await async_wait_recording_done(hass)
        rows = await get_instance(hass).async_add_executor_job(partial(
            statistics_during_period, hass, start, start + timedelta(hours=1),
            {entity_id}, 'hour'))
        assert rows[entity_id][0]['sum'] == 2
        # A restored lower counter is not substituted with recorder's prior value.
        freezer.move_to(start + timedelta(hours=1))
        hub.observed_meals_dispensed_total = 0
        hub._notify()
        await hass.async_block_till_done()
        freezer.move_to(start + timedelta(hours=1, minutes=1))
        hub._record_event(FeedEvent('dispensed', 2, start + timedelta(hours=1, minutes=1)))
        await async_wait_recording_done(hass)
        for minute in range(0, 60, 5):
            do_adhoc_statistics(hass, start=start + timedelta(hours=1, minutes=minute))
        await async_wait_recording_done(hass)
        rows = await get_instance(hass).async_add_executor_job(partial(
            statistics_during_period, hass, start, start + timedelta(hours=2),
            {entity_id}, 'hour'))
        assert [row['sum'] for row in rows[entity_id]] == [2, 3]
        assert hub.observed_meals_dispensed_total == 1
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_only_new_dispensed_events_increment_owned(hass, owned):
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    hub = make_hub(hass, owned)
    await hub.async_start()
    try:
        # Use each production observation boundary; all transport remains synthetic.
        at = datetime.now(timezone.utc)
        async def report(seq, dps, epoch=1):
            hub._device_report(DeviceReport(epoch, seq, at, dps))
            await hass.async_block_till_done()
        await report(1, {'145': 'unavailable'})
        await report(2, {'145': 2, '14': 0, '150': 0})
        assert hub.observed_meals_dispensed_total == 0
        for seq, dps in enumerate(({'145': 3}, {'145': 3}, {'145': 1},
                                   {'14': 1}, {'14': 9}, {'14': 10}, {'150': 1}), 3):
            await report(seq, dps)
        assert hub.observed_meals_dispensed_total == 1
        assert [e.kind for e in hub.events].count('dispensed') == 1
        # Reconnect snapshot is a new baseline, never an offline increment.
        await report(11, {'145': 'unavailable'})
        await report(12, {'145': 2}, epoch=2)
        assert hub.observed_meals_dispensed_total == 1
        await report(13, {'145': 3}, epoch=2)
        assert hub.observed_meals_dispensed_total == 2
    finally:
        await hub.async_stop()


async def test_total_survives_pruning_refill_reload_and_orderly_stop(hass, owned):
    hub = make_hub(hass, owned)
    await hub.async_start()
    now = datetime.now(timezone.utc)
    for i in range(502):
        hub._record_event(FeedEvent('dispensed', 2, now))
    hub._record_event(FeedEvent('refilled', 1, now))
    assert len(hub.events) == 500
    assert hub.observed_meals_dispensed_total == 502
    await hub.async_stop()
    restarted = type(hub)(hass, hub.entry)
    await restarted.async_start()
    try:
        assert restarted.observed_meals_dispensed_total == 502
    finally:
        await restarted.async_stop()
