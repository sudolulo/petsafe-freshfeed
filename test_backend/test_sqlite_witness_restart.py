"""Owned process death and synthetic transport; no power-loss/physical proof."""

import asyncio
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from custom_components.petsafe_freshfeed import sqlite_witness as sw
from custom_components.petsafe_freshfeed.critical_transport import CriticalTransport

STORE, BINDING = "1" * 32, "2" * 32
OLD = {"operation_id": "3" * 32, "operation": "feed", "state": "pending"}
NEW = {"operation_id": "4" * 32, "operation": "settings", "state": "unknown"}

CHILD_WRITE = r'''
import asyncio, os, sys
from unittest.mock import patch
import custom_components.petsafe_freshfeed.sqlite_witness as sw
path, phase = sys.argv[1:3]
assert phase in ("before", "after", "ack")
NEW = {"operation_id": "4" * 32, "operation": "settings", "state": "unknown"}
STORE, BINDING = "1" * 32, "2" * 32
async def prepare():
    witness = sw.SQLiteIntentWitness(path, store_id=STORE, binding_id=BINDING)
    if phase == "ack":
        await witness.async_prepare_intent(NEW)
        assert await witness.async_wait_idle(2)
        print("READY", flush=True)
        os.read(0, 1)
        return
    real_execute = sw._execute
    commits = 0
    def intercept_commit(conn, sql, parameters=()):
        nonlocal commits
        if sql.strip().upper() == "COMMIT":
            commits += 1
            assert commits == 1, "expected exactly one intercepted COMMIT"
            if phase == "before":
                print("READY", flush=True)
                os.read(0, 1)
            result = real_execute(conn, sql, parameters)
            if phase == "after":
                print("READY", flush=True)
                os.read(0, 1)
            return result
        return real_execute(conn, sql, parameters)
    with patch.object(sw, "_execute", intercept_commit):
        await witness.async_prepare_intent(NEW)
    assert commits == 1, "expected exactly one intercepted COMMIT"
asyncio.run(prepare())
'''

CHILD_READ = r'''
import asyncio, json, sys
from custom_components.petsafe_freshfeed.sqlite_witness import SQLiteIntentWitness
async def read():
    witness = SQLiteIntentWitness(sys.argv[1], store_id="1"*32, binding_id="2"*32)
    record = await witness.async_load_last_intent()
    assert await witness.async_wait_idle(2)
    print(json.dumps(record, sort_keys=True), flush=True)
asyncio.run(read())
'''


class BoundedPipes:
    LIMITS = {"stdout": 512, "stderr": 2048}

    def __init__(self, child):
        self.child = child
        self.selector = selectors.DefaultSelector()
        self.data = {"stdout": bytearray(), "stderr": bytearray()}
        self.eof, self.overflow, self.closed = set(), set(), set()
        try:
            for name in ("stdout", "stderr"):
                pipe = getattr(child, name)
                os.set_blocking(pipe.fileno(), False)
                self.selector.register(pipe, selectors.EVENT_READ, name)
        except BaseException:
            self.close()
            raise

    def _close_stream(self, name):
        if name not in self.closed:
            self.selector.unregister(getattr(self.child, name))
            getattr(self.child, name).close()
            self.closed.add(name)

    def pump(self, deadline):
        if not self.selector.get_map():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("owned child pipe deadline expired")
        for key, _ in self.selector.select(min(0.05, remaining)):
            name = key.data
            available = self.LIMITS[name] - len(self.data[name])
            try:
                chunk = os.read(key.fd, min(4096, available + 1))
            except BlockingIOError:
                continue
            if not chunk:
                self.eof.add(name)
                self._close_stream(name)
                continue
            self.data[name].extend(chunk[:available])
            if len(chunk) > available:
                # One sentinel byte bounds detection; never discard an
                # unbounded producer stream while pretending it was captured.
                self.overflow.add(name)
                self._close_stream(name)

    def drain(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.selector.get_map():
            self.pump(deadline)
        if self.overflow:
            raise AssertionError("owned child output exceeded bounds: " + ", ".join(sorted(self.overflow)))
        if self.data["stderr"]:
            raise AssertionError("unexpected child stderr: " + repr(bytes(self.data["stderr"])))

    def close(self):
        errors = []
        for name in ("stdout", "stderr"):
            pipe = getattr(self.child, name, None)
            if pipe is not None and name not in self.closed:
                try:
                    if pipe in (key.fileobj for key in self.selector.get_map().values()):
                        self.selector.unregister(pipe)
                    pipe.close()
                    self.closed.add(name)
                except BaseException as error:
                    errors.append(error)
        try:
            self.selector.close()
        except BaseException as error:
            errors.append(error)
        if errors:
            raise RuntimeError("pipe/selector close failed: " + repr(errors))


def stop_owned(child, capture_or_none):
    errors = []
    try:
        if child.poll() is None:
            child.kill()  # Only this owned Popen PID, never a process group.
        child.wait(timeout=5)
    except BaseException as error:
        errors.append(error)
    if capture_or_none is not None:
        try:
            capture_or_none.drain(timeout=5)
        except BaseException as error:
            errors.append(error)
        try:
            capture_or_none.close()
        except BaseException as error:
            errors.append(error)
    for pipe in (child.stdin, child.stdout, child.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except BaseException as error:
                errors.append(error)
    if errors:
        captured = ""
        if capture_or_none is not None:
            captured = (" stdout=" + repr(bytes(capture_or_none.data["stdout"])) +
                        " stderr=" + repr(bytes(capture_or_none.data["stderr"])))
        raise RuntimeError("owned child cleanup failed: " + repr(errors) + captured)


def wait_ready(child, capture):
    deadline = time.monotonic() + 5
    while True:
        stdout = bytes(capture.data["stdout"])
        if capture.overflow:
            raise AssertionError("owned child output exceeded bounds: " + repr(capture.overflow))
        if capture.data["stderr"]:
            raise AssertionError("unexpected child stderr: " + repr(bytes(capture.data["stderr"])))
        if stdout == b"READY\n":
            if child.poll() is not None:
                raise AssertionError("owned child exited before READY was accepted")
            return
        if not b"READY\n".startswith(stdout):
            raise AssertionError("invalid owned child READY prefix: " + repr(stdout))
        if "stdout" in capture.eof or child.poll() is not None:
            raise AssertionError("owned child exited before READY")
        if time.monotonic() >= deadline:
            raise TimeoutError("owned child did not reach READY")
        capture.pump(deadline)


def kill_at_public_boundary(path, phase):
    child = subprocess.Popen([sys.executable, "-B", "-c", CHILD_WRITE, path, phase],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    capture = None
    try:
        capture = BoundedPipes(child)
        wait_ready(child, capture)
        child.kill()
        if child.wait(timeout=5) != -signal.SIGKILL:
            raise AssertionError("owned child did not die by SIGKILL")
    finally:
        stop_owned(child, capture)
        if capture is not None and bytes(capture.data["stdout"]) != b"READY\n":
            raise AssertionError("owned child stdout after cleanup was not exactly READY: " +
                                 repr(bytes(capture.data["stdout"])))


def read_in_fresh_process(path):
    child = subprocess.Popen([sys.executable, "-B", "-c", CHILD_READ, path],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    capture = None
    try:
        capture = BoundedPipes(child)
        deadline = time.monotonic() + 5
        capture.drain(timeout=max(0, deadline - time.monotonic()))
        if child.wait(timeout=max(0, deadline - time.monotonic())) != 0:
            raise AssertionError("owned reader failed")
        if capture.eof != {"stdout", "stderr"}:
            raise AssertionError("owned reader streams did not both reach EOF: " + repr(capture.eof))
        data = bytes(capture.data["stdout"])
        if not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise AssertionError("owned reader must emit exactly one newline-terminated JSON line")
        result = json.loads(data[:-1])
        if data != (json.dumps(result, sort_keys=True) + "\n").encode("utf-8"):
            raise AssertionError("owned reader emitted unexpected, noncanonical, or trailing bytes")
        return result
    finally:
        stop_owned(child, capture)


async def run_owned_job(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    finally:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # Repeated cancellation cannot abandon the owned thread.
            except BaseException:
                if not task.done():
                    raise
                # Retrieve and re-raise the settled worker failure below.
        task.result()


log = logging.getLogger("petsafe.sqlite.synthetic")


class SyntheticSocket:
    def __init__(self):
        self.timeout, self.closed, self.writes = 12.0, False, []
    def gettimeout(self):
        return self.timeout
    def settimeout(self, value):
        self.timeout = value
    def sendall(self, data):
        self.writes.append(data)
    def recv(self, size):
        return b"x"[:size]
    def close(self):
        self.closed = True


class SyntheticAPI:
    def __init__(self):
        self.socket = SyntheticSocket()
        self.parent, self.children = None, {}
        self.socketRetryLimit = 4
        self.calls, self.payloads = [], []
    def set_socketRetryLimit(self, value):
        self.socketRetryLimit = value
    def generate_payload(self, command, dps):
        log.debug("synthetic payload")
        self.payloads.append((command, dict(dps)))
        return b"synthetic-control"
    def _send_receive(self, payload, *, getresponse, decode_response):
        log.debug("synthetic exchange")
        self.calls.append((getresponse, decode_response))
        self.socket.sendall(payload)
        return None
    def _get_socket(self, *args):
        raise AssertionError("no connection allowed")
    def status(self):
        raise AssertionError("no status allowed")


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="sqlite-restart-", dir=Path(__file__).resolve().parent)
        self.root = Path(self.tmp.name)
        os.chmod(self.root, 0o700)
        self.witnesses, self.engines = [], []
        self.log_level = log.level
        log.setLevel(logging.INFO)
        self.api = SyntheticAPI()
        self.wrapper = SimpleNamespace(_api=self.api, _api_lock=asyncio.Lock())

    async def create_store(self, label):
        parent = self.root / label
        parent.mkdir(mode=0o700)
        path = str(parent / "witness.sqlite")
        witness = await sw.SQLiteIntentWitness.async_provision(path, store_id=STORE, binding_id=BINDING)
        self.witnesses.append(witness)
        return path, witness

    async def engine(self, witness):
        engine = await CriticalTransport.async_create(
            self.wrapper, witness, compatibility_check=lambda *args: True)
        self.engines.append(engine)
        return engine

    async def asyncTearDown(self):
        try:
            for engine in self.engines:
                self.assertTrue(await engine.async_wait_idle(3))
            for witness in self.witnesses:
                self.assertTrue(await witness.async_wait_idle(3))
        finally:
            log.setLevel(self.log_level)
            if all(w._gate.actual is None and w._gate.pending == 0 for w in self.witnesses):
                self.tmp.cleanup()
            else:
                raise AssertionError("owned work did not settle; refuse directory cleanup")

    async def test_owned_process_death_reopens_without_virgin_reset(self):
        for phase in ("before", "after", "ack"):
            with self.subTest(phase=phase):
                path, witness = await self.create_store("death-" + phase)
                await witness.async_prepare_intent(OLD)
                await run_owned_job(kill_at_public_boundary, path, phase)
                recovered = await run_owned_job(read_in_fresh_process, path)
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered, OLD if phase == "before" else NEW)
                self.assertEqual(await witness.async_load_last_intent(), recovered)
                self.assertEqual(await run_owned_job(read_in_fresh_process, path), recovered)

    async def test_prepare_failure_permits_zero_exchange(self):
        path, witness = await self.create_store("prepare-fault")
        engine = await self.engine(witness)
        with patch.object(sw, "_fsync_db", side_effect=OSError("private-sql-marker")):
            receipt = await engine.async_send("settings", {"110": True})
        self.assertEqual(receipt.reason, "intent_prepare_failed")
        self.assertEqual(receipt.outcome, "not_sent")
        self.assertFalse(receipt.attempted)
        self.assertTrue(engine.storage_fault)
        self.assertTrue(engine.needs_reconciliation)
        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.socket.writes, [])
        self.assertIsNotNone(await witness.async_load_last_intent())

    async def test_before_commit_prepare_failure_retains_previous_ack_and_zero_exchange(self):
        path, witness = await self.create_store("before-commit-fault")
        await witness.async_prepare_intent(OLD)
        engine = await self.engine(witness)
        async def verify():
            return True
        self.assertTrue(await engine.async_reconcile(OLD["operation_id"], verify,
            confirmed=True, session_id=engine.recovery_session_id))
        real_execute = sw._execute
        commits = []
        def execute(conn, sql, parameters=()):
            if sql == "COMMIT":
                commits.append(sql)
                raise OSError("private-sql-marker")
            return real_execute(conn, sql, parameters)
        with patch.object(sw, "_execute", execute):
            receipt = await engine.async_send("settings", {"110": True})
        self.assertEqual(commits, ["COMMIT"])
        self.assertEqual(receipt.reason, "intent_prepare_failed")
        self.assertFalse(receipt.attempted)
        self.assertTrue(engine.storage_fault)
        self.assertEqual(self.api.payloads, [])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.socket.writes, [])
        self.assertEqual(await witness.async_load_last_intent(), OLD)

    async def test_restart_nonce_reconcile_never_clears_or_replays_db(self):
        path, witness = await self.create_store("transport-restart")
        await witness.async_prepare_intent(OLD)
        before = Path(path).read_bytes()
        first = await self.engine(witness)
        old_nonce = first.recovery_session_id
        self.assertTrue(first.needs_reconciliation)
        self.assertEqual(first.pending_operation_id, OLD["operation_id"])
        fresh = sw.SQLiteIntentWitness(path, store_id=STORE, binding_id=BINDING)
        self.witnesses.append(fresh)
        second = await self.engine(fresh)
        self.assertNotEqual(second.recovery_session_id, old_nonce)
        self.assertTrue(second.needs_reconciliation)
        checks = []
        async def verify():
            checks.append("fresh")
            async with self.wrapper._api_lock:
                return True
        for opid, confirmed, session in ((OLD["operation_id"], True, old_nonce),
                (OLD["operation_id"], False, second.recovery_session_id),
                (NEW["operation_id"], True, second.recovery_session_id)):
            self.assertFalse(await second.async_reconcile(opid, verify, confirmed=confirmed, session_id=session))
        self.assertEqual(checks, [])
        async def not_verified():
            checks.append("false")
            return False
        self.assertFalse(await second.async_reconcile(OLD["operation_id"], not_verified,
            confirmed=True, session_id=second.recovery_session_id))
        self.assertEqual(Path(path).read_bytes(), before)
        blocked = await second.async_send("settings", {"110": True})
        self.assertEqual(blocked.reason, "blocked")
        self.assertTrue(await second.async_reconcile(OLD["operation_id"], verify,
            confirmed=True, session_id=second.recovery_session_id))
        self.assertEqual(checks, ["false", "fresh"])
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertEqual(await fresh.async_load_last_intent(), OLD)
        third = await self.engine(fresh)
        self.assertTrue(third.needs_reconciliation)
        self.assertNotEqual(third.recovery_session_id, second.recovery_session_id)
        self.assertEqual(third.pending_operation_id, OLD["operation_id"])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.socket.writes, [])
        self.assertEqual(Path(path).read_bytes(), before)

    async def test_synthetic_send_from_virgin_rearms_on_reconstruction_without_replay(self):
        path, witness = await self.create_store("sent-restart")
        first = await self.engine(witness)
        receipt = await first.async_send("settings", {"110": True})
        self.assertTrue(receipt.attempted)
        self.assertEqual(receipt.outcome, "unknown")
        self.assertTrue(await first.async_wait_idle(3))
        self.assertTrue(await witness.async_wait_idle(3))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(self.api.socket.writes), 1)
        before = Path(path).read_bytes()
        second = await self.engine(witness)
        self.assertTrue(second.needs_reconciliation)
        self.assertEqual(second.pending_operation_id, receipt.operation_id)
        self.assertNotEqual(second.recovery_session_id, first.recovery_session_id)
        self.assertEqual((await second.async_send("settings", {"110": True})).reason, "blocked")
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual(len(self.api.socket.writes), 1)
        self.assertEqual(Path(path).read_bytes(), before)
