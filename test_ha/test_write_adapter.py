"""Durable write admission and explicit recovery orchestration (Task 6).

Synthetic DPS and an explicitly provisioned, owned SQLite witness under the
test's temporary directory only: never HA ``.storage``, no network, no
credentials. The shared client is the REAL pinned tuya_local monitor with the
instrumented fake TinyTuya client from ``test_tuya_bridge``.
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

from test_tuya_bridge import (  # noqa: F401 - `owned` is a fixture
    FakeSocket, _fake_log, _frame, owned, until,
)

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
def writes(monkeypatch):
    """Every byte string that actually reached a (fake) device socket."""
    sent = []
    monkeypatch.setattr(FakeSocket, "sendall", lambda self, data: sent.append(bytes(data)))
    return sent


async def _adapter(ctx, witness, *, ack=_observed_on_dp1, recovery=_standby_after_request,
                   schedule_value=SCHEDULE):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    from custom_components.petsafe_freshfeed.write_adapter import async_create_adapter
    if not hasattr(ctx, "adapters"):
        ctx.adapters = []
        await until(lambda: ctx.api.calls.count("receive") >= 1 and ctx.bridge.is_current())
        ctx.api.reset()
        await ctx.bridge.async_claim_receive()
        # The takeover's first owned iteration is a full status poll (DP4
        # standby); let it land so it cannot count as post-request evidence.
        await until(lambda: "status" in ctx.api.calls and ctx.bridge.is_current())
        await asyncio.sleep(0.1)
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
    ctx.api.inbox.put({"dps": dict(dps)})


def _queue_reports(ctx, *items):
    """Number several decoded reports in ONE synchronous owning-loop turn."""
    from datetime import datetime, timezone
    bridge = ctx.bridge
    with bridge._guard:
        for dps in items:
            bridge._events.append((bridge._generation, (
                "report", datetime.now(timezone.utc), dict(dps))))
    bridge._flush()


BASE_KEYS = {"state", "operation_id", "operation", "session_id", "reason", "storage_fault",
             "integrity_fault", "recovery_can_start", "recovery_phase",
             "can_feed", "can_defrost", "can_refilled", "blocked_reason"}


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


def _revision(value):
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    return ScheduleState().with_report(value, dt_util.utcnow()).revision


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
    from custom_components.petsafe_freshfeed.tuya_bridge import DeviceReport
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
    ctx.api.response = _frame(response)
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
    ctx.api.response = _frame({"1": VALUE})
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
    ctx.api.response = _frame({"1": VALUE})
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    assert result["admitted"] is True and len(writes) == 1
    assert result["state"] == "uncertain"
    _push(ctx, {"1": VALUE})
    await until(lambda: not adapter.transport.needs_reconciliation)
    _check_view(adapter, state="idle", reason="observed_failure", recovery_phase="idle")
    assert not adapter.transport.needs_reconciliation
    assert (await _open(store).async_load_last_intent())["operation_id"] is not None


async def test_pre_mark_echo_ignored_and_late_post_mark_echo_releases(adapters, store, writes):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    witness.gate = asyncio.Event()
    ctx.api.response = None
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
    from custom_components.petsafe_freshfeed.tuya_bridge import DeviceReport
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
    ctx.api.response = None
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
    original = type(api).set_socketRetryLimit

    def broken(self, limit):
        original(self, 99 if limit else limit)  # restoration is not exact
    monkeypatch.setattr(type(api), "set_socketRetryLimit", broken)
    api.response = _frame({"1": VALUE})
    result = await adapter.async_submit("schedule", {"1": VALUE},
                                        base_revision=_revision(SCHEDULE))
    monkeypatch.setattr(type(api), "set_socketRetryLimit", original)
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
    ctx.api.response = _frame({"1": VALUE})
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
    ctx.api.response = _frame({"1": VALUE})
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
    from custom_components.petsafe_freshfeed import tuya_bridge
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    data = ctx.hass.data["tuya_local"]["fakeid"]
    lock = data["tuyadevicelock"]
    if change == "code":
        monkeypatch.setattr(tuya_bridge, "check_loaded_source", lambda *args: False)
    elif change == "control_flow":
        monkeypatch.setattr(tuya_bridge, "check_receive_control_flow", lambda *args: False)
    elif change == "binding":
        data["tuyadevicelock"] = asyncio.Lock()
    elif change == "logger":
        _fake_log.setLevel(logging.DEBUG)
    else:
        ctx.api.socket.closed = True
    try:
        result = await adapter.async_submit("schedule", {"1": VALUE},
                                            base_revision=_revision(SCHEDULE))
    finally:
        _fake_log.setLevel(logging.INFO)
        data["tuyadevicelock"] = lock
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
        (("settings", {"3": 1}), {"confirmed": True}, "invalid_operation"),
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
    ctx.api.response = None
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
        if func != ctx.bridge._code_ok:
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
    ctx.api.response = None
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
    ctx.api.response = None
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
    ctx.api.socket = FakeSocket()  # hidden reconnect: a new connection epoch
    ctx.bridge.is_current()
    ctx.bridge._flush()
    assert await asyncio.wait_for(task, 2) is False
    _check_view(adapter, state="uncertain", recovery_phase="failed",
                reason="connection_interrupted")
    await until(ctx.bridge.is_current)
    _push(ctx, {"4": "standby"})
    await asyncio.sleep(0.2)
    _check_view(adapter, state="uncertain", recovery_phase="failed")
    assert adapter.transport.needs_reconciliation


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
            "report", datetime.now(timezone.utc), {"4": "feeding"})))
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
    ctx.api.response = _frame({"1": VALUE})
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
    holder = {}
    ctx.api.before_send = lambda: ctx.hass.loop.call_soon_threadsafe(holder["task"].cancel)
    ctx.api.response = _frame({"1": VALUE})
    holder["task"] = ctx.hass.async_create_task(adapter.async_submit(
        "schedule", {"1": VALUE}, base_revision=_revision(SCHEDULE)))
    with pytest.raises(asyncio.CancelledError):
        await holder["task"]
    ctx.api.before_send = None
    assert writes == [] and len(witness.prepares) == 1
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
        hass, source_entry, raw_entities, loaded_source):
    from test_hub import make_hub
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    order = []
    try:
        assert hub.witness is None and hub.adapter is None
        view = hub.write_view
        assert set(view) == BASE_KEYS and view["state"] == "blocked"
        assert view["reason"] == "controls_unavailable" and view["recovery_can_start"] is False
        bridge = hub.bridge
        detach = bridge.async_detach

        async def recorded_detach():
            order.append("detach")
            await detach()
        bridge.async_detach = recorded_detach

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
