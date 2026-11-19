"""PetSafe-owned local source over one pinned TinyTuya client per feeder.

No Home Assistant, TinyTuya or Tuya Local import at module level: TinyTuya is
imported lazily, inside this source's own executor, by the default client
factory. ``LocalConnection`` is a pure, redacted, non-picklable holder.

Ownership. ``OwnedSource`` owns exactly one client, one asyncio lock, one
receive monitor and one single-worker executor for a physical feeder; a
process-wide claim refuses a second owner for the same device until the first
has actually drained and closed. Every source-owned client call runs in that
executor while holding the lock, and the lock is held until the actual worker
future completes, through cancellation (``_settle``). The critical transport
keeps its own guarded executor and takes the same lock.

Admission (``async_start``), in order: vendor logger check (before the library
is touched), client construction with an explicit address and protocol 3.5,
loaded TinyTuya code/version check, vendor logger check again, hook capture,
and only then connection I/O: one read-only ``status()`` that negotiates the
authenticated 3.5 session and must decode successfully. A decoded ``devId``
or ``gwId`` that differs from the configured device is refused; their absence
is accepted (not every status response carries identity). Reports queued by
an admission that fails are discarded, never delivered.

Read-only scope (pinned TinyTuya 1.20.0, protocol 3.5): session negotiation
(SESS_KEY_NEG_START 3 / SESS_KEY_NEG_FINISH 5), ``status()`` = DP_QUERY sent
as DP_QUERY_NEW (16) with an empty request object, ``heartbeat()`` =
HEART_BEAT (9), and ``receive()`` which sends nothing. No DP enumeration or
update request is ever sent, and status() is not filtered per request: its
received values are filtered to ``OBSERVED_DPS`` after decoding. The monitor
retries only those read-only calls, after its own backoff. Reconnecting is a
new connection epoch; it never authorizes replay of a pending operation.

Reports come only from the instance hooks (pre-cache), never a second time
from the status()/receive() return values. Socket changes queue ordered epoch
breaks before later reports; ``is_current`` rejects a pending break.
"""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import functools
import ipaddress
import logging
import re
import threading
from time import monotonic

from .critical_transport import CompatibilityError, CriticalTransport, underlying_socket
from .local_source import DeviceReport, DispatchMark, _settle, split_frame
from .owned_compat import (
    OWNED_HOOKS, check_loaded_client, check_runtime_owner, disown_hook, hooks_intact,
    own_hook, vendor_logging_safe,
)

_LOGGER = logging.getLogger(__name__)

INVALID_CONFIGURATION = "invalid_local_configuration"
UNSUPPORTED_CONFIGURATION = "unsupported_local_configuration"
FAILURE = "owned_source_incompatible"
ADMISSION_FAILED = "local_admission_failed"
LOGGING_UNSAFE = "vendor_logging_unsafe"
OWNER_CONFLICT = "owned_source_conflict"
SUPPORTED_PROTOCOLS = frozenset({"3.5"})

_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_RFC1918 = (ipaddress.IPv4Network("10.0.0.0/8"), ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"))
# Error codes the pinned client returns for a broken connection or session.
_BACKOFF_ERRORS = frozenset({"901", "902", "905", "906", "914"})

# One owner per physical feeder in this process: device_id -> OwnedSource.
_CLAIMS = {}
_CLAIMS_GUARD = threading.Lock()


class LocalConnectionError(ValueError):
    """Fixed code only; never the rejected value."""


class OwnedSourceError(ValueError):
    """Fixed code only; never vendor exception text or context."""


def _rfc1918_ipv4(value):
    if type(value) is not str:
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return str(address) == value and any(address in network for network in _RFC1918)


class LocalConnection:
    """Validated local configuration of one feeder: redacted, immutable, unpicklable."""

    __slots__ = ("device_id", "host", "_local_key", "version")

    def __init__(self, device_id, host, local_key, version):
        code = None
        if type(device_id) is not str or _DEVICE_ID.fullmatch(device_id) is None:
            code = INVALID_CONFIGURATION
        elif (type(local_key) is not str or len(local_key) != 16
              or not all(0x20 <= ord(char) <= 0x7E for char in local_key)):
            code = INVALID_CONFIGURATION
        elif type(version) is not str or version not in SUPPORTED_PROTOCOLS:
            code = UNSUPPORTED_CONFIGURATION
        elif not _rfc1918_ipv4(host):
            code = UNSUPPORTED_CONFIGURATION
        if code is not None:
            raise LocalConnectionError(code)
        for name, value in (("device_id", device_id), ("host", host),
                            ("_local_key", local_key), ("version", version)):
            object.__setattr__(self, name, value)

    @property
    def local_key(self):
        return self._local_key

    def __setattr__(self, name, value):
        raise AttributeError("LocalConnection is immutable")

    def __delattr__(self, name):
        raise AttributeError("LocalConnection is immutable")

    def __repr__(self):
        return "LocalConnection(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("LocalConnection cannot be serialized")

    def __reduce__(self):
        raise TypeError("LocalConnection cannot be serialized")

    def __getstate__(self):
        raise TypeError("LocalConnection cannot be serialized")

    def __copy__(self):
        raise TypeError("LocalConnection cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("LocalConnection cannot be copied")


def default_client_factory(connection):
    """Executor: the pinned TinyTuya client; explicit address and protocol, no I/O."""
    import tinytuya  # noqa: PLC0415 - lazy: optional at import, file I/O in executor

    return tinytuya.Device(
        connection.device_id, address=connection.host, local_key=connection.local_key,
        version=float(connection.version), connection_timeout=3,
        connection_retry_limit=1, connection_retry_delay=0, persist=True)


class OwnedSource:
    """LocalSource contract over a PetSafe-owned TinyTuya client."""

    # Monitor timing: idle gap, error backoff, full status and heartbeat periods.
    IDLE = 0.1
    BACKOFF = 5.0
    POLL_INTERVAL = 30.0
    HEARTBEAT_INTERVAL = 5.0

    def __init__(self, hass, connection, *, client_factory=None):
        if type(connection) is not LocalConnection:
            raise OwnedSourceError(UNSUPPORTED_CONFIGURATION)
        if client_factory is not None and not callable(client_factory):
            raise OwnedSourceError(FAILURE)
        self.hass = hass
        self._connection = connection
        self._factory = client_factory or default_client_factory
        self.api = None
        self.lock = asyncio.Lock()
        self.epoch = 1
        self.last_sequence = 0
        self.connected = False
        self._state = "new"  # new -> starting -> running -> stopping -> stopped
        self._admitted = False
        self._hooks_active = False
        self._installed = {}
        self._executor = None
        self._monitor = None
        self._subscribers = []
        self._cache = {}
        self._guard = threading.RLock()
        self._seen_socket = None
        self._events = deque()
        self._pending_sockets = 0
        self._generation = object()
        self._transitions = 0
        self._status_identity = None

    def __repr__(self):
        return f"OwnedSource(state={self._state}, epoch={self.epoch})"

    # ----- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        """Admit and start; a fixed OwnedSourceError, never vendor context."""
        if self._state != "new":
            raise OwnedSourceError(OWNER_CONFLICT)
        device_id = self._connection.device_id
        with _CLAIMS_GUARD:
            if device_id in _CLAIMS:
                raise OwnedSourceError(OWNER_CONFLICT)
            _CLAIMS[device_id] = self
        self._state = "starting"
        self._transitions += 1
        code = None
        try:
            code = await self._admit()
        except asyncio.CancelledError:
            await self._teardown()
            raise
        except Exception:  # noqa: BLE001 - fixed failure, never vendor context
            code = FAILURE
        finally:
            self._transitions -= 1
        if code is not None:
            await self._teardown()
            # Raised outside any except block: no vendor exception as context.
            raise OwnedSourceError(code)

    async def _admit(self):
        """Returns None once running, else a fixed failure code."""
        if not vendor_logging_safe():
            return LOGGING_UNSAFE
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="petsafe-owned")
        api = await self._run(self._construct)
        if api is None:
            return ADMISSION_FAILED
        self.api = api
        if await self._run(check_loaded_client, api) is not True:
            return FAILURE
        if not vendor_logging_safe():
            return LOGGING_UNSAFE
        try:
            if (getattr(api, "version", None) != float(self._connection.version)
                    or getattr(api, "parent", None) is not None or api.socket is not None):
                return FAILURE
            # Defensive: never switch to device22 DP brute-force request shapes.
            api.disabledetect = True
            self._install_hooks()
        except Exception:  # noqa: BLE001 - unknown shape is incompatibility
            return FAILURE
        # --- first connection I/O: authenticated read-only status ---------
        result = await self._locked_io(self._generation, self._status_call)
        if result is not True or not self._identity_ok():
            return ADMISSION_FAILED
        self._admitted = True
        self._state = "running"
        self.flush()
        if not self.is_current():
            return ADMISSION_FAILED
        generation = self._generation
        self._monitor = self.hass.async_create_background_task(
            self._monitor_loop(generation), "petsafe_freshfeed owned receive monitor")
        return None

    def _construct(self):
        """Executor: build the client; vendor exceptions are never retained."""
        try:
            return self._factory(self._connection)
        except BaseException:  # noqa: BLE001 - fixed failure, no vendor context
            return None

    def _status_call(self):
        """Executor: one read-only status; True only for a decoded admission."""
        api = self.api
        try:
            result = api.status()
        except BaseException:  # noqa: BLE001
            return False
        ok = (type(result) is dict and "Err" not in result and "Error" not in result
              and type(result.get("dps")) is dict and api.socket is not None
              and getattr(api, "version", None) == 3.5)
        if ok:
            identity = []
            for container in (result, result.get("data")):
                if type(container) is dict:
                    identity += [container[field] for field in ("devId", "gwId")
                                 if field in container]
            self._status_identity = identity
        return ok

    def _identity_ok(self):
        identity = getattr(self, "_status_identity", None)
        return identity is not None and all(
            value == self._connection.device_id for value in identity)

    async def _run(self, func, *args):
        """Source executor call with no client lock (construction, file checks)."""
        return await _settle(asyncio.wrap_future(self._executor.submit(func, *args)))

    async def _locked_io(self, generation, func, *args):
        """Client I/O holding the shared lock until the worker actually settles."""
        await self.lock.acquire()
        try:
            if generation is not self._generation:
                return None
            return await _settle(asyncio.wrap_future(self._executor.submit(func, *args)))
        finally:
            self.lock.release()

    async def _monitor_loop(self, generation):
        api = self.api
        last_poll = last_beat = monotonic()
        while generation is self._generation and self._state == "running":
            if not vendor_logging_safe():
                # A TinyTuya DEBUG level raised at runtime would log keys and
                # frames: no client I/O at all until it is lowered (reports
                # go stale; the level is never changed behind the user's back).
                await asyncio.sleep(self.BACKOFF)
                continue
            backoff = False
            try:
                now = monotonic()
                if api.socket is None or now - last_poll > self.POLL_INTERVAL:
                    result = await self._locked_io(generation, api.status)
                    last_poll = last_beat = monotonic()
                else:
                    if now - last_beat > self.HEARTBEAT_INTERVAL:
                        await self._locked_io(generation, api.heartbeat, True)
                        last_beat = monotonic()
                    result = await self._locked_io(generation, api.receive)
                    if type(result) is dict and result.get("Err") == "904":
                        result = None
                if type(result) is dict and "Err" in result:
                    backoff = True
                    if result["Err"] in _BACKOFF_ERRORS:
                        last_poll = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - type only: vendor text can carry values
                _LOGGER.warning("FreshFeed owned receive error %s", type(error).__name__)
                backoff = True
            if api.socket is None:
                backoff = True
            await asyncio.sleep(self.BACKOFF if backoff else self.IDLE)

    async def async_stop(self, timeout: float = 5.0) -> bool:
        """Drain actual I/O, close and release; False keeps ownership claimed."""
        if type(timeout) not in (int, float) or not timeout > 0:
            raise ValueError("invalid_timeout")
        if self._state in ("new", "stopped"):
            self._state = "stopped"
            return True
        if self._state == "starting":
            return False
        deadline = monotonic() + timeout
        self._state = "stopping"
        # Obsolete every queued or future callback before any await.
        with self._guard:
            self._generation = object()
        self._subscribers.clear()
        self.connected = False
        self._transitions += 1
        try:
            monitor = self._monitor
            if monitor is not None and not monitor.done():
                monitor.cancel()
                await asyncio.wait({monitor}, timeout=max(0.0, deadline - monotonic()))
                if not monitor.done():
                    return False  # actual executor I/O still live: keep everything
            return await self._teardown(deadline)
        finally:
            self._transitions -= 1

    async def _teardown(self, deadline=None):
        """Close the client and release the claim once no I/O can be live."""
        with self._guard:
            self._generation = object()
        self._subscribers.clear()
        self.connected = False
        try:
            if deadline is None:
                await self.lock.acquire()
            else:
                async with asyncio.timeout(max(0.0, deadline - monotonic())):
                    await self.lock.acquire()
        except TimeoutError:
            return False  # critical transport still owns the lock
        try:
            if self.api is not None and self._executor is not None:
                await _settle(asyncio.wrap_future(self._executor.submit(self._close_client)))
            self._uninstall_hooks()
        finally:
            self.lock.release()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        self._state = "stopped"
        self._admitted = False
        with _CLAIMS_GUARD:
            if _CLAIMS.get(self._connection.device_id) is self:
                del _CLAIMS[self._connection.device_id]
        return True

    def _close_client(self):
        try:
            self.api.close()
        except BaseException:  # noqa: BLE001
            pass

    # ----- hooks (socket lifecycle and pre-cache reports) --------------------

    def _install_hooks(self):
        api = self.api
        if any(name in vars(api) for name in OWNED_HOOKS):
            raise ValueError(FAILURE)
        with self._guard:
            self._seen_socket = underlying_socket(api.socket)
            self._hooks_active = True
        try:
            for name in OWNED_HOOKS:
                self._install_hook(name)
        except Exception:
            self._uninstall_hooks()
            raise ValueError(FAILURE) from None

    def _install_hook(self, name):
        api = self.api
        original = getattr(api, name)

        def hook(*args, _name=name, _original=original, **kwargs):
            parts = ()
            at = received = None
            with self._guard:
                self._note_socket()
                # Copy actual decoded results BEFORE the client cache merges.
                if _name == "_cache_response" and args and isinstance(args[0], dict):
                    parts = split_frame(args[0].get("dps"), exchange=False)
                    at = datetime.now(timezone.utc)
                    received = monotonic()
            try:
                return _original(*args, **kwargs)
            finally:
                with self._guard:
                    self._note_socket()
                    if parts and self._hooks_active:
                        for dps, eligible in parts:
                            self._enqueue(("report", at, dps, eligible, received))

        own_hook(api, name, hook, original)
        self._installed[name] = hook
        setattr(api, name, hook)

    def _uninstall_hooks(self):
        api = self.api
        if api is None:
            return
        with self._guard:
            self._hooks_active = False
            for name, hook in self._installed.items():
                if vars(api).get(name) is hook:
                    delattr(api, name)
                disown_hook(api, name, hook)
            self._installed.clear()

    def _note_socket(self):
        """Any thread, under _guard: queue a connection break in arrival order."""
        socket = underlying_socket(self.api.socket)
        if socket is self._seen_socket:
            return
        self._seen_socket = socket
        if self._enqueue(("socket", socket is not None)):
            self._pending_sockets += 1

    def _enqueue(self, item):
        """Any thread, under _guard. Numbering happens on the loop only."""
        if not self._hooks_active:
            return False
        self._events.append((self._generation, item))
        try:
            self.hass.loop.call_soon_threadsafe(self.flush)
        except RuntimeError:
            pass  # closed loop: nothing can be delivered any more
        return True

    def flush(self):
        """Owning loop: assign epochs/sequences and deliver in arrival order.

        Before admission succeeds nothing is delivered (queued items wait);
        a failed admission replaces the generation, discarding them.
        """
        if not self._admitted:
            return
        with self._guard:
            items = list(self._events)
            self._events.clear()
        for generation, item in items:
            if item[0] == "socket":
                with self._guard:
                    self._pending_sockets -= 1
            if generation is not self._generation or self._state in ("stopping", "stopped"):
                continue
            if item[0] == "socket":
                self.epoch += 1
                self.connected = item[1]
                self._deliver_connection(self.epoch, False)
                if item[1]:
                    self._deliver_connection(self.epoch, True)
            else:
                self.last_sequence += 1
                self._cache.update(item[2])
                self._deliver_report(DeviceReport(self.epoch, self.last_sequence, item[1],
                                                  item[2], item[3], item[4]))

    def _deliver_connection(self, epoch, connected):
        for _, callback in tuple(self._subscribers):
            try:
                callback(epoch, connected)
            except Exception:  # noqa: BLE001 - one subscriber must not stall ordering
                _LOGGER.error("FreshFeed connection subscriber failed")

    def _deliver_report(self, report):
        for callback, _ in tuple(self._subscribers):
            try:
                callback(report)
            except Exception:  # noqa: BLE001
                _LOGGER.error("FreshFeed report subscriber failed")

    # ----- LocalSource contract ------------------------------------------------

    def subscribe(self, report_cb, connection_cb):
        pair = (report_cb, connection_cb)
        self._subscribers.append(pair)

        def unsubscribe():
            if pair in self._subscribers:
                self._subscribers.remove(pair)
        return unsubscribe

    def snapshot_dps(self) -> dict:
        """Last delivered value of each observed DP (string IDs); not freshness."""
        return dict(self._cache)

    def is_current(self) -> bool:
        api = self.api
        if api is None or not self._hooks_active:
            return False
        with self._guard:
            self._note_socket()
            pending = self._pending_sockets > 0
        # While vendor logging is unsafe the monitor does no I/O: the cache is frozen.
        return (self._state == "running" and self._admitted and self.connected and not pending
                and vendor_logging_safe()
                and all(vars(api).get(name) is hook for name, hook in self._installed.items())
                and len(self._installed) == len(OWNED_HOOKS))

    def code_ok(self) -> bool:
        """Executor: loaded TinyTuya provenance plus intact owned hooks."""
        api = self.api
        return api is not None and check_loaded_client(api) is True and hooks_intact(api)

    async def async_claim_receive(self) -> None:
        """The owned monitor already owns receive; refuses unless it runs."""
        monitor = self._monitor
        if self._state != "running" or monitor is None or monitor.done():
            raise ValueError(FAILURE)

    async def async_enable_transport(self, witness, *, on_dispatch) -> CriticalTransport:
        """Single-attempt transport over THIS client and lock; no second client."""
        if not callable(on_dispatch):
            raise ValueError(FAILURE)
        await self.async_claim_receive()
        if await _settle(self.hass.async_add_executor_job(self.code_ok)) is not True:
            raise ValueError(FAILURE)
        generation = self._generation
        if self._state != "running" or not self.is_current():
            raise ValueError(FAILURE)
        try:
            return await CriticalTransport.async_create_owned(
                self, witness,
                compatibility_check=functools.partial(self._transport_compatible, generation),
                dispatch_callback=functools.partial(self._dispatch_mark, generation, on_dispatch),
                report_callback=functools.partial(self._transport_report, generation))
        except CompatibilityError:
            raise ValueError(FAILURE) from None

    def _owned(self, generation):
        return (generation is self._generation and self._state == "running"
                and self._admitted and self._monitor is not None and not self._monitor.done())

    def _transport_compatible(self, generation, owner, api, lock):
        """Owning loop, under the shared lock: synchronous, no file I/O."""
        return (owner is self and api is self.api and lock is self.lock
                and self._owned(generation) and check_runtime_owner(owner, api, lock))

    def _dispatch_mark(self, generation, on_dispatch, operation_id):
        """Owning loop, one synchronous turn: drain, revalidate, mark, notify."""
        if not self._owned(generation):
            raise ValueError(FAILURE)
        epoch = self.epoch
        self.flush()
        if self.epoch != epoch or not self._owned(generation) or not self.is_current():
            raise ValueError(FAILURE)
        mark = DispatchMark(operation_id, self.epoch, self.last_sequence)
        on_dispatch(mark)
        return mark

    def _transport_report(self, generation, dps):
        """Worker: an uncorrelated exchange frame is observation only."""
        with self._guard:
            if generation is not self._generation:
                return
            self._note_socket()
            at = datetime.now(timezone.utc)
            received = monotonic()
            for actual, eligible in split_frame(dps, exchange=True):
                self._enqueue(("report", at, actual, eligible, received))

    async def async_wait_idle(self, timeout: float = 5.0) -> bool:
        """True once no start/stop transition runs and deliveries are flushed."""
        if type(timeout) not in (int, float) or not timeout > 0:
            raise ValueError("invalid_timeout")
        deadline = monotonic() + timeout
        while self._transitions:
            if monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        self.flush()
        return True

    async def async_detach(self) -> bool:
        """LocalSource detach: the stop result (False keeps the source claimed)."""
        return await self.async_stop()
