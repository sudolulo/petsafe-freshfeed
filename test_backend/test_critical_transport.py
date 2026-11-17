"""Offline safety tests; all DPS values are synthetic, NOT write authorization."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import io
import json
import logging
import threading
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from intent_witness_model import DurableWitnessModel

from custom_components.petsafe_freshfeed import critical_transport as transport
from custom_components.petsafe_freshfeed.critical_transport import (
    CompatibilityError,
    CriticalTransport,
)


log = logging.getLogger("petsafe.synthetic.vendor")
log.setLevel(logging.INFO)


class MemoryWitness:
    def __init__(self, record=None):
        self.record = record
        self.preparations = []
        self.fail = False
        self.entered = asyncio.Event()
        self.gate = None

    async def async_load_last_intent(self):
        return dict(self.record) if type(self.record) is dict else self.record

    async def async_prepare_intent(self, record):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("synthetic-private-exception")
        self.record = dict(record)
        self.preparations.append(dict(record))


class FakeSocket:
    def __init__(self):
        self.timeout = 12.0
        self.timeouts = []
        self.writes = []
        self.closed = False
        self.on_send = None
        self.on_recv = None
        self.fail_restore = False

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        if self.fail_restore and value == 12.0:
            raise RuntimeError("synthetic-private-timeout")
        self.timeout = value
        self.timeouts.append(value)

    def sendall(self, data):
        self.writes.append(data)
        if self.on_send:
            self.on_send()

    def recv(self, size):
        return self.on_recv(size) if self.on_recv else b"x"[:size]

    def close(self):
        self.closed = True


class FakeAPI:
    def __init__(self):
        self.socket = FakeSocket()
        self.parent = None
        self.children = {}
        self.socketRetryLimit = 4
        self.payloads = []
        self.calls = []
        self.action = None
        self.result = None
        self.fail_restore = False

    def set_socketRetryLimit(self, value):
        if self.fail_restore and value == 4:
            raise RuntimeError("synthetic-private-retry")
        self.socketRetryLimit = value

    def generate_payload(self, command, dps):
        log.debug("payload %r", dps)
        self.payloads.append((command, dict(dps)))
        return b"synthetic-control"

    def _send_receive(self, payload, *, getresponse, decode_response):
        log.debug("wire %r", payload)
        self.calls.append((getresponse, decode_response, self.socketRetryLimit))
        if self.action:
            return self.action(self, payload)
        self.socket.sendall(payload)
        return self.result

    def _get_socket(self, *args):
        raise AssertionError("No connection/preflight permitted")

    def status(self):
        raise AssertionError("No status permitted")


async def wait_thread_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeAPI()
        self.sock = self.api.socket
        self.wrapper = SimpleNamespace(_api=self.api, _api_lock=asyncio.Lock())
        self.witness = MemoryWitness()
        self.witness.fail = False
        self.engine = None
        self.thread_gates = []

    async def make(self, **kwargs):
        self.engine = await CriticalTransport.async_create(
            self.wrapper, self.witness,
            compatibility_check=kwargs.pop("compatibility_check", lambda *args: True),
            **kwargs,
        )
        return self.engine

    async def asyncTearDown(self):
        for gate in self.thread_gates:
            gate.set()
        if self.witness.gate:
            self.witness.gate.set()
        if self.engine:
            self.assertTrue(await self.engine.async_wait_idle(timeout=2))

    def hold_thread(self):
        entered, release = threading.Event(), threading.Event()
        self.thread_gates.append(release)

        def hold():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test event was not released")

        self.sock.on_send = hold
        return entered, release

    async def test_no_socket_no_payload_no_executor(self):
        self.api.socket = None
        await self.make()
        with patch.object(transport, "ThreadPoolExecutor", side_effect=AssertionError):
            receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual((receipt.outcome, receipt.attempted, receipt.client_returned,
                          receipt.reason), ("not_sent", False, False, "no_socket"))
        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.witness.record["state"], "pending")
        self.assertFalse(self.engine.needs_reconciliation)
        self.assertFalse(self.wrapper._api_lock.locked())

    async def test_compatibility_exact_true_required(self):
        for result in (False, None, 1, "yes"):
            with self.subTest(result=result), self.assertRaises(CompatibilityError):
                await self.make(compatibility_check=lambda *args: result)
        with self.assertRaises(TypeError):
            await CriticalTransport.async_create(self.wrapper, self.witness)
        self.assertEqual(self.witness.preparations, [])

    async def test_compatibility_error_never_exposes_payload(self):
        def fail(*args):
            raise RuntimeError("synthetic-private-exception")
        with self.assertRaises(CompatibilityError) as caught:
            await self.make(compatibility_check=fail)
        self.assertNotIn("synthetic-private", str(caught.exception))

    async def test_aliases_and_callback_rechecked_under_exact_lock(self):
        calls = []
        original_lock = self.wrapper._api_lock

        def check(wrapper, api, lock):
            self.assertIs(wrapper, self.wrapper)
            self.assertIs(api, self.api)
            self.assertIs(lock, original_lock)
            self.assertTrue(lock.locked())
            calls.append(True)
            return len(calls) == 1

        await self.make(compatibility_check=check)
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.outcome, "not_sent")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sock.writes, [])

    async def test_changed_api_or_lock_rejected(self):
        for alias in ("_api", "_api_lock"):
            with self.subTest(alias=alias):
                await self.make()
                original = getattr(self.wrapper, alias)
                setattr(self.wrapper, alias, object())
                receipt = await self.engine.async_send("settings", {"110": True})
                self.assertEqual(receipt.outcome, "not_sent")
                self.assertEqual(self.sock.writes, [])
                setattr(self.wrapper, alias, original)
                self.witness = MemoryWitness()

    async def test_callback_cannot_change_aliases(self):
        def check(*args):
            self.wrapper._api = object()
            return True
        with self.assertRaises(CompatibilityError):
            await self.make(compatibility_check=check)

    async def test_strict_dps_and_operation_shape(self):
        class DictSubclass(dict):
            pass
        class IntSubclass(int):
            pass
        class StrSubclass(str):
            pass
        await self.make()
        invalid = [{}, {"24": True}, {"150": 0}, {"999": 0}, {110: True},
                   {"0110": True}, {" 110": True}, DictSubclass({"110": True}),
                   {"110": None}, {"110": 1.0}, {"110": []}, {"110": {}},
                   {"110": IntSubclass(1)}, {"110": StrSubclass("x")},
                   {StrSubclass("110"): True}, {"110": 2**31},
                   {"110": -(2**31)-1}, {"110": "é" * 2049},
                   {"110": "\ud800"}, {"110": "a" * 4096, "111": "b" * 4096}]
        for dps in invalid:
            with self.subTest(kind=type(dps).__name__):
                receipt = await self.engine.async_send("settings", dps)
                self.assertEqual(receipt.outcome, "not_sent")
                self.assertFalse(receipt.attempted)
        for operation in ("unknown", "Feed", None, StrSubclass("feed")):
            receipt = await self.engine.async_send(operation, {"110": True})
            self.assertEqual(receipt.outcome, "not_sent")
        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.witness.preparations, [])

    async def test_valid_boundaries_and_operation_enums(self):
        await self.make()
        for operation in ("feed", "cancel", "defrost", "refilled", "settings", "schedule"):
            receipt = await self.engine.async_send(operation, {
                "1": False, "3": -(2**31), "110": True, "111": 2**31-1,
                "120": "é" * 2048, "125": "", "140": 0, "142": 1,
            })
            self.assertTrue(receipt.attempted)
            self.assertTrue(await self.engine.async_reconcile(
                receipt.operation_id, self.true_verifier, confirmed=True,
                session_id=self.engine.recovery_session_id))

    async def true_verifier(self):
        return True

    async def test_reconcile_retains_last_intent(self):
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.outcome, "unknown")
        operation_id = self.engine.pending_operation_id
        async def verified():
            return True
        self.assertTrue(await self.engine.async_reconcile(
            operation_id, verified, confirmed=True, session_id=self.engine.recovery_session_id))
        self.assertIsNotNone(self.witness.record)

    async def test_startup_does_not_rewrite_pending(self):
        self.witness.record = {
            "operation_id": "1" * 32,
            "operation": "settings",
            "state": "pending",
        }
        await self.make()
        self.assertEqual(self.witness.preparations, [])

    async def test_persist_before_send_and_metadata_only(self):
        await self.make()
        observed = []
        self.sock.on_send = lambda: observed.append(dict(self.witness.record))
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertRegex(receipt.operation_id, r"^[0-9a-f]{32}$")
        self.assertEqual(observed, [{"operation_id": receipt.operation_id,
                                     "operation": "settings", "state": "pending"}])
        self.assertEqual(self.witness.record, {"operation_id": receipt.operation_id,
                                              "operation": "settings", "state": "pending"})
        self.assertEqual(len(self.witness.preparations), 1)
        for record in self.witness.preparations:
            self.assertEqual(set(record), {"operation_id", "operation", "state"})
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.engine.pending_operation_id, receipt.operation_id)
        with self.assertRaises(FrozenInstanceError):
            receipt.outcome = "success"
        self.assertEqual(set(receipt.__dataclass_fields__),
                         {"operation_id", "outcome", "attempted", "client_returned", "reason"})

    async def test_prepare_failure_never_sends_and_faults_instance(self):
        self.witness.fail = True
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.outcome, "not_sent")
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertTrue(self.engine.storage_fault)
        self.assertEqual(self.sock.writes, [])
        self.assertFalse((await self.engine.async_send("feed", {"110": True})).attempted)

    async def test_no_postexchange_preparation(self):
        await self.make()
        self.sock.on_send = lambda: setattr(self.witness, "fail", True)
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.outcome, "unknown")
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.witness.record["state"], "pending")
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertFalse(self.engine.storage_fault)

    async def test_presend_rejection_retained_and_restart_rearms(self):
        self.api.socket = None
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.outcome, "not_sent")
        self.assertFalse(self.engine.needs_reconciliation)
        self.assertIsNone(self.engine.pending_operation_id)
        self.assertEqual(self.witness.record["operation_id"], receipt.operation_id)
        old_session = self.engine.recovery_session_id
        await self.make()
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.engine.pending_operation_id, receipt.operation_id)
        self.assertNotEqual(self.engine.recovery_session_id, old_session)
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertEqual(self.api.calls, [])

    async def test_debug_rejected_before_generation_without_logging_mutation(self):
        await self.make()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            receipt = await self.engine.async_send("settings", {"110": True})
            self.assertEqual(receipt.outcome, "not_sent")
            self.assertEqual(log.level, logging.DEBUG)
            self.assertEqual(self.api.payloads, [])
            self.assertEqual(stream.getvalue(), "")
        finally:
            log.removeHandler(handler)
            log.setLevel(logging.INFO)

    async def test_gateway_parent_and_children_rejected(self):
        await self.make()
        for attr, value in (("parent", object()), ("children", {"child": object()})):
            setattr(self.api, attr, value)
            receipt = await self.engine.async_send("settings", {"110": True})
            self.assertEqual(receipt.outcome, "not_sent")
            self.assertEqual(self.api.payloads, [])
            setattr(self.api, attr, None if attr == "parent" else {})

    async def test_null_error_and_normal_return_are_only_unknown(self):
        await self.make()
        for result in (None, {"Err": "synthetic-private"}, object()):
            self.api.result = result
            receipt = await self.engine.async_send("settings", {"110": True})
            self.assertEqual(receipt.outcome, "unknown")
            self.assertTrue(receipt.attempted)
            self.assertTrue(receipt.client_returned)
            self.assertTrue(await self.engine.async_reconcile(
                receipt.operation_id, self.true_verifier, confirmed=True,
                session_id=self.engine.recovery_session_id))
        self.assertEqual(self.api.calls, [(True, False, 0)] * 3)
        self.assertEqual(self.api.payloads, [(7, {"110": True})] * 3)
        self.assertIs(self.api.socket, self.sock)
        self.assertEqual(self.api.socketRetryLimit, 4)
        self.assertEqual(self.sock.timeout, 12.0)

    async def test_second_send_blocked_even_when_client_retries(self):
        def twice(api, payload):
            api.socket.sendall(payload)
            try:
                api.socket.sendall(payload)
            except Exception:
                pass  # Model a vendor swallowing its retry error.
        self.api.action = twice
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(len(self.sock.writes), 1)
        self.assertEqual(receipt.outcome, "unknown")
        self.assertTrue(self.engine.needs_reconciliation)

    async def test_partial_send_counts_attempt_before_exception(self):
        def partial():
            raise OSError("synthetic-private-partial")
        self.sock.on_send = partial

        def retry(api, payload):
            for _ in range(2):
                try:
                    api.socket.sendall(payload)
                except Exception:
                    pass
        self.api.action = retry
        await self.make()
        with self.assertNoLogs(log, level="INFO"):
            receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(len(self.sock.writes), 1)
        self.assertTrue(receipt.attempted)
        self.assertEqual(receipt.outcome, "unknown")
        self.assertNotIn("synthetic-private", repr(receipt))

    async def test_encoded_send_size_cap(self):
        self.api.action = lambda api, payload: api.socket.sendall(b"a" * 8193)
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(self.sock.writes, [])
        self.assertFalse(receipt.attempted)
        self.assertEqual(receipt.outcome, "unknown")

    async def test_receive_aggregate_cap_and_timeout_clamping(self):
        sizes = []

        def recv(size):
            sizes.append(size)
            return b"x" * size

        def receive(api, payload):
            api.socket.sendall(payload)
            for _ in range(3):
                api.socket.recv(40000)

        self.sock.on_recv = recv
        self.api.action = receive
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(sum(sizes), 65536)
        self.assertEqual(receipt.reason, "receive_limit")
        self.assertTrue(all(0 < v <= 3 for v in self.sock.timeouts[:-1]))
        self.assertEqual(self.sock.timeout, 12.0)

    async def test_slow_drip_prefix_scan_has_one_absolute_deadline(self):
        clock = [time.monotonic()]
        count = []

        def recv(size):
            clock[0] += 0.7
            count.append(size)
            return b"x"

        def receive(api, payload):
            api.socket.sendall(payload)
            for _ in range(100):
                api.socket.recv(1)  # Models endless prefix resynchronization.

        self.sock.on_recv = recv
        self.api.action = receive
        await self.make()
        with patch.object(transport, "monotonic", side_effect=lambda: clock[0]):
            receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(len(count), 5)
        self.assertEqual(receipt.reason, "socket_deadline")

    async def test_socket_closed_or_none_never_resurrected(self):
        await self.make()

        def close(api, payload):
            api.socket.sendall(payload)
            api.socket.close()
            api.socket = None

        self.api.action = close
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertTrue(self.sock.closed)
        self.assertIsNone(self.api.socket)
        self.assertEqual(self.api.socketRetryLimit, 4)
        self.assertEqual(receipt.outcome, "unknown")

    async def assert_integrity_barrier(self, receipt, *, durable_state="pending"):
        # Worker completion is not permission to reconcile an unsafe client.
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.engine.pending_operation_id, receipt.operation_id)
        expected = {"operation_id": receipt.operation_id,
                    "operation": "settings", "state": durable_state}
        self.assertEqual(self.witness.record, expected)
        preparations = list(self.witness.preparations)
        payloads, calls, writes = (list(self.api.payloads), list(self.api.calls),
                                  list(self.sock.writes))
        verifications = []

        async def verify_physical_operation():
            verifications.append(True)
            return True

        for _ in range(2):
            self.assertFalse(await self.engine.async_reconcile(
                receipt.operation_id, verify_physical_operation, confirmed=True,
                session_id=self.engine.recovery_session_id))
            self.assertEqual(verifications, [])
            self.assertTrue(self.engine.needs_reconciliation)
            self.assertEqual(self.engine.pending_operation_id, receipt.operation_id)
            self.assertEqual(self.witness.record, expected)
            self.assertEqual(self.witness.preparations, preparations)
            blocked = await self.engine.async_send("settings", {"110": True})
            self.assertEqual((blocked.operation_id, blocked.outcome, blocked.attempted,
                              blocked.client_returned, blocked.reason),
                             (receipt.operation_id, "not_sent", False, False, "blocked"))
            self.assertEqual(self.api.payloads, payloads)
            self.assertEqual(self.api.calls, calls)
            self.assertEqual(self.sock.writes, writes)

    async def test_restore_failures_report_integrity_and_keep_barrier(self):
        for target in (self.api, self.sock):
            with self.subTest(target=type(target).__name__):
                self.api.socketRetryLimit = 4
                self.sock.timeout = 12.0
                target.fail_restore = True
                await self.make()
                receipt = await self.engine.async_send("settings", {"110": True})
                self.assertEqual(receipt.reason, "transport_integrity")
                self.assertTrue(self.engine.needs_reconciliation)
                self.assertFalse(self.wrapper._api_lock.locked())
                target.fail_restore = False
                try:
                    await self.assert_integrity_barrier(receipt)
                finally:
                    self.witness = MemoryWitness()

    async def test_socket_assignment_restore_failure_is_fixed_integrity(self):
        class RestoreFailureAPI(FakeAPI):
            @property
            def socket(self):
                return self._socket

            @socket.setter
            def socket(self, value):
                if getattr(self, "reject_restore", False) and value is self.original:
                    raise RuntimeError("synthetic-private-socket-restore")
                self._socket = value

        self.api = RestoreFailureAPI()
        self.sock = self.api.socket
        self.api.original = self.sock
        self.api.reject_restore = True
        self.wrapper._api = self.api
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.reason, "transport_integrity")
        self.assertEqual(receipt.outcome, "unknown")
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertFalse(self.wrapper._api_lock.locked())
        self.api.reject_restore = False
        await self.assert_integrity_barrier(receipt)

    async def test_restore_fault_independent_of_witness_health(self):
        self.api.fail_restore = True
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.reason, "transport_integrity")
        self.assertFalse(self.engine.storage_fault)
        self.witness.fail = True
        await self.assert_integrity_barrier(receipt)
        self.witness.fail = False
        await self.assert_integrity_barrier(receipt)

    async def test_unexpected_thread_exception_permanently_blocks_reconciliation(self):
        class ThreadFailureAPI(FakeAPI):
            @property
            def socket(self):
                if threading.current_thread().name.startswith("petsafe-critical"):
                    # Fails before _exchange's try/finally: integrity is unknown.
                    raise RuntimeError("synthetic-private-thread-failure")
                return self._socket

            @socket.setter
            def socket(self, value):
                self._socket = value

        self.api = ThreadFailureAPI()
        self.sock = self.api.socket
        self.wrapper._api = self.api
        await self.make()
        with self.assertNoLogs(log), self.assertNoLogs("asyncio"):
            receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.reason, "transport_integrity")
        self.assertEqual(receipt.outcome, "unknown")
        self.assertNotIn("synthetic-private", repr(receipt))
        self.assertFalse(self.wrapper._api_lock.locked())
        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.sock.writes, [])
        await self.assert_integrity_barrier(receipt)

    async def test_socket_none_without_close_remains_none(self):
        def discard(api, payload):
            api.socket.sendall(payload)
            api.socket = None
        self.api.action = discard
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertIsNone(self.api.socket)
        self.assertFalse(self.sock.closed)
        self.assertEqual(self.sock.timeout, 12.0)
        self.assertEqual(receipt.outcome, "unknown")

    async def test_dead_socket_is_rejected_before_payload(self):
        self.sock.closed = True
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.reason, "no_socket")
        self.assertEqual(self.api.payloads, [])

    async def test_caller_cannot_mutate_validated_dps_while_witness_waits(self):
        await self.make()
        self.witness.gate = asyncio.Event()
        dps = {"110": True}
        caller = asyncio.create_task(self.engine.async_send("settings", dps))
        await self.witness.entered.wait()
        dps.clear()
        dps["24"] = True
        self.witness.gate.set()
        await caller
        self.assertEqual(self.api.payloads, [(7, {"110": True})])

    async def test_startup_load_error_faults_without_guessed_id(self):
        async def fail_load():
            raise RuntimeError("synthetic-private-load")
        with patch.object(self.witness, "async_load_last_intent", side_effect=fail_load):
            await self.make()
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertIsNone(self.engine.pending_operation_id)
        self.assertTrue(self.engine.storage_fault)
        with self.assertRaises(AttributeError):
            self.engine.storage_fault = False
        self.assertFalse(await self.engine.async_reconcile(
            "b" * 32, self.true_verifier, confirmed=True,
            session_id=self.engine.recovery_session_id))
        self.witness.record = {"operation_id": "b" * 32, "operation": "settings", "state": "pending"}
        self.witness.fail = True
        await self.make()
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertFalse(self.engine.storage_fault)
        self.assertEqual(self.engine.pending_operation_id, "b" * 32)
        self.assertEqual(self.witness.record["state"], "pending")

    async def test_verifier_cancellation_keeps_barrier(self):
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        entered = asyncio.Event()

        async def wait():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.engine.async_reconcile(
            receipt.operation_id, wait, confirmed=True,
            session_id=self.engine.recovery_session_id))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.witness.record["state"], "pending")
        self.assertFalse(self.engine._busy)

    async def test_busy_before_first_await_never_queues(self):
        await self.make()
        self.witness.gate = asyncio.Event()
        first = asyncio.create_task(self.engine.async_send("settings", {"110": True}))
        await self.witness.entered.wait()
        self.assertIsNotNone(self.engine.pending_operation_id)
        second = await self.engine.async_send("feed", {"110": True})
        self.assertEqual(second.outcome, "not_sent")
        self.assertFalse(second.attempted)
        self.witness.gate.set()
        await first
        self.assertEqual(len(self.sock.writes), 1)

    async def test_timeout_during_witness_cannot_late_dispatch(self):
        await self.make()
        self.witness.gate = asyncio.Event()
        receipt = await self.engine.async_send("settings", {"110": True}, timeout=0.01)
        self.assertEqual(receipt.outcome, "not_sent")
        self.witness.gate.set()
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertEqual(self.sock.writes, [])
        self.assertFalse(self.engine.needs_reconciliation)

    async def test_timeout_queued_for_lock_cannot_late_dispatch(self):
        await self.make()
        await self.wrapper._api_lock.acquire()
        try:
            receipt = await self.engine.async_send("settings", {"110": True}, timeout=0.01)
            self.assertEqual(receipt.outcome, "not_sent")
            self.assertFalse(await self.engine.async_wait_idle(timeout=0.01))
        finally:
            self.wrapper._api_lock.release()
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertEqual(self.sock.writes, [])

    async def test_caller_cancel_witness_and_lock_cannot_late_dispatch(self):
        for where in ("witness", "lock"):
            with self.subTest(where=where):
                self.witness = MemoryWitness()
                await self.make()
                self.witness.entered.clear()
                if where == "witness":
                    self.witness.gate = asyncio.Event()
                else:
                    await self.wrapper._api_lock.acquire()
                started = asyncio.Event()
                checkpoint = CriticalTransport._checkpoint
                def observe(job):
                    started.set()
                    checkpoint(job)
                with patch.object(CriticalTransport, "_checkpoint", side_effect=observe):
                    task = asyncio.create_task(self.engine.async_send("settings", {"110": True}))
                    await (self.witness.entered.wait() if where == "witness" else started.wait())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                if where == "witness":
                    self.witness.gate.set()
                    self.witness.gate = None
                else:
                    self.wrapper._api_lock.release()
                self.assertTrue(await self.engine.async_wait_idle())
                self.assertEqual(self.sock.writes, [])

    async def test_cancel_requested_before_caller_handler_prevents_dispatch(self):
        await self.make()
        caller = None
        original_prepare = self.witness.async_prepare_intent

        async def cancel_during_prepare(record):
            await original_prepare(record)
            if record is not None and record["state"] == "pending":
                # Cancellation is requested but the caller's except block has
                # not run. The worker must notice before its next submit.
                caller.cancel()

        with patch.object(self.witness, "async_prepare_intent", side_effect=cancel_during_prepare):
            caller = asyncio.create_task(self.engine.async_send("settings", {"110": True}))
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertTrue(await self.engine.async_wait_idle())
        self.assertEqual(self.sock.writes, [])
        self.assertEqual(self.api.payloads, [])

    async def test_worker_cancel_before_dispatch_never_sends(self):
        await self.make()
        self.witness.gate = asyncio.Event()
        caller = asyncio.create_task(self.engine.async_send("settings", {"110": True}))
        await self.witness.entered.wait()
        self.engine._worker.cancel()
        await asyncio.sleep(0)
        self.witness.gate.set()
        try:
            await caller
        except asyncio.CancelledError:
            pass
        self.witness.gate.set()
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertEqual(self.sock.writes, [])


        self.assertTrue(self.engine.needs_reconciliation)

    async def test_after_dispatch_timeout_retains_actual_thread_and_lock(self):
        entered, release = self.hold_thread()
        await self.make()
        task = asyncio.create_task(self.engine.async_send("settings", {"110": True}, timeout=0.02))
        await wait_thread_event(entered)
        receipt = await task
        self.assertEqual(receipt.outcome, "unknown")
        self.assertTrue(self.wrapper._api_lock.locked())
        self.assertFalse(await self.engine.async_wait_idle(timeout=0.01))
        self.assertFalse(await self.engine.async_reconcile(
            receipt.operation_id, self.true_verifier, confirmed=True,
            session_id=self.engine.recovery_session_id))
        release.set()
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertFalse(self.wrapper._api_lock.locked())
        self.assertIs(self.api.socket, self.sock)
        self.assertEqual(self.sock.timeout, 12.0)
        self.assertEqual(self.witness.record["state"], "pending")

    async def test_repeated_cancellation_drains_actual_future(self):
        entered, release = self.hold_thread()
        await self.make()
        caller = asyncio.create_task(self.engine.async_send("settings", {"110": True}))
        await wait_thread_event(entered)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        worker = self.engine._worker
        for _ in range(3):
            worker.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.wrapper._api_lock.locked())
            self.assertFalse(await self.engine.async_wait_idle(timeout=0.001))
        release.set()
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertFalse(self.wrapper._api_lock.locked())
        self.assertEqual(self.api.socketRetryLimit, 4)
        self.assertEqual(self.witness.record["state"], "pending")
        self.assertFalse(any(t.name.startswith("petsafe-critical") for t in threading.enumerate()))

    async def test_executor_queued_expiry_never_writes(self):
        entered, release = threading.Event(), threading.Event()
        self.thread_gates.append(release)
        executor = ThreadPoolExecutor(max_workers=1)

        def occupy():
            entered.set()
            release.wait(2)

        executor.submit(occupy)
        await wait_thread_event(entered)
        await self.make()
        with patch.object(transport, "ThreadPoolExecutor", return_value=executor):
            receipt = await self.engine.async_send("settings", {"110": True}, timeout=0.01)
            self.assertEqual(receipt.outcome, "unknown")
            self.assertTrue(self.wrapper._api_lock.locked())
            release.set()
            self.assertTrue(await self.engine.async_wait_idle())
        self.assertEqual(self.sock.writes, [])
        self.assertEqual(self.api.payloads, [])

    async def test_reconcile_requires_right_id_exact_true_and_no_writes(self):
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertFalse(await self.engine.async_reconcile(
            "0" * 32, self.true_verifier, confirmed=True,
            session_id=self.engine.recovery_session_id))
        for result in (False, None, 1, "true"):
            async def verify():
                return result
            self.assertFalse(await self.engine.async_reconcile(
                receipt.operation_id, verify, confirmed=True,
                session_id=self.engine.recovery_session_id))
            self.assertTrue(self.engine.needs_reconciliation)

        async def fail():
            raise RuntimeError("synthetic-private-verifier")
        self.assertFalse(await self.engine.async_reconcile(
            receipt.operation_id, fail, confirmed=True,
            session_id=self.engine.recovery_session_id))
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertFalse(self.engine._busy)
        retained = dict(self.witness.record)
        self.witness.fail = True  # Reconciliation must never prepare or clear.

        async def fresh_read():
            async with self.wrapper._api_lock:
                return True
        self.assertTrue(await asyncio.wait_for(
            self.engine.async_reconcile(receipt.operation_id, fresh_read, confirmed=True,
                                        session_id=self.engine.recovery_session_id), 1))
        self.assertEqual(self.witness.record, retained)
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertFalse(self.engine.needs_reconciliation)

    async def test_startup_pending_becomes_unknown_and_never_sends(self):
        self.witness.record = {"operation_id": "a" * 32, "operation": "feed", "state": "pending"}
        await self.make()
        self.assertEqual(self.witness.record["state"], "pending")
        self.assertEqual(self.engine._pending["state"], "unknown")
        self.assertEqual(self.witness.preparations, [])
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.engine.pending_operation_id, "a" * 32)
        self.assertFalse((await self.engine.async_send("settings", {"110": True})).attempted)
        self.assertEqual(self.sock.writes, [])

    async def test_malformed_witness_fails_closed(self):
        for record in ({}, [], {"operation_id": "a" * 32, "operation": "feed", "state": "success"},
                       {"operation_id": "A" * 32, "operation": "feed", "state": "unknown"},
                       {"operation_id": "a" * 32, "operation": "feed", "state": "unknown", "dps": {}},
                       {"operation_id": "bad", "operation": "feed", "state": "unknown"}):
            self.witness.record = record
            await self.make()
            self.assertTrue(self.engine.needs_reconciliation)
            self.assertTrue(self.engine.storage_fault)
            self.assertIsNone(self.engine.pending_operation_id)
            self.assertFalse((await self.engine.async_send("settings", {"110": True})).attempted)
        self.assertEqual(self.sock.writes, [])


    async def test_restart_rearms_after_local_reconciliation(self):
        first = await self.make()
        receipt = await first.async_send("settings", {"110": True})
        self.assertEqual(receipt.outcome, "unknown")
        operation_id = first.pending_operation_id
        old_session = first.recovery_session_id
        checks = 0

        async def fresh_check():
            nonlocal checks
            checks += 1
            return True

        self.assertTrue(await first.async_reconcile(
            operation_id, fresh_check, confirmed=True, session_id=old_session,
        ))
        self.assertFalse(first.needs_reconciliation)
        durable = dict(self.witness.record)
        second = await CriticalTransport.async_create(
            self.wrapper, self.witness, compatibility_check=lambda *args: True,
        )
        self.engine = second
        self.assertEqual(second.pending_operation_id, operation_id)
        self.assertNotEqual(second.recovery_session_id, old_session)
        self.assertTrue(second.needs_reconciliation)
        self.assertFalse(await second.async_reconcile(
            operation_id, fresh_check, confirmed=True, session_id=old_session,
        ))
        self.assertEqual(checks, 1)
        self.assertEqual(self.witness.record, durable)
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertEqual(len(self.api.calls), 1)
        blocked = await second.async_send("settings", {"110": True})
        self.assertEqual(blocked.reason, "blocked")
        self.assertEqual(len(self.api.calls), 1)

    async def test_recovery_gates_reject_before_verifier(self):
        class StringSubclass(str):
            pass

        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        session = self.engine.recovery_session_id
        self.assertRegex(session, r"^[0-9a-f]{32}$")
        with self.assertRaises(AttributeError):
            self.engine.recovery_session_id = "0" * 32
        calls = []

        async def verify():
            calls.append(True)
            return True

        for operation_id, confirmed, session_id in (
            ("0" * 32, True, session), (None, True, session),
            (StringSubclass(receipt.operation_id), True, session),
            (receipt.operation_id, False, session), (receipt.operation_id, 1, session),
            (receipt.operation_id, "true", session), (receipt.operation_id, True, None),
            (receipt.operation_id, True, "0" * 32),
            (receipt.operation_id, True, StringSubclass(session)),
        ):
            with self.subTest(confirmed=confirmed, session=session_id):
                self.assertFalse(await self.engine.async_reconcile(
                    operation_id, verify, confirmed=confirmed, session_id=session_id))
        self.assertFalse(await self.engine.async_reconcile(receipt.operation_id, verify))
        self.assertEqual(calls, [])
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertEqual(len(self.api.calls), 1)
        self.assertTrue(self.engine.needs_reconciliation)

    async def test_verifier_swallowed_cancellation_cannot_release(self):
        await self.make()
        receipt = await self.engine.async_send("settings", {"110": True})
        entered = asyncio.Event()

        async def swallow():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return True

        task = asyncio.create_task(self.engine.async_reconcile(
            receipt.operation_id, swallow, confirmed=True,
            session_id=self.engine.recovery_session_id))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.engine.pending_operation_id, receipt.operation_id)
        self.assertFalse(self.engine._busy)
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertEqual(len(self.api.calls), 1)

    async def test_startup_copies_valid_pending_and_unknown_without_writes(self):
        for state in ("pending", "unknown"):
            record = {"operation_id": "c" * 32, "operation": "settings", "state": state}
            self.witness = MemoryWitness(record)
            # Deliberately return the same object to catch mutation by the core.
            async def load():
                return record
            with patch.object(self.witness, "async_load_last_intent", side_effect=load):
                await self.make()
            self.assertEqual(record["state"], state)
            self.assertIsNot(self.engine._pending, record)
            self.assertEqual(self.engine._pending["state"], "unknown")
            self.assertFalse(self.engine.storage_fault)
            self.assertTrue(self.engine.needs_reconciliation)
            self.assertEqual(self.witness.preparations, [])
            self.assertEqual((await self.engine.async_send("settings", {"110": True})).reason,
                             "blocked")
        self.assertEqual(self.api.calls, [])

    async def test_startup_cancellation_returns_no_trusted_engine(self):
        entered = asyncio.Event()

        async def load():
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.witness, "async_load_last_intent", side_effect=load):
            task = asyncio.create_task(self.make())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(self.engine)
        self.assertEqual(self.witness.preparations, [])
        self.assertEqual(self.api.calls, [])

    async def test_worker_swallowed_prepare_cancel_still_cannot_dispatch(self):
        await self.make()
        entered = asyncio.Event()
        prepare = self.witness.async_prepare_intent

        async def swallow(record):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Deliberate ordinary contract violation, no uncancel(). The
                # checkpoint must still notice this worker's cancellation count.
                await prepare(record)

        with patch.object(self.witness, "async_prepare_intent", side_effect=swallow):
            caller = asyncio.create_task(self.engine.async_send("settings", {"110": True}))
            await asyncio.wait_for(entered.wait(), 1)
            self.engine._worker.cancel()
            receipt = await caller
        self.assertEqual(receipt.outcome, "not_sent")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.sock.writes, [])
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertFalse(self.engine.storage_fault)

    async def test_worker_cancel_requested_after_positive_prepare_cannot_dispatch(self):
        await self.make()
        prepare = self.witness.async_prepare_intent

        async def cancel_after(record):
            await prepare(record)
            asyncio.current_task().cancel()

        with patch.object(self.witness, "async_prepare_intent", side_effect=cancel_after):
            try:
                await self.engine.async_send("settings", {"110": True})
            except asyncio.CancelledError:
                pass
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertEqual(self.api.calls, [])
        self.assertEqual(len(self.witness.preparations), 1)
        self.assertFalse(self.engine.storage_fault)

    async def test_caller_or_worker_cancel_before_preparation(self):
        checkpoint = CriticalTransport._checkpoint
        for target in ("caller", "worker"):
            with self.subTest(target=target):
                self.witness = MemoryWitness()
                await self.make()

                def cancel_first(job):
                    task = job.caller if target == "caller" else asyncio.current_task()
                    task.cancel()
                    checkpoint(job)

                with patch.object(CriticalTransport, "_checkpoint", side_effect=cancel_first):
                    caller = asyncio.create_task(self.engine.async_send("settings", {"110": True}))
                    try:
                        await caller
                    except asyncio.CancelledError:
                        pass
                self.assertTrue(await self.engine.async_wait_idle())
                self.assertEqual(self.witness.preparations, [])
                self.assertFalse(self.engine.storage_fault)
                self.assertEqual(self.api.calls, [])

    async def test_worker_cancelled_before_first_step_settles_admission(self):
        await self.make()
        create_task = asyncio.create_task

        def cancel_worker(coro):
            task = create_task(coro)
            task.cancel()  # _run's try/finally has not started yet.
            return task

        with patch.object(transport.asyncio, "create_task", side_effect=cancel_worker):
            with self.assertRaises(asyncio.CancelledError):
                await self.engine.async_send("settings", {"110": True})
        self.assertTrue(await self.engine.async_wait_idle())
        self.assertFalse(self.engine.needs_reconciliation)
        self.assertFalse(self.engine.storage_fault)
        self.assertEqual(self.witness.preparations, [])
        self.assertEqual(self.api.calls, [])


class Message:
    """Synthetic undecoded frame (TinyTuya TuyaMessage-like); public fields only."""

    def __init__(self, payload, crc_good=True):
        self.payload = payload
        self.crc_good = crc_good


class HookedAPI(FakeAPI):
    """Adds the client's own decoder; the exchange returns an undecoded frame."""

    def __init__(self):
        super().__init__()
        self.decoded = []
        self.decode_error = False
        self.response = Message(b'{"dps":{"1":"echo","999":[1]}}')
        self.before_send = None

    def generate_payload(self, command, dps):
        payload = super().generate_payload(command, dps)
        if self.before_send:
            self.before_send()
        return payload

    def _send_receive(self, payload, *, getresponse, decode_response):
        log.debug("wire %r", payload)
        self.calls.append((getresponse, decode_response, self.socketRetryLimit))
        try:
            self.socket.sendall(payload)
        except Exception:
            return {"Err": "901"}
        return self.response

    def _decode_payload(self, payload):
        log.debug("decode %r", payload)
        self.decoded.append(payload)
        if self.decode_error:
            raise RuntimeError("synthetic-private-decode")
        return json.loads(payload)


class DispatchHookTests(unittest.IsolatedAsyncioTestCase):
    """Send-boundary watermark and bounded decoded responses (synthetic)."""

    async def asyncSetUp(self):
        self.api = HookedAPI()
        self.sock = self.api.socket
        self.wrapper = SimpleNamespace(_api=self.api, _api_lock=asyncio.Lock())
        self.witness = MemoryWitness()
        self.engine = None
        self.events = []
        self.loop_thread = threading.get_ident()
        self.sock.on_send = lambda: self.events.append(("write", threading.get_ident()))
        self.dispatch_error = None

    async def asyncTearDown(self):
        if self.engine:
            self.assertTrue(await self.engine.async_wait_idle(timeout=3))

    def dispatch(self, operation_id):
        self.events.append(("mark", threading.get_ident()))
        if self.dispatch_error:
            raise self.dispatch_error
        return SimpleNamespace(operation_id=operation_id, epoch=1, sequence=7)

    def report(self, dps):
        self.events.append(("report", dict(dps)))

    async def make(self, **kwargs):
        kwargs.setdefault("dispatch_callback", self.dispatch)
        kwargs.setdefault("report_callback", self.report)
        self.engine = await CriticalTransport.async_create(
            self.wrapper, self.witness, compatibility_check=lambda *args: True, **kwargs)
        return self.engine

    def kinds(self):
        return [event[0] for event in self.events]

    async def _true(self):
        return True

    async def test_mark_on_owning_loop_before_write_then_decoded_response(self):
        await self.make()
        receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        self.assertEqual(self.kinds(), ["mark", "write", "report"])
        self.assertEqual(self.events[0][1], self.loop_thread)
        self.assertNotEqual(self.events[1][1], self.loop_thread)
        # Actual decoded DPS only; never the merged cache or a guessed value.
        self.assertEqual(self.events[2][1], {"1": "echo"})
        self.assertEqual(self.api.decoded, [b'{"dps":{"1":"echo","999":[1]}}'])
        self.assertEqual(self.api.calls, [(True, False, 0)])
        self.assertEqual((receipt.outcome, receipt.attempted, receipt.client_returned),
                         ("unknown", True, True))
        self.assertTrue(self.engine.needs_reconciliation)

    async def test_defaults_preserve_foundation_behavior(self):
        await self.make(dispatch_callback=None, report_callback=None)
        receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        self.assertEqual(self.kinds(), ["write"])
        self.assertEqual(self.api.decoded, [])
        self.assertEqual(receipt.reason, "client_returned")

    async def test_invalid_hooks_rejected(self):
        for hooks in ({"dispatch_callback": 1}, {"report_callback": "x"}):
            with self.subTest(hooks=hooks), self.assertRaises(ValueError):
                await self.make(**hooks)

    async def test_dispatch_failure_forbids_send(self):
        self.dispatch_error = RuntimeError("synthetic-private-dispatch")
        await self.make()
        with self.assertNoLogs(level="DEBUG"):
            receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        self.assertEqual(self.kinds(), ["mark"])
        self.assertEqual(self.sock.writes, [])
        self.assertFalse(receipt.attempted)
        self.assertEqual(receipt.reason, "dispatch_handoff_failed")
        self.assertNotIn("synthetic-private", repr(receipt))
        # Prepared intent is retained; no acknowledgement is invented.
        self.assertTrue(self.engine.needs_reconciliation)
        self.assertEqual(self.api.decoded, [])

    async def test_wrong_mark_forbids_send(self):
        await self.make(dispatch_callback=lambda operation_id: SimpleNamespace(
            operation_id="0" * 32, epoch=1, sequence=1))
        receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        self.assertEqual(self.sock.writes, [])
        self.assertEqual(receipt.reason, "dispatch_handoff_failed")

    async def test_caller_cancel_during_handoff_forbids_send_and_mark(self):
        await self.make()
        loop = asyncio.get_running_loop()
        holder = {}
        self.api.before_send = lambda: loop.call_soon_threadsafe(holder["caller"].cancel)
        holder["caller"] = asyncio.create_task(
            self.engine.async_send("schedule", {"1": "/w=="}))
        with self.assertRaises(asyncio.CancelledError):
            await holder["caller"]
        self.assertTrue(await self.engine.async_wait_idle(timeout=3))
        self.assertEqual(self.sock.writes, [])
        self.assertNotIn("mark", self.kinds())
        self.assertTrue(self.engine.needs_reconciliation)

    async def test_blocked_loop_handoff_times_out_without_late_mark(self):
        await self.make()
        loop = asyncio.get_running_loop()
        self.api.before_send = lambda: loop.call_soon_threadsafe(time.sleep, 0.6)
        receipt = await self.engine.async_send("schedule", {"1": "/w=="}, timeout=0.3)
        self.assertTrue(await self.engine.async_wait_idle(timeout=3))
        await asyncio.sleep(0.05)
        self.assertEqual(self.sock.writes, [])
        self.assertNotIn("mark", self.kinds())  # an abandoned handoff never runs later
        self.assertIn(receipt.reason, ("caller_timeout", "dispatch_handoff_failed"))
        self.assertEqual(self.engine._job.reason, "dispatch_handoff_failed")
        self.assertTrue(self.engine.needs_reconciliation)

    async def test_closed_loop_handoff_forbids_send(self):
        await self.make()

        def closed(*args):
            raise RuntimeError("closed")
        self.engine._loop = SimpleNamespace(call_soon_threadsafe=closed)
        receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        self.assertEqual(self.sock.writes, [])
        self.assertEqual(receipt.reason, "dispatch_handoff_failed")

    async def test_decoder_failure_keeps_uncertainty_without_private_text(self):
        self.api.decode_error = True
        await self.make()
        with self.assertNoLogs(level="DEBUG"):
            receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        self.assertEqual(self.kinds(), ["mark", "write"])
        self.assertEqual(receipt.outcome, "unknown")
        self.assertEqual(receipt.reason, "response_undecodable")
        self.assertNotIn("synthetic-private", repr(receipt))
        self.assertTrue(self.engine.needs_reconciliation)

    async def test_report_callback_failure_keeps_uncertainty(self):
        def fail(dps):
            raise RuntimeError("synthetic-private-report")
        await self.make(report_callback=fail)
        with self.assertNoLogs(level="DEBUG"):
            receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        self.assertEqual(receipt.outcome, "unknown")
        self.assertEqual(receipt.reason, "report_delivery_failed")
        self.assertTrue(self.engine.needs_reconciliation)

    async def test_non_frames_and_empty_payloads_are_not_reports(self):
        await self.make()
        for response in (None, {"Err": "914"}, Message(b""),
                         Message(b'{"dps":{"1":"x"}}', False),
                         Message(b'{"Err":"900"}'), Message(b'{"dps":{"999":[1]}}')):
            with self.subTest(response=response):
                self.events.clear()
                self.api.response = response
                receipt = await self.engine.async_send("schedule", {"1": "/w=="})
                self.assertEqual(self.kinds(), ["mark", "write"])
                self.assertEqual(receipt.outcome, "unknown")
                self.assertTrue(await self.engine.async_reconcile(
                    receipt.operation_id, self._true, confirmed=True,
                    session_id=self.engine.recovery_session_id))

    async def test_debug_decoder_logger_rejected_before_generation(self):
        await self.make()
        other_log = logging.getLogger("petsafe.synthetic.decoder")
        namespace = {"log": other_log}
        exec("def _decode_payload(self, payload):\n    return {}\n", namespace)
        self.api._decode_payload = namespace["_decode_payload"].__get__(self.api)
        other_log.setLevel(logging.DEBUG)
        try:
            receipt = await self.engine.async_send("schedule", {"1": "/w=="})
        finally:
            other_log.setLevel(logging.NOTSET)
        self.assertEqual(receipt.reason, "unsafe_logging")
        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.sock.writes, [])


class DurableWitnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeAPI()
        self.wrapper = SimpleNamespace(_api=self.api, _api_lock=asyncio.Lock())
        self.models = []
        self.engines = []
        self.tasks = []

    async def asyncTearDown(self):
        for model in self.models:
            model.release_all()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for engine in self.engines:
            self.assertTrue(await engine.async_wait_idle(2))
        self.assertFalse(any(t.name.startswith("synthetic-witness")
                             for t in threading.enumerate()))

    def model(self, record=None, **kwargs):
        model = DurableWitnessModel(record, **kwargs)
        self.models.append(model)
        return model

    async def engine(self, model):
        engine = await CriticalTransport.async_create(
            self.wrapper, model, compatibility_check=lambda *args: True)
        self.engines.append(engine)
        return engine

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def reconcile(self, engine):
        async def fresh_check():
            return True
        return await engine.async_reconcile(
            engine.pending_operation_id, fresh_check, confirmed=True,
            session_id=engine.recovery_session_id)

    async def assert_storage_fault(self, engine):
        self.assertTrue(engine.storage_fault)
        self.assertTrue(engine.needs_reconciliation)
        invoked = []

        async def verifier():
            invoked.append(True)
            return True

        self.assertFalse(await engine.async_reconcile(
            engine.pending_operation_id, verifier, confirmed=True,
            session_id=engine.recovery_session_id))
        self.assertEqual(invoked, [])
        self.assertEqual((await engine.async_send("settings", {"110": True})).reason, "blocked")

    async def test_replacement_load_error_or_cancel_retires_predecessor(self):
        for mode in ("error", "cancel", "swallow"):
            with self.subTest(mode=mode):
                model = self.model(virgin=True)
                first = await self.engine(model)
                entered = asyncio.Event()

                async def load():
                    entered.set()
                    if mode == "error":
                        raise RuntimeError("synthetic")
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        if mode == "swallow":
                            return None
                        raise

                with patch.object(model, "async_load_last_intent", side_effect=load):
                    task = self.task(self.engine(model))
                    await entered.wait()
                    if mode != "error":
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        replacement = await task
                        self.assertTrue(replacement.storage_fault)
                stale = await first.async_send("feed", {"110": True})
                self.assertEqual((stale.outcome, stale.attempted), ("not_sent", False))
                self.assertEqual(model.preparations, [])

    async def test_waiting_constructor_cancel_does_not_retire_owner(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        await self.wrapper._api_lock.acquire()
        started = asyncio.Event()

        async def construct():
            started.set()
            return await self.engine(model)

        try:
            task = self.task(construct())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            self.wrapper._api_lock.release()
        receipt = await first.async_send("feed", {"110": True})
        self.assertEqual((receipt.outcome, receipt.attempted), ("unknown", True))

    async def test_loader_error_after_swallowed_cancel_propagates_and_retires_predecessor(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        entered = asyncio.Event()

        async def load():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("synthetic-private-load-after-cancel")

        with patch.object(model, "async_load_last_intent", side_effect=load):
            task = self.task(self.engine(model))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertTrue(task.cancelled())
        self.assertEqual(task.cancelling(), 1)
        self.assertNotIn("synthetic-private", "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(self.engines, [first])
        self.assertFalse(self.wrapper._api_lock.locked())
        stale = await first.async_send("feed", {"110": True})
        self.assertEqual((stale.outcome, stale.attempted, stale.reason),
                         ("not_sent", False, "blocked"))
        self.assertIsNone(model.durable)
        self.assertEqual(model.preparations, [])
        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.socket.writes, [])

    async def test_verifier_nontrue_or_error_after_swallowed_cancel_propagates(self):
        model = self.model(virgin=True)
        engine = await self.engine(model)
        receipt = await engine.async_send("settings", {"110": True})
        self.assertEqual((receipt.outcome, receipt.attempted), ("unknown", True))
        retained = {"operation_id": receipt.operation_id,
                    "operation": "settings", "state": "pending"}

        for outcome in (False, 1, None, "error"):
            with self.subTest(outcome=outcome):
                entered = asyncio.Event()

                async def verify():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        if outcome == "error":
                            raise RuntimeError("synthetic-private-verifier-after-cancel")
                        return outcome

                task = self.task(engine.async_reconcile(
                    receipt.operation_id, verify, confirmed=True,
                    session_id=engine.recovery_session_id))
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                self.assertTrue(task.cancelled())
                self.assertEqual(task.cancelling(), 1)
                self.assertNotIn("synthetic-private",
                                 "".join(traceback.format_exception(caught.exception)))
                self.assertTrue(engine.needs_reconciliation)
                self.assertEqual(engine.pending_operation_id, receipt.operation_id)
                self.assertEqual(engine._pending["state"], "unknown")
                self.assertFalse(engine._busy)
                self.assertTrue(await engine.async_wait_idle())
                self.assertFalse(self.wrapper._api_lock.locked())
                self.assertFalse(engine.storage_fault)
                blocked = await engine.async_send("feed", {"110": True})
                self.assertEqual((blocked.outcome, blocked.attempted, blocked.reason),
                                 ("not_sent", False, "blocked"))
                self.assertEqual(model.durable, retained)
                self.assertEqual(model.visible, retained)
                self.assertEqual(model.preparations, [retained])
                self.assertEqual(self.api.payloads, [(7, {"110": True})])
                self.assertEqual(self.api.calls, [(True, False, 0)])
                self.assertEqual(self.api.socket.writes, [b"synthetic-control"])

    async def test_queued_predecessor_rechecks_owner_before_preparation(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        construction_started = asyncio.Event()
        caller_started = asyncio.Event()

        async def construct():
            construction_started.set()
            return await self.engine(model)

        async def send():
            caller_started.set()
            return await first.async_send("feed", {"110": True})

        # The replacement queues first, but the predecessor still owns admission
        # until that constructor acquires the borrowed lock and publishes its token.
        await self.wrapper._api_lock.acquire()
        try:
            construction = self.task(construct())
            await asyncio.wait_for(construction_started.wait(), 1)
            caller = self.task(send())
            await asyncio.wait_for(caller_started.wait(), 1)
            self.assertIsNotNone(first.pending_operation_id)
            self.assertEqual(model.preparations, [])
        finally:
            self.wrapper._api_lock.release()

        replacement = await construction
        stale = await caller
        self.assertEqual((stale.outcome, stale.attempted), ("not_sent", False))
        self.assertFalse(first.storage_fault)
        self.assertEqual(model.preparations, [])
        self.assertEqual(self.api.calls, [])
        self.assertFalse(replacement.needs_reconciliation)
        admitted = await replacement.async_send("settings", {"110": True})
        self.assertEqual((admitted.outcome, admitted.attempted), ("unknown", True))
        self.assertEqual(model.durable["operation_id"], admitted.operation_id)
        self.assertEqual(len(model.preparations), 1)
        self.assertEqual(len(self.api.calls), 1)

    async def test_stale_reconcile_never_invokes_verifier(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        receipt = await first.async_send("feed", {"110": True})
        await self.engine(model)
        calls = []

        async def verify():
            calls.append(True)
            return True

        self.assertFalse(await first.async_reconcile(
            receipt.operation_id, verify, confirmed=True, session_id=first.recovery_session_id))
        self.assertEqual(calls, [])
        self.assertTrue(first.needs_reconciliation)

    async def test_replacement_during_verifier_cannot_release_old_barrier(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        receipt = await first.async_send("feed", {"110": True})
        entered, release = asyncio.Event(), asyncio.Event()

        async def verify():
            entered.set()
            await release.wait()
            return True

        task = self.task(first.async_reconcile(
            receipt.operation_id, verify, confirmed=True, session_id=first.recovery_session_id))
        await entered.wait()
        second = await self.engine(model)
        release.set()
        self.assertFalse(await task)
        self.assertTrue(first.needs_reconciliation)
        self.assertEqual(second.pending_operation_id, receipt.operation_id)

    async def test_replacement_waits_for_exchange_before_load(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def hold():
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise RuntimeError("synthetic_wire_gate_timeout")

        self.api.socket.on_send = hold
        caller = self.task(first.async_send("feed", {"110": True}))
        try:
            await entered.wait()
            model.load_entered.clear()
            started = asyncio.Event()

            async def construct():
                started.set()
                return await self.engine(model)

            construction = self.task(construct())
            await started.wait()
            self.assertFalse(construction.done())
            self.assertFalse(model.load_entered.is_set())
        finally:
            release.set()
        receipt = await caller
        second = await construction
        self.assertEqual(second.pending_operation_id, receipt.operation_id)

    async def test_only_explicit_provisioned_virgin_load_admits(self):
        for kwargs, fault in (({}, True), ({"virgin": True}, False),
                              ({"virgin": True, "load_error": True}, True)):
            with self.subTest(kwargs=kwargs):
                model = self.model(**kwargs)
                engine = await self.engine(model)
                self.assertEqual(engine.storage_fault, fault)
                self.assertEqual(engine.needs_reconciliation, fault)
                self.assertIsNone(engine.pending_operation_id)
                if fault:
                    await self.assert_storage_fault(engine)
                self.assertEqual(model.preparations, [])
        self.assertEqual(self.api.calls, [])

    async def test_model_corruption_and_load_error_are_unrecoverable_instance_faults(self):
        for record, load_error in (({}, False), ([], False),
                                   ({"operation_id": "a" * 32, "operation": "settings",
                                     "state": "pending"}, True)):
            model = self.model(record, load_error=load_error)
            engine = await self.engine(model)
            await self.assert_storage_fault(engine)
            self.assertIsNone(engine.pending_operation_id)
            self.assertEqual(model.preparations, [])
        self.assertEqual(self.api.calls, [])

    async def test_prepare_failure_matrix_preserves_conservative_crash_snapshot(self):
        old = {"operation_id": "d" * 32, "operation": "settings", "state": "pending"}
        cases = (("error_before", "old"), ("error_after", "new"),
                 ("fsync_old", "old"), ("fsync_new", "new"),
                 ("cancel_before", "old"), ("cancel_after", "new"))
        for mode, expected in cases:
            with self.subTest(mode=mode):
                model = self.model(old)
                engine = await self.engine(model)
                self.assertTrue(await self.reconcile(engine))
                step = model.queue_step(mode)
                receipt = await engine.async_send("feed", {"110": True})
                self.assertEqual(receipt.outcome, "not_sent")
                self.assertFalse(receipt.attempted)
                self.assertTrue(step.future.done())
                await self.assert_storage_fault(engine)
                new = {"operation_id": receipt.operation_id, "operation": "feed", "state": "pending"}
                self.assertEqual(model.preparations, [new])
                self.assertEqual(model.durable, old if expected == "old" else new)
                if mode == "fsync_old":
                    self.assertEqual(model.visible, new)
                    self.assertNotEqual(model.visible, model.durable)
                restarted = model.crash_restart()
                self.models.append(restarted)
                second = await self.engine(restarted)
                self.assertTrue(second.needs_reconciliation)
                self.assertFalse(second.storage_fault)
                self.assertEqual(second.pending_operation_id,
                                 old["operation_id"] if expected == "old" else receipt.operation_id)
                self.assertEqual((await second.async_send("settings", {"110": True})).reason,
                                 "blocked")
                self.assertEqual(restarted.preparations, [])
                self.assertEqual(self.api.calls, [])

    async def test_success_reconcile_and_crash_rearm_without_storage_mutation(self):
        model = self.model(virgin=True)
        engine = await self.engine(model)
        observed = []
        self.api.socket.on_send = lambda: observed.append(dict(model.durable))
        receipt = await engine.async_send("settings", {"110": True})
        expected = {"operation_id": receipt.operation_id, "operation": "settings", "state": "pending"}
        self.assertEqual(observed, [expected])
        self.assertEqual(receipt.outcome, "unknown")
        self.assertTrue(await self.reconcile(engine))
        self.assertEqual(model.durable, expected)
        self.assertEqual(model.preparations, [expected])
        restarted = model.crash_restart()
        self.models.append(restarted)
        second = await self.engine(restarted)
        self.assertEqual(second.pending_operation_id, receipt.operation_id)
        self.assertTrue(second.needs_reconciliation)
        self.assertNotEqual(second.recovery_session_id, engine.recovery_session_id)
        self.assertEqual(restarted.preparations, [])
        self.assertEqual(len(self.api.calls), 1)

    async def test_newest_coordinator_permanently_retires_virgin_predecessor(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        replacement = await self.engine(model)
        self.assertFalse(replacement.needs_reconciliation)
        self.assertNotEqual(first.recovery_session_id, replacement.recovery_session_id)

        stale = await first.async_send("feed", {"110": True})
        self.assertEqual((stale.outcome, stale.attempted, stale.client_returned),
                         ("not_sent", False, False))
        self.assertEqual(model.preparations, [])
        self.assertEqual(self.api.calls, [])
        self.assertIsNone(model.durable)

        admitted = await replacement.async_send("settings", {"110": True})
        self.assertEqual((admitted.outcome, admitted.attempted), ("unknown", True))
        self.assertEqual(model.durable["operation_id"], admitted.operation_id)
        self.assertEqual(len(model.preparations), 1)
        self.assertEqual(len(self.api.calls), 1)
        self.assertTrue(await self.reconcile(replacement))

        # Releasing the newest coordinator's local barrier cannot revive the old one.
        stale_again = await first.async_send("feed", {"110": True})
        self.assertEqual((stale_again.outcome, stale_again.attempted), ("not_sent", False))
        self.assertEqual(len(model.preparations), 1)
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(model.durable["operation_id"], admitted.operation_id)

    async def test_stale_coordinator_cannot_overwrite_dispatched_replacement_intent(self):
        model = self.model(virgin=True)
        first = await self.engine(model)
        replacement = await self.engine(model)
        loop = asyncio.get_running_loop()
        dispatched = asyncio.Event()
        stale_prepared = asyncio.Event()
        release_wire = threading.Event()
        observed_at_write = []
        prepare = model.async_prepare_intent

        async def observe_prepare(record):
            # Observe the real synthetic model's successful preparation; do not
            # impose an ownership invariant or alter its serialization behavior.
            await prepare(record)
            if record["operation"] == "feed":
                stale_prepared.set()

        def hold_dispatched_write():
            observed_at_write.append(dict(model.durable))
            loop.call_soon_threadsafe(dispatched.set)
            if not release_wire.wait(2):
                raise RuntimeError("synthetic_wire_gate_timeout")

        self.api.socket.on_send = hold_dispatched_write
        newest_caller = stale_caller = prepared_waiter = None
        try:
            with patch.object(model, "async_prepare_intent", side_effect=observe_prepare):
                newest_caller = self.task(replacement.async_send("settings", {"110": True}))
                await asyncio.wait_for(dispatched.wait(), 1)
                self.assertTrue(self.wrapper._api_lock.locked())
                stale_caller = self.task(first.async_send("feed", {"110": True}))
                prepared_waiter = self.task(stale_prepared.wait())
                done, _ = await asyncio.wait(
                    (stale_caller, prepared_waiter), timeout=1,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self.assertTrue(done, "stale caller neither rejected nor completed preparation")
                retained_while_dispatched = dict(model.durable)
                if stale_caller.done():
                    stale = await stale_caller
                    self.assertEqual((stale.outcome, stale.attempted), ("not_sent", False))
                else:
                    # The buggy predecessor has prepared and is waiting for the
                    # API lock. Cancel it before releasing the real wire owner.
                    stale_caller.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await stale_caller
        finally:
            release_wire.set()
            for task in (stale_caller, prepared_waiter):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (stale_caller, prepared_waiter)
                                   if task is not None), return_exceptions=True)
            if newest_caller is not None:
                await newest_caller
            for engine in (first, replacement):
                self.assertTrue(await engine.async_wait_idle(2))

        actual = newest_caller.result()
        self.assertEqual((actual.outcome, actual.attempted), ("unknown", True))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(observed_at_write[0]["operation_id"], actual.operation_id)
        self.assertEqual(
            retained_while_dispatched["operation_id"], actual.operation_id,
            "retained intent must identify the actually dispatched job, not the stale caller",
        )
        self.assertEqual(model.durable["operation_id"], actual.operation_id)
        self.assertEqual(len(model.preparations), 1)

    async def test_actual_prepare_future_cancel_drains_before_cross_engine_load(self):
        for pause in ("before", "after"):
            with self.subTest(pause=pause):
                self.api.calls.clear()
                model = self.model(virgin=True)
                first = await self.engine(model)
                step = model.queue_step(pause=pause)
                caller = self.task(first.async_send("settings", {"110": True}))
                await wait_thread_event(step.entered if pause == "before" else step.committed)
                old_id = first.pending_operation_id
                # An independently cancelled asyncio wrapper is already done;
                # the underlying writer is still running and owns serialization.
                wrapper = asyncio.wrap_future(step.future)
                wrapper.cancel()
                self.assertTrue(wrapper.done())
                self.assertFalse(step.future.done())
                for _ in range(3):
                    step.cancellation_seen.clear()
                    first._worker.cancel()
                    await asyncio.wait_for(step.cancellation_seen.wait(), 1)
                    self.assertFalse(step.future.done())
                    self.assertFalse(await first.async_wait_idle(0.001))
                with self.assertRaises(RuntimeError):
                    model.crash_restart()
                model.load_entered.clear()
                started = asyncio.Event()
                async def construct():
                    started.set()
                    return await self.engine(model)
                construction = self.task(construct())
                await started.wait()
                self.assertFalse(construction.done())
                self.assertFalse(model.load_entered.is_set())
                self.assertEqual(len(model.preparations), 1)
                self.assertEqual(self.api.calls, [])
                model.release_all()
                receipt = await caller
                second = await construction
                self.assertEqual(receipt.outcome, "not_sent")
                self.assertTrue(step.future.done())
                await self.assert_storage_fault(first)
                self.assertEqual(second.pending_operation_id, old_id)
                self.assertTrue(second.needs_reconciliation)
                self.assertEqual((await second.async_send("settings", {"110": True})).reason,
                                 "blocked")
                self.assertEqual(self.api.calls, [])
                # Only now can a newer preparation happen. The older producer
                # cannot finish later and overwrite this acknowledged intent.
                self.assertTrue(await self.reconcile(second))
                newer = await second.async_send("feed", {"110": True})
                self.assertNotEqual(newer.operation_id, old_id)
                self.assertEqual(model.durable["operation_id"], newer.operation_id)
                self.assertEqual(model.max_active, 1)
                self.assertEqual(model.active, 0)
                self.assertEqual(len(model.preparations), 2)
                self.assertEqual(len(self.api.calls), 1)
                crash = model.crash_restart()
                self.models.append(crash)
                third = await self.engine(crash)
                self.assertEqual(third.pending_operation_id, newer.operation_id)
                self.assertEqual(len(self.api.calls), 1)

    async def test_actual_prepare_caller_cancel_retains_intent_and_blocks_live_restart(self):
        for pause in ("before", "after"):
            with self.subTest(pause=pause):
                model = self.model(virgin=True)
                first = await self.engine(model)
                step = model.queue_step(pause=pause)
                caller = self.task(first.async_send("settings", {"110": True}))
                await wait_thread_event(step.entered if pause == "before" else step.committed)
                operation_id = first.pending_operation_id
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                self.assertFalse(step.future.done())
                self.assertFalse(await first.async_wait_idle(0.001))
                self.assertEqual((await first.async_send("settings", {"110": True})).reason,
                                 "blocked")
                model.load_entered.clear()
                started = asyncio.Event()
                async def construct():
                    started.set()
                    return await self.engine(model)
                construction = self.task(construct())
                await started.wait()
                self.assertFalse(construction.done())
                self.assertFalse(model.load_entered.is_set())
                model.release_all()
                self.assertTrue(await first.async_wait_idle())
                second = await construction
                self.assertTrue(step.future.done())
                self.assertFalse(first.storage_fault)  # Shielded preparation succeeded.
                self.assertFalse(first.needs_reconciliation)
                self.assertEqual(second.pending_operation_id, operation_id)
                self.assertTrue(second.needs_reconciliation)
                self.assertEqual(model.durable["operation_id"], operation_id)
                self.assertEqual(len(model.preparations), 1)
                self.assertEqual(self.api.calls, [])

    async def test_queued_newer_writer_cannot_overtake_cancelled_actual_future(self):
        old = {"operation_id": "e" * 32, "operation": "settings", "state": "pending"}
        new = {"operation_id": "f" * 32, "operation": "feed", "state": "pending"}
        model = self.model(virgin=True)
        slow = model.queue_step(pause="before")
        next_step = model.queue_step()
        older = self.task(model.async_prepare_intent(old))
        await wait_thread_event(slow.entered)
        older.cancel()
        await asyncio.wait_for(slow.cancellation_seen.wait(), 1)
        newer = self.task(model.async_prepare_intent(new))
        await asyncio.sleep(0)  # Give the newer coroutine its attempt at the lease.
        self.assertFalse(next_step.entered.is_set())
        self.assertFalse(slow.future.done())
        self.assertFalse(older.done())
        self.assertFalse(newer.done())
        self.assertEqual(model.preparations, [old])
        model.release_all()
        with self.assertRaises(asyncio.CancelledError):
            await older
        await newer
        self.assertTrue(slow.future.done())
        self.assertTrue(next_step.future.done())
        self.assertEqual(model.preparations, [old, new])
        self.assertEqual(model.durable, new)
        self.assertEqual(model.max_active, 1)
        second = await self.engine(model)
        self.assertEqual(second.pending_operation_id, new["operation_id"])
        self.assertTrue(second.needs_reconciliation)
        self.assertEqual(self.api.calls, [])


class Owner:
    """Synthetic PetSafe-owned client owner (no Tuya Local wrapper shape)."""

    def __init__(self, api, lock):
        self.api = api
        self.lock = lock
        self.current = True

    def is_current(self):
        return self.current


class OwnedTransportTests(unittest.IsolatedAsyncioTestCase):
    """async_create_owned shares the core; the real owner is validated."""

    async def asyncSetUp(self):
        self.api = FakeAPI()
        self.owner = Owner(self.api, asyncio.Lock())
        self.witness = MemoryWitness()
        self.checked = []
        self.engine = None

    async def asyncTearDown(self):
        if self.engine:
            self.assertTrue(await self.engine.async_wait_idle(timeout=2))

    def check(self, owner, api, lock):
        self.checked.append((owner, api, lock))
        return True

    async def make(self, **kwargs):
        self.engine = await CriticalTransport.async_create_owned(
            self.owner, self.witness, compatibility_check=self.check, **kwargs)
        return self.engine

    async def test_owned_check_receives_real_owner(self):
        await self.make()
        self.assertEqual(self.checked, [(self.owner, self.api, self.owner.lock)])
        self.assertFalse(hasattr(self.owner, "_api"))
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.outcome, "unknown")
        self.assertEqual(len(self.api.socket.writes), 1)

    async def test_owned_not_current_refused(self):
        self.owner.current = False
        with self.assertRaises(CompatibilityError):
            await self.make()
        self.owner.current = True
        await self.make()
        self.owner.current = False
        receipt = await self.engine.async_send("settings", {"110": True})
        self.assertEqual((receipt.outcome, receipt.reason), ("not_sent", "incompatible"))
        self.assertEqual(self.api.socket.writes, [])

    async def test_owned_owner_replacement_refused(self):
        for attribute in ("api", "lock"):
            with self.subTest(attribute=attribute):
                self.api = FakeAPI()
                original = (self.api, asyncio.Lock())
                self.owner = Owner(*original)
                self.witness = MemoryWitness()
                await self.make()
                setattr(self.owner, attribute,
                        FakeAPI() if attribute == "api" else asyncio.Lock())
                receipt = await self.engine.async_send("settings", {"110": True})
                self.assertEqual((receipt.outcome, receipt.attempted, receipt.reason),
                                 ("not_sent", False, "incompatible"))
                self.assertEqual(self.api.payloads, [])
                self.assertEqual(self.api.socket.writes, [])
                self.assertEqual(self.owner.api.socket.writes, [])
                # The retained durable record stays authoritative.
                self.assertEqual(self.witness.record["state"], "pending")
                self.assertEqual(self.witness.record["operation_id"], receipt.operation_id)
                self.assertTrue(await self.engine.async_wait_idle(timeout=2))
                self.owner.api, self.owner.lock = original
                fresh = await self.make()
                self.assertTrue(fresh.needs_reconciliation)
                self.assertEqual(fresh.pending_operation_id, receipt.operation_id)
                again = await fresh.async_send("settings", {"110": True})
                self.assertEqual(again.reason, "blocked")
                self.assertEqual(self.api.socket.writes, [])

    async def test_owned_bounds_and_hooks_validated(self):
        with self.assertRaises(ValueError):
            await self.make(socket_deadline=4.0)
        with self.assertRaises(ValueError):
            await self.make(dispatch_callback=1)
        with self.assertRaises(CompatibilityError):
            await CriticalTransport.async_create_owned(
                SimpleNamespace(api=self.api, lock=object(), is_current=lambda: True),
                self.witness, compatibility_check=self.check)

    async def test_legacy_create_signature_unchanged(self):
        import inspect
        params = inspect.signature(CriticalTransport.async_create).parameters
        self.assertEqual(list(params), ["wrapper", "witness", "compatibility_check",
                                        "socket_deadline", "max_receive_bytes",
                                        "dispatch_callback", "report_callback"])
        self.assertEqual(params["wrapper"].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for name in ("compatibility_check", "socket_deadline", "max_receive_bytes",
                     "dispatch_callback", "report_callback"):
            self.assertEqual(params[name].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual((params["socket_deadline"].default, params["max_receive_bytes"].default,
                          params["dispatch_callback"].default, params["report_callback"].default),
                         (3.0, 65536, None, None))
        owned = inspect.signature(CriticalTransport.async_create_owned).parameters
        self.assertEqual(list(owned), ["owner", "witness", "compatibility_check",
                                       "socket_deadline", "max_receive_bytes",
                                       "dispatch_callback", "report_callback"])
        # Legacy check still receives the actual Tuya Local wrapper.
        wrapper = SimpleNamespace(_api=self.api, _api_lock=asyncio.Lock())
        seen = []
        self.engine = await CriticalTransport.async_create(
            wrapper, self.witness, compatibility_check=lambda *args: seen.append(args) or True)
        self.assertEqual(seen, [(wrapper, self.api, wrapper._api_lock)])

    async def test_report_types_reexported(self):
        from custom_components.petsafe_freshfeed import local_source, tuya_bridge
        self.assertIs(tuya_bridge.DeviceReport, local_source.DeviceReport)
        self.assertIs(tuya_bridge.DispatchMark, local_source.DispatchMark)
        self.assertIs(tuya_bridge._settle, local_source._settle)
        report = local_source.DeviceReport(1, 2, None, {"1": "x"})
        self.assertTrue(report.ack_eligible)
        with self.assertRaises(FrozenInstanceError):
            report.sequence = 3
        with self.assertRaises(FrozenInstanceError):
            local_source.DispatchMark("a", 1, 2).epoch = 5


class ShutdownTests(unittest.TestCase):
    def test_asyncio_run_shutdown_waits_for_actual_thread_restoration(self):
        entered, release, finishing = threading.Event(), threading.Event(), threading.Event()
        state = {}

        async def main():
            api = FakeAPI()
            original = api.socket
            wrapper = SimpleNamespace(_api=api, _api_lock=asyncio.Lock())
            witness = MemoryWitness()
            witness.fail = False

            def hold():
                entered.set()
                release.wait(2)

            original.on_send = hold
            engine = await CriticalTransport.async_create(
                wrapper, witness, compatibility_check=lambda *args: True)
            state.update(engine=engine, api=api, original=original, witness=witness, wrapper=wrapper)
            state["caller"] = asyncio.create_task(engine.async_send("settings", {"110": True}))
            await wait_thread_event(entered)
            finishing.set()
            # Returning enters asyncio.run's cancellation/finalization barrier.

        def run():
            try:
                asyncio.run(main())
            except BaseException as error:
                state["error"] = type(error).__name__

        runner = threading.Thread(target=run, name="synthetic-loop-owner")
        runner.start()
        try:
            self.assertTrue(finishing.wait(2))
            self.assertTrue(state["wrapper"]._api_lock.locked())
            self.assertTrue(runner.is_alive())
        finally:
            release.set()
            runner.join(3)
        self.assertFalse(runner.is_alive())
        self.assertNotIn("error", state)
        self.assertFalse(state["wrapper"]._api_lock.locked())
        self.assertIs(state["api"].socket, state["original"])
        self.assertEqual(state["original"].timeout, 12.0)
        self.assertEqual(state["api"].socketRetryLimit, 4)
        self.assertEqual(state["witness"].record["state"], "pending")
        self.assertFalse(any(t.name.startswith("petsafe-critical") for t in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
