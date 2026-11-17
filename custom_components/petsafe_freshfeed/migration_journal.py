"""Durable, private migration journal for one PetSafe config entry.

Layout: <config_root>/.storage/petsafe_freshfeed_migrations/<sha256(entry_id)>.json,
outside the retained-intent store namespace. The filename is the SHA-256 hex of
the UTF-8 entry ID, so no untrusted path text reaches the filesystem; the record
carries the original entry ID and loading compares it exactly.

Writes go to a private 0600 temporary file, are fsynced, atomically replaced
and then the parent directory is fsynced. An operation reports success only
after all of that settled; any earlier failure leaves the previous journal
untouched (no replacement on an uncertain write). A failed parent fsync after
the replace is reported as a fault: the new content may or may not be durable,
and the caller must reload rather than assume either. Loads are strict and
bounded to 16 KiB; nothing found is ever repaired or adopted. Deletion is
limited to this module's own stale temporary file and ``async_remove``, the
explicit removal of this entry's own journal when its config entry is deleted.
A finished cancellation (LEGACY_RESTORED) may be replaced by a fresh PREPARED
record (``async_renew``) with a strictly greater revision.

All filesystem work runs in a per-operation executor whose actual Future
decides settlement, serialized per journal and process-wide. Cancellation is
absorbed until the worker returns and then dominates. Durability is tested only
on the supported Linux/POSIX environment; no wider filesystem guarantee is made.
Same-UID hostile actors are outside the threat model.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import stat
import threading

from .migration_state import (
    CHECKPOINT_PHASES, IMMUTABLE_FIELDS, RECORD_FIELDS, TRANSITIONS, MigrationFault,
    MigrationPhase, MigrationRecord, valid_entry_id)

MAX_BYTES = 16 * 1024
_STORAGE = ".storage"
_DIRECTORY = "petsafe_freshfeed_migrations"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_IO_LOCK = threading.Lock()  # process-wide serialization of journal I/O


class _Reject(Exception):
    """Private worker control flow; never crosses the worker boundary."""


class _Missing(Exception):
    """Private: the journal (or its directory) does not exist."""


# Narrow seams so tests can inject faults without patching os globally.
def _euid():
    return os.geteuid()


def _fsync(fd):
    os.fsync(fd)


def _write(fd, data):
    return os.write(fd, data)


def _replace(src, dst, dir_fd):
    os.replace(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _open_file(name, flags, mode, dir_fd):
    return os.open(name, flags, mode, dir_fd=dir_fd)


def _submit(executor, function, *args):
    return executor.submit(function, *args)


def _require(condition):
    if not condition:
        raise _Reject()


def _valid_root(config_root):
    if type(config_root) is not str or not config_root.startswith("/"):
        return False
    if "\x00" in config_root:
        return False
    try:
        config_root.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    if config_root == "/":
        return True
    return all(part not in ("", ".", "..") for part in config_root.split("/")[1:])


def _encode(record):
    document = {}
    for name in RECORD_FIELDS:
        value = getattr(record, name)
        if name == "phase":
            value = value.value
        elif name == "retiring":
            value = list(value)
        elif name == "entity_disabled_by":
            value = [[entity_id, label] for entity_id, label in value]
        elif name == "setting_targets":
            if not value:
                continue  # omitted when empty: earlier journals stay canonical
            value = [[role, index] for role, index in value]
        document[name] = value
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        _require(key not in out)
        out[key] = value
    return out


def _no_constant(_name):
    raise _Reject()


def _decode(data, entry_id):
    """Strict canonical decode; raises on any deviation (caught by the worker)."""
    _require(len(data) <= MAX_BYTES)
    document = json.loads(data.decode("ascii"), object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constant)
    _require(type(document) is dict and set(document) in (
        set(RECORD_FIELDS), set(RECORD_FIELDS) - {"setting_targets"}))
    targets = document.get("setting_targets", [])
    _require(type(document["retiring"]) is list
             and type(document["entity_disabled_by"]) is list
             and all(type(item) is list for item in document["entity_disabled_by"])
             and type(targets) is list and all(type(item) is list for item in targets)
             and type(document["phase"]) is str)
    values = dict(document)
    values["retiring"] = tuple(document["retiring"])
    values["entity_disabled_by"] = tuple(tuple(item) for item in document["entity_disabled_by"])
    values["setting_targets"] = tuple(tuple(item) for item in targets)
    values["phase"] = MigrationPhase(document["phase"])
    record = MigrationRecord(**values)
    _require(record.entry_id == entry_id)
    _require(_encode(record) == data)
    return record


def _close_all(descriptors):
    clean = True
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except BaseException:
            clean = False
    descriptors.clear()
    return clean


def _owned_dir(info, private):
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == _euid())
    if private:
        _require(stat.S_IMODE(info.st_mode) == 0o700)


def _open_dir_at(parent_fd, name, descriptors, private):
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _owned_dir(before, private)
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    descriptors.append(fd)
    opened = os.fstat(fd)
    _owned_dir(opened, private)
    _require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino))
    return fd


def _open_journal_dir(config_root, descriptors, create):
    root_fd = os.open(config_root, _DIR_FLAGS)
    descriptors.append(root_fd)
    _owned_dir(os.fstat(root_fd), False)
    try:
        storage_fd = _open_dir_at(root_fd, _STORAGE, descriptors, False)
    except FileNotFoundError:
        raise _Missing() from None
    missing = False
    try:
        os.stat(_DIRECTORY, dir_fd=storage_fd, follow_symlinks=False)
    except FileNotFoundError:
        missing = True
    if missing:
        if not create:
            raise _Missing()
        os.mkdir(_DIRECTORY, 0o700, dir_fd=storage_fd)
        _fsync(storage_fd)
    return _open_dir_at(storage_fd, _DIRECTORY, descriptors, True)


def _read(dir_fd, name, entry_id):
    try:
        fd = _open_file(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0, dir_fd)
    except FileNotFoundError:
        raise _Missing() from None
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == _euid()
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                 and info.st_size <= MAX_BYTES)
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            _require(total <= MAX_BYTES)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    return _decode(data, entry_id)


def _load_worker(config_root, name, entry_id):
    """Returns the record, or None when absent. Raises on any fault."""
    with _IO_LOCK:
        descriptors = []
        try:
            try:
                dir_fd = _open_journal_dir(config_root, descriptors, False)
                return _read(dir_fd, name, entry_id)
            except _Missing:
                return None
        finally:
            _require(_close_all(descriptors))


def _check_advance(current, expected_phase, record):
    _require(current.phase is expected_phase)
    _require(record.phase in TRANSITIONS[current.phase])
    _require(record.revision == current.revision + 1)
    for name in IMMUTABLE_FIELDS:
        _require(getattr(record, name) == getattr(current, name))
    if record.phase not in CHECKPOINT_PHASES:
        _require(record.origin_marker == current.origin_marker)
    if current.phase is MigrationPhase.VERIFIED:
        # Set once on OWNED_CONFIGURED->VERIFIED (record invariant), then fixed.
        _require(record.verified_marker == current.verified_marker)


def _write_worker(config_root, name, entry_id, data, expected_phase, record):
    """Create (expected_phase None) or advance; returns True only after settlement."""
    with _IO_LOCK:
        descriptors = []
        tmp = name[:-len(".json")] + ".tmp"
        dir_fd = None
        replaced = False
        try:
            dir_fd = _open_journal_dir(config_root, descriptors, expected_phase is None)
            try:
                current = _read(dir_fd, name, entry_id)
            except _Missing:
                current = None
            if expected_phase is None:
                _require(current is None)
            elif expected_phase is _RENEW:
                _require(current is not None and current.phase is MigrationPhase.LEGACY_RESTORED
                         and record.revision == current.revision + 1)
            else:
                _require(current is not None)
                _check_advance(current, expected_phase, record)
            try:
                os.unlink(tmp, dir_fd=dir_fd)  # only this module's own stale temporary
            except FileNotFoundError:
                pass
            fd = _open_file(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                            | os.O_CLOEXEC, 0o600, dir_fd)
            try:
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600)
                view = memoryview(data)
                while view:
                    written = _write(fd, view)
                    _require(type(written) is int and 0 < written <= len(view))
                    view = view[written:]
                _fsync(fd)
            finally:
                os.close(fd)
            _replace(tmp, name, dir_fd)
            replaced = True
            _fsync(dir_fd)
            return True
        except BaseException:
            if dir_fd is not None and not replaced:
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except BaseException:
                    pass
            raise
        finally:
            _close_all(descriptors)


# Sentinel expected phase: replace a finished cancellation with a fresh record.
_RENEW = object()


def _remove_worker(config_root, name, entry_id):
    """Delete this entry's journal (and own temporary); absent is success."""
    with _IO_LOCK:
        descriptors = []
        try:
            try:
                dir_fd = _open_journal_dir(config_root, descriptors, False)
            except _Missing:
                return True
            tmp = name[:-len(".json")] + ".tmp"
            removed = False
            for target in (tmp, name):
                try:
                    info = os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                _require(stat.S_ISREG(info.st_mode) and info.st_uid == _euid())
                os.unlink(target, dir_fd=dir_fd)
                removed = True
            if removed:
                _fsync(dir_fd)
            return True
        finally:
            _close_all(descriptors)


def _cancelling():
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _drained(function, *args):
    """Run one worker in its own executor; return (cancelled, ok, value)."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="petsafe-migration")
    cancelled = False
    ok, value = False, None
    settled = loop.create_future()

    def wake():
        if not settled.done():
            settled.set_result(None)

    def notify(_actual):
        try:
            loop.call_soon_threadsafe(wake)
        except RuntimeError:
            pass

    try:
        actual = _submit(executor, function, *args)
    except Exception:
        actual = None
    if actual is not None:
        actual.add_done_callback(notify)
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                cancelled = True
        try:
            value = actual.result()
            ok = True
        except BaseException:
            ok = False
    executor.shutdown(wait=True)
    return cancelled or _cancelling(), ok, value


class MigrationJournal:
    """One entry's journal. Every fault is a fixed MigrationFault without context."""

    __slots__ = ("_root", "_entry_id", "_name", "_lock")

    def __init__(self, config_root: str, entry_id: str):
        valid = _valid_root(config_root) and valid_entry_id(entry_id)
        if valid:
            try:
                entry_id.encode("utf-8", errors="strict")
            except UnicodeError:
                valid = False
        if not valid:
            raise MigrationFault()
        self._root = config_root
        self._entry_id = entry_id
        self._name = hashlib.sha256(entry_id.encode("utf-8")).hexdigest() + ".json"
        self._lock = asyncio.Lock()

    def __repr__(self):
        return "<MigrationJournal>"

    async def _run(self, function, *args):
        if _cancelling():
            raise asyncio.CancelledError()
        async with self._lock:
            if _cancelling():
                raise asyncio.CancelledError()
            cancelled, ok, value = await _drained(function, *args)
        if cancelled:
            raise asyncio.CancelledError()
        # Raised outside every handler: fixed message, no private cause or context.
        if not ok:
            raise MigrationFault()
        return value

    async def async_load(self) -> MigrationRecord | None:
        return await self._run(_load_worker, self._root, self._name, self._entry_id)

    def _encoded(self, record):
        valid = type(record) is MigrationRecord and record.entry_id == self._entry_id
        data = _encode(record) if valid else b""
        if not valid or len(data) > MAX_BYTES:
            raise MigrationFault()
        return data

    async def async_create(self, record: MigrationRecord) -> None:
        data = self._encoded(record)
        if (record.phase is not MigrationPhase.PREPARED or record.revision != 1
                or record.verified_marker is not None):
            raise MigrationFault()
        await self._run(_write_worker, self._root, self._name, self._entry_id, data, None, record)

    async def async_renew(self, record: MigrationRecord) -> None:
        """Replace a LEGACY_RESTORED journal with a fresh PREPARED record."""
        data = self._encoded(record)
        if record.phase is not MigrationPhase.PREPARED or record.verified_marker is not None:
            raise MigrationFault()
        await self._run(_write_worker, self._root, self._name, self._entry_id, data,
                        _RENEW, record)

    async def async_remove(self) -> None:
        """Delete this entry's journal; only for a deleted config entry."""
        await self._run(_remove_worker, self._root, self._name, self._entry_id)

    async def async_advance(self, expected_phase: MigrationPhase,
                            record: MigrationRecord) -> None:
        data = self._encoded(record)
        if type(expected_phase) is not MigrationPhase:
            raise MigrationFault()
        await self._run(_write_worker, self._root, self._name, self._entry_id, data,
                        expected_phase, record)
