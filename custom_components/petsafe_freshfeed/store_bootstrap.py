"""Explicit, one-time provisioning of the retained-intent store directory tree.

open_store only validates arguments and constructs the existing
SQLiteIntentWitness: zero filesystem I/O and never any creation, so a missing,
zero, corrupt, wrong-ID or prior-failed store faults on load instead of becoming
an authoritative virgin. async_provision_store is the only creator and is meant
to be called once, from an explicit configuration step, with a caller-supplied
store_id (never generated here) and the source-owned HA Device Registry leaf ID
as binding_id.

Layout: <config_root>/petsafe_freshfeed/<binding_id>/intent.sqlite. The config
root must already exist and be owned by the current euid; it is never created or
modified. The namespace and binding directories are private 0700. The binding
directory is created exclusively and is the reservation: any pre-existing
binding path is refused, never adopted, repaired, overwritten or deleted. Any
failure leaves whatever partial tree exists untouched for human recovery.

Every filesystem step runs in a per-operation executor whose actual concurrent
Future decides settlement; cancellation is absorbed until that real work has
drained and the executor joined, and then dominates the outcome. Same-UID
hostile actors are outside the threat model, as for the witness itself.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import re
import stat

from .sqlite_witness import SQLiteIntentWitness

_NAMESPACE = "petsafe_freshfeed"
_DATABASE = "intent.sqlite"
_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class StoreBootstrapError(RuntimeError):
    """Fixed public message: store_bootstrap_fault."""

    def __init__(self):
        super().__init__("store_bootstrap_fault")


class StoreDescriptor:
    """Opaque, immutable result of a successful provision.

    path is the absolute database path; repr deliberately omits it.
    """

    __slots__ = ("path", "store_id", "binding_id")

    def __init__(self, path, store_id, binding_id):
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "store_id", store_id)
        object.__setattr__(self, "binding_id", binding_id)

    def __setattr__(self, name, value):
        raise AttributeError("StoreDescriptor is read-only")

    def __delattr__(self, name):
        raise AttributeError("StoreDescriptor is read-only")

    def __repr__(self):
        return "<StoreDescriptor>"

    __str__ = __repr__


class _Reject(Exception):
    """Private worker control flow; never crosses the worker boundary."""


# Narrow seams so tests can inject faults without patching os globally.
def _euid():
    return os.geteuid()


def _mkdir(name, fd):
    os.mkdir(name, 0o700, dir_fd=fd)


def _fsync(fd):
    os.fsync(fd)


def _stat_at(fd, name):
    return os.stat(name, dir_fd=fd, follow_symlinks=False)


def _fstat(fd):
    return os.fstat(fd)


def _submit(executor, function, *args):
    return executor.submit(function, *args)


def _require(condition):
    if not condition:
        raise _Reject()


def _identity(info):
    return info.st_dev, info.st_ino


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


def _validate(config_root, binding_id, store_id):
    valid = (_valid_root(config_root)
             and type(binding_id) is str and _ID.fullmatch(binding_id) is not None
             and type(store_id) is str and _ID.fullmatch(store_id) is not None)
    # Raised outside any handler: no private UnicodeError context is attached.
    if not valid:
        raise ValueError("invalid_store_argument")
    base = "" if config_root == "/" else config_root
    return f"{base}/{_NAMESPACE}/{binding_id}/{_DATABASE}"


def _components(config_root):
    return [] if config_root == "/" else config_root.split("/")[1:]


def _private_dir(info):
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == _euid()
             and stat.S_IMODE(info.st_mode) == 0o700)


def _open_at(parent_fd, name, descriptors):
    """No-follow open of a directory, checking named versus opened identity."""
    before = _stat_at(parent_fd, name)
    _require(stat.S_ISDIR(before.st_mode))
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    descriptors.append(fd)
    opened = _fstat(fd)
    _require(_identity(opened) == _identity(before))
    return fd, before, opened


def _open_root(config_root, descriptors):
    """Walk from / by dirfd; returns the owned config root without changing it."""
    fd = os.open("/", _DIR_FLAGS)
    descriptors.append(fd)
    named = opened = _fstat(fd)
    for component in _components(config_root):
        fd, named, opened = _open_at(fd, component, descriptors)
    # Owned real directory. 0755 is fine; group/other write is refused because
    # it would let another account rename the namespace out from under us.
    for info in (named, opened):
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == _euid()
                 and stat.S_IMODE(info.st_mode) & 0o022 == 0)
    return fd, _identity(opened)


def _open_private(parent_fd, name, descriptors):
    fd, named, opened = _open_at(parent_fd, name, descriptors)
    _private_dir(named)
    _private_dir(opened)
    return fd, _identity(opened)


def _tree(config_root, binding_id, descriptors):
    root_fd, root_id = _open_root(config_root, descriptors)
    ns_fd, ns_id = _open_private(root_fd, _NAMESPACE, descriptors)
    binding_fd, binding_dir_id = _open_private(ns_fd, binding_id, descriptors)
    return binding_fd, (root_id, ns_id, binding_dir_id)


def _close_all(descriptors):
    clean = True
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except BaseException:
            clean = False
    descriptors.clear()
    return clean


def _prepare(config_root, binding_id):
    """Worker: create namespace if missing and the binding directory exclusively.

    Returns the (root, namespace, binding) identities, or None on any failure.
    Never raises, never retries, never removes or repairs anything it finds.
    """
    descriptors = []
    result = None
    try:
        root_fd, root_id = _open_root(config_root, descriptors)
        missing = False
        try:
            _stat_at(root_fd, _NAMESPACE)
        except FileNotFoundError:
            missing = True
        if missing:
            try:
                _mkdir(_NAMESPACE, root_fd)
            except FileExistsError:
                # A concurrent creator won; it is fully revalidated below.
                pass
            _fsync(root_fd)
        ns_fd, ns_id = _open_private(root_fd, _NAMESPACE, descriptors)
        # The exclusive mkdir is the reservation. EEXIST of any kind (directory,
        # file, symlink, prior failure, concurrent winner) is a plain refusal.
        _mkdir(binding_id, ns_fd)
        _fsync(ns_fd)
        binding_fd, binding_dir_id = _open_private(ns_fd, binding_id, descriptors)
        _require(os.listdir(binding_fd) == [])
        ids = (root_id, ns_id, binding_dir_id)
        # Re-walk by name from /: the tree we created must still be the one there.
        again = []
        try:
            _, current = _tree(config_root, binding_id, again)
        finally:
            _require(_close_all(again))
        _require(current == ids)
        result = ids
    except BaseException:
        result = None
    finally:
        if not _close_all(descriptors):
            result = None
    return result


def _postcheck(config_root, binding_id, ids):
    """Worker: the provisioned tree is unchanged and holds only the database."""
    descriptors = []
    result = False
    try:
        binding_fd, current = _tree(config_root, binding_id, descriptors)
        _require(current == ids)
        _require(os.listdir(binding_fd) == [_DATABASE])
        info = _stat_at(binding_fd, _DATABASE)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == _euid()
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                 and info.st_size > 0)
        result = True
    except BaseException:
        result = False
    finally:
        if not _close_all(descriptors):
            result = False
    return result


def _cancelling():
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _drained(function, *args):
    """Run one worker in its own executor; return (cancelled, ok, value).

    Settlement is decided by the actual concurrent Future. Cancellation is
    recorded but never abandons the worker; the executor is joined before return.
    """
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="petsafe-bootstrap")
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
            # Loop already closed: nothing can await settlement any more.
            pass

    try:
        # No await between the caller's cancellation check and this submit.
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
    # The worker has returned; this joins only an exiting thread.
    executor.shutdown(wait=True)
    return cancelled or _cancelling(), ok, value


def open_store(config_root: str, *, binding_id: str, store_id: str) -> SQLiteIntentWitness:
    """Existing-only reopen with persisted identity. Zero filesystem I/O."""
    path = _validate(config_root, binding_id, store_id)
    return SQLiteIntentWitness(path, store_id=store_id, binding_id=binding_id)


async def async_provision_store(config_root: str, *, binding_id: str,
                                store_id: str) -> StoreDescriptor:
    path = _validate(config_root, binding_id, store_id)
    if _cancelling():
        raise asyncio.CancelledError()

    cancelled, ok, ids = await _drained(_prepare, config_root, binding_id)
    if cancelled:
        raise asyncio.CancelledError()
    failed = not ok or ids is None

    if not failed:
        witness = None
        try:
            witness = await SQLiteIntentWitness.async_provision(
                path, store_id=store_id, binding_id=binding_id)
            failed = not await witness.async_wait_idle(5.0)
        except asyncio.CancelledError:
            # The witness re-raises cancellation only after its worker drained.
            cancelled = True
        except Exception:
            failed = True
        if cancelled or _cancelling():
            raise asyncio.CancelledError()

    if not failed:
        cancelled, ok, verified = await _drained(_postcheck, config_root, binding_id, ids)
        if cancelled:
            raise asyncio.CancelledError()
        failed = not ok or verified is not True

    # Raised outside every handler: fixed message, no private cause or context.
    if failed:
        raise StoreBootstrapError()
    return StoreDescriptor(path, store_id, binding_id)
