"""Durable write admission and explicit recovery over the owned shared client.

One adapter per coordinator construction. It borrows the bridge's single
authenticated client through ``TuyaBridge.async_enable_transport``: no second
client, no wrapper setters, no raw HA services. Every admitted operation is
durably prepared by the transport's witness before at most one CONTROL; the
witness is never cleared, so every new construction re-arms uncertainty for the
last prepared intent, even after a local acknowledgement or recovery release.

Acknowledgement requires an eligible device report on the dispatch epoch with
sequence strictly greater than the send-boundary ``DispatchMark``, approved by
the injected predicate. Uncorrelated exchange frames are observation only.
An eligible early report is retained until the transport drains, and applied
only without transport, storage or integrity faults. Sequence alone cannot prove
an ordinary asynchronous push was not buffered before dispatch. A retained startup intent has no in-memory context and is
never released by a cached or matching report: only explicit recovery can
release it locally.

Recovery is an explicit, confirmed request for the current operation/session.
It collects only actual incoming reports numbered after the request on the
same uninterrupted epoch, for at most ``RECOVERY_TIMEOUT`` seconds. No DP query,
write or cached evidence is ever used. Final validation runs synchronously in
the verifier handed to ``CriticalTransport.async_reconcile``, with no await
between it and the local release. A release clears THIS construction's barrier
only; it is not evidence of physical execution.

Semantics (which report means what) belong to the injected predicates; the
defaults acknowledge nothing and approve no recovery.

Compatibility gates, stated plainly: the loaded-file and monitor control-flow
fingerprints (``TuyaBridge._code_ok``, executor file I/O) are checked at each
submit immediately before the synchronous admission turn, not again during the
lock wait and witness fsync, nor at recovery. Every later gate (admission,
send boundary, recovery start and final validation) re-runs the synchronous,
I/O-free binding/generation/ownership/logger/socket checks instead.
``async_recovery_snapshot`` is evidence-only: it never releases anything and
does not change the recovery phase.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import math
from time import monotonic
from typing import Callable, Literal

from .const import (
    DEFROST_START_VALUE, FEED_START_COMMAND, RECOVERY_TIMEOUT, REFILLED_VALUE,
    WRITE_OPERATION_DPS, WRITE_SEND_TIMEOUT,
)
from .critical_transport import _socket_open
from .schedule_codec import ScheduleCodecError, decode_base64
from .services import operation_reason, unsafe_reason
from .tuya_bridge import DeviceReport, DispatchMark, _settle

_LOGGER = logging.getLogger(__name__)

AckDecision = Literal["ignore", "observed", "observed_failure"]
_OBSERVED = frozenset(("observed", "observed_failure"))
_MAX_EVIDENCE = 256
_FIXED_VALUES = {"feed": FEED_START_COMMAND, "defrost": DEFROST_START_VALUE,
                 "refilled": REFILLED_VALUE}


@dataclass(frozen=True)
class IntentContext:
    """In-memory only. Retained startup contexts carry ``dps={}``."""

    operation: str
    operation_id: str
    session_id: str
    epoch: int
    dps: dict[str, bool | int | str]


@dataclass(frozen=True)
class RecoveryEvidence:
    context: IntentContext
    start_sequence: int
    end_sequence: int
    reports: tuple[DeviceReport, ...]


AckPredicate = Callable[[IntentContext, DispatchMark, DeviceReport], AckDecision]
RecoveryPredicate = Callable[[IntentContext, RecoveryEvidence], bool]


def ignore_acknowledgement(context: IntentContext, mark: DispatchMark,
                           report: DeviceReport) -> AckDecision:
    """Fail-closed default: nothing acknowledges an operation."""
    return "ignore"


def deny_recovery(context: IntentContext, evidence: RecoveryEvidence) -> bool:
    """Fail-closed default: no evidence approves recovery."""
    return False


def _uncancel():
    """Forget cancellation requests this task deliberately absorbed."""
    task = asyncio.current_task()
    if task is not None:
        while task.cancelling():
            task.uncancel()


class _Collector:
    """Post-request evidence for one recovery request; never reused."""

    def __init__(self, context, start_sequence, predicate):
        self.context = context
        self.start_sequence = start_sequence
        self.reports = []
        self.complete = False
        self.reason = None
        self.done = asyncio.Event()
        self.timer = None
        self._predicate = predicate

    def evidence(self):
        reports = tuple(self.reports)
        end = reports[-1].sequence if reports else self.start_sequence
        return RecoveryEvidence(self.context, self.start_sequence, end, reports)

    def approves(self):
        try:
            return self._predicate(self.context, self.evidence()) is True
        except Exception:  # noqa: BLE001 - a failing predicate approves nothing
            return False

    def invalidate(self, reason):
        if self.reason is None:
            self.reason = reason
        self.done.set()

    def expire(self):
        self.timer = None
        if not self.complete:
            self.invalidate("awaiting_device_report")

    def add(self, report):
        if self.reason is not None:
            return
        if report.epoch != self.context.epoch:
            self.invalidate("connection_interrupted")
            return
        if report.sequence <= self.start_sequence:
            return
        if len(self.reports) >= _MAX_EVIDENCE:
            self.invalidate("evidence_overflow")
            return
        # Keep collecting after approval: final validation re-evaluates the
        # predicate over every later report, including safety-status changes.
        self.reports.append(report)
        if not self.complete and self.approves():
            self.complete = True
            self.done.set()


class WriteAdapter:
    """Serialized admission, operation lifecycle and recovery orchestration."""

    def __init__(self, hass, entry, bridge, witness, ack_predicate, recovery_predicate):
        self.hass = hass
        self.entry = entry
        self.bridge = bridge
        self.witness = witness
        self.transport = None
        self._ack = ack_predicate
        self._recovery_predicate = recovery_predicate
        self._stopped = False
        self._submitting = None   # IntentContext of the in-flight admission
        self._caller = None       # the task that requested it
        self._current = None      # this construction's last dispatched context
        self._marks = {}
        self._decisions = {}
        self._result = None
        self._reason = None
        self._phase = "idle"
        self._collector = None
        self._tasks = set()
        self._listeners = []
        self._unsubscribe = None
        self._live_dps = {}
        self._live_epoch = bridge.epoch
        self._live_sequence = -1

    # ----- shared state helpers ------------------------------------------------

    def _faults(self):
        transport = self.transport
        if transport is None:
            return False, False
        return (transport.storage_fault is True,
                getattr(transport, "_integrity_fault", False) is True)

    def _pending_operation(self):
        pending = getattr(self.transport, "_pending", None)
        return pending.get("operation") if isinstance(pending, dict) else None

    def _source_ok(self):
        try:
            return (not self._stopped and self.transport is not None
                    and self.transport._is_owner() and self.bridge.is_current())
        except Exception:  # noqa: BLE001 - unknown shape is not current
            return False

    def _compatible_now(self):
        """Synchronous binding/generation/logging/socket gate; no I/O."""
        if not self._source_ok():
            return False
        try:
            self.transport._compatible()
            return _socket_open(self.bridge.api.socket)
        except Exception:  # noqa: BLE001 - fixed rejection, no client values
            return False

    def _phase_view(self):
        if not self.transport.needs_reconciliation:
            return "resolved" if self._phase == "resolved" else "idle"
        if self._submitting is not None:
            return "idle"
        if self._phase in ("collecting", "failed"):
            return self._phase
        return "awaiting_user_confirmation"

    def _can_start(self, phase):
        storage, integrity = self._faults()
        transport = self.transport
        return (phase in ("awaiting_user_confirmation", "failed")
                and not storage and not integrity and not self._stopped
                and self._submitting is None and self._collector is None
                and transport.needs_reconciliation
                and transport.pending_operation_id is not None
                and self._pending_operation() is not None
                and getattr(transport, "_busy", True) is False
                and not unsafe_reason(self._live_dps)
                and self._compatible_now())

    @property
    def view(self) -> dict:
        # Eligibility reads bridge.is_current(), which may queue an observed
        # socket replacement for numbering; it never performs I/O.
        transport = self.transport
        storage, integrity = self._faults()
        operation_id = transport.pending_operation_id
        operation = self._pending_operation() if operation_id else None
        reason = self._reason
        if storage or integrity:
            state, reason = "blocked", "storage_fault" if storage else "integrity_fault"
        elif self._submitting is not None:
            state, reason = "pending", None
            operation = self._submitting.operation
        elif transport.needs_reconciliation:
            state = "uncertain"
        elif self._stopped:
            state, reason = "blocked", "stopped"
        elif self._result == "verified" and self._current is not None:
            state = "verified"
            operation_id, operation = self._current.operation_id, self._current.operation
        else:
            state = "idle"
        phase = self._phase_view()
        can_start = self._can_start(phase)
        if (state == "uncertain" and phase in ("awaiting_user_confirmation", "failed")
                and not can_start and not self._compatible_now()):
            reason = "source_incompatible"
        if (state == 'uncertain' and phase in ('awaiting_user_confirmation', 'failed')
                and not can_start and self._compatible_now() and unsafe_reason(self._live_dps)):
            reason = unsafe_reason(self._live_dps)
        common = self._gate_reason()
        reasons = {op: common or operation_reason(op, self._live_dps)
                   for op in ('feed', 'defrost', 'refilled')}
        return {
            "can_feed": reasons['feed'] is None,
            "can_defrost": reasons['defrost'] is None,
            "can_refilled": reasons['refilled'] is None,
            "blocked_reason": common or next((r for r in reasons.values() if r), None),
            "state": state,
            "operation_id": operation_id,
            "operation": operation,
            "session_id": transport.recovery_session_id,
            "reason": reason,
            "storage_fault": storage,
            "integrity_fault": integrity,
            "recovery_can_start": can_start,
            "recovery_phase": phase,
        }

    def add_listener(self, callback) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove():
            if callback in self._listeners:
                self._listeners.remove(callback)
        return remove

    def _notify(self):
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                _LOGGER.error("FreshFeed write status listener failed")

    def _track(self, task):
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _response(self, admitted, reason=None):
        view = self.view
        return {"admitted": admitted, "state": view["state"],
                "operation_id": view["operation_id"],
                "reason": reason if reason is not None else view["reason"]}

    # ----- admission ---------------------------------------------------------------

    @staticmethod
    def _request_reason(operation, dps, base_revision, confirmed):
        if type(operation) is not str or operation not in WRITE_OPERATION_DPS:
            return "invalid_operation"
        key = WRITE_OPERATION_DPS[operation]
        if (type(dps) is not dict or len(dps) != 1
                or any(type(name) is not str for name in dps) or key not in dps):
            return "invalid_request"
        value = dps[key]
        if operation == "schedule":
            if type(value) is not str or (base_revision is not None
                                          and type(base_revision) is not str):
                return "invalid_request"
            try:
                decode_base64(value)
            except ScheduleCodecError:
                return "invalid_request"
            return None
        expected = _FIXED_VALUES[operation]
        if base_revision is not None or type(value) is not type(expected) or value != expected:
            return "invalid_request"
        if confirmed is not True:
            return "confirmation_required"
        return None

    def _gate_reason(self):
        transport = self.transport
        if self._stopped:
            return "stopped"
        storage, integrity = self._faults()
        if storage:
            return "storage_fault"
        if integrity:
            return "integrity_fault"
        if self._submitting is not None or getattr(transport, "_busy", True) is not False:
            return "busy"
        if self._collector is not None:
            return "recovery_in_progress"
        if transport.needs_reconciliation:
            return "intent_uncertain"
        if not self._compatible_now():
            return "source_incompatible"
        return None

    def _revision_reason(self, operation, base_revision):
        if operation != "schedule":
            return None
        schedule = getattr(getattr(self.entry, "runtime_data", None), "schedule", None)
        if getattr(schedule, "entries", None) is None:
            return "schedule_unknown"
        # Base revision is the digest of the last PUSHED canonical value.
        if base_revision != schedule.revision:
            return "stale_schedule"
        return None

    async def async_submit(self, operation: str, dps: dict[str, bool | int | str], *,
                           base_revision: str | None = None, confirmed: bool = False) -> dict:
        reason = (self._request_reason(operation, dps, base_revision, confirmed)
                  or self._gate_reason() or operation_reason(operation, self._live_dps))
        if reason is not None:
            return self._response(False, reason)
        # Loaded-code provenance needs file I/O: settle it BEFORE the final
        # synchronous admission turn, never abandoning the executor job.
        try:
            code_ok = await _settle(self.hass.async_add_executor_job(self.bridge._code_ok))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            code_ok = False
        if code_ok is not True:
            return self._response(False, "source_incompatible")
        # One synchronous turn: final gates, revision, then transport admission.
        reason = (self._request_reason(operation, dps, base_revision, confirmed)
                  or self._gate_reason() or self._revision_reason(operation, base_revision)
                  or operation_reason(operation, self._live_dps))
        if reason is not None:
            return self._response(False, reason)
        context = IntentContext(operation, "", self.transport.recovery_session_id,
                                self.bridge.epoch, dict(dps))
        self._admitted_revision = base_revision
        self._submitting, self._caller = context, asyncio.current_task()
        self._marks, self._decisions = {}, {}
        self._result = self._reason = None
        self._phase = "idle"
        # Eager start: transport admission (busy/pending) happens in THIS turn.
        task = self._track(asyncio.Task(self._run_submit(context), loop=self.hass.loop,
                                        eager_start=True))
        self._notify()
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                if not cancelled:
                    # The transport refuses a not-yet-granted send boundary for a
                    # cancelling caller; the actual work is drained, not abandoned.
                    task.cancel()
                cancelled = True
        if not task.cancelled() and task.exception() is not None:
            # Type only: never log payloads or vendor exception text.
            _LOGGER.error("FreshFeed write finalization failed: %s",
                          type(task.exception()).__name__)
        result = (self._response(True) if task.cancelled() or task.exception() is not None
                  else task.result())
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def _run_submit(self, context):
        transport = self.transport
        receipt = None
        try:
            receipt = await transport.async_send(context.operation, dict(context.dps),
                                                 timeout=WRITE_SEND_TIMEOUT)
        except asyncio.CancelledError:
            # Deliberately absorbed (including an external cancel): the
            # transport's actual work must drain before this admission unlatches.
            _uncancel()
        except Exception as error:  # noqa: BLE001 - never latch _submitting forever
            _LOGGER.error("FreshFeed write send failed: %s", type(error).__name__)
        await self._drain_transport()
        return await self._finish_submit(receipt)

    async def _drain_transport(self):
        transport = self.transport
        while True:
            try:
                if await transport.async_wait_idle(WRITE_SEND_TIMEOUT):
                    return
            except asyncio.CancelledError:
                _uncancel()  # absorbed: never abandon the actual exchange

    async def _finish_submit(self, receipt):
        transport = self.transport
        context = self._submitting
        self._submitting = self._caller = None
        operation_id = context.operation_id or (receipt.operation_id if receipt else "")
        storage, integrity = self._faults()
        if not transport.needs_reconciliation:
            # Confirmed pre-dispatch rejection: the transport released itself.
            self._current = None
            self._reason = receipt.reason if receipt is not None else "send_cancelled"
        elif storage or integrity:
            self._reason = None
        elif operation_id in self._marks:
            decision = self._decisions.get(operation_id)
            if decision in _OBSERVED:
                await self._release(operation_id)
            else:
                self._reason = "awaiting_device_report"
        else:
            self._reason = receipt.reason if receipt is not None else "send_cancelled"
        self._notify()
        return self._response(True)

    async def _release(self, operation_id):
        """Local release of THIS construction on an observed acknowledgement."""
        transport = self.transport

        def valid():
            storage, integrity = self._faults()
            return (not self._stopped and not storage and not integrity
                    and self._submitting is None and self._collector is None
                    and self._phase != "failed"
                    and transport.pending_operation_id == operation_id
                    and operation_id in self._marks
                    and self._decisions.get(operation_id) in _OBSERVED
                    and self._compatible_now()
                    and self.bridge.epoch == self._marks[operation_id].epoch)

        if not valid():
            return False

        async def verifier():
            return valid()

        _uncancel()
        released = await transport.async_reconcile(
            operation_id, verifier, confirmed=True, session_id=transport.recovery_session_id)
        if released:
            decision = self._decisions[operation_id]
            self._result = "verified" if decision == "observed" else None
            self._reason = None if decision == "observed" else "observed_failure"
            self._phase = "idle"
            if self._collector is not None:
                self._collector.invalidate("superseded")
            self._notify()
        return released

    # ----- bridge callbacks (owning loop) -------------------------------------------

    def _on_dispatch(self, mark):
        """Send linearization point: refusing here forbids the write."""
        context = self._submitting
        caller = self._caller
        if (self._stopped or context is None or context.operation_id
                or type(mark) is not DispatchMark
                or mark.operation_id != self.transport.pending_operation_id
                or (caller is not None and (caller.cancelling() or caller.done()))):
            raise ValueError("write_adapter_refused")
        if (mark.epoch != context.epoch or not self._compatible_now()
                or operation_reason(context.operation, self._live_dps)
                or (context.operation == 'schedule' and self._revision_reason(
                    'schedule', self._admitted_revision))):
            raise ValueError('write_adapter_refused')
        context = replace(context, operation_id=mark.operation_id, epoch=mark.epoch)
        self._submitting = self._current = context
        self._marks[mark.operation_id] = mark

    def on_report(self, report: DeviceReport) -> None:
        transport = self.transport
        if self._stopped or transport is None or type(report) is not DeviceReport:
            return
        if report.epoch != self.bridge.epoch:
            return
        if self._live_epoch != report.epoch:
            self._live_dps.clear()
            self._live_epoch = report.epoch
            self._live_sequence = -1
        if report.sequence <= self._live_sequence:
            return
        self._live_sequence = report.sequence
        self._live_dps.update(report.dps)
        self._notify()
        collector = self._collector
        if collector is not None:
            if transport._is_owner():
                collector.add(report)
            else:
                collector.invalidate("superseded")
        context = self._current
        # A confirmed recovery owns release until its evidence and final safety
        # validation complete; an echo alone must not bypass that process.
        if (collector is not None or self._phase == "failed"
                or context is None or not context.operation_id):
            return
        operation_id = context.operation_id
        mark = self._marks.get(operation_id)
        if (mark is None or operation_id in self._decisions
                or report.ack_eligible is not True
                or report.epoch != mark.epoch or report.sequence <= mark.sequence
                or transport.pending_operation_id != operation_id):
            return
        try:
            decision = self._ack(context, mark, report)
        except Exception:  # noqa: BLE001 - a failing predicate observes nothing
            decision = "ignore"
        if decision not in _OBSERVED:
            return
        self._decisions[operation_id] = decision
        if self._submitting is None:
            # Late acknowledgement after the drained send returned uncertain.
            self._track(self.hass.async_create_task(self._release(operation_id)))

    def _on_connection(self, epoch, connected):
        self._live_dps.clear()
        self._live_epoch = epoch
        self._live_sequence = -1
        if self._collector is not None:
            self._collector.invalidate("connection_interrupted")
        self._notify()

    # ----- recovery ------------------------------------------------------------------

    def recovery_context(self) -> IntentContext | None:
        """Context of the current uncertain intent for this construction/epoch."""
        transport = self.transport
        operation_id, operation = transport.pending_operation_id, self._pending_operation()
        if not transport.needs_reconciliation or not operation_id or operation is None:
            return None
        current = self._current
        dps = dict(current.dps) if current is not None and current.operation_id == operation_id \
            else {}
        return IntentContext(operation, operation_id, transport.recovery_session_id,
                             self.bridge.epoch, dps)

    def _context_current(self, context):
        transport = self.transport
        return (type(context) is IntentContext and not self._stopped
                and transport.needs_reconciliation
                and context.operation_id == transport.pending_operation_id
                and context.operation == self._pending_operation()
                and context.session_id == transport.recovery_session_id
                and context.epoch == self.bridge.epoch)

    def _begin_collect(self, context, timeout):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= RECOVERY_TIMEOUT):
            return None
        # Number every already-decoded report now: all of it is pre-request.
        self.bridge._flush()
        if (self._collector is not None or self._submitting is not None
                or any(self._faults()) or not self._context_current(context)
                or not self._compatible_now() or unsafe_reason(self._live_dps)):
            return None
        collector = _Collector(context, self.bridge.last_sequence, self._recovery_predicate)
        collector.timer = self.hass.loop.call_later(timeout, collector.expire)
        self._collector = collector
        return collector

    def _end_collect(self, collector):
        if collector.timer is not None:
            collector.timer.cancel()
            collector.timer = None
        if self._collector is collector:
            self._collector = None

    async def async_recovery_snapshot(self, context: IntentContext, *,
                                      timeout: float = RECOVERY_TIMEOUT) -> RecoveryEvidence | None:
        """Post-request, same-epoch evidence approved by the predicate, else None."""
        collector = self._begin_collect(context, timeout)
        if collector is None:
            return None
        try:
            await collector.done.wait()
        except asyncio.CancelledError:
            collector.invalidate("recovery_cancelled")
            raise
        finally:
            self._end_collect(collector)
        if collector.reason is not None or not collector.complete:
            return None
        return collector.evidence()

    def _recovery_valid(self, collector):
        """Final validation; synchronous, immediately before local release."""
        # Number every report a worker already decoded, so a just-arrived
        # active status (or connection break) is part of the evidence judged.
        self.bridge._flush()
        transport = self.transport
        context = collector.context
        storage, integrity = self._faults()
        if (self._stopped or self._collector is not collector or collector.reason is not None
                or not collector.complete or storage or integrity
                or self._submitting is not None
                or transport.pending_operation_id != context.operation_id
                or transport.recovery_session_id != context.session_id
                or self._pending_operation() != context.operation
                or self.bridge.epoch != context.epoch or not self._compatible_now()
                or unsafe_reason(self._live_dps)):
            return False
        return collector.approves()

    async def async_recover(self, operation_id: str, session_id: str, *,
                            confirmed: bool) -> bool:
        transport = self.transport
        if (confirmed is not True or type(operation_id) is not str
                or type(session_id) is not str
                or not self._can_start(self._phase_view())
                or operation_id != transport.pending_operation_id
                or session_id != transport.recovery_session_id):
            return False
        collector = self._begin_collect(self.recovery_context(), RECOVERY_TIMEOUT)
        if collector is None:
            return False
        self._phase, self._reason = "collecting", None
        self._notify()
        task = self._track(asyncio.Task(self._run_recover(collector), loop=self.hass.loop,
                                        eager_start=True))
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
                collector.invalidate("recovery_cancelled")
        result = task.result() is True if not task.cancelled() and task.exception() is None \
            else False
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def _run_recover(self, collector):
        transport = self.transport
        context = collector.context
        try:
            await collector.done.wait()
            if not transport.needs_reconciliation:
                return False  # released meanwhile by an observed acknowledgement
            if collector.reason is not None or not collector.complete:
                self._phase = "failed"
                self._reason = collector.reason or "awaiting_device_report"
                return False

            async def verifier():
                return self._recovery_valid(collector)

            released = await transport.async_reconcile(
                context.operation_id, verifier, confirmed=True, session_id=context.session_id)
            if released:
                self._phase, self._reason, self._result = "resolved", "local_uncertainty_cleared", None
                return True
            if transport.needs_reconciliation:
                self._phase = "failed"
                self._reason = collector.reason or "recovery_rejected"
            return False
        except asyncio.CancelledError:
            collector.invalidate("recovery_cancelled")
            self._phase, self._reason = "failed", "recovery_cancelled"
            raise
        finally:
            self._end_collect(collector)
            self._notify()

    # ----- shutdown --------------------------------------------------------------------

    async def async_stop(self, timeout: float = 5.0) -> bool:
        """Quiesce admission, invalidate evidence and drain adapter-owned work.

        Never closes the witness; the lifecycle fence drains the transport
        before the witness. False means work is still running.
        """
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid_timeout")
        deadline = monotonic() + timeout
        self._stopped = True
        if self._collector is not None:
            self._collector.invalidate("stopped")
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        pending = {task for task in self._tasks if not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=max(0.0, deadline - monotonic()))
            if pending:
                return False
        if self.transport is not None and not await self.transport.async_wait_idle(
                max(0.001, deadline - monotonic())):
            return False
        self._notify()
        return True


async def async_create_adapter(hass, entry, bridge, *, ack_predicate: AckPredicate,
                               recovery_predicate: RecoveryPredicate) -> WriteAdapter:
    """Adapter over an already opened witness in ``entry.runtime_data.witness``.

    Never opens or provisions storage. Raises the bridge's fixed failure when
    the owned transport cannot be enabled.
    """
    witness = getattr(getattr(entry, "runtime_data", None), "witness", None)
    if witness is None or not callable(ack_predicate) or not callable(recovery_predicate):
        raise ValueError("write_adapter_unavailable")
    adapter = WriteAdapter(hass, entry, bridge, witness, ack_predicate, recovery_predicate)
    adapter._unsubscribe = bridge.subscribe(adapter.on_report, adapter._on_connection)
    try:
        adapter.transport = await bridge.async_enable_transport(
            witness, on_dispatch=adapter._on_dispatch)
    except BaseException:
        adapter._unsubscribe()
        adapter._unsubscribe = None
        raise
    return adapter
