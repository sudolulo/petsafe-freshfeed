"""Existing-only retained-intent witness; isolated candidate, not HA admission.

Durability depends on honest SQLite/VFS/OS/device fsync and atomicity. Admission
assumes no other writer, aliases, namespace swaps, or external snapshot rollback.
Path pre/post checks detect some replacements; SQLite's pathname open does not
isolate against a hostile same-UID actor. A retained intent is not proof of any
physical execution. No reset, migration, replay, or implicit bootstrap exists.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
import weakref

_PYTHON_VERSION = tuple(sys.version_info[:3])
_SQLITE_VERSION = sqlite3.sqlite_version_info
_META_DDL = "CREATE TABLE witness_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), format_version INTEGER NOT NULL CHECK(format_version=1), store_id TEXT NOT NULL, binding_id TEXT NOT NULL) STRICT"
_INTENT_DDL = "CREATE TABLE witness_intent (singleton INTEGER PRIMARY KEY CHECK(singleton=1), operation_id TEXT, operation TEXT, state TEXT, CHECK((operation_id IS NULL AND operation IS NULL AND state IS NULL) OR (operation_id IS NOT NULL AND operation IS NOT NULL AND state IS NOT NULL))) STRICT"
_META_SELECT = "SELECT singleton, format_version, store_id, binding_id FROM witness_meta"
_INTENT_SELECT = "SELECT singleton, operation_id, operation, state FROM witness_intent"
_UPDATE = "UPDATE witness_intent SET operation_id=?, operation=?, state=? WHERE singleton=1"
_gates = weakref.WeakValueDictionary()
_unhealthy_gates = {}
_registry_lock = threading.Lock()


class _Rejected(Exception):
    """Internal fixed control flow; never crosses the worker boundary."""


class _CleanupUnconfirmed(Exception):
    """A statement close could not be positively confirmed."""


def _stat_at(parent_fd, name):
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _stat_parent(parent_fd):
    return os.fstat(parent_fd)


def _open_sqlite(path):
    return sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True,
                           autocommit=True, timeout=0, cached_statements=0)


def _close_cursor(cursor):
    cursor.close()


def _close_connection(conn):
    conn.close()


def _execute(conn, sql, parameters=()):
    cursor = conn.cursor()
    try:
        cursor.execute(sql, parameters)
        return (cursor.fetchall() if cursor.description is not None else [], cursor.rowcount)
    finally:
        uncertain = False
        try:
            _close_cursor(cursor)
        except BaseException:
            uncertain = True
        if uncertain:
            raise _CleanupUnconfirmed()


def _fsync_db(fd):
    os.fsync(fd)


def _fsync_parent(fd):
    os.fsync(fd)


def _submit(executor, function, *args):
    return executor.submit(function, *args)


def _require(condition):
    if not condition:
        raise _Rejected()


def _identity(info):
    return info.st_dev, info.st_ino


def _regular(info, maximum, minimum=0):
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
             and minimum <= info.st_size <= maximum)


def _open_parent(path, descriptors):
    # Walk from root by dirfd. No component may resolve through a symlink.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    descriptors.append(fd)
    for component in path.split("/")[1:-1]:
        before = _stat_at(fd, component)
        _require(stat.S_ISDIR(before.st_mode))
        fd = os.open(component, flags, dir_fd=fd)
        descriptors.append(fd)
        _require(_identity(_stat_parent(fd)) == _identity(before))
    info = _stat_parent(fd)
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o700)
    return fd, _identity(info)


def _optional_stat(fd, name):
    try:
        return _stat_at(fd, name)
    except FileNotFoundError:
        return None


def _sidecars(fd, name, provision=False):
    for suffix in ("-journal", "-wal", "-shm"):
        info = _optional_stat(fd, name + suffix)
        if info is not None:
            _require(not provision and suffix == "-journal")
            _regular(info, 2_097_152)


def _settings(conn, provision):
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    _require(conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE) is True)
    _require(conn.getconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA) is False)
    if provision:
        _execute(conn, "PRAGMA journal_mode=DELETE")
    _require(_execute(conn, "PRAGMA journal_mode")[0] == [("delete",)])
    _execute(conn, "PRAGMA synchronous=EXTRA")
    _require(_execute(conn, "PRAGMA synchronous")[0] == [(3,)])


def _validate_store(conn, store_id, binding_id):
    _require(_execute(conn, "PRAGMA application_id")[0] == [(0x50534657,)])
    _require(_execute(conn, "PRAGMA user_version")[0] == [(1,)])
    rows = _execute(conn, "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name")[0]
    _require(rows == [("table", "witness_intent", "witness_intent", _INTENT_DDL),
                      ("table", "witness_meta", "witness_meta", _META_DDL)])
    _require(_execute(conn, "PRAGMA table_xinfo(witness_meta)")[0] == [
        (0, "singleton", "INTEGER", 0, None, 1, 0),
        (1, "format_version", "INTEGER", 1, None, 0, 0),
        (2, "store_id", "TEXT", 1, None, 0, 0), (3, "binding_id", "TEXT", 1, None, 0, 0)])
    _require(_execute(conn, "PRAGMA table_xinfo(witness_intent)")[0] == [
        (0, "singleton", "INTEGER", 0, None, 1, 0), (1, "operation_id", "TEXT", 0, None, 0, 0),
        (2, "operation", "TEXT", 0, None, 0, 0), (3, "state", "TEXT", 0, None, 0, 0)])
    tables = _execute(conn, "PRAGMA table_list")[0]
    _require(sorted(tables) == sorted([
        ("main", "witness_meta", "table", 4, 0, 1),
        ("main", "witness_intent", "table", 4, 0, 1),
        ("main", "sqlite_schema", "table", 5, 0, 0),
        ("temp", "sqlite_temp_schema", "table", 5, 0, 0)]))
    meta = _execute(conn, _META_SELECT)[0]
    _require(len(meta) == 1 and len(meta[0]) == 4
             and tuple(type(value) for value in meta[0]) == (int, int, str, str)
             and meta[0] == (1, 1, store_id, binding_id))
    intent = _execute(conn, _INTENT_SELECT)[0]
    _require(len(intent) == 1 and len(intent[0]) == 4
             and type(intent[0][0]) is int and intent[0][0] == 1)
    _, operation_id, operation, state = intent[0]
    if operation_id is None and operation is None and state is None:
        return None
    return _copy_record(dict(operation_id=operation_id, operation=operation, state=state))


def _worker(path, store_id, binding_id, action, record):
    # No exception/traceback leaves this thread. Cleanup certainty is separate
    # from operation success: an ordinary cleaned fault permits fresh validation.
    descriptors = []
    conn = None
    success, cleanup, result = False, True, None
    try:
        _require(_PYTHON_VERSION >= (3, 13) and _SQLITE_VERSION >= (3, 37))
        parent, parent_id = _open_parent(path, descriptors)
        name = path.rsplit("/", 1)[1]
        provision = action == "provision"
        _sidecars(parent, name, provision)
        info = _optional_stat(parent, name)
        if provision:
            _require(info is None)
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            descriptors.append(fd)
            info = _stat_at(parent, name)
            _regular(info, 1_048_576, 0)
            _require(info.st_size == 0)
        else:
            _require(info is not None)
            _regular(info, 1_048_576, 1)
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=parent)
            descriptors.append(fd)
        db_id = _identity(info)
        _require(_identity(os.fstat(fd)) == db_id)
        _regular(os.fstat(fd), 1_048_576, 0 if provision else 1)
        if provision:
            _fsync_db(fd)
            _fsync_parent(parent)
        conn = _open_sqlite(path)
        _settings(conn, provision)
        _execute(conn, "BEGIN IMMEDIATE")
        if provision:
            _execute(conn, _META_DDL)
            _execute(conn, _INTENT_DDL)
            _execute(conn, "INSERT INTO witness_meta VALUES(1,1,?,?)", (store_id, binding_id))
            _execute(conn, "INSERT INTO witness_intent VALUES(1,NULL,NULL,NULL)")
            _execute(conn, "PRAGMA application_id=1347634775")
            _execute(conn, "PRAGMA user_version=1")
        result = _validate_store(conn, store_id, binding_id)
        if action == "prepare":
            _, count = _execute(conn, _UPDATE, (record["operation_id"], record["operation"], record["state"]))
            _require(type(count) is int and count == 1)
            result = None
        _execute(conn, "COMMIT")
        _require(conn.in_transaction is False)
        # Move ownership before calling close: a failed close is not retried or
        # upgraded to success by later readback, even if it actually closed.
        closing, conn = conn, None
        try:
            _close_connection(closing)
        except BaseException:
            cleanup = False
            raise
        _fsync_db(fd)
        _fsync_parent(parent)
        again, again_id = _open_parent(path, descriptors)
        _require(again_id == parent_id and _identity(_stat_parent(parent)) == parent_id)
        final = _stat_at(again, name)
        _regular(final, 1_048_576, 1)
        _regular(os.fstat(fd), 1_048_576, 1)
        _require(_identity(final) == db_id and _identity(os.fstat(fd)) == db_id)
        _sidecars(again, name)
        success = True
    except _CleanupUnconfirmed:
        cleanup = False
    except BaseException:
        success = False
    finally:
        if conn is not None:
            try:
                if conn.in_transaction:
                    _execute(conn, "ROLLBACK")
                _require(conn.in_transaction is False)
            except BaseException:
                cleanup = False
            try:
                _close_connection(conn)
            except BaseException:
                cleanup = False
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except BaseException:
                cleanup = False
    return success and cleanup, result if success and cleanup else None, cleanup


def _valid_id(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _validate_path(path):
    valid = (type(path) is str and path.startswith("/") and "\x00" not in path
             and all(part not in ("", ".", "..") for part in path.split("/")[1:]))
    if valid:
        try:
            path.encode("utf-8", errors="strict")
        except UnicodeError:
            valid = False
    if not valid:
        raise ValueError("invalid_witness_path")


def _copy_record(record):
    # Exact builtin key types are checked before any set/membership/hash/equality
    # operation, so caller-defined key hooks are never invoked by validation.
    if (type(record) is not dict or len(record) != 3
            or any(type(key) is not str for key in record)
            or set(record) != {"operation_id", "operation", "state"}
            or not _valid_id(record["operation_id"])
            or type(record["operation"]) is not str
            or record["operation"] not in {"feed", "defrost", "refilled", "settings", "schedule"}
            or type(record["state"]) is not str or record["state"] not in {"pending", "unknown"}):
        raise ValueError("invalid_witness_record")
    return dict(record)


class WitnessStorageError(RuntimeError):
    def __init__(self):
        super().__init__("sqlite_witness_storage_fault")


class _PathGate:
    def __init__(self, loop):
        self.loop = loop
        self.lock = asyncio.Lock()
        self.idle = asyncio.Event()
        self.idle.set()
        self.pending = 0
        self.healthy = True
        self.actual = None


def _latch(path, gate):
    with _registry_lock:
        gate.healthy = False
        _unhealthy_gates[path] = gate


def _cancelling():
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class SQLiteIntentWitness:
    """One shared owning-loop gate per eligible path, with operation-local I/O.

    A False idle result forbids unload. Hung kernel I/O cannot be forcibly
    drained; callers must keep the owning loop alive until actual settlement.
    """

    def __init__(self, path: str, *, store_id: str, binding_id: str):
        _validate_path(path)
        if not _valid_id(store_id) or not _valid_id(binding_id):
            raise ValueError("invalid_witness_identity")
        self._path, self._store_id, self._binding_id = path, store_id, binding_id
        loop = asyncio.get_running_loop()
        with _registry_lock:
            gate = _gates.get(path)
            if gate is None:
                gate = _PathGate(loop)
                _gates[path] = gate
            elif gate.loop is not loop:
                raise ValueError("invalid_witness_loop")
        self._gate = gate

    async def _run(self, action, record=None):
        gate = self._gate
        if asyncio.get_running_loop() is not gate.loop:
            raise ValueError("invalid_witness_loop")
        gate.pending += 1
        gate.idle.clear()
        acquired = transferred = cancelled = False
        executor = None
        outcome = (False, None, True)
        try:
            if _cancelling():
                raise asyncio.CancelledError()
            await gate.lock.acquire()
            acquired = True
            if _cancelling():
                raise asyncio.CancelledError()
            if gate.healthy:
                completed = gate.loop.create_future()
                path = self._path
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="petsafe-witness")

                def finish(future):
                    # Runs only on the owning loop, after the ACTUAL producer
                    # settled. Joining here never joins the current worker.
                    status = (False, None, False)
                    try:
                        status = future.result()
                    except BaseException:
                        status = (False, None, False)
                    try:
                        executor.shutdown(wait=True)
                    except BaseException:
                        status = (False, None, False)
                    if not status[2]:
                        _latch(path, gate)
                    gate.actual = None
                    gate.lock.release()
                    gate.pending -= 1
                    if gate.pending == 0:
                        gate.idle.set()
                    completed.set_result(status)

                def notify(future):
                    try:
                        gate.loop.call_soon_threadsafe(finish, future)
                    except RuntimeError:
                        # Forced loop closure cannot certify idle or release its
                        # asyncio lock. Still stop the owned executor, without
                        # joining this worker from its own completion callback.
                        _latch(path, gate)
                        executor.shutdown(wait=False)

                # No await between final cancellation check and actual submit.
                if _cancelling():
                    raise asyncio.CancelledError()
                actual = _submit(executor, _worker, self._path, self._store_id,
                                 self._binding_id, action, record)
                gate.actual = actual
                transferred = True
                actual.add_done_callback(notify)
                while not completed.done():
                    try:
                        await asyncio.shield(completed)
                    except asyncio.CancelledError:
                        cancelled = True
                outcome = completed.result()
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # Submission/setup faults have no raw public cause or context.
            outcome = (False, None, True)
        finally:
            if not transferred:
                if executor is not None:
                    try:
                        executor.shutdown(wait=True)
                    except BaseException:
                        _latch(self._path, gate)
                        outcome = (False, None, False)
                if acquired:
                    gate.lock.release()
                gate.pending -= 1
                if gate.pending == 0:
                    gate.idle.set()
        if cancelled or _cancelling():
            raise asyncio.CancelledError()
        if not outcome[0]:
            raise WitnessStorageError()
        return outcome[1]

    async def async_load_last_intent(self) -> dict | None:
        return await self._run("load")

    async def async_prepare_intent(self, record: dict) -> None:
        record = _copy_record(record)
        return await self._run("prepare", record)

    @classmethod
    async def async_provision(cls, path: str, *, store_id: str, binding_id: str) -> "SQLiteIntentWitness":
        witness = cls(path, store_id=store_id, binding_id=binding_id)
        await witness._run("provision")
        return witness

    async def async_wait_idle(self, timeout: float = 5.0) -> bool:
        # Accepted domain: exact builtin int/float (not bool), finite, nonnegative
        # and representable as a finite float. Normalize before any fast path so
        # idle and pending callers see the same fixed rejection.
        if type(timeout) not in (int, float):
            raise ValueError("invalid_witness_timeout")
        normalized = None
        try:
            normalized = float(timeout)
        except OverflowError:
            pass
        # Raised outside the except block: no private OverflowError context.
        if normalized is None or not math.isfinite(normalized) or normalized < 0:
            raise ValueError("invalid_witness_timeout")
        gate = self._gate
        if asyncio.get_running_loop() is not gate.loop:
            raise ValueError("invalid_witness_loop")
        if _cancelling():
            raise asyncio.CancelledError()
        if not gate.healthy:
            return False
        if gate.pending == 0:
            return True
        try:
            # Directly time this task's Event.wait: no detached waiter Task and
            # no cancellation path from the idle caller to any producer.
            async with asyncio.timeout(normalized):
                await gate.idle.wait()
        except TimeoutError:
            return False
        return gate.healthy and gate.pending == 0
