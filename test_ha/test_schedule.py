"""Owned schedule receive, epoch and shutdown contracts."""
from datetime import datetime, timezone

import pytest

from custom_components.petsafe_freshfeed.local_source import DeviceReport
from custom_components.petsafe_freshfeed.sensor import FreshFeedSensor
from owned_test_support import until
from test_hub import owned_hub, report


async def test_cloud_schedule_view_demand_and_local_precedence(owned):
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.account import DPRead
    hub = owned_hub(owned)
    async def fetch():
        return DPRead('ok', dp1='fwgAAQE=')
    hub.cloud_read = CloudRead(fetch)
    await hub.async_start()
    try:
        await owned.hass.async_block_till_done()
        # Initial missing DP140 demand also supplies the schedule when present.
        assert hub.schedule.provenance == 'cloud'
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'schedule')
        await sensor.async_update()
        assert hub.schedule.provenance == 'cloud'
        next_sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'next_meal')
        assert sensor.extra_state_attributes['provenance'] == 'cloud'
        assert next_sensor.extra_state_attributes['provenance'] == 'cloud'
        revision = hub.schedule.revision
        await report(owned, hub, {'1': '/w=='})
        assert hub.schedule.provenance == 'lan'
        assert hub.schedule.revision != revision
    finally:
        await hub.async_stop()


async def test_fully_local_schedule_refresh_never_requests_cloud_or_reauth(owned, monkeypatch):
    from custom_components.petsafe_freshfeed.cloud_read import create_entry_reader
    from custom_components.petsafe_freshfeed import account, repairs
    hub = owned_hub(owned)
    hub.entry.add_to_hass(owned.hass)
    calls = []
    monkeypatch.setattr(account, 'read_dps', lambda *args, **kwargs: calls.append('network'))
    monkeypatch.setattr(repairs, 'async_cloud_auth_issue', lambda *args: calls.append('repairs'))
    monkeypatch.setattr(type(hub.entry), 'async_start_reauth', lambda *args: calls.append('reauth'))
    hub.cloud_read = create_entry_reader(owned.hass, hub.entry)
    await hub.async_start()
    try:
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'schedule')
        await sensor.async_update()
        await sensor.async_update()
        assert calls == []
        assert hub.schedule.source_state == 'unknown'
    finally:
        await hub.async_stop()


async def test_other_need_cloud_read_updates_schedule(owned):
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.account import DPRead
    hub = owned_hub(owned)
    now = [0.0]
    results = [DPRead('unavailable'), DPRead('ok', dp1='fwgAAQE=')]
    async def fetch():
        return results.pop(0)
    hub.cloud_read = CloudRead(fetch, clock=lambda: now[0])
    await hub.async_start()
    try:
        await owned.hass.async_block_till_done()
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'schedule')
        await sensor.async_update()
        await sensor.async_update()
        assert len(results) == 1  # Connection and repeated view refresh share one budget.
        assert hub.schedule.source_state == 'unknown'
        now[0] = 60.0
        await sensor.async_update()
        assert hub.schedule.provenance == 'cloud'
        assert hub.schedule.entries is not None
    finally:
        await hub.async_stop()


@pytest.mark.parametrize('window', ['submit', 'ack'])
async def test_cloud_result_during_write_window_is_not_consumed(owned, window):
    import asyncio
    from types import SimpleNamespace
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.account import DPRead
    hub = owned_hub(owned)
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        started.set()
        await release.wait()
        return DPRead('ok', dp1='fwgAAQE=')
    receipt_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    from unittest.mock import patch
    wall_time = [receipt_time]
    hub.cloud_read = CloudRead(fetch)
    await hub.async_start()
    try:
        owned.hass.async_create_task(hub._demand_cloud_schedule(hub.bridge.epoch, hub.bridge, hub._binding_generation))
        await started.wait()
        hub.adapter = SimpleNamespace(
            _submitting=object() if window == 'submit' else None,
            _ack_wait=object() if window == 'ack' else None, _collector=None,
            transport=SimpleNamespace(pending_operation_id='pending' if window == 'pending' else None))
        with patch('homeassistant.util.dt.utcnow', side_effect=lambda: wall_time[0]):
            release.set()
            await owned.hass.async_block_till_done()
            assert hub.schedule.source_state == 'unknown'
            assert hub.schedule.provenance is None
            wall_time[0] = later_time
            hub.adapter._submitting = None
            hub.adapter._ack_wait = None
            hub.adapter.transport.pending_operation_id = None
            hub._notify()
            assert hub.schedule.provenance == 'cloud'
            assert hub.schedule.observed_at == receipt_time
            assert hub._serialize()['schedule']['observed_at'] == receipt_time.isoformat()
    finally:
        hub.adapter = None
        release.set()
        await hub.async_stop()


async def test_retained_uncertain_intent_allows_cloud_schedule_demand_and_consumption(owned):
    from types import SimpleNamespace
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.account import DPRead
    hub = owned_hub(owned)
    calls = []
    async def fetch():
        calls.append(True)
        return DPRead('ok', dp1='fwgAAQE=')
    hub.cloud_read = CloudRead(fetch)
    await hub.async_start()
    try:
        # A schedule view can read despite retained uncertainty.
        hub.adapter = SimpleNamespace(_submitting=None, _ack_wait=None, _collector=None,
                                      transport=SimpleNamespace(pending_operation_id='retained'))
        await hub._demand_cloud_schedule(hub.bridge.epoch, hub.bridge, hub._binding_generation)
        assert calls == [True]
        assert hub.schedule.provenance == 'cloud'
        assert hub.adapter.transport.pending_operation_id == 'retained'
    finally:
        hub.adapter = None
        await hub.async_stop()


async def test_deferred_cloud_schedule_cannot_override_later_local_dp1(owned):
    import asyncio
    from types import SimpleNamespace
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.account import DPRead
    hub = owned_hub(owned)
    started, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        started.set()
        await release.wait()
        return DPRead('ok', dp1='fwgAAQE=')
    hub.cloud_read = CloudRead(fetch)
    await hub.async_start()
    try:
        owned.hass.async_create_task(hub._demand_cloud_schedule(hub.bridge.epoch, hub.bridge, hub._binding_generation))
        await started.wait()
        hub.adapter = SimpleNamespace(_submitting=object(), _ack_wait=None, _collector=None,
                                      transport=SimpleNamespace(pending_operation_id=None))
        release.set()
        await owned.hass.async_block_till_done()
        await report(owned, hub, {'1': '/w=='})
        revision = hub.schedule.revision
        hub.adapter._submitting = None
        hub._notify()
        assert hub.schedule.provenance == 'lan' and hub.schedule.revision == revision
    finally:
        hub.adapter = None
        release.set()
        await hub.async_stop()


async def test_cloud_provenance_survives_owned_start_failure_and_retry(owned, hass_storage):
    from homeassistant.util import dt as dt_util
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    from custom_components.petsafe_freshfeed.owned_source import FAILURE
    hub = owned_hub(owned)
    cached = ScheduleState().with_cloud_report('fwgAAQE=', dt_util.utcnow())
    key = f'petsafe_freshfeed.{hub.entry.entry_id}'
    hass_storage[key] = {'version': 1, 'minor_version': 1, 'key': key, 'data': {
        'version': 2, 'events': [], 'schedule': cached.to_dict()}}
    owned.device.refuse = True
    await hub.async_start()
    try:
        assert hub.schedule.error == FAILURE
        assert hub.schedule.provenance == 'cloud'
        owned.device.refuse = False
        await hub._async_attach_owned()
        assert hub.schedule.error is None
        assert hub.schedule.provenance == 'cloud'
        assert hub.schedule.revision == cached.revision
    finally:
        await hub.async_stop()


async def test_loaded_receive_not_optimistic_state(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        assert hub.bridge is not None
        owned.hass.states.async_set('text.optimistic', '/w==')
        await owned.hass.async_block_till_done()
        assert hub.schedule.source_state == 'unknown'
        await report(owned, hub, {'1': '/w=='})
        assert hub.schedule.source_state == 'current'
        owned.device.refuse = True
        owned.device.sockets[-1].drop()
        await until(lambda: hub.schedule.source_state == 'last_known')
        assert hub.schedule.entries == ()
    finally:
        await hub.async_stop()


async def test_queued_report_survives_disconnect_as_last_known(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        # Cache hook queues a genuine receive before the close notification.
        hub.bridge.api._cache_response({'dps': {'1': '/w=='}})
        owned.device.refuse = True
        hub.bridge.api.close()
        await until(lambda: hub.schedule.source_state == 'last_known')
        assert hub.schedule.entries == ()
    finally:
        await hub.async_stop()


async def test_reconnect_demotes_without_schedule_report(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        await report(owned, hub, {'1': '/w=='})
        assert hub.schedule.source_state == 'current'
        old_socket = hub.bridge.api.socket
        owned.device.refuse = True
        old_socket.drop()
        await until(lambda: not hub.bridge.connected)
        assert hub.schedule.source_state == 'last_known'
        owned.device.refuse = False
        await until(lambda: hub.bridge.connected)
        assert hub.bridge.api.socket is not old_socket
        assert hub.schedule.source_state == 'last_known'
    finally:
        await hub.async_stop()


async def test_close_before_cache_retains_last_known(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        owned.device.refuse = True
        hub.bridge.api.close()
        hub.bridge.api._cache_response({'dps': {'1': '/w=='}})
        await until(lambda: hub.schedule.entries == ())
        assert hub.schedule.source_state == 'last_known'
    finally:
        await hub.async_stop()


async def test_disconnect_clears_quick_feed(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'quick_feed')
        await report(owned, hub, {'140': 'EAIA'})
        assert sensor.native_value == 'feeding'
        owned.device.refuse = True
        owned.device.sockets[-1].drop()
        await until(lambda: not hub.bridge.connected)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {'command': None, 'minutes': None}
    finally:
        await hub.async_stop()


async def test_schedule_only_reports_promote_and_disconnect_demotes(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, 'schedule')
        assert sensor.native_value is None
        await report(owned, hub, {'145': 5})
        assert hub.schedule.source_state == 'unknown'
        await report(owned, hub, {'1': '/w=='})
        assert sensor.native_value == 0
        assert sensor.extra_state_attributes['entries'] == []
        assert set(sensor.extra_state_attributes) == {
            'entries', 'source_state', 'provenance', 'observed_at', 'revision', 'source_entity_id', 'error'}
        assert hub.schedule.source_state == 'current'
        await report(owned, hub, {'1': 'invalid'})
        assert hub.schedule.entries == ()
        assert hub.schedule.error == 'invalid_schedule'
        owned.device.refuse = True
        owned.device.sockets[-1].drop()
        await until(lambda: not hub.bridge.connected)
        assert hub.schedule.source_state == 'last_known'
        assert sensor.available
    finally:
        await hub.async_stop()


async def test_restart_old_client_cannot_promote_schedule(owned):
    hub = owned_hub(owned)
    await hub.async_start()
    old_api = hub.bridge.api
    await hub.async_stop()
    restarted = type(hub)(owned.hass, hub.entry)
    await restarted.async_start()
    try:
        assert restarted.bridge.api is not old_api
        old_api._cache_response({'dps': {'1': '/w=='}})
        await owned.hass.async_block_till_done()
        assert restarted.schedule.source_state == 'unknown'
        await report(owned, restarted, {'1': '/w=='})
        assert restarted.schedule.source_state == 'current'
    finally:
        await restarted.async_stop()
    assert '_cache_response' not in old_api.__dict__


async def test_unload_invalidates_before_lock_released(owned):
    import asyncio
    hub = owned_hub(owned)
    await hub.async_start()
    await report(owned, hub, {'1': '/w=='})
    bridge = hub.bridge
    await bridge.lock.acquire()
    task = asyncio.create_task(hub.async_stop())
    try:
        await until(lambda: not bridge.is_current())
        bridge.api._cache_response({'dps': {'1': 'fwgAAQE='}})
        await owned.hass.async_block_till_done(wait_background_tasks=False)
        assert hub.schedule.entries == ()
    finally:
        bridge.lock.release()
        assert await task
    assert hub.bridge is None
    assert hub.schedule.source_state == 'last_known'
