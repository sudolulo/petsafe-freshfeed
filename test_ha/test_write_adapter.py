"""Durable write admission and explicit recovery orchestration (Task 6).

Synthetic DPS and an explicitly provisioned, owned SQLite witness under the
test's temporary directory only: never HA ``.storage``, no network, no
credentials. The real owned source uses pinned TinyTuya and an injected
protocol-3.5 device; no Tuya Local imports are permitted.
"""
import asyncio
from datetime import timedelta
import logging
import os
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from test_owned_source import (  # noqa: F401 - fixtures
    FakeDevice, FakeSocket, pinned, no_tuya_local, start, until, HEADER_35,
)
from owned_test_support import _frame
import json


@pytest.fixture
async def owned(hass, pinned, monkeypatch):
    device = FakeDevice(pinned, dps={"4": "standby"}).install(monkeypatch)
    device.response = None
    device.before_send = None
    def on_control(sock, payload):
        if device.before_send:
            device.before_send()
        response = device.response
        body = b"" if response is None else HEADER_35 + response.payload
        sock.push(device.frame(13, body, sock.session_key))
    device.on_control = on_control
    source = await start(hass, device)
    ctx = SimpleNamespace(hass=hass, device=device, api=source.api, bridge=source)
    try:
        yield ctx
    finally:
        assert await source.async_stop()


@pytest.mark.parametrize('first', ['EAEA', 'invalid'])
async def test_cloud_veto_before_adapter_construction_survives_idle(first):
    from custom_components.petsafe_freshfeed.account import DPRead
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.quick_feed import decode_quick_feed
    from custom_components.petsafe_freshfeed.write_adapter import WriteAdapter

    clock = [0.0]
    values = iter([first, 'EAAA'])
    async def fetch():
        return DPRead('ok', None, decode_quick_feed(next(values)), True)
    cloud = CloudRead(fetch, clock=lambda: clock[0])
    await cloud.demand(epoch=1, sequences={})
    clock[0] = 60.0
    await cloud.demand(epoch=1, sequences={})
    entry = SimpleNamespace(runtime_data=SimpleNamespace(cloud_read=cloud))
    adapter = WriteAdapter(None, entry, SimpleNamespace(epoch=1), None, None, None)
    assert adapter._cloud_veto
    await cloud.close()


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
async def test_failed_adapter_construction_unsubscribes_cloud(failure):
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.write_adapter import async_create_adapter

    async def fetch():
        raise AssertionError('no fetch expected')
    cloud = CloudRead(fetch)
    unsubscribed = []
    async def enable(*args, **kwargs):
        raise failure()
    bridge = SimpleNamespace(epoch=1, subscribe=lambda *args: lambda: unsubscribed.append(True),
                             async_enable_transport=enable)
    entry = SimpleNamespace(runtime_data=SimpleNamespace(witness=object(), cloud_read=cloud))
    with pytest.raises(failure):
        await async_create_adapter(None, entry, bridge, ack_predicate=lambda *a: None,
                                   recovery_predicate=lambda *a: None)
    assert unsubscribed == [True]
    assert cloud._listeners == []
    await cloud.close()


SCHEDULE = "fwceAQA="  # every day, 07:30, enabled, silent (last pushed value)
VALUE = "fwgAAQE="     # every day, 08:00, enabled, audible


def _observed_on_dp1(context, mark, report):
    if context.operation == "schedule" and report.dps.get("1") == context.dps.get("1"):
        return "observed"
    return "ignore"


def _standby_after_request(context, evidence):
    # Approves only when the LATEST DP4 in the post-request evidence is idle.
    states = [report.dps["4"] for report in evidence.reports if "4" in report.dps]
    return bool(states) and states[-1] == "standby"


class GatedWitness:
    """Delegates to the real witness; counts and optionally gates preparation."""

    def __init__(self, inner):
        self.inner = inner
        self.prepares = []
        self.gate = None
        self.entered = asyncio.Event()
        self.fail = False

    async def async_load_last_intent(self):
        return await self.inner.async_load_last_intent()

    async def async_prepare_intent(self, record):
        self.prepares.append(dict(record))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("synthetic-private-storage")
        await self.inner.async_prepare_intent(record)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "owned-witness"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return SimpleNamespace(path=str(root / "witness.sqlite"),
                           store_id=uuid4().hex, binding_id=uuid4().hex)


async def _provision(store):
    from custom_components.petsafe_freshfeed.sqlite_witness import SQLiteIntentWitness
    return await SQLiteIntentWitness.async_provision(
        store.path, store_id=store.store_id, binding_id=store.binding_id)


def _open(store):
    from custom_components.petsafe_freshfeed.sqlite_witness import SQLiteIntentWitness
    return SQLiteIntentWitness(store.path, store_id=store.store_id, binding_id=store.binding_id)


@pytest.fixture
def writes(owned):
    """Every CONTROL frame decoded by the synthetic protocol-3.5 device."""
    return owned.device.controls


async def _adapter(ctx, witness, *, ack=_observed_on_dp1, recovery=_standby_after_request,
                   schedule_value=SCHEDULE):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    from custom_components.petsafe_freshfeed.write_adapter import async_create_adapter
    if not hasattr(ctx, "adapters"):
        ctx.adapters = []
        assert ctx.bridge.is_current()
        await ctx.bridge.async_claim_receive()
    schedule = (ScheduleState() if schedule_value is None
                else ScheduleState().with_report(schedule_value, dt_util.utcnow()))
    entry = SimpleNamespace(entry_id="synthetic", runtime_data=SimpleNamespace(
        witness=witness, schedule=schedule))
    adapter = await async_create_adapter(ctx.hass, entry, ctx.bridge,
                                         ack_predicate=ack, recovery_predicate=recovery)
    ctx.adapters.append(adapter)
    return adapter


@pytest.fixture
async def adapters(owned):
    yield owned
    for adapter in getattr(owned, "adapters", ()):
        assert await adapter.async_stop()
        assert all(task.done() for task in adapter._tasks)
        assert adapter._collector is None
    assert not [thread for thread in threading.enumerate()
                if thread.name.startswith("petsafe-critical")]


def _push(ctx, dps):
    ctx.device.push_status(dict(dps))


def _queue_reports(ctx, *items):
    """Number several decoded reports in ONE synchronous owning-loop turn."""
    from datetime import datetime, timezone
    from time import monotonic
    bridge = ctx.bridge
    with bridge._guard:
        for dps in items:
            bridge._events.append((bridge._generation, (
                "report", datetime.now(timezone.utc), dict(dps), True, monotonic())))
    bridge.flush()


async def test_queued_old_report_cannot_supply_fresh_admission(adapters, store, writes, monkeypatch):
    from custom_components.petsafe_freshfeed import owned_source, write_adapter
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    clock = [100.0]
    monkeypatch.setattr(owned_source, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(write_adapter, 'monotonic', lambda: clock[0])
    with ctx.bridge._guard:
        ctx.bridge.api._cache_response({'dps': {'4': 'standby', '142': 0, '145': 2}})
    clock[0] = 136.0
    ctx.bridge.flush()
    assert not adapter.view['can_feed']
    result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
    assert not result['admitted']
    assert not writes


async def test_queued_report_ages_before_dispatch(adapters, store, writes, monkeypatch):
    from custom_components.petsafe_freshfeed import owned_source, write_adapter
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    witness.gate = asyncio.Event()
    adapter = await _adapter(ctx, witness)
    clock = [100.0]
    monkeypatch.setattr(owned_source, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(write_adapter, 'monotonic', lambda: clock[0])
    ctx.bridge.api._cache_response({'dps': {'4': 'standby', '142': 0, '145': 2}})
    ctx.bridge.flush()
    task = ctx.hass.async_create_task(adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True))
    await asyncio.wait_for(witness.entered.wait(), 3)
    ctx.bridge.api._cache_response({'dps': {'4': 'standby', '142': 0, '145': 2}})
    clock[0] = 136.0
    witness.gate.set()
    await task
    assert not writes


async def test_timed_out_connection_cloud_fetch_allows_local_fallback(adapters, store, writes):
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from types import SimpleNamespace

    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SimpleNamespace(outcome='unavailable')

    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    cloud = CloudRead(fetch, deadline=0.01)
    adapter.entry.runtime_data.cloud_read = cloud
    adapter._on_connection(ctx.bridge.epoch, True)
    await entered.wait()
    await until(lambda: cloud._waiters == 0)
    _queue_reports(ctx, {'4': 'standby', '142': 0, '145': 2})
    try:
        assert cloud.in_flight
        result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
        assert result['admitted']
        assert writes
        assert cloud.in_flight
    finally:
        release.set()
        await cloud.close()


async def test_schedule_edit_demands_dp1_even_with_local_dp140(adapters, store, writes):
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.account import DPRead
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    calls = []
    async def fetch():
        calls.append(True)
        return DPRead('unavailable')
    cloud = CloudRead(fetch)
    adapter.entry.runtime_data.cloud_read = cloud
    _queue_reports(ctx, {'140': 'EAAA'})
    try:
        await adapter.async_submit('schedule', {'1': '/w=='}, base_revision='unknown')
        assert calls == [True]
        assert not writes
    finally:
        await cloud.close()


async def test_missing_quick_feed_uses_fresh_local_evidence_for_feed(adapters, store, writes):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    _queue_reports(ctx, {'4': 'standby', '142': 0, '145': 2})
    assert adapter.view['can_feed'] is True
    result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
    assert result['admitted'] is True
    assert writes[-1]['data']['dps']['140'] == 'EA=='


@pytest.mark.parametrize('outcome', ['ok', 'malformed'])
@pytest.mark.parametrize('quick', ['EAEA', 'EAIA', 'invalid'])
async def test_recovery_reads_cloud_veto_despite_uncertain_intent(adapters, store, writes, outcome, quick):
    from custom_components.petsafe_freshfeed.account import DPRead
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.quick_feed import decode_quick_feed
    ctx = adapters
    witness, operation_id = await _retained(store, 'feed')
    adapter = await _adapter(ctx, witness)
    calls = []
    async def fetch():
        calls.append(True)
        return DPRead(outcome, None, decode_quick_feed(quick), True)
    cloud = CloudRead(fetch)
    adapter.entry.runtime_data.cloud_read = cloud
    cloud.subscribe(adapter._cloud_updated)
    _queue_reports(ctx, {'4': 'standby', '142': 0, '145': 2})
    assert adapter._gate_reason() == 'intent_uncertain'
    # Avoid waiting for recovery if the regression drops the veto.
    adapter._can_start = lambda phase: False
    assert not await adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True)
    assert calls == [True]
    assert adapter._cloud_veto
    assert '140' not in adapter._live_dps
    assert adapter.transport.needs_reconciliation
    assert not writes
    await cloud.close()


@pytest.mark.parametrize('operation', ['feed', 'defrost', 'refilled', 'settings', 'cancel', 'schedule'])
@pytest.mark.parametrize('local', [None, 'EAAA', 'EAMA', 'EAQA', 'EAEA', 'EAIA', 'invalid'])
@pytest.mark.parametrize('cloud,age', [(None, None), ('EAAA', 0), ('EAMA', 0), ('EAQA', 0),
    ('EAEA', 0), ('EAIA', 0), ('invalid', 0), ('EAMA', 60), ('EAEA', 60), ('invalid', 60)])
def test_dp140_table_admission_and_dispatch(monkeypatch, operation, local, cloud, age):
    from custom_components.petsafe_freshfeed import write_adapter as module
    from custom_components.petsafe_freshfeed.local_source import DispatchMark
    from custom_components.petsafe_freshfeed.const import FEED_NOW_COMMAND, FEED_START_COMMAND
    adapter = object.__new__(module.WriteAdapter)
    monkeypatch.setattr(module, 'monotonic', lambda: 100)
    adapter._live_dps = {'4': 'standby', '142': 0, '145': 2}
    if local is not None:
        adapter._live_dps['140'] = local
    adapter._live_times = dict.fromkeys(adapter._live_dps, 100)
    adapter._live_sequences = dict.fromkeys(adapter._live_dps, 1)
    adapter._cloud_veto = False
    adapter._cloud_evidence = lambda: (SimpleNamespace(value=cloud, received_at=100-age)
                                     if age is not None else None)
    selected = local if local is not None else cloud
    allowed = (operation == 'schedule' or
        (operation == 'cancel' and local == 'EAEA') or
        (operation not in ('schedule', 'cancel') and selected in (None, 'EAAA', 'EAMA', 'EAQA')))
    expected = (FEED_NOW_COMMAND if selected == 'EAMA' and (local is not None or age < 60)
                else FEED_START_COMMAND) if allowed and operation == 'feed' else None
    reason, command = adapter._decision(operation)
    assert (reason is None) == allowed
    assert command == expected
    adapter._stopped = False
    adapter._caller = None
    adapter.bridge = SimpleNamespace(flush=lambda: None)
    adapter.transport = SimpleNamespace(pending_operation_id='op')
    adapter._compatible_now = lambda: True
    adapter._revision_reason = lambda *args: None
    adapter._admitted_revision = 'revision'
    adapter._marks, adapter._ack_deadlines = {}, {}
    payload = {'140': expected} if operation == 'feed' else {'110': True}
    adapter._submitting = module.IntentContext(operation, '', 'session', 1, payload)
    if allowed:
        adapter._on_dispatch(DispatchMark('op', 1, 2))
        assert adapter._current.dps == payload
    else:
        with pytest.raises(ValueError, match='write_adapter_refused'):
            adapter._on_dispatch(DispatchMark('op', 1, 2))


@pytest.mark.parametrize('cloud', [None, 'EAAA', 'EAMA', 'EAQA'],
                         ids=['absent', 'idle', 'ready', 'failed'])
@pytest.mark.parametrize('operation,dps,allowed', [
    ('feed', {}, True), ('defrost', {}, True),
    ('refilled', {}, True), ('settings', {}, True),
    ('feed', {'142': True}, False), ('feed', {'145': True}, False),
    ('feed', {'4': 'no_food'}, False), ('feed', {'145': 6}, False),
    ('refilled', {'145': 1}, False), ('defrost', {'4': 'no_food'}, True),
    ('refilled', {'4': 'no_food'}, True), ('settings', {'4': 'no_food'}, True),
])
def test_fresh_local_edge_cases_at_admission_and_dispatch(monkeypatch, operation, dps, allowed, cloud):
    from custom_components.petsafe_freshfeed import write_adapter as module
    from custom_components.petsafe_freshfeed.local_source import DispatchMark
    from custom_components.petsafe_freshfeed.const import FEED_NOW_COMMAND, FEED_START_COMMAND
    adapter = object.__new__(module.WriteAdapter)
    monkeypatch.setattr(module, 'monotonic', lambda: 100)
    adapter._live_dps = {'4': 'standby', '142': 0, '145': 2, **dps}
    adapter._live_times = dict.fromkeys(adapter._live_dps, 100)
    adapter._live_sequences = dict.fromkeys(adapter._live_dps, 1)
    adapter._cloud_veto = False
    adapter._cloud_evidence = lambda: (SimpleNamespace(value=cloud, received_at=100)
                                     if cloud is not None else None)
    # Raw no_food is permitted only by E, never the valid-cloud C predicate.
    if cloud is not None and dps.get('4') == 'no_food':
        allowed = False
    feed_command = FEED_NOW_COMMAND if cloud == 'EAMA' else FEED_START_COMMAND
    reason, command = adapter._decision(operation)
    assert (reason is None) is allowed
    assert command == (feed_command if allowed and operation == 'feed' else None)
    adapter._stopped, adapter._caller = False, None
    adapter.bridge = SimpleNamespace(flush=lambda: None)
    adapter.transport = SimpleNamespace(pending_operation_id='op')
    adapter._compatible_now = lambda: True
    adapter._revision_reason = lambda *args: None
    adapter._admitted_revision = 'revision'
    adapter._marks, adapter._ack_deadlines = {}, {}
    payload = {'140': feed_command} if operation == 'feed' else {'110': True}
    adapter._submitting = module.IntentContext(operation, '', 'session', 1, payload)
    if allowed:
        adapter._on_dispatch(DispatchMark('op', 1, 2))
        assert adapter._current.dps == payload
    else:
        with pytest.raises(ValueError, match='write_adapter_refused'):
            adapter._on_dispatch(DispatchMark('op', 1, 2))


@pytest.mark.parametrize('change', ['expiry', 'local_idle', 'local_pending'])
def test_cloud_ready_feed_command_is_rechecked_at_dispatch(monkeypatch, change):
    from custom_components.petsafe_freshfeed import write_adapter as module
    from custom_components.petsafe_freshfeed.local_source import DispatchMark
    from custom_components.petsafe_freshfeed.const import FEED_NOW_COMMAND
    adapter = object.__new__(module.WriteAdapter)
    now = [100]
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    adapter._live_dps = {'4': 'standby', '142': 0, '145': 2}
    adapter._live_times = dict.fromkeys(adapter._live_dps, 100)
    adapter._live_sequences = dict.fromkeys(adapter._live_dps, 1)
    adapter._cloud_veto = False
    adapter._cloud_evidence = lambda: SimpleNamespace(value='EAMA', received_at=100)
    assert adapter._decision('feed') == (None, FEED_NOW_COMMAND)
    adapter._stopped, adapter._caller = False, None
    adapter.bridge = SimpleNamespace(flush=lambda: None)
    adapter.transport = SimpleNamespace(pending_operation_id='op')
    adapter._compatible_now = lambda: True
    adapter._revision_reason = lambda *args: None
    adapter._admitted_revision = 'revision'
    adapter._marks, adapter._ack_deadlines = {}, {}
    adapter._submitting = module.IntentContext('feed', '', 'session', 1, {'140': FEED_NOW_COMMAND})
    if change == 'expiry':
        now[0] = 160
        adapter._live_times.update(dict.fromkeys(adapter._live_dps, 160))
    else:
        adapter._live_dps['140'] = 'EAAA' if change == 'local_idle' else 'EAEA'
        adapter._live_sequences['140'] = 2
    with pytest.raises(ValueError, match='write_adapter_refused'):
        adapter._on_dispatch(DispatchMark('op', 1, 2))


BASE_KEYS = {"recovery_resolution", "state", "operation_id", "operation", "session_id", "reason", "observed_failure", "storage_fault",
             "integrity_fault", "recovery_can_start", "recovery_phase",
             "can_feed", "can_defrost", "can_refilled", "can_cancel", "cancel_reason", "blocked_reason"}


def _check_view(adapter, **expected):
    view = adapter.view
    assert set(view) == BASE_KEYS
    for key, value in expected.items():
        assert view[key] == value, (key, view)
    return view


async def _retained(store, operation="schedule"):
    witness = await _provision(store)
    operation_id = uuid4().hex
    await witness.async_prepare_intent(
        {"operation_id": operation_id, "operation": operation, "state": "pending"})
    return _open(store), operation_id


@pytest.mark.parametrize('operation', ['feed', 'cancel', 'defrost', 'refilled', 'schedule', 'settings'])
async def test_never_observed_dp140_abandons_without_inventing_idle(adapters, store, writes, operation):
    from custom_components.petsafe_freshfeed.services import acknowledgement, recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, operation)
    retained = await witness.async_load_last_intent()
    adapter = await _adapter(ctx, witness, ack=acknowledgement, recovery=recovery_allowed, schedule_value=None)
    session = adapter.view['session_id']
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, session, confirmed=True))
    await until(lambda: adapter._collector is not None)
    command_boundary, query_boundary = len(ctx.device.commands), len(ctx.device.queries)
    ctx.device.dps = {'4': 'no_food', '142': 0, '145': 6}
    ctx.bridge.POLL_INTERVAL = 0.01  # accelerate the unchanged full-status monitor
    assert await asyncio.wait_for(task, 3)
    assert 16 in ctx.device.commands[command_boundary:]
    assert set(ctx.device.commands[command_boundary:]) <= {9, 16}
    assert ctx.device.queries[query_boundary:]
    assert all(payload == b'{}' for payload in ctx.device.queries[query_boundary:])
    assert adapter.view['recovery_resolution'] == dict(operation_id=operation_id, session_id=session, operation=operation, outcome='observed_safe' if operation in ('defrost', 'refilled') else 'abandoned')
    assert '140' not in adapter._live_dps
    assert not adapter.view['can_feed']
    assert adapter.entry.runtime_data.schedule.entries is None
    assert await witness.async_load_last_intent() == retained
    assert not writes
    assert not ctx.device.control_frames()
    assert set(ctx.device.commands) <= {3, 5, 9, 16}
    assert all(payload == b'{}' for payload in ctx.device.queries)
    adapter._on_connection(ctx.bridge.epoch + 1, False)
    assert adapter.view['recovery_resolution'] is None


async def test_recovery_uses_observed_same_epoch_safety_baseline(adapters, store, writes):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    _queue_reports(ctx, {'4': 'no_food', '142': 0, '145': 6, '140': 'IAAA'})
    session = adapter.view['session_id']
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, session, confirmed=True))
    await until(lambda: adapter._collector is not None)
    command_boundary, query_boundary = len(ctx.device.commands), len(ctx.device.queries)
    ctx.device.dps = {'110': True}
    ctx.bridge.POLL_INTERVAL = 0.01  # sparse report via the real read-only status path
    assert await asyncio.wait_for(task, 3)
    commands = ctx.device.commands[command_boundary:]
    queries = ctx.device.queries[query_boundary:]
    assert 16 in commands  # decoded DP_QUERY_NEW, not just adapter call counts
    assert set(commands) <= {9, 16}  # heartbeat/status only; no CONTROL or UPDATEDPS
    assert queries and all(payload == b'{}' for payload in queries)
    assert not ctx.device.control_frames()
    assert adapter.view['recovery_resolution']['outcome'] == 'observed_safe'
    assert not writes


@pytest.mark.parametrize('invalid', [
    {'4': value} for value in ('feeding', 'defrost', 'defrosting', 'enum_4', 'idle', 'ready', 'empty', 'run_out', 'unknown', None, [], {})
] + [{'140': value} for value in ('EAEA', 'bad')] + [
    {'142': value} for value in (True, -1, 1, '0', None)
] + [{'145': value} for value in (True, 0, 7, '6', None)])
async def test_recovery_invalid_then_safe_never_releases(adapters, store, invalid):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    _queue_reports(ctx, invalid, {'4': 'standby', '140': 'IAAA', '142': 0, '145': 6})
    assert not await task
    assert adapter.view['state'] == 'uncertain'
    assert adapter.view['recovery_resolution'] is None


@pytest.mark.parametrize('sequence_delta', [0, -1])
async def test_recovery_reordered_report_invalidates_attempt(adapters, store, sequence_delta):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    _queue_reports(ctx, {'110': True})
    adapter.on_report(DeviceReport(ctx.bridge.epoch, adapter._live_sequence + sequence_delta,
                                   dt_util.utcnow(), {'142': 0}))
    _queue_reports(ctx, {'4': 'standby', '140': 'IAAA', '142': 0, '145': 6})
    assert not await task
    assert adapter.view['recovery_resolution'] is None


async def test_no_fresh_report_never_releases_and_baseline_is_frozen(adapters, store):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    _queue_reports(ctx, {'4': 'standby', '140': 'IAAA', '142': 0, '145': 6})
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    collector = adapter._collector
    adapter._live_dps['145'] = 2
    evidence = collector.evidence()
    assert evidence.baseline_dps['145'] == 6
    evidence.baseline_dps['145'] = 3
    assert collector.evidence().baseline_dps['145'] == 6
    collector.timer.cancel()
    collector.expire()
    assert not await task
    assert adapter.view['recovery_resolution'] is None
    assert adapter.transport.needs_reconciliation


@pytest.mark.parametrize('age_minutes', [None, 0, 256, 10000, -60])
@pytest.mark.parametrize('fresh', [False, True])
async def test_external_age_is_not_recovery_evidence(adapters, store, age_minutes, fresh):
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    retained = await witness.async_load_last_intent()
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    # An external timestamp is not part of the durable witness or recovery context.
    at = dt_util.utcnow() if age_minutes is None else dt_util.utcnow() - timedelta(minutes=age_minutes)
    dps = {'4': 'no_food', '142': 0, '145': 6} if fresh else {'110': True}
    adapter.on_report(DeviceReport(ctx.bridge.epoch, ctx.bridge.last_sequence + 1, at, dps))
    if not fresh:
        adapter._collector.timer.cancel()
        adapter._collector.expire()
    assert await task is fresh
    assert adapter.view['recovery_resolution'] == (dict(operation_id=operation_id, session_id=adapter.view['session_id'], operation='cancel', outcome='abandoned') if fresh else None)
    assert await witness.async_load_last_intent() == retained


@pytest.mark.parametrize('active', [{'4': 'feeding'}, {'140': 'EAEA'}])
async def test_ancient_external_timestamp_cannot_erase_active_veto(adapters, store, active):
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    ancient = dt_util.utcnow() - timedelta(days=100)
    adapter.on_report(DeviceReport(ctx.bridge.epoch, ctx.bridge.last_sequence + 1, ancient, active))
    adapter.on_report(DeviceReport(ctx.bridge.epoch, ctx.bridge.last_sequence + 2, ancient,
                                   {'4': 'standby', '140': 'IAAA', '142': 0, '145': 6}))
    assert not await task
    assert adapter.transport.needs_reconciliation
    assert adapter.view['recovery_resolution'] is None


@pytest.mark.parametrize('change', ['late_active', 'late_invalid', 'socket', 'disconnect', 'session', 'operation', 'operation_id', 'owner', 'storage', 'integrity'])
async def test_recovery_final_validation_rejects_invalidation(adapters, store, monkeypatch, change):
    from datetime import datetime, timezone
    from time import monotonic
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    _queue_reports(ctx, {'4': 'standby', '140': 'IAAA', '142': 0, '145': 6})
    if change.startswith('late_'):
        with ctx.bridge._guard:
            ctx.bridge._events.append((ctx.bridge._generation, ('report', datetime.now(timezone.utc),
                {'4': 'feeding'} if change == 'late_active' else {'142': True}, True, monotonic())))
    elif change == 'socket':
        ctx.api.socket = FakeSocket(ctx.device)
    elif change == 'disconnect':
        adapter._on_connection(ctx.bridge.epoch + 1, False)
    elif change == 'session':
        monkeypatch.setattr(adapter.transport, '_recovery_session_id', 'different')
    elif change == 'operation':
        adapter.transport._pending['operation'] = 'feed'
    elif change == 'operation_id':
        adapter.transport._pending['operation_id'] = uuid4().hex
    elif change == 'owner':
        monkeypatch.setattr(adapter.transport, '_is_owner', lambda: False)
    elif change == 'storage':
        monkeypatch.setattr(adapter, '_faults', lambda: (True, False))
    else:
        monkeypatch.setattr(adapter, '_faults', lambda: (False, True))
    assert not await task
    if change == 'late_active':
        assert adapter._live_dps['4'] == 'feeding'
    elif change == 'late_invalid':
        assert adapter._live_dps['142'] is True
    assert adapter.transport.needs_reconciliation
    assert adapter.view['recovery_resolution'] is None


async def test_old_epoch_baseline_cannot_support_sparse_recovery(adapters, store):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store, 'cancel')
    adapter = await _adapter(ctx, witness, recovery=recovery_allowed)
    _queue_reports(ctx, {'4': 'standby', '140': 'IAAA', '142': 0, '145': 6})
    adapter._live_epoch = ctx.bridge.epoch - 1
    task = ctx.hass.async_create_task(adapter.async_recover(operation_id, adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    assert adapter._collector.baseline_dps == {}
    _queue_reports(ctx, {'110': True})
    adapter._collector.timer.cancel()
    adapter._collector.expire()
    assert not await task
    assert adapter.transport.needs_reconciliation


async def test_read_only_recovery_poll_cannot_acknowledge_write(adapters, store):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = adapters
    calls = []
    adapter = await _adapter(ctx, await _provision(store),
        ack=lambda *args: calls.append(args) or 'ignore', recovery=recovery_allowed)
    _queue_reports(ctx, {'4': 'standby', '145': 6, '140': 'EAEA', '142': 0})
    result = await adapter.async_submit('cancel', {'140': 'IA=='}, confirmed=True)
    assert result['state'] == 'uncertain'
    _queue_reports(ctx, {'140': 'IAAA'})  # actual idle report permits recovery to start
    calls.clear()
    task = ctx.hass.async_create_task(adapter.async_recover(result['operation_id'], adapter.view['session_id'], confirmed=True))
    await until(lambda: adapter._collector is not None)
    command_boundary, query_boundary = len(ctx.device.commands), len(ctx.device.queries)
    ctx.device.dps = {'4': 'no_food', '142': 0, '145': 6}
    ctx.bridge.POLL_INTERVAL = 0.01
    assert await asyncio.wait_for(task, 3)
    assert 16 in ctx.device.commands[command_boundary:]
    assert set(ctx.device.commands[command_boundary:]) <= {9, 16}
    assert ctx.device.queries[query_boundary:] and all(q == b'{}' for q in ctx.device.queries[query_boundary:])
    assert calls == [] and adapter._decisions == {}
    assert adapter.view['recovery_resolution']['outcome'] == 'observed_safe'
    assert adapter.view['state'] == 'idle' and adapter._result is None


def _revision(value):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    return ScheduleState().with_report(value, dt_util.utcnow()).revision


async def test_cancel_sends_fixed_payload_and_retains_context(adapters, store, writes):
    from custom_components.petsafe_freshfeed.services import acknowledgement, recovery_allowed
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness, ack=acknowledgement, recovery=recovery_allowed)
    _queue_reports(ctx, {'4': 'standby', '145': 6, '140': 'EAEA', '142': 0})
    assert adapter.view['can_cancel'] is True
    before = int(dt_util.utcnow().timestamp())
    result = await adapter.async_submit('cancel', {'140': 'IA=='}, confirmed=True)
    after = int(dt_util.utcnow().timestamp())
    assert result['admitted']
    assert len(writes) == 1
    timestamp = writes[0]['t']
    assert type(timestamp) is int and before <= timestamp <= after
    assert writes == [{'protocol': 5, 't': timestamp, 'data': {'dps': {'140': 'IA=='}}}]
    assert len(ctx.device.control_frames()) == 1
    assert adapter._current.dps == {'140': 'IA=='}
    assert witness.prepares[0]['operation'] == 'cancel'
    assert adapter.view['state'] == 'uncertain'


async def test_cancel_refused_if_pending_changes_during_witness(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    witness.gate = asyncio.Event()
    adapter = await _adapter(ctx, witness)
    _queue_reports(ctx, {'4': 'standby', '145': 1, '140': 'EAEA', '142': 0})
    task = ctx.hass.async_create_task(adapter.async_submit('cancel', {'140': 'IA=='}, confirmed=True))
    await asyncio.wait_for(witness.entered.wait(), 3)
    _queue_reports(ctx, {'140': 'EAIA'})
    witness.gate.set()
    await task
    assert not writes


# ----- admission routes -------------------------------------------------------

async def test_production_recovery_timeout_then_fresh_complete_retry(adapters, store, writes):
    from custom_components.petsafe_freshfeed.services import acknowledgement, recovery_allowed
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness, ack=acknowledgement, recovery=recovery_allowed)
    session = adapter.view['session_id']
    assert adapter.view['recovery_can_start'] is True
    _queue_reports(ctx, {'140': 'EAIA'})
    assert adapter.view['recovery_can_start'] is False
    assert adapter.view['reason'] == 'quick_feed_active'
    _queue_reports(ctx, {'140': 'EAAA'})
    request = ctx.hass.async_create_task(adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    assert adapter.view['recovery_phase'] == 'collecting'
    assert not await adapter.async_recover(operation_id, session, confirmed=True)
    adapter._collector.timer.cancel()
    adapter._collector.expire()
    assert not await request
    assert adapter.view['recovery_phase'] == 'failed'
    assert adapter.view['reason'] == 'awaiting_device_report'
    assert adapter.view['recovery_can_start'] is True
    request = ctx.hass.async_create_task(adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    _queue_reports(ctx, {'4':'standby','145':2})
    assert not request.done()
    _queue_reports(ctx, {'1': '/w=='})
    assert await request
    assert adapter.view['recovery_phase'] == 'resolved'
    assert not writes
    assert await witness.async_load_last_intent() is not None


async def test_actual_status_gates_commands_and_connection_clears_it(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
    assert result['reason'] == 'status_unavailable'
    assert not witness.prepares and not writes
    _queue_reports(ctx, {'4':'standby','145':2,'140':'EAAA','142':0})
    assert adapter.view['can_feed'] is True
    _queue_reports(ctx, {'140':'EAIA'})
    assert adapter.view['can_feed'] is False
    result = await adapter.async_submit('feed', {'140':'EA=='}, confirmed=True)
    assert result['reason'] == 'quick_feed_active'
    assert not witness.prepares and not writes
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    adapter.on_report(DeviceReport(ctx.bridge.epoch, 0, dt_util.utcnow(),
                                  {'140': 'EAAA'}))
    assert adapter.view['can_feed'] is False  # older report cannot restore permission
    adapter._on_connection(ctx.bridge.epoch, False)
    assert adapter.view['can_refilled'] is False



@pytest.mark.parametrize('operation,dps,response', [
    ('schedule', {'1': VALUE}, {'1': VALUE}),
    ('feed', {'140': 'EA=='}, {'140': 'AAEA'}),
])
async def test_buffered_exchange_response_is_observed_but_cannot_ack(
        adapters, store, writes, operation, dps, response):
    ctx = adapters
    seen, predicate_calls = [], []
    adapter = await _adapter(ctx, await _provision(store),
                             ack=lambda *args: predicate_calls.append(args) or 'observed')
    ctx.bridge.subscribe(seen.append, lambda *args: None)
    _queue_reports(ctx, {'4': 'standby', '145': 2, '140': 'EAAA', '142': 0})
    # This frame exists before the send; the exchange consumes it after the mark.
    ctx.device.response = _frame(response)
    kwargs = {'base_revision': _revision(SCHEDULE)} if operation == 'schedule' else {'confirmed': True}
    result = await adapter.async_submit(operation, dps, **kwargs)
    assert result['state'] == 'uncertain'
    matching = [report for report in seen if report.dps == response]
    assert len(matching) == 1
    mark = adapter._marks[result['operation_id']]
    assert matching[0].sequence > mark.sequence
    assert matching[0].ack_eligible is False
    assert not predicate_calls and adapter.transport.needs_reconciliation
    assert len(writes) == 1
    _push(ctx, response)
    await until(lambda: adapter.view['state'] == 'verified')
    assert len(predicate_calls) == 1 and predicate_calls[0][2].ack_eligible is True


async def test_later_eligible_echo_verifies_and_retains_witness(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    seen = []

    def ack(context, mark, report):
        seen.append((context, mark, report))
        return _observed_on_dp1(context, mark, report)
    adapter = await _adapter(ctx, witness, ack=ack)
    _check_view(adapter, state="idle", operation_id=None, recovery_phase="idle",
                recovery_can_start=False, storage_fault=False, integrity_fault=False)
    ctx.device.response = _frame({"1": VALUE})
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    assert result["admitted"] is True and result["state"] == "uncertain"
    _push(ctx, {"1": VALUE})
    await until(lambda: adapter.view["state"] == "verified")
    assert len(writes) == 1 and len(witness.prepares) == 1
    operation_id = witness.prepares[0]["operation_id"]
    view = _check_view(adapter, state="verified", operation_id=operation_id,
                       operation="schedule", recovery_phase="idle", recovery_can_start=False)
    assert "dps" not in view and VALUE not in repr(view)
    context, mark, report = seen[-1]
    assert context.operation_id == mark.operation_id == operation_id
    assert report.epoch == mark.epoch and report.sequence > mark.sequence
    assert not adapter.transport.needs_reconciliation
    # The durable witness is never cleared by a local acknowledgement.
    assert (await _open(store).async_load_last_intent())["operation_id"] == operation_id


async def test_observed_failure_releases_without_verifying(adapters, store, writes):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store),
                             ack=lambda *args: "observed_failure")
    ctx.device.response = _frame({"1": VALUE})
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    assert result["admitted"] is True and len(writes) == 1
    assert result["state"] == "uncertain"
    _push(ctx, {"1": VALUE})
    await until(lambda: not adapter.transport.needs_reconciliation)
    _check_view(adapter, state="idle", reason="observed_failure", recovery_phase="idle")
    assert not adapter.transport.needs_reconciliation
    assert (await _open(store).async_load_last_intent())["operation_id"] is not None


async def test_cloud_schedule_revision_changes_before_dispatch_refuse_send(adapters, store, writes):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    witness.gate = asyncio.Event()
    adapter = await _adapter(ctx, witness, schedule_value=None)
    hub = adapter.entry.runtime_data
    hub.schedule = ScheduleState().with_cloud_report(SCHEDULE, dt_util.utcnow())
    revision = hub.schedule.revision
    submit = ctx.hass.async_create_task(adapter.async_submit('schedule', {'1': VALUE}, base_revision=revision))
    await witness.entered.wait()
    hub.schedule = hub.schedule.with_report(VALUE, dt_util.utcnow())
    witness.gate.set()
    result = await submit
    assert result['admitted']  # conservative receipt semantics, not proof of dispatch
    assert not adapter._marks and not writes


async def test_cloud_schedule_evidence_never_acknowledges_write(adapters, store, writes):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    ctx = adapters
    adapter = await _adapter(ctx, GatedWitness(await _provision(store)), schedule_value=None)
    hub = adapter.entry.runtime_data
    hub.schedule = ScheduleState().with_cloud_report(SCHEDULE, dt_util.utcnow())
    result = await adapter.async_submit('schedule', {'1': VALUE}, base_revision=hub.schedule.revision)
    assert result['state'] == 'uncertain' and len(writes) == 1
    hub.schedule = hub.schedule.with_cloud_report(VALUE, dt_util.utcnow())
    assert adapter.view['state'] == 'uncertain'
    assert adapter.transport.needs_reconciliation


async def test_fully_local_unknown_schedule_refuses_save(adapters, store, writes):
    ctx = adapters
    adapter = await _adapter(ctx, GatedWitness(await _provision(store)), schedule_value=None)
    assert adapter.entry.runtime_data.schedule.entries is None
    result = await adapter.async_submit('schedule', {'1': '/w=='}, base_revision='')
    assert not result['admitted'] and result['reason'] == 'schedule_unknown'
    assert not writes


async def test_pre_mark_echo_ignored_and_late_post_mark_echo_releases(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    witness.gate = asyncio.Event()
    ctx.device.response = None
    submit = ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE)))
    await witness.entered.wait()
    _check_view(adapter, state="pending", recovery_can_start=False, recovery_phase="idle")
    # Duplicate while pending: rejected before any preparation.
    duplicate = await adapter.async_submit("schedule", {"1": VALUE},
                                           base_revision=_revision(SCHEDULE))
    assert duplicate["admitted"] is False and duplicate["reason"] == "busy"
    _queue_reports(ctx, {"1": VALUE})  # matching, but before the send boundary
    witness.gate.set()
    result = await submit
    assert result["admitted"] is True and len(writes) == 1 and len(witness.prepares) == 1
    _check_view(adapter, state="uncertain", reason="awaiting_device_report",
                recovery_phase="awaiting_user_confirmation", recovery_can_start=True)
    assert adapter.transport.needs_reconciliation
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    (mark,) = adapter._marks.values()
    now = dt_util.utcnow()
    for report in (DeviceReport(mark.epoch, mark.sequence, now, {"1": VALUE}),
                   DeviceReport(mark.epoch + 1, mark.sequence + 50, now, {"1": VALUE})):
        adapter.on_report(report)  # not after the mark on the dispatch epoch
    await asyncio.sleep(0)
    _check_view(adapter, state="uncertain")
    assert adapter._decisions == {}
    _push(ctx, {"1": VALUE})  # genuine post-mark device echo on the same epoch
    await until(lambda: adapter.view["state"] == "verified")
    _check_view(adapter, recovery_phase="idle", recovery_can_start=False)
    assert len(writes) == 1


async def test_queued_ack_does_not_release_after_disconnect(adapters, store, writes, monkeypatch):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    ctx.device.response = None
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    assert result["state"] == "uncertain"
    # Force a queued (non-eager) release to reproduce the scheduling race.
    monkeypatch.setattr(ctx.hass, "async_create_task", lambda coro: asyncio.create_task(coro))
    _queue_reports(ctx, {"1": VALUE})
    ctx.api.close()
    await asyncio.sleep(0.1)
    assert adapter.transport.needs_reconciliation
    assert adapter.view["state"] == "uncertain"


async def test_observed_echo_with_integrity_fault_never_releases(adapters, store, writes,
                                                               monkeypatch):
    ctx = adapters
    api = ctx.api
    adapter = await _adapter(ctx, await _provision(store))
    original = FakeSocket.settimeout
    timeout = api.socket.gettimeout()

    def broken(self, value):
        original(self, value)
        if ctx.device.controls and value == timeout:
            raise RuntimeError('synthetic restoration failure')
    monkeypatch.setattr(FakeSocket, "settimeout", broken)
    ctx.device.response = _frame({"1": VALUE})
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    monkeypatch.setattr(FakeSocket, "settimeout", original)
    api.socketRetryLimit = 1
    assert result["admitted"] is True and len(writes) == 1
    view = _check_view(adapter, state="blocked", integrity_fault=True,
                       recovery_can_start=False, reason="integrity_fault")
    assert adapter.transport.needs_reconciliation
    assert not await adapter.async_recover(view["operation_id"], view["session_id"],
                                           confirmed=True)
    _push(ctx, {"1": VALUE})
    await asyncio.sleep(0.2)
    _check_view(adapter, state="blocked", integrity_fault=True)


async def test_storage_fault_blocks_without_send(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    witness.fail = True
    adapter = await _adapter(ctx, witness)
    ctx.device.response = _frame({"1": VALUE})
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    assert result["admitted"] is True and writes == []
    _check_view(adapter, state="blocked", storage_fault=True, reason="storage_fault",
                recovery_can_start=False)
    again = await adapter.async_submit("schedule", {"1": VALUE},
                                       base_revision=_revision(SCHEDULE))
    assert again["admitted"] is False and len(witness.prepares) == 1 and writes == []


async def test_concurrent_duplicates_admit_at_most_one(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    witness.gate = asyncio.Event()
    ctx.device.response = _frame({"1": VALUE})
    tasks = [ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE))) for _ in range(4)]
    await witness.entered.wait()
    await until(lambda: sum(task.done() for task in tasks) == 3)
    witness.gate.set()
    results = [await task for task in tasks]
    assert sum(result["admitted"] for result in results) == 1
    assert {result["reason"] for result in results if not result["admitted"]} == {"busy"}
    assert len(witness.prepares) == 1 and len(writes) == 1


@pytest.mark.parametrize("change", ["code", "control_flow", "binding", "logger", "socket"])
async def test_changed_source_rejects_before_prepare_or_send(adapters, store, writes,
                                                             monkeypatch, change):
    from custom_components.petsafe_freshfeed import owned_source
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    lock = ctx.bridge.lock
    logger = logging.getLogger('tinytuya')
    if change == "code":
        monkeypatch.setattr(owned_source, "check_loaded_client", lambda *args: False)
    elif change == "control_flow":
        monkeypatch.setattr(owned_source, "hooks_intact", lambda *args: False)
    elif change == "binding":
        monkeypatch.setattr(ctx.api, '_get_socket', lambda *args: True)
    elif change == "logger":
        logger.setLevel(logging.DEBUG)
    else:
        ctx.api.socket.closed = True
    try:
        result = await adapter.async_submit("schedule", {"1": VALUE},
                                            base_revision=_revision(SCHEDULE))
    finally:
        logger.setLevel(logging.WARNING)
        ctx.bridge.lock = lock
        ctx.api.socket.closed = False
    assert result["admitted"] is False and result["reason"] == "source_incompatible"
    assert witness.prepares == [] and writes == []
    _check_view(adapter, state="idle")


async def test_request_validation_rejects_before_prepare(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    cases = [
        (("schedule", {"1": VALUE}), {"base_revision": "0" * 64}, "stale_schedule"),
        (("schedule", {"1": VALUE}), {}, "stale_schedule"),
        (("schedule", {"1": "7f080001"}), {"base_revision": _revision(SCHEDULE)},
         "invalid_request"),
        (("schedule", {"1": VALUE, "4": "standby"}), {"base_revision": _revision(SCHEDULE)},
         "invalid_request"),
        (("feed", {"140": "EA=="}), {}, "confirmation_required"),
        (("feed", {"140": "EA=="}), {"confirmed": 1}, "confirmation_required"),
        (("feed", {"140": "IA=="}), {"confirmed": True}, "invalid_request"),  # cancel
        (("feed", {"140": "MA=="}), {"confirmed": True}, "invalid_request"),  # now
        (("defrost", {"142": True}), {"confirmed": True}, "invalid_request"),
        (("refilled", {"120": 1}), {"confirmed": True}, "invalid_request"),
        (("settings", {"3": 1}), {"confirmed": True}, "invalid_request"),
        (("feed", {"140": "EA=="}), {"confirmed": True, "base_revision": "x"},
         "invalid_request"),
    ]
    for args, kwargs, reason in cases:
        result = await adapter.async_submit(*args, **kwargs)
        assert result == {"admitted": False, "state": "idle", "operation_id": None,
                          "reason": reason}, (args, kwargs)
    assert witness.prepares == [] and writes == []


async def test_unknown_schedule_rejects_even_empty_vector(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness, schedule_value=None)
    result = await adapter.async_submit("schedule", {"1": "/w=="}, base_revision="")
    assert result["admitted"] is False and result["reason"] == "schedule_unknown"
    assert witness.prepares == [] and writes == []


async def test_confirmed_operations_route_to_the_owned_transport(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    ctx.device.response = None
    _queue_reports(ctx, {"4": "standby", "145": 2, "140": "EAAA", "142": 0})
    result = await adapter.async_submit("feed", {"140": "EA=="}, confirmed=True)
    assert result["admitted"] is True and result["state"] == "uncertain"
    assert [record["operation"] for record in witness.prepares] == ["feed"]
    assert len(writes) == 1
    # Default predicates never acknowledge: a status report cannot verify.
    _push(ctx, {"140": "AAEA"})
    await asyncio.sleep(0.2)
    _check_view(adapter, state="uncertain", operation="feed")
    blocked = await adapter.async_submit("defrost", {"142": 1}, confirmed=True)
    assert blocked["admitted"] is False and blocked["reason"] == "intent_uncertain"
    assert len(writes) == 1


@pytest.mark.parametrize('status,expected', [
    ('EAAA', 'EA=='), ('EAQA', 'EA=='), ('EAMA', 'MA=='),
])
async def test_feed_selects_reported_state_payload(adapters, store, writes, status, expected):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    sent = []
    original = adapter.transport.async_send

    async def capture(operation, dps, **kwargs):
        sent.append((operation, dict(dps)))
        return await original(operation, dps, **kwargs)

    adapter.transport.async_send = capture
    _queue_reports(ctx, {'4': 'standby', '145': 2, '140': status, '142': 0})
    result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
    assert result['admitted'] is True
    assert sent == [('feed', {'140': expected})]
    assert len(writes) == 1
    assert adapter.recovery_context().dps == {'140': expected}


@pytest.mark.parametrize('operation,payload', [('feed', {'140': 'EA=='}), ('defrost', {'142': 1}), ('refilled', {'120': True}), ('settings', {'110': False})])
async def test_latest_local_idle_allows_operations_after_active(adapters, store, writes, operation, payload):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    _queue_reports(ctx, {'4': 'standby', '145': 2, '142': 0, '140': 'EAEA'}, {'140': 'EAIA'}, {'140': 'EAAA'})
    if operation != 'settings':
        assert adapter.view['can_' + operation]
    assert adapter._decision(operation)[0] is None
    result = await adapter.async_submit(operation, payload, confirmed=True)
    assert result['admitted']
    assert len(writes) == 1


@pytest.mark.parametrize('unsafe,reason', [('EAEA', 'quick_feed_active'), ('EAIA', 'quick_feed_active'), ('invalid', 'status_unavailable')])
async def test_latest_unsafe_local_after_idle_denies(adapters, store, writes, unsafe, reason):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    _queue_reports(ctx, {'4': 'standby', '145': 2, '142': 0, '140': 'EAAA'}, {'140': unsafe})
    for operation in ('feed', 'defrost', 'refilled', 'settings'):
        assert adapter._decision(operation)[0] == reason
    assert not adapter.view['can_feed']
    assert not (await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True))['admitted']
    assert not writes


@pytest.mark.parametrize('unsafe', ['EAEA', 'EAIA', 'invalid'])
async def test_latest_idle_at_dispatch_supersedes_earlier_unsafe(adapters, store, writes, unsafe):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    witness.gate = asyncio.Event()
    _queue_reports(ctx, {'4': 'standby', '145': 2, '142': 0, '140': 'EAAA'})
    task = ctx.hass.async_create_task(adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True))
    await witness.entered.wait()
    _queue_reports(ctx, {'140': unsafe}, {'140': 'EAAA'})
    witness.gate.set()
    assert (await task)['admitted']
    assert len(writes) == 1


@pytest.mark.parametrize('initial,later', [('EAMA', 'EAAA'), ('EAAA', 'EAMA')])
async def test_feed_state_change_during_witness_refuses_send(adapters, store, writes, initial, later):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    witness.gate = asyncio.Event()
    _queue_reports(ctx, {'4': 'standby', '145': 2, '140': initial, '142': 0})
    task = ctx.hass.async_create_task(adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True))
    await witness.entered.wait()
    _queue_reports(ctx, {'140': later})
    witness.gate.set()
    await task
    assert writes == []


@pytest.mark.parametrize('initial,later,expected', [('EAMA', 'EAAA', 'EA=='), ('EAAA', 'EAMA', 'MA==')])
async def test_feed_state_change_during_provenance_selects_final_state(
        adapters, store, writes, monkeypatch, initial, later, expected):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    gate, entered = asyncio.Event(), asyncio.Event()
    original_executor = ctx.hass.async_add_executor_job
    original_send = adapter.transport.async_send
    sent = []

    def gated_executor(func, *args):
        if func != ctx.bridge.code_ok:
            return original_executor(func, *args)

        async def run():
            entered.set()
            await gate.wait()
            return await original_executor(func, *args)
        return asyncio.create_task(run())

    async def capture(operation, dps, **kwargs):
        sent.append(dict(dps))
        return await original_send(operation, dps, **kwargs)

    monkeypatch.setattr(ctx.hass, 'async_add_executor_job', gated_executor)
    adapter.transport.async_send = capture
    _queue_reports(ctx, {'4': 'standby', '145': 2, '140': initial, '142': 0})
    task = ctx.hass.async_create_task(adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True))
    await entered.wait()
    _queue_reports(ctx, {'140': later})
    gate.set()
    assert (await task)['admitted'] is True
    assert sent == [{'140': expected}] and len(writes) == 1


@pytest.mark.parametrize('status,counter,reason', [
    ('EAEA', 2, 'quick_feed_active'), ('EAIA', 2, 'quick_feed_active'),
    ('invalid', 2, 'status_unavailable'), ('EAMA', 6, 'empty_tray'),
])
async def test_feed_unsafe_states_never_send(adapters, store, writes, status, counter, reason):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    _queue_reports(ctx, {'4': 'standby', '145': counter, '140': status, '142': 0})
    result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
    assert result['admitted'] is False and result['reason'] == reason
    assert not witness.prepares and not writes


# ----- recovery admission and phases -------------------------------------------


async def test_recovery_phases_timeout_then_fresh_evidence_resolves(adapters, store, writes):
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    session = adapter.view["session_id"]
    _check_view(adapter, state="uncertain", operation_id=operation_id, operation="schedule",
                recovery_phase="awaiting_user_confirmation", recovery_can_start=True,
                storage_fault=False, integrity_fault=False)
    for kwargs in ({"confirmed": False}, {"confirmed": 1}):
        assert not await adapter.async_recover(operation_id, session, **kwargs)
    assert not await adapter.async_recover(operation_id, uuid4().hex, confirmed=True)
    assert not await adapter.async_recover(uuid4().hex, session, confirmed=True)
    _check_view(adapter, recovery_phase="awaiting_user_confirmation", recovery_can_start=True)
    # ZERO post-request reports: the confirmed request is admitted immediately.
    first = ctx.hass.async_create_task(
        adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    _check_view(adapter, state="uncertain", recovery_phase="collecting",
                recovery_can_start=False)
    handle = adapter._collector.timer
    assert not await adapter.async_recover(operation_id, session, confirmed=True)
    assert adapter._collector.timer is handle and not handle.cancelled()
    blocked = await adapter.async_submit("schedule", {"1": VALUE},
                                         base_revision=_revision(SCHEDULE))
    assert blocked["admitted"] is False
    async_fire_time_changed(ctx.hass, dt_util.utcnow() + timedelta(seconds=30))
    await asyncio.sleep(0)
    assert not first.done()
    async_fire_time_changed(ctx.hass, dt_util.utcnow() + timedelta(seconds=36))
    assert await first is False
    _check_view(adapter, state="uncertain", operation_id=operation_id,
                recovery_phase="failed", reason="awaiting_device_report",
                recovery_can_start=True)
    assert adapter.transport.needs_reconciliation
    # Reports before the retry request are cached/pre-request evidence.
    _queue_reports(ctx, {"4": "standby"})
    retry = ctx.hass.async_create_task(
        adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    _check_view(adapter, recovery_phase="collecting", recovery_can_start=False)
    await asyncio.sleep(0.2)
    assert not retry.done()
    _push(ctx, {"4": "standby"})  # fresh, actual, current-epoch report
    assert await retry is True
    _check_view(adapter, state="idle", operation_id=None, recovery_phase="resolved",
                recovery_can_start=False)
    assert writes == [] and not adapter.transport.needs_reconciliation
    assert (await _open(store).async_load_last_intent())["operation_id"] == operation_id


async def test_late_echo_during_recovery_cannot_release_without_full_evidence(adapters, store, writes):
    from custom_components.petsafe_freshfeed.services import acknowledgement, recovery_allowed

    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store), ack=acknowledgement,
                             recovery=recovery_allowed)
    ctx.device.response = None
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    operation_id = result["operation_id"]
    task = ctx.hass.async_create_task(adapter.async_recover(
        operation_id, adapter.view["session_id"], confirmed=True), eager_start=True)
    _queue_reports(ctx, {"1": VALUE})
    await asyncio.sleep(0.1)
    _check_view(adapter, state="uncertain", recovery_phase="collecting")
    assert adapter.transport.needs_reconciliation
    adapter._collector.timer.cancel()
    adapter._collector.expire()
    assert not await task
    _check_view(adapter, state="uncertain", recovery_phase="failed",
                reason="awaiting_device_report")
    assert adapter.transport.needs_reconciliation
    _queue_reports(ctx, {"1": VALUE})
    await asyncio.sleep(0.1)
    _check_view(adapter, state="uncertain", recovery_phase="failed",
                reason="awaiting_device_report", recovery_can_start=True)
    assert adapter.transport.needs_reconciliation
    retry = ctx.hass.async_create_task(adapter.async_recover(
        operation_id, adapter.view["session_id"], confirmed=True), eager_start=True)
    _queue_reports(ctx, {"4": "standby", "145": 2, "1": VALUE})
    assert await retry
    _check_view(adapter, state="idle", recovery_phase="resolved")
    assert len(writes) == 1


async def test_own_send_recovery_preserves_context_dps(adapters, store, writes):
    ctx = adapters
    contexts = []

    def recovery(context, evidence):
        contexts.append(context)
        return _standby_after_request(context, evidence)

    adapter = await _adapter(ctx, await _provision(store), recovery=recovery)
    ctx.device.response = None
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    context = adapter.recovery_context()
    assert context.operation_id == result["operation_id"] and context.dps == {"1": VALUE}
    task = ctx.hass.async_create_task(adapter.async_recover(
        context.operation_id, context.session_id, confirmed=True), eager_start=True)
    _queue_reports(ctx, {"4": "standby"})
    assert await task is True
    assert contexts and all(item.dps == {"1": VALUE} for item in contexts)
    assert len(writes) == 1 and not adapter.transport.needs_reconciliation


async def test_stop_drains_collecting_recovery(adapters, store, writes):
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    task = ctx.hass.async_create_task(adapter.async_recover(
        operation_id, adapter.view["session_id"], confirmed=True), eager_start=True)
    handle = adapter._collector.timer
    assert await adapter.async_stop() is True
    assert await task is False
    assert handle.cancelled() and adapter._collector is None
    assert all(item.done() for item in adapter._tasks)
    assert adapter.transport.needs_reconciliation and writes == []


async def test_ineligible_source_exposes_fixed_recovery_reason(adapters, store):
    ctx = adapters
    witness, _ = await _retained(store)
    adapter = await _adapter(ctx, witness)
    ctx.api.socket.closed = True
    try:
        _check_view(adapter, state="uncertain", recovery_can_start=False,
                    recovery_phase="awaiting_user_confirmation", reason="source_incompatible")
    finally:
        ctx.api.socket.closed = False


async def test_disconnect_during_wait_invalidates_even_after_reconnect(adapters, store):
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    session = adapter.view["session_id"]
    task = ctx.hass.async_create_task(
        adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    ctx.api.socket = FakeSocket(ctx.device)  # hidden reconnect: a new connection epoch
    ctx.bridge.is_current()
    ctx.bridge.flush()
    assert await asyncio.wait_for(task, 2) is False
    _check_view(adapter, state="uncertain", recovery_phase="failed",
                reason="connection_interrupted")
    await until(ctx.bridge.is_current)
    _push(ctx, {"4": "standby"})
    await asyncio.sleep(0.2)
    _check_view(adapter, state="uncertain", recovery_phase="failed")
    assert adapter.transport.needs_reconciliation


@pytest.mark.parametrize("status", [[], {}, ["standby"], {"status": "standby"}, None, True, 4])
async def test_malformed_dp4_public_view_and_recovery_retry_fail_closed(adapters, store, status):
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    session = adapter.view["session_id"]
    task = ctx.hass.async_create_task(
        adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    _queue_reports(ctx, {"4": status})
    assert await task is False
    _check_view(adapter, state="uncertain", recovery_phase="failed",
                reason="status_unavailable", recovery_can_start=False)
    assert await adapter.async_recover(operation_id, session, confirmed=True) is False
    assert adapter.view["recovery_resolution"] is None
    assert adapter.transport.needs_reconciliation
    assert (await witness.async_load_last_intent())["operation_id"] == operation_id


async def test_status_change_after_evidence_rejected_at_final_validation(adapters, store):
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    session = adapter.view["session_id"]
    task = ctx.hass.async_create_task(
        adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    # Approved evidence, then an active status, before the waiter resumes.
    _queue_reports(ctx, {"4": "standby"}, {"4": "feeding"})
    assert await task is False
    _check_view(adapter, state="uncertain", recovery_phase="failed", reason="feeding_active",
                recovery_can_start=False)
    assert adapter.transport.needs_reconciliation


async def test_decoded_but_unnumbered_active_status_rejected_at_final_validation(
        adapters, store):
    from datetime import datetime, timezone
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    session = adapter.view["session_id"]
    task = ctx.hass.async_create_task(
        adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    _queue_reports(ctx, {"4": "standby"})
    bridge = ctx.bridge
    with bridge._guard:  # decoded by a worker, its flush not yet run on the loop
        bridge._events.append((bridge._generation, (
            "report", datetime.now(timezone.utc), {"4": "feeding"}, True, __import__('time').monotonic())))
    assert await task is False
    _check_view(adapter, state="uncertain", recovery_phase="failed", reason="feeding_active")
    assert adapter.transport.needs_reconciliation


async def test_replacement_coordinator_rejects_old_operation_session(adapters, store, writes):
    ctx = adapters
    witness, operation_id = await _retained(store)
    old = await _adapter(ctx, witness)
    old_session = old.view["session_id"]
    task = ctx.hass.async_create_task(
        old.async_recover(operation_id, old_session, confirmed=True), eager_start=True)
    new = await _adapter(ctx, _open(store))
    new_session = new.view["session_id"]
    assert new_session != old_session
    _check_view(new, state="uncertain", operation_id=operation_id,
                recovery_phase="awaiting_user_confirmation", recovery_can_start=True)
    _push(ctx, {"4": "standby"})
    assert await asyncio.wait_for(task, 2) is False
    assert old.view["recovery_can_start"] is False
    assert not await old.async_recover(operation_id, old_session, confirmed=True)
    assert not await new.async_recover(operation_id, old_session, confirmed=True)
    assert new.transport.needs_reconciliation
    release = ctx.hass.async_create_task(
        new.async_recover(operation_id, new_session, confirmed=True), eager_start=True)
    _push(ctx, {"4": "standby"})
    assert await release is True and writes == []


async def test_cancelled_recovery_retains_barrier(adapters, store):
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    session = adapter.view["session_id"]
    task = ctx.hass.async_create_task(
        adapter.async_recover(operation_id, session, confirmed=True), eager_start=True)
    handle = adapter._collector.timer
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter._collector is None and handle.cancelled()
    _check_view(adapter, state="uncertain", recovery_phase="failed",
                reason="recovery_cancelled", recovery_can_start=True)
    assert adapter.transport.needs_reconciliation


async def test_snapshot_collects_only_post_request_current_epoch_reports(adapters, store):
    from custom_components.petsafe_freshfeed.write_adapter import IntentContext
    ctx = adapters
    witness, operation_id = await _retained(store)
    adapter = await _adapter(ctx, witness)
    context = adapter.recovery_context()
    assert type(context) is IntentContext and context.dps == {}
    assert context.operation_id == operation_id and context.epoch == ctx.bridge.epoch
    _queue_reports(ctx, {"4": "standby"})
    stale = IntentContext("schedule", operation_id, uuid4().hex, context.epoch, {})
    assert await adapter.async_recovery_snapshot(stale, timeout=1) is None
    snapshot = ctx.hass.async_create_task(
        adapter.async_recovery_snapshot(context, timeout=35.0), eager_start=True)
    start = adapter._collector.start_sequence
    _push(ctx, {"4": "standby"})
    evidence = await snapshot
    assert evidence.context is context and evidence.start_sequence == start
    assert all(report.sequence > start and report.epoch == context.epoch
               for report in evidence.reports)
    assert evidence.end_sequence == evidence.reports[-1].sequence
    assert adapter._collector is None and adapter.transport.needs_reconciliation


# ----- durability and cancellation -------------------------------------------------


async def test_reconstruction_retains_operation_after_local_release(adapters, store, writes):
    ctx = adapters
    first = await _adapter(ctx, await _provision(store))
    ctx.device.response = _frame({"1": VALUE})
    result = await first.async_submit("schedule", {"1": VALUE},
                                      base_revision=_revision(SCHEDULE))
    assert result["state"] == "uncertain" and len(writes) == 1
    _push(ctx, {"1": VALUE})
    await until(lambda: first.view["state"] == "verified")
    operation_id, session = result["operation_id"], first.view["session_id"]
    assert await first.async_stop()
    second = await _adapter(ctx, _open(store), ack=lambda *args: "observed")
    _check_view(second, state="uncertain", operation_id=operation_id, operation="schedule",
                recovery_phase="awaiting_user_confirmation", recovery_can_start=True)
    assert second.view["session_id"] != session
    # A matching (even always-"observed") report never releases a retained intent.
    _push(ctx, {"1": VALUE})
    await asyncio.sleep(0.2)
    _check_view(second, state="uncertain", operation_id=operation_id)
    assert len(writes) == 1


async def test_cancel_before_preparation(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    task = ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert witness.prepares == [] and writes == []
    _check_view(adapter, state="idle")


async def test_cancel_during_preparation_drains_actual_io(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    witness.gate = asyncio.Event()
    task = ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE)))
    await witness.entered.wait()
    task.cancel()
    await asyncio.sleep(0.1)
    assert not task.done()  # the actual preparation has not settled
    _check_view(adapter, state="pending")
    witness.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writes == [] and adapter._submitting is None
    assert await adapter.transport.async_wait_idle()
    # Cancelled before dispatch: nothing sent; the durable record re-arms restart.
    _check_view(adapter, state="idle", reason="send_cancelled")
    assert (await _open(store).async_load_last_intent())["operation_id"] == \
        witness.prepares[0]["operation_id"]


async def test_cancel_after_preparation_at_send_boundary(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    dispatch = adapter.transport._dispatch
    boundaries = []

    def cancel_before_dispatch(operation_id):
        # The real transport reaches this hook only after durable preparation,
        # before the socket guard grants a CONTROL write. Keep the owned hook
        # chain intact so its caller-cancellation check must refuse the send.
        boundaries.append(operation_id)
        task.cancel()
        return dispatch(operation_id)

    adapter.transport._dispatch = cancel_before_dispatch
    task = ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE)))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(witness.prepares) == 1
    operation_id = witness.prepares[0]["operation_id"]
    assert boundaries == [operation_id]
    assert (await _open(store).async_load_last_intent())["operation_id"] == operation_id
    assert writes == [] and ctx.device.control_frames() == []
    assert adapter._current is None and adapter._marks == {}
    assert adapter._submitting is None
    assert await adapter.transport.async_wait_idle()
    # The executor was launched even though the socket boundary refused its
    # write; the transport conservatively retains reconciliation uncertainty.
    _check_view(adapter, state="uncertain", recovery_phase="awaiting_user_confirmation")
    assert adapter.transport.needs_reconciliation


async def test_cancel_in_flight_socket_send_drains_actual_io(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    entered = asyncio.Event()
    release = threading.Event()

    def hold_socket_send():
        ctx.hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "socket send was not released"

    ctx.device.before_send = hold_socket_send
    ctx.device.response = _frame({"1": VALUE})
    task = ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE)))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()  # cancellation must drain the in-flight socket call
        assert not await adapter.transport.async_wait_idle(timeout=0.01)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    ctx.device.before_send = None
    assert len(witness.prepares) == 1
    assert len(writes) == 1 and writes[0]["data"]["dps"] == {"1": VALUE}
    assert await adapter.transport.async_wait_idle()
    _check_view(adapter, state="uncertain", recovery_phase="awaiting_user_confirmation")
    assert adapter.transport.needs_reconciliation


async def test_stop_quiesces_admission_and_refuses_pending_send(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    witness.gate = asyncio.Event()
    task = ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE)))
    await witness.entered.wait()
    stop = ctx.hass.async_create_task(adapter.async_stop())
    await asyncio.sleep(0.05)
    witness.gate.set()
    assert await stop is True
    await task
    assert writes == []  # admitted before stop, but the send boundary refuses
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    assert result["admitted"] is False and result["reason"] == "stopped"
    assert not await adapter.async_recover("0" * 32, adapter.view["session_id"],
                                           confirmed=True)


# ----- hub ownership -----------------------------------------------------------


async def test_hub_without_adapter_is_unavailable_and_stops_adapter_before_detach(
        hass, owned_context):
    from test_hub import owned_hub
    hub = owned_hub(owned_context)
    await hub.async_start()
    order = []
    try:
        assert hub.witness is None and hub.adapter is None
        view = hub.write_view
        assert set(view) == BASE_KEYS and view["state"] == "blocked"
        assert view["reason"] == "controls_unavailable" and view["recovery_can_start"] is False
        bridge = hub.bridge
        detach = bridge.async_stop

        async def recorded_detach():
            order.append("detach")
            return await detach()
        bridge.async_stop = recorded_detach

        async def stop(timeout=5.0):
            order.append("adapter_stop")
            return True

        def idle(label):
            async def wait_idle(timeout=5.0):
                order.append(label)
                return True
            return SimpleNamespace(async_wait_idle=wait_idle)
        # Task 7: retirement fences transport then witness before detaching.
        hub.adapter = SimpleNamespace(async_stop=stop, view=dict(view, state="idle"),
                                      transport=idle("transport_idle"),
                                      witness=idle("witness_idle"))
        assert hub.write_view["state"] == "idle"
    finally:
        await hub.async_stop()
    assert order == ["adapter_stop", "transport_idle", "witness_idle", "detach"]
    assert hub.adapter is None


@pytest.mark.parametrize("status", [["standby"], {"status": "standby"}, None])
async def test_malformed_dp4_device_frame_invalidates_safe_status(adapters, store, writes, status):
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    _push(ctx, {"4": "standby", "145": 2, "140": "EAAA", "142": 0})
    await until(lambda: adapter._live_dps.get("4") == "standby" and
                adapter._live_dps.get("145") == 2 and adapter._live_dps.get("140") == "EAAA")
    _push(ctx, {"4": status})
    await until(lambda: "4" in adapter._live_dps and adapter._live_dps["4"] is None)
    _check_view(adapter, can_cancel=False, cancel_reason="status_unavailable",
                can_feed=False, can_defrost=False, can_refilled=False)
    for operation, payload in (("cancel", "IA=="), ("feed", "EA==")):
        result = await adapter.async_submit(operation, {"140": payload}, confirmed=True)
        assert result["admitted"] is False and result["reason"] == "status_unavailable"
    assert writes == []


@pytest.mark.parametrize("status", [[], {}, ["standby"], {"status": "standby"}, True, 4])
async def test_malformed_dp4_without_retained_intent_fails_closed(adapters, store, writes, status):
    """No retained intent: the cancel branch must not raise on a malformed DP4."""
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store))
    _queue_reports(ctx, {"4": status, "145": 2, "140": "EAEA", "142": 0})
    view = _check_view(adapter, can_cancel=False, cancel_reason="status_unavailable",
                       can_feed=False, can_defrost=False, can_refilled=False)
    assert view["state"] != "uncertain"
    result = await adapter.async_submit("cancel", {"140": "IA=="}, confirmed=True)
    assert result["admitted"] is False and result["reason"] == "status_unavailable"
    assert writes == []


@pytest.mark.parametrize('seeded', ['EAEA', 'EAIA', 'invalid'])
async def test_local_dp140_reported_before_adapter_blocks_missing_dp140_exception(
        adapters, store, writes, seeded):
    """A DP140 the hub saw this epoch before adapter construction still counts."""
    ctx = adapters
    witness = await _provision(store)
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    from custom_components.petsafe_freshfeed.write_adapter import async_create_adapter
    ctx.adapters = []
    assert ctx.bridge.is_current()
    await ctx.bridge.async_claim_receive()
    _queue_reports(ctx, {'140': seeded})
    epoch, sequence = ctx.bridge.epoch, ctx.bridge.last_sequence
    entry = SimpleNamespace(entry_id="synthetic", runtime_data=SimpleNamespace(
        witness=witness, schedule=ScheduleState().with_report(SCHEDULE, dt_util.utcnow()),
        reported_dps=lambda at: {'140': (seeded, sequence)} if at == epoch else {}))
    adapter = await async_create_adapter(ctx.hass, entry, ctx.bridge,
                                         ack_predicate=_observed_on_dp1,
                                         recovery_predicate=_standby_after_request)
    ctx.adapters.append(adapter)
    _queue_reports(ctx, {'4': 'standby', '142': 0, '145': 2})
    for operation in ('feed', 'defrost', 'refilled'):
        assert not adapter.view[f'can_{operation}'], operation
    result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
    assert not result['admitted']
    assert writes == []


async def _seeded_adapter(ctx, store, seed, ack=_observed_on_dp1):
    witness = await _provision(store)
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    from custom_components.petsafe_freshfeed.write_adapter import async_create_adapter
    ctx.adapters = []
    assert ctx.bridge.is_current()
    await ctx.bridge.async_claim_receive()
    _queue_reports(ctx, {'140': 'EAAA'})
    epoch, sequence = ctx.bridge.epoch, ctx.bridge.last_sequence
    entry = SimpleNamespace(entry_id="synthetic", runtime_data=SimpleNamespace(
        witness=witness, schedule=ScheduleState().with_report(SCHEDULE, dt_util.utcnow()),
        reported_dps=lambda at: ({'140': ('EAAA', sequence), **seed} if at == epoch else {})))
    adapter = await async_create_adapter(ctx.hass, entry, ctx.bridge,
                                         ack_predicate=ack,
                                         recovery_predicate=_standby_after_request)
    ctx.adapters.append(adapter)
    _queue_reports(ctx, {'4': 'standby', '142': 0, '145': 2})
    return adapter


async def test_seeded_latest_idle_selects_strict_local_not_missing_exception(adapters, store, writes):
    adapter = await _seeded_adapter(adapters, store, {})
    assert '140' in adapter._live_sequences and '140' not in adapter._live_times
    _queue_reports(adapters, {'4': 'no_food'})
    assert adapter._decision('defrost')[0] == 'status_unavailable'  # L, not E
    _queue_reports(adapters, {'4': 'standby'})
    assert adapter.view['can_feed']
    result = await adapter.async_submit('feed', {'140': 'EA=='}, confirmed=True)
    assert result['admitted'] and len(writes) == 1


async def test_observed_cancel_needs_no_hub_history_release(adapters, store, writes):
    ctx = adapters
    adapter = await _seeded_adapter(
        ctx, store, {},
        ack=lambda context, mark, report: 'observed' if context.operation == 'cancel' else 'ignore')
    _queue_reports(ctx, {'140': 'EAEA'})
    result = await adapter.async_submit('cancel', {'140': 'IA=='}, confirmed=True)
    _push(ctx, {'140': 'IAAA'})
    await until(lambda: adapter.view['state'] == 'verified')
    assert result['operation_id']
