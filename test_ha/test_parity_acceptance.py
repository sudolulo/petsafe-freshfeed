"""Local parity acceptance: actual receive, SQLite witness and coordinator restart."""
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ServiceValidationError

from test_write_lifecycle import pinned, _provision, _setup, _ids, _current, _load
from test_write_adapter import _queue_reports
from test_tuya_bridge import _frame, until


async def test_feed_service_dispatches_once_and_waits_for_actual_status(pinned):
    from homeassistant.core import Context
    from unittest.mock import AsyncMock

    ctx = pinned
    store_id = await _provision(ctx)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await _current(hub)
    ctx.hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    ctx.api.response = _frame({'4': 'standby'})
    response = await ctx.hass.services.async_call(
        'petsafe_freshfeed', 'command',
        {'entry_id': entry.entry_id, 'operation': 'feed', 'confirmed': True},
        context=Context(user_id='acceptance-admin'), blocking=True, return_response=True)
    assert response['admitted']
    assert ctx.writes == [b'synthetic-control']
    assert (await _load(ctx, store_id))['operation'] == 'feed'
    assert hub.write_view['state'] == 'uncertain'
    _queue_reports(SimpleNamespace(bridge=hub.bridge), {'140': 'EAEA'})
    await until(lambda: hub.write_view['state'] == 'verified')
    assert len(ctx.writes) == 1


async def test_schedule_verified_restart_recovery_and_restart_again(pinned):
    ctx = pinned
    store_id = await _provision(ctx)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await _current(hub)
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from homeassistant.core import Context
    ctx.user = SimpleNamespace(id='acceptance-admin')
    ctx.hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    async def service(service, data):
        return await ctx.hass.services.async_call(
            'petsafe_freshfeed', service, data,
            context=Context(user_id=ctx.user.id), blocking=True, return_response=True)
    with pytest.raises(ServiceValidationError, match='schedule_unknown'):
        await service('save_schedule', {'entry_id': entry.entry_id, 'entries': [], 'base_revision': 'unknown'})
    assert not ctx.writes
    _queue_reports(SimpleNamespace(bridge=hub.bridge), {'1': '/w=='})
    revision = hub.schedule.revision
    _queue_reports(SimpleNamespace(bridge=hub.bridge), {'1': 'fwgAAQE='})
    with pytest.raises(ServiceValidationError, match='stale_schedule'):
        await service('save_schedule', {'entry_id': entry.entry_id, 'entries': [], 'base_revision': revision})
    assert not ctx.writes
    # The HA service routes to the adapter; the matching post-dispatch DP1 echo verifies it.
    hub.schedule = hub.schedule.disconnected()
    ctx.api.response = _frame({'1': '/w=='})
    response = await service('save_schedule', {
        'entry_id': entry.entry_id, 'base_revision': hub.schedule.revision,
        'entries': [{'weekmask': 1, 'hour': 0, 'minute': 0, 'enabled': True, 'audible': False}],
    })
    assert response['admitted']
    assert ctx.writes == [b'synthetic-control']
    _queue_reports(SimpleNamespace(bridge=hub.bridge), {'1': 'AQAAAQA='})
    await until(lambda: hub.write_view['state'] == 'verified')
    assert len(ctx.writes) == 1
    first = dict(hub.write_view)
    record = await _load(ctx, store_id)
    assert record['operation_id'] == first['operation_id']
    assert 'dps' not in record and '/w==' not in repr(record)
    # The HA event Store records observations separately; schedule configuration
    # writes do not fabricate feed history and the witness stores intent metadata only.
    assert all(event.kind != 'dispensed' for event in hub.events)
    saved = await hub._store.async_load()
    assert saved['events'] == []
    assert saved['schedule']['value'] == 'AQAAAQA='
    assert saved['schedule']['observed_at'] == hub.schedule.observed_at.isoformat()
    assert 'dps' not in record and 'AQAAAQA=' not in repr(record)
    for iteration in range(2):
        assert await ctx.hass.config_entries.async_reload(entry.entry_id)
        hub = entry.runtime_data
        await _current(hub)
        view = dict(hub.write_view)
        assert view['state'] == 'uncertain'
        assert view['operation_id'] == first['operation_id']
        assert view['session_id'] != first['session_id']
        assert len(ctx.writes) == 1
        with pytest.raises(ServiceValidationError, match='recovery_rejected'):
            await service('recover', {'entry_id': entry.entry_id, 'operation_id': view['operation_id'],
                                      'session_id': first['session_id'], 'confirmed': True})
        if iteration:
            break
        request = ctx.hass.async_create_task(service('recover', {
            'entry_id': entry.entry_id, 'operation_id': view['operation_id'],
            'session_id': view['session_id'], 'confirmed': True}), eager_start=True)
        assert hub.write_view['recovery_phase'] == 'collecting'
        assert not request.done()
        _queue_reports(SimpleNamespace(bridge=hub.bridge),
                       {'1': '/w==', '4': 'standby', '145': 2, '140': 'EAAA', '142': 0})
        assert await request == {'resolved': True}
        assert len(ctx.writes) == 1
        assert hub.write_view['recovery_phase'] == 'resolved'
        assert (await _load(ctx, store_id)) == record
        first = view
