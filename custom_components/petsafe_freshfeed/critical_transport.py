"""Single-attempt transport using an already authenticated client.

``async_create_owned`` admits a PetSafe-owned ``ClientOwner`` with public ``api``,
``lock`` and ``is_current()``; the check receives the real owner, and a
replaced api/lock or a non-current owner refuses every later send.

This module authorizes no datapoint semantics. A future adapter must prove the
actual loaded client, its authenticated connection and its shared lock. Nothing
here connects, refreshes, queues a command, or interprets physical execution.

Restoration failures or unexpected thread failures permanently fault THIS
transport instance. Worker completion and physical-operation verification do
not attest to shared-client integrity. Recovery requires explicit shared-client
repair, fresh compatibility-gated transport construction, and then operation
reconciliation; that repair/adapter gate is not implemented here. The fault
latch is in-memory only: restart retains the metadata-only pending/unknown
barrier, not the fault flag. A future adapter must gate the loaded client.

IntentWitness has a trusted required durability contract documented on its
Protocol below. This module has no durable storage integration claim; actual
adapter, storage identity and filesystem conformance have not been proven.

Unload MUST await async_wait_idle and refuse unloading on False. Normal asyncio
shutdown cancels tasks and waits: the worker deliberately drains through those
cancellations. Forcibly closing an event loop without draining cannot release
its asyncio lock safely. In that case the thread still restores the client and
shuts down its executor; the retained durable intent and lock fail closed,
rather than inventing safe unload completion. No post-exchange write is needed.

Optional hooks (both default to None, preserving the behaviour above):
``dispatch_callback(operation_id)`` runs on the owning loop, in one synchronous
turn, at the first guarded ``sendall`` after encoding and the final pre-write
checks; the worker waits for it (bounded, cancellation-aware) while the shared
API lock stays held. Only a returned mark for this operation grants the single
write; failure, refusal or an abandoned handoff writes nothing.
``report_callback(dps)`` receives the actual exchange response decoded by the
client's own ``_decode_payload``, from the worker, only after a granted mark.
A frame the device sent before receiving the command but that is read as the
exchange response cannot be told apart at this layer (v3.5 sequence numbers
are global); the callback receives an uncorrelated observation, not proof of
acknowledgement. Decoder or callback failure only changes the fixed reason: the
outcome stays uncertain.
"""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import functools
import json
import logging
import math
import re
import threading
from time import monotonic
from typing import Awaitable, Callable, Literal, Protocol
from uuid import uuid4
from weakref import WeakKeyDictionary


# Public TinyTuya command_types.CONTROL. Importing TinyTuya is not required.
CONTROL = 7
_OPERATIONS = frozenset({"feed", "cancel", "defrost", "refilled", "settings", "schedule"})
_DPS = frozenset({"1", "3", "110", "111", "120", "125", "140", "142"})
# Owning-loop-only fencing for coordinators borrowing the same API lock.
# Weak keys and opaque tokens retain neither engines nor locks; no process fence.
_OWNERS = WeakKeyDictionary()


class CompatibilityError(RuntimeError):
    """Fixed public error, without client values or callback exception text."""

    def __init__(self):
        super().__init__("critical_transport_incompatible")


class IntentWitness(Protocol):
    """Trusted adapter contract; core cannot enforce arbitrary backend I/O atomicity.

    ``async_load_last_intent`` returns authoritative durable metadata, not an
    optimistic cache. Only explicitly provisioned virgin storage returns None.
    Missing, corrupt or wrong-identity storage must raise, never auto-create,
    reset or fall back to an older record hiding a potentially dispatched intent.

    ``async_prepare_intent`` returns only after positive durability of the entire
    record. Failure may leave an old or new conservative witness or storage fault,
    never turn an acknowledged intent into virgin/None. There is no clear method.
    Loads and writes must serialize with all old producers across constructions;
    a cancelled asyncio wrapper is not completion of its actual I/O future.
    The adapter owns/drains actual I/O and re-raises cancellation after settlement;
    it must not uncancel, erase intent and return normally. Task cancellation
    counts defend ordinary swallowed cancellation, not arbitrary trusted code
    violating this rule. Startup cancellation must propagate as well.

    Synthetic models prove core flow only, not storage atomicity, fsync, VFS,
    power loss, authoritative identity or real-adapter serialization. Actual
    adapter conformance remains a mandatory future admission/deployment gate.
    """

    async def async_load_last_intent(self) -> dict | None: ...

    async def async_prepare_intent(self, record: dict) -> None: ...


@dataclass(frozen=True)
class SendReceipt:
    operation_id: str
    outcome: Literal["not_sent", "unknown"]
    attempted: bool
    client_returned: bool
    reason: str


class _Rejected(Exception):
    """Internal fixed-reason control flow; never carries client exceptions."""


@dataclass
class _Job:
    operation_id: str
    operation: str
    expires: float
    caller: asyncio.Task | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    dispatched: bool = False
    attempted: bool = False
    client_returned: bool = False
    reason: str = "not_dispatched"
    future: Future | None = None
    # Opaque mark returned by the owning-loop dispatch hook at the actual send
    # boundary; set only when that loop turn granted the single write.
    mark: object | None = None

    def expired(self):
        return self.stop.is_set() or monotonic() >= self.expires

    def receipt(self, reason=None):
        return SendReceipt(self.operation_id,
                           "unknown" if self.dispatched else "not_sent",
                           self.attempted, self.client_returned,
                           self.reason if reason is None else reason)


def _positive_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _valid_record(record):
    return (
        type(record) is dict
        and set(record) == {"operation_id", "operation", "state"}
        and type(record["operation_id"]) is str
        and re.fullmatch(r"[0-9a-f]{32}", record["operation_id"]) is not None
        and type(record["operation"]) is str and record["operation"] in _OPERATIONS
        and type(record["state"]) is str and record["state"] in {"pending", "unknown"}
    )


def _raise_if_cancelling():
    # Owning-loop only: callbacks may swallow cancellation or replace it with an
    # ordinary exception. Preserve intent without exposing that private error.
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError() from None


def _valid_dps(dps):
    if type(dps) is not dict or not 1 <= len(dps) <= 8:
        return False
    for key, value in dps.items():
        if type(key) is not str or key not in _DPS:
            return False
        if type(value) is bool:
            continue
        if type(value) is int:
            if not -(2**31) <= value < 2**31:
                return False
        elif type(value) is str:
            try:
                if len(value.encode("utf-8")) > 4096:
                    return False
            except UnicodeError:
                return False
        else:
            return False
    return len(json.dumps(dps, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 8192


def _socket_open(sock):
    if sock is None or getattr(sock, "_closed", False) or getattr(sock, "closed", False):
        return False
    fileno = getattr(sock, "fileno", None)
    return not callable(fileno) or fileno() >= 0


def _logger_safe(api, decoder=False):
    # Inspect the actual bound callables, not a guessed logger name or package
    # version. Unknown callable implementations fail closed. Identity and nested
    # dependency safety are additional obligations of the compatibility adapter.
    # With a response hook, the client's own decoder is a required callsite.
    required = ("generate_payload", "_send_receive") + (("_decode_payload",) if decoder else ())
    optional = ("_encode_message", "_receive", "_recv_all", "_get_socket",
                "_check_socket_close", "_get_retcode", "set_socketRetryLimit")
    for name in required + optional:
        method = getattr(api, name, None)
        if method is None and name in optional:
            continue
        function = getattr(method, "__func__", method)
        namespace = getattr(function, "__globals__", None)
        if namespace is None:
            return False
        loggers = [value for value in namespace.values() if isinstance(value, logging.Logger)]
        if name in required and not loggers:
            return False
        if any(logger.isEnabledFor(logging.DEBUG) for logger in loggers):
            return False
    return True


class _SocketGuard:
    """Only the socket surface the bounded exchange needs; no connect escape."""

    def __init__(self, sock, job, deadline, receive_limit, timeout, before_write=None):
        self._socket = sock
        self._before_write = before_write
        self._job = job
        self._deadline = deadline
        self._receive_limit = receive_limit
        self._received = 0
        self._send_calls = 0
        self._timeout = timeout
        self.closed = False
        self.reason = None

    def _reject(self, reason):
        if self.reason is None:
            self.reason = reason
        raise _Rejected(reason)

    def _remaining(self):
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            self._reject("socket_deadline")
        if self.closed:
            self._reject("socket_closed")
        return remaining

    def _clamp(self):
        remaining = self._remaining()
        timeout = remaining if self._timeout is None else min(self._timeout, remaining)
        self._socket.settimeout(timeout)

    def sendall(self, data):
        self._send_calls += 1
        if self._send_calls != 1:
            self._reject("send_limit")
        if type(data) not in (bytes, bytearray) or len(data) > 8192:
            self._reject("send_limit")
        if self._job.expired():
            self._reject("expired_before_write")
        self._clamp()
        if self._before_write is not None:
            # Send linearization point: after encoding and the final pre-write
            # checks, before the original socket is invoked. A failed or
            # refused owning-loop handoff forbids the write entirely.
            if not self._before_write():
                self._reject("dispatch_handoff_failed")
            self._clamp()
        # A partial write followed by an exception is still an attempted write.
        self._job.attempted = True
        self._socket.sendall(data)
        self._remaining()

    def recv(self, size):
        remaining_bytes = self._receive_limit - self._received
        if remaining_bytes <= 0:
            self._reject("receive_limit")
        if type(size) is not int or size <= 0:
            self._reject("receive_limit")
        size = min(size, remaining_bytes)
        self._clamp()
        data = self._socket.recv(size)
        if type(data) is not bytes or len(data) > size:
            self._reject("receive_limit")
        self._received += len(data)
        self._remaining()
        return data

    def gettimeout(self):
        return self._socket.gettimeout()

    def settimeout(self, value):
        if value is not None and (type(value) not in (int, float)
                                  or not math.isfinite(value) or value < 0):
            self._reject("transport_integrity")
        self._timeout = value
        self._clamp()

    def close(self):
        self.closed = True
        self._socket.close()


def underlying_socket(sock):
    """The connection a transport socket guard stands in for, by identity.

    Installing the guard for one bounded exchange is not a reconnect. A closed
    guard means the connection is gone.
    """
    if isinstance(sock, _SocketGuard):
        return None if sock.closed else sock._socket
    return sock


class _OwnedBinding:
    """A PetSafe-owned client owner: public ``api``/``lock``/``is_current``."""

    __slots__ = ("target",)

    def __init__(self, owner):
        self.target = owner

    @property
    def api(self):
        return getattr(self.target, "api", None)

    @property
    def lock(self):
        return getattr(self.target, "lock", None)

    def current(self):
        return self.target.is_current() is True


class CriticalTransport:
    """Fail-closed admission, durable uncertainty, and actual-thread ownership."""

    @classmethod
    async def async_create_owned(cls, owner, witness: IntentWitness, *,
                                 compatibility_check: Callable[[object, object, asyncio.Lock], bool],
                                 socket_deadline: float = 3.0,
                                 max_receive_bytes: int = 65536,
                                 dispatch_callback: Callable[[str], object] | None = None,
                                 report_callback: Callable[[dict], None] | None = None):
        """PetSafe-owned client; the check receives the real owner, which must
        also report ``is_current() is True`` at every compatibility gate."""
        return await cls._async_create(
            _OwnedBinding(owner), witness, compatibility_check=compatibility_check,
            socket_deadline=socket_deadline, max_receive_bytes=max_receive_bytes,
            dispatch_callback=dispatch_callback, report_callback=report_callback)

    @classmethod
    async def _async_create(cls, binding, witness, *, compatibility_check, socket_deadline,
                            max_receive_bytes, dispatch_callback, report_callback):
        if (not _positive_number(socket_deadline) or socket_deadline > 3.0
                or type(max_receive_bytes) is not int or not 1 <= max_receive_bytes <= 65536):
            raise ValueError("invalid_transport_bounds")
        if ((dispatch_callback is not None and not callable(dispatch_callback))
                or (report_callback is not None and not callable(report_callback))):
            raise ValueError("invalid_transport_hooks")
        self = cls()
        # Owning loop: dispatch marks are captured only in one of its turns.
        self._loop = asyncio.get_running_loop()
        self._dispatch = dispatch_callback
        self._report = report_callback
        self._binding = binding
        self._witness = witness
        self._check = compatibility_check
        try:
            self._api, self._lock = binding.api, binding.lock
        except Exception:
            raise CompatibilityError() from None
        self._socket_deadline = socket_deadline
        self._receive_limit = max_receive_bytes
        self._pending = None
        self._blocked = False
        self._integrity_fault = False
        self._storage_fault = False
        self._recovery_session_id = uuid4().hex
        self._busy = False
        self._worker = None
        self._job = None
        self._generation = object()
        if not isinstance(self._lock, asyncio.Lock) or not callable(self._check):
            raise CompatibilityError()
        async with self._lock:
            self._compatible()
            _OWNERS[self._lock] = self._generation
            try:
                record = await witness.async_load_last_intent()
            except Exception:
                _raise_if_cancelling()
                self._blocked = self._storage_fault = True
                return self
            _raise_if_cancelling()
            if record is not None:
                self._blocked = True
                if _valid_record(record):
                    self._pending = {**record, "state": "unknown"}
                else:
                    self._storage_fault = True
        return self

    def _is_owner(self):
        return _OWNERS.get(self._lock) is self._generation

    def _compatible(self):
        try:
            binding = self._binding
            if (self._api is None or binding.api is not self._api
                    or binding.lock is not self._lock or not binding.current()):
                raise CompatibilityError()
            result = self._check(binding.target, self._api, self._lock)
            if (result is not True or binding.api is not self._api
                    or binding.lock is not self._lock or not binding.current()):
                raise CompatibilityError()
        except Exception:
            raise CompatibilityError() from None

    @property
    def needs_reconciliation(self):
        return self._blocked

    @property
    def pending_operation_id(self):
        return None if self._pending is None else self._pending["operation_id"]

    @property
    def storage_fault(self) -> bool:
        return self._storage_fault

    @property
    def recovery_session_id(self) -> str:
        """Nonsecret, unpersisted nonce binding confirmation to this construction."""
        return self._recovery_session_id

    async def async_send(self, operation: str, dps: dict[str, bool | int | str], *,
                         timeout: float = 5.0) -> SendReceipt:
        if not self._is_owner() or self._busy or self._blocked or self._integrity_fault or self._storage_fault:
            return SendReceipt(self.pending_operation_id or "", "not_sent", False, False, "blocked")
        if type(operation) is not str or operation not in _OPERATIONS or not _valid_dps(dps):
            return SendReceipt("", "not_sent", False, False, "invalid_request")
        if not _positive_number(timeout):
            return SendReceipt("", "not_sent", False, False, "invalid_timeout")
        # No await before admission. Immutable scalar values make this shallow
        # copy sufficient to isolate later caller mutations of the dictionary.
        private_dps = dict(dps)
        job = _Job(uuid4().hex, operation, monotonic() + timeout, asyncio.current_task())
        self._busy = self._blocked = True
        self._pending = {"operation_id": job.operation_id, "operation": operation, "state": "pending"}
        self._job = job
        worker = self._worker = asyncio.create_task(self._run(job, private_dps))
        try:
            return await asyncio.wait_for(asyncio.shield(worker), timeout)
        except TimeoutError:
            job.stop.set()
            return job.receipt("caller_timeout")
        except asyncio.CancelledError:
            job.stop.set()
            # Cancellation before the coroutine's first step bypasses _run's
            # cleanup entirely. No preparation or I/O producer exists in that
            # case. Never let an older caller release a newer job's admission.
            if worker.cancelled() and not job.started and self._job is job:
                self._pending = None
                self._blocked = self._busy = False
            raise

    @staticmethod
    def _checkpoint(job):
        # Task.cancel() requests cancellation before the caller's except block
        # gets a turn. Observe that request while still on the owning loop.
        if job.caller is not None and job.caller.cancelling():
            job.stop.set()
        worker = asyncio.current_task()
        if worker is not None and worker.cancelling():
            job.stop.set()
        if job.expired():
            raise _Rejected("expired_before_dispatch")

    async def _drain(self, job):
        # Cancelling an asyncio wrapper does NOT stop its underlying thread.
        # The concurrent future, retained on the job, alone decides completion.
        # Bridge completion ONLY, not its exception: even unexpected thread
        # failures must not become asyncio's unhandled private-exception logs.
        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def mark_completed():
            if not completed.done():
                completed.set_result(None)

        def notify(future):
            try:
                loop.call_soon_threadsafe(mark_completed)
            except RuntimeError:
                # A forcibly closed loop cannot safely release this lock.
                # The retained durable intent remains the restart gate.
                job.stop.set()

        job.future.add_done_callback(notify)
        while not job.future.done():
            try:
                await asyncio.shield(completed)
            except asyncio.CancelledError:
                job.stop.set()
        try:
            integrity_fault = job.future.result()
        except BaseException:
            job.reason = "transport_integrity"
            integrity_fault = True
        # Consume the completed thread's result on the owning loop, before
        # unlocking/finalizing. Storage faults cannot erase this separate latch.
        if integrity_fault:
            self._integrity_fault = True

    async def _run(self, job, dps):
        job.started = True
        acquired = False
        executor = None
        prepared = False
        uncertain = False
        try:
            self._checkpoint(job)
            await self._lock.acquire()
            acquired = True
            self._checkpoint(job)
            if not self._is_owner():
                raise _Rejected("obsolete")
            try:
                await self._witness.async_prepare_intent(dict(self._pending))
            except (Exception, asyncio.CancelledError):
                # Neither operation verification nor a later local release can
                # attest to storage health after an unacknowledged preparation.
                self._storage_fault = True
                raise
            prepared = True
            self._checkpoint(job)
            self._compatible()
            if getattr(self._api, "parent", None) is not None or getattr(self._api, "children", {}):
                raise _Rejected("gateway_unsupported")
            if not _socket_open(getattr(self._api, "socket", None)):
                raise _Rejected("no_socket")
            if not _logger_safe(self._api, self._report is not None):
                raise _Rejected("unsafe_logging")
            self._checkpoint(job)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="petsafe-critical")
            self._checkpoint(job)
            # NO await between the final deadline/cancellation check and submit.
            job.future = executor.submit(self._exchange, job, dps)
            job.dispatched = True
            # Even a forcibly closed loop must not strand an owned executor.
            job.future.add_done_callback(lambda future: executor.shutdown(wait=False))
            await self._drain(job)
        except asyncio.CancelledError:
            job.stop.set()
            uncertain = True
            job.reason = "cancelled_before_dispatch"
        except CompatibilityError:
            job.reason = "incompatible"
        except _Rejected as error:
            job.reason = error.args[0]
        except Exception:
            uncertain = True
            job.reason = "intent_prepare_failed" if not prepared else "transport_error"
        finally:
            # GeneratorExit/forced loop finalization must NEVER unlock a live
            # thread. The done callback still shuts down its owned executor.
            if job.future is None or job.future.done():
                if executor is not None:
                    executor.shutdown(wait=True)
                if acquired:
                    self._lock.release()
        if job.dispatched or uncertain or self._storage_fault:
            self._pending["state"] = "unknown"
        else:
            # Confirmed pre-dispatch rejection releases this instance only.
            # Any retained intent re-arms the barrier on the next construction.
            self._blocked = False
            self._pending = None
        self._busy = False
        return job.receipt()

    def _exchange(self, job, dps):
        api = self._api
        original = api.socket
        retry = None
        timeout = None
        guard = None
        saved_retry = saved_timeout = False
        integrity = False
        try:
            # The event-loop checkpoints observe Task.cancelling(); threads
            # only read the thread-safe stop event and the absolute deadline.
            if job.expired():
                raise _Rejected("expired_before_dispatch")
            if not _logger_safe(api, self._report is not None):
                raise _Rejected("unsafe_logging")
            retry = api.socketRetryLimit
            saved_retry = True
            timeout = original.gettimeout()
            saved_timeout = True
            deadline = monotonic() + self._socket_deadline
            before_write = None
            if self._dispatch is not None:
                before_write = functools.partial(self._handoff, job, deadline)
            guard = _SocketGuard(original, job, deadline, self._receive_limit, timeout,
                                 before_write)
            api.socket = guard
            api.set_socketRetryLimit(0)
            if type(api.socketRetryLimit) is not int or api.socketRetryLimit != 0:
                raise _Rejected("transport_integrity")
            payload = api.generate_payload(CONTROL, dps)
            # decode_response=False keeps the upstream cache merge and its
            # wrong-CID requeue out of this exchange; the frame is NOT dropped:
            # it is decoded below by the client's own admitted decoder.
            response = api._send_receive(payload, getresponse=True, decode_response=False)
            job.client_returned = True
            job.reason = "client_returned"
            if (self._report is not None and job.attempted
                    and (self._dispatch is None or job.mark is not None)):
                self._deliver_response(api, response, job)
        except _Rejected as error:
            job.reason = error.args[0]
        except BaseException:
            # Never retain/return/log vendor exceptions or their private reprs.
            job.reason = "transport_error"
        finally:
            if guard is not None and guard.reason is not None:
                job.reason = guard.reason
            if saved_retry:
                try:
                    api.set_socketRetryLimit(retry)
                    if api.socketRetryLimit is not retry:
                        raise _Rejected("transport_integrity")
                except BaseException:
                    integrity = True
            alive = False
            try:
                alive = _socket_open(original) and not (guard and guard.closed)
            except BaseException:
                integrity = True
            if saved_timeout and alive:
                try:
                    original.settimeout(timeout)
                    if original.gettimeout() != timeout:
                        raise _Rejected("transport_integrity")
                except BaseException:
                    integrity = True
            try:
                if guard is not None and api.socket is guard:
                    api.socket = original if alive else None
                    if api.socket is not (original if alive else None):
                        raise _Rejected("transport_integrity")
                elif guard is not None and api.socket is not None:
                    # An unexpected replacement belongs to neither this guard
                    # nor this job. Never overwrite or close an unknown socket.
                    integrity = True
            except BaseException:
                # A failing setter is an integrity fault, not a reason to try
                # assigning again (or to resurrect an already closed socket).
                integrity = True
            if integrity:
                job.reason = "transport_integrity"
        return integrity

    def _grant(self, job):
        """Owning loop, one synchronous turn: final checks, then the mark."""
        for task in (job.caller, self._worker):
            if task is not None and (task.cancelling() or task.done()):
                job.stop.set()
        if job.expired() or not self._is_owner() or self._integrity_fault or self._storage_fault:
            return False
        mark = self._dispatch(job.operation_id)
        if getattr(mark, "operation_id", None) != job.operation_id:
            return False
        job.mark = mark
        return True

    def _handoff(self, job, deadline):
        """Worker: hand the send boundary to the owning loop and wait, bounded.

        The worker keeps the shared API lock; the loop callback never needs it.
        A request the worker abandons (timeout, stop) can never run later.
        """
        guard = threading.Lock()
        done = threading.Event()
        state = {"phase": "waiting", "granted": False}

        def on_loop():
            with guard:
                if state["phase"] != "waiting":
                    return
                state["phase"] = "running"
            granted = False
            try:
                granted = self._grant(job) is True
            except BaseException:  # noqa: BLE001 - never expose callback values
                granted = False
            finally:
                with guard:
                    state["phase"], state["granted"] = "done", granted
                done.set()

        try:
            self._loop.call_soon_threadsafe(on_loop)
        except BaseException:  # noqa: BLE001 - closed loop: no mark, no send
            return False
        while not done.is_set():
            remaining = min(job.expires, deadline) - monotonic()
            if remaining <= 0 or job.stop.is_set():
                with guard:
                    if state["phase"] == "waiting":
                        state["phase"] = "abandoned"
                        return False
                # Already running in its single synchronous loop turn.
                return done.wait(1.0) and state["granted"]
            done.wait(min(remaining, 0.05))
        return state["granted"]

    def _deliver_response(self, api, response, job):
        """Worker: decode the actual exchange frame; hand off its DPS once."""
        payload = getattr(response, "payload", None)
        if (isinstance(response, dict) or type(payload) is not bytes or not payload
                or getattr(response, "crc_good", True) is not True):
            return
        try:
            result = api._decode_payload(payload)
        except BaseException:  # noqa: BLE001 - never retain vendor exceptions
            job.reason = "response_undecodable"
            return
        dps = result.get("dps") if type(result) is dict else None
        if type(dps) is not dict:
            return
        actual = {key: value for key, value in dps.items()
                  if type(key) is str and type(value) in (bool, int, str)}
        if not actual:
            return
        try:
            self._report(actual)
        except BaseException:  # noqa: BLE001
            job.reason = "report_delivery_failed"

    async def async_wait_idle(self, timeout: float = 5.0) -> bool:
        if not _positive_number(timeout):
            raise ValueError("invalid_timeout")
        worker = self._worker
        if worker is None:
            return not self._busy
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout)
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            if not worker.cancelled():
                raise
        return not self._busy and (self._job.future is None or self._job.future.done())

    async def async_reconcile(self, operation_id: str,
                              verifier: Callable[[], Awaitable[bool]], *,
                              confirmed: bool = False, session_id: str | None = None) -> bool:
        if (not self._is_owner() or self._integrity_fault or self._storage_fault or self._busy or not self._blocked
                or not _valid_record(self._pending) or confirmed is not True
                or type(operation_id) is not str or operation_id != self.pending_operation_id
                or type(session_id) is not str or session_id != self._recovery_session_id):
            return False
        self._busy = True
        try:
            # Operation-specific fresh recovery check, not cached status or a
            # historical physical-success guess.
            result = await verifier()
            _raise_if_cancelling()
            if result is not True:
                return False
            if not self._is_owner():
                return False
            # Local release is one event-loop turn with no intervening I/O await.
            self._pending = None
            self._blocked = False
            return True
        except Exception:
            _raise_if_cancelling()
            return False
        finally:
            self._busy = False
