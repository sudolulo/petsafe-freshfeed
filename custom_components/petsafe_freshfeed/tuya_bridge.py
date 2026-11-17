"""Observe decoded receive results on the existing authenticated client only.

Two layers, both on the SAME loaded tuya_local wrapper and TinyTuya client:

* Observation hooks (installed at attach) copy actual decoded responses before
  the upstream cache merges them. Workers only enqueue; epoch and sequence are
  assigned, in arrival order, on the owning event loop.
* Receive ownership (``async_claim_receive``) gracefully stops the upstream
  monitor and restarts the wrapper's own ``receive_loop`` over an instance-local
  ``async_receive`` adapter. The adapter preserves the pinned poll / update-DPS /
  heartbeat / backoff ordering. Polls keep using the wrapper's own
  ``_retry_on_failed_connection`` lock path; direct receive and heartbeat
  executor calls hold the shared API lock until the actual executor future
  settles, including through cancellation. No second client, no outer lock and
  no recursive acquisition.

Costs of ownership changes, stated plainly: stopping either monitor runs the
upstream loop-exit path, which closes the persistent socket and resets the
wrapper's cached state. Claim and detach therefore each cause one ordinary
reconnect (a new connection epoch) and a brief cache reset, exactly as an
upstream stop/start would. While owned, a direct receive holds the lock for up
to the client's socket timeout, so a lock waiter can wait that long.

Draining never abandons I/O, so it is bounded only by upstream bounds: a
detach can wait for one in-flight poll through the wrapper's full
``_retry_on_failed_connection`` attempt sequence (each attempt bounded by the
client's socket timeout). Never call ``async_detach`` or
``async_claim_receive`` while holding the shared API lock: both may wait for a
monitor that needs it.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import functools
import logging
import threading
from time import monotonic, time

from .critical_transport import (
    CompatibilityError, CriticalTransport, IntentWitness, _socket_open, underlying_socket,
)
# Re-exported for compatibility: the neutral contract lives in local_source.
from .local_source import (  # noqa: F401
    ACK_DPS, OBSERVED_DPS, DeviceReport, DispatchMark, _settle, split_frame,
)
from .source_binding import SourceBindingError, bind_source
from .tuya_compat import (
    FAILURE, check_loaded_source, check_receive_control_flow, disown_attribute,
    logging_safe, own_attribute, owned_original,
)

_LOGGER = logging.getLogger(__name__)
_HOOKS = ("_cache_response", "_get_socket", "_check_socket_close", "set_socketPersistent", "close")
# Upper bound for the upstream monitor to observe its stop flag and exit
# through its own bounded I/O and backoff. Exceeding it aborts the claim.
_STOP_TIMEOUT = 60.0


class TuyaBridge:
    """Read-only hooks plus optional exclusive receive ownership.

    Nonpersistent responses can arrive after socket close; they remain genuine
    reports but cannot establish freshness. No cached or pending value is read,
    and no socket is opened by this bridge itself.
    """

    # Pinned upstream asyncio.sleep values between monitor iterations.
    BACKOFF = 5.0
    IDLE = 0.1

    def __init__(self, hass, wrapper, api, lock, *, entry=None, registry_device=None,
                 binding=None):
        self.hass, self.wrapper, self.api, self.lock = hass, wrapper, api, lock
        self.epoch = 1
        self.last_sequence = 0
        self.connected = api.socket is not None
        self._entry = entry
        self._device_id = getattr(registry_device, "id", None)
        self._binding = binding
        self._active = False
        self._subscribers = []
        self._originals = {}
        self._installed = {}
        self._guard = threading.RLock()
        # Worker side (under _guard): last seen socket and ordered events.
        self._seen_socket = underlying_socket(api.socket)
        self._events = deque()
        self._pending_sockets = 0
        self._generation = object()
        # Receive ownership (owning loop only).
        self._claim = None
        self._adapter = None
        self._monitor = None
        self._wake = None
        self._detaching = False
        self._abort_requested = asyncio.Event()
        self._transition = asyncio.Lock()
        self._transitions = 0

    # ----- observation hooks -------------------------------------------------

    def install(self):
        if self._active or any(name in self.api.__dict__ for name in _HOOKS):
            raise ValueError(FAILURE)
        self._active = True
        try:
            for name in _HOOKS:
                self._install_hook(name)
        except Exception:
            # Never leave a partial, unowned or undetachable hook set behind.
            self._active = False
            for name, hook in self._installed.items():
                if self.api.__dict__.get(name) is hook:
                    delattr(self.api, name)
                disown_attribute(self.api, name, hook)
            self._installed.clear()
            raise ValueError(FAILURE) from None

    def _install_hook(self, name):
        original = getattr(self.api, name)
        self._originals[name] = original

        def hook(*args, _name=name, _original=original, **kwargs):
            parts = ()
            with self._guard:
                self._note_socket()
                # Copy actual decoded results BEFORE the upstream cache merges.
                if _name == "_cache_response" and args and isinstance(args[0], dict):
                    parts = split_frame(args[0].get("dps"), exchange=False)
            try:
                return _original(*args, **kwargs)
            finally:
                with self._guard:
                    self._note_socket()
                    if parts and self._active:
                        at = datetime.now(timezone.utc)
                        for dps, eligible in parts:
                            self._enqueue(("report", at, dps, eligible))

        own_attribute(self.api, name, hook, original)
        self._installed[name] = hook
        setattr(self.api, name, hook)

    def _note_socket(self):
        """Any thread, under _guard: queue a connection break in arrival order."""
        # The critical transport's temporary guard is the same connection.
        socket = underlying_socket(self.api.socket)
        if socket is self._seen_socket:
            return
        self._seen_socket = socket
        # Replacement is a break even when HA availability never changes.
        if self._enqueue(("socket", socket is not None)):
            self._pending_sockets += 1

    def _enqueue(self, item):
        """Any thread, under _guard. Numbering happens on the loop only."""
        if not self._active:
            return False
        self._events.append((self._generation, item))
        try:
            self.hass.loop.call_soon_threadsafe(self.flush)
        except RuntimeError:
            pass  # closed loop: nothing can be delivered any more
        return True

    def flush(self):
        """Owning loop: assign epochs/sequences and deliver in arrival order."""
        with self._guard:
            items = list(self._events)
            self._events.clear()
        for generation, item in items:
            if item[0] == "socket":
                with self._guard:
                    self._pending_sockets -= 1
            if generation is not self._generation or not self._active or self._detaching:
                continue
            if item[0] == "socket":
                self.epoch += 1
                self.connected = item[1]
                self._deliver_connection(self.epoch, False)
                if item[1]:
                    self._deliver_connection(self.epoch, True)
            else:
                self.last_sequence += 1
                eligible = item[3] if len(item) > 3 else True
                self._deliver_report(DeviceReport(self.epoch, self.last_sequence, item[1], item[2], eligible))

    # Class-level alias (not a wrapper): bound-method equality must hold.
    _flush = flush

    def _deliver_connection(self, epoch, connected):
        for _, callback in tuple(self._subscribers):
            if not self._active:
                return
            try:
                callback(epoch, connected)
            except Exception:  # noqa: BLE001 - one subscriber must not stall ordering
                _LOGGER.error("FreshFeed connection subscriber failed")

    def _deliver_report(self, report):
        for callback, _ in tuple(self._subscribers):
            if not self._active:
                return
            try:
                callback(report)
            except Exception:  # noqa: BLE001
                _LOGGER.error("FreshFeed report subscriber failed")

    def subscribe(self, report_cb, connection_cb):
        pair = (report_cb, connection_cb)
        self._subscribers.append(pair)

        def unsubscribe():
            if pair in self._subscribers:
                self._subscribers.remove(pair)
        return unsubscribe

    def is_current(self):
        with self._guard:
            self._note_socket()
            pending = self._pending_sockets > 0
        return (self._active and self.connected and not pending
                and self.wrapper._api is self.api)

    # ----- receive ownership ----------------------------------------------------

    @asynccontextmanager
    async def _transitioning(self):
        self._transitions += 1
        try:
            async with self._transition:
                yield
        finally:
            self._transitions -= 1

    def _binding_current(self):
        if self._binding is None or self._entry is None or self._device_id is None:
            return False
        from homeassistant.helpers import device_registry as dr
        device = dr.async_get(self.hass).async_get(self._device_id)
        return device is not None and self._binding.is_current(
            entry=self._entry, registry_device=device,
            domain_data=self.hass.data.get("tuya_local"))

    def _preflight(self):
        """Synchronous, mutation-free gate; raises the fixed failure."""
        wrapper = self.wrapper
        try:
            task = wrapper._refresh_task
            ok = (self._active and not self._detaching and self.is_current()
                  and not self.hass.is_stopping
                  and getattr(self.api, "parent", None) is None
                  and _socket_open(self.api.socket)
                  and logging_safe(self.api)
                  and "async_receive" not in vars(wrapper)
                  and owned_original(wrapper, "async_receive") is None
                  and wrapper._running is True and not self._source_stopping()
                  and isinstance(task, asyncio.Task) and not task.done()
                  and self._binding_current())
        except Exception:  # noqa: BLE001 - unknown shape is incompatibility
            ok = False
        if not ok:
            raise ValueError(FAILURE)

    def code_ok(self):
        """Executor: provenance of loaded code and exact monitor control flow."""
        return (check_loaded_source(self.wrapper, self.api, self.lock) is True
                and check_receive_control_flow(self.wrapper) is True)

    # Class-level alias (not a wrapper): existing executor gates compare
    # bound methods, so ``bridge._code_ok == bridge.code_ok`` must hold.
    _code_ok = code_ok

    def _restart_original(self):
        wrapper = self.wrapper
        wrapper._running = True
        wrapper._refresh_task = wrapper._hass.async_create_task(wrapper.receive_loop())

    def _source_stopping(self):
        # Every pinned upstream stop path (last entity unregistered, HA stop,
        # device deletion) runs async_stop, which clears _children first.
        return self.hass.is_stopping or not self.wrapper._children

    def _abort_claim(self, old):
        """Undo OUR stop request only; never resurrect a source upstream stops."""
        wrapper = self.wrapper
        if self._source_stopping():
            return
        if not old.done():
            # The untouched upstream loop simply continues.
            wrapper._running = True
        elif wrapper._refresh_task is old:
            self._restart_original()

    async def async_claim_receive(self) -> None:
        """Take exclusive receive ownership; fixed failure before any mutation."""
        async with self._transitioning():
            wrapper = self.wrapper
            if self._claim is not None:
                task = wrapper._refresh_task
                if (vars(wrapper).get("async_receive") is self._adapter and wrapper._running
                        and isinstance(task, asyncio.Task) and not task.done()):
                    self._monitor = task  # possibly restarted upstream over our adapter
                    return
                raise ValueError(FAILURE)
            self._preflight()
            if not await self.hass.async_add_executor_job(self.code_ok):
                raise ValueError(FAILURE)
            self._preflight()
            # --- mutation starts here: stop the upstream monitor gracefully ---
            old = wrapper._refresh_task
            wrapper._running = False
            abort = asyncio.ensure_future(self._abort_requested.wait())
            deadline = monotonic() + _STOP_TIMEOUT
            try:
                # Sliced so an upstream restart (register_entity -> start sets
                # _running again) fails the claim promptly instead of at timeout.
                while (not old.done() and not abort.done() and wrapper._running is False
                       and monotonic() < deadline):
                    await asyncio.wait({old, abort}, return_when=asyncio.FIRST_COMPLETED,
                                       timeout=min(0.25, max(0.0, deadline - monotonic())))
            except asyncio.CancelledError:
                self._abort_claim(old)
                raise
            finally:
                abort.cancel()
            # A cancelled monitor may have abandoned executor I/O: never own it.
            if (not old.done() or old.cancelled() or wrapper._refresh_task is not old
                    or self._detaching or not self._active or self._source_stopping()
                    or "async_receive" in vars(wrapper) or not self._binding_current()):
                self._abort_claim(old)
                raise ValueError(FAILURE)
            token = object()
            adapter = functools.partial(self._receive, token)
            try:
                own_attribute(wrapper, "async_receive", adapter, wrapper.async_receive)
                try:
                    wrapper.async_receive = adapter
                except Exception:
                    disown_attribute(wrapper, "async_receive", adapter)
                    raise
            except Exception:  # noqa: BLE001 - stale registration: restore, fail closed
                self._abort_claim(old)
                raise ValueError(FAILURE) from None
            self._claim, self._adapter, self._wake = token, adapter, asyncio.Event()
            wrapper._running = True
            self._monitor = wrapper._refresh_task = wrapper._hass.async_create_task(
                wrapper.receive_loop())

    async def _release_receive(self):
        """Stop/drain every adapter-driven monitor, restore, restart if live."""
        token, self._claim = self._claim, None
        if token is None:
            return
        self._wake.set()
        wrapper, adapter = self.wrapper, self._adapter
        # While the adapter is installed, ANY receive_loop (ours, or one
        # upstream restarted after its own stop/start) runs it; a stale token
        # ends each of them. Drain until no such task remains live, with no
        # await between the final check and restoring the method.
        while True:
            live = {task for task in (self._monitor, wrapper._refresh_task)
                    if isinstance(task, asyncio.Task) and not task.done()}
            if not live:
                break
            await asyncio.wait(live)
        if vars(wrapper).get("async_receive") is adapter:
            del wrapper.async_receive
        disown_attribute(wrapper, "async_receive", adapter)
        self._monitor = self._adapter = None
        # A stopping source keeps its method back but is never restarted.
        if wrapper._running is True and not self._source_stopping():
            self._restart_original()

    async def _locked_io(self, token, func, *args):
        """Direct executor I/O holding the shared API lock until it settles."""
        await self.lock.acquire()
        try:
            if self._claim is not token:
                return None
            return await _settle(self.wrapper._hass.async_add_executor_job(func, *args))
        finally:
            self.lock.release()

    async def _backoff(self, token, seconds):
        if self._claim is not token:
            return
        try:
            async with asyncio.timeout(seconds):
                await self._wake.wait()
        except TimeoutError:
            pass

    async def _receive(self, token):
        """Instance-local replacement for the pinned ``async_receive``.

        Mirrors the pinned generator's control flow except: direct
        receive/heartbeat hold the API lock until the executor future settles;
        polls run as a task drained through cancellation; a stale claim token
        ends it (and a stale direct I/O step is skipped); the backoff sleep is
        woken early on release. Logging is reduced on purpose: the pinned
        first-failure warning with the device error text, the error-payload
        and persistence debug lines are dropped, and loop exceptions log the
        exception type only (no traceback or vendor text).
        """
        if self._claim is not token:
            return
        device = self.wrapper
        persist = not device.should_poll
        dps_updated = False

        device._api.set_socketPersistent(persist)
        if device._api.parent:
            device._api.parent.set_socketPersistent(persist)

        last_heartbeat = device._cached_state.get("updated_at", 0)
        wifi_api = device._api.parent or device._api
        while device._running and self._claim is token:
            error_count = device._api_working_protocol_failures
            force_backoff = False
            try:
                last_cache = device._cached_state.get("updated_at", 0)
                now = time()
                full_poll = False
                if (persist == device.should_poll) or (persist and (wifi_api.socket is None)):
                    persist = not device.should_poll
                    device._api.set_socketPersistent(persist)
                    if device._api.parent:
                        device._api.parent.set_socketPersistent(persist)
                    device._last_full_poll = 0

                needs_full_poll = now - device._last_full_poll > device._POLLING_INTERVAL
                if now - last_cache > device._POLLING_INTERVAL or (persist and needs_full_poll):
                    if (device._force_dps and not dps_updated
                            and device._api_protocol_working):
                        poll = await _settle(asyncio.ensure_future(
                            device._retry_on_failed_connection(
                                lambda: device._api.updatedps(device._force_dps),
                                f"Failed to update device dps for {device.name}")))
                        dps_updated = True
                    else:
                        poll = await _settle(asyncio.ensure_future(
                            device._retry_on_failed_connection(
                                lambda: device._api.status(),
                                f"Failed to fetch device status for {device.name}")))
                        dps_updated = False
                        full_poll = True
                    device._last_full_poll = now
                    last_heartbeat = now
                elif persist:
                    if now - last_heartbeat > device._HEARTBEAT_INTERVAL:
                        await self._locked_io(token, device._api.heartbeat, True)
                        last_heartbeat = now
                    poll = await self._locked_io(token, device._api.receive)
                    if poll and "Err" in poll and poll["Err"] == "904":
                        poll = None
                else:
                    force_backoff = True
                    poll = None

                if poll:
                    if "Err" in poll:
                        if poll["Err"] in {"901", "902", "905", "906", "914"}:
                            force_backoff = True
                            persist = False
                            device._api.set_socketPersistent(False)
                            if device._api.parent:
                                device._api.parent.set_socketPersistent(False)
                        if error_count == device._api_working_protocol_failures:
                            device._api_working_protocol_failures += 1
                        _LOGGER.debug("%s error reading", device.name)
                    else:
                        if "dps" in poll:
                            poll = poll["dps"]
                        if isinstance(poll, dict):
                            poll["full_poll"] = full_poll
                            yield poll

            except asyncio.CancelledError:
                device._running = False
                persist = False
                device._api.set_socketPersistent(False)
                if device._api.parent:
                    device._api.parent.set_socketPersistent(False)
                raise
            except Exception as error:  # noqa: BLE001 - pinned loop semantics
                # Type only: vendor exception text can carry private values.
                _LOGGER.warning("%s receive loop error %s", device.name, type(error).__name__)
                persist = False
                device._api.set_socketPersistent(False)
                if device._api.parent:
                    device._api.parent.set_socketPersistent(False)
                force_backoff = True

            if not device.has_returned_state or wifi_api.socket is None:
                force_backoff = True
            await self._backoff(token, self.BACKOFF if force_backoff else self.IDLE)

        device._api.set_socketPersistent(False)
        if device._api.parent:
            device._api.parent.set_socketPersistent(False)

    # ----- critical transport on the owned client -------------------------------

    async def async_enable_transport(self, witness: IntentWitness, *,
                                     on_dispatch) -> CriticalTransport:
        """Single-attempt transport over THIS owned client; no second client.

        ``on_dispatch`` runs on the owning loop at the actual send boundary,
        before the original socket write, possibly before ``async_send``
        returns. If it raises, nothing is written. Exchange responses are
        decoded by the client's own decoder and numbered in this same stream
        after that mark, but are not acknowledgement-eligible: they may have
        been buffered before the command.
        """
        if not callable(on_dispatch):
            raise ValueError(FAILURE)
        await self.async_claim_receive()
        if not await self.hass.async_add_executor_job(self.code_ok):
            raise ValueError(FAILURE)
        generation = self._generation
        if self._detaching or not self._active or self._claim is None:
            raise ValueError(FAILURE)
        try:
            return await CriticalTransport.async_create(
                self.wrapper, witness,
                compatibility_check=functools.partial(self._transport_compatible, generation),
                dispatch_callback=functools.partial(self._dispatch_mark, generation, on_dispatch),
                report_callback=functools.partial(self._transport_report, generation))
        except CompatibilityError:
            raise ValueError(FAILURE) from None

    def _owned(self, generation):
        wrapper = self.wrapper
        return (generation is self._generation and self._active and not self._detaching
                and self._claim is not None and self._adapter is not None
                and vars(wrapper).get("async_receive") is self._adapter
                and wrapper._api is self.api and wrapper._api_lock is self.lock)

    def _transport_compatible(self, generation, wrapper, api, lock):
        """Owning loop, under the API lock: synchronous, no file I/O."""
        return (wrapper is self.wrapper and api is self.api and lock is self.lock
                and self._owned(generation) and self.is_current()
                and getattr(api, "parent", None) is None
                and logging_safe(api) and self._binding_current())

    def _dispatch_mark(self, generation, on_dispatch, operation_id):
        """Owning loop, one synchronous turn: drain, revalidate, mark, notify.

        Reports decoded before the write are numbered first; a connection
        break still pending at this boundary refuses the send.
        """
        if not self._owned(generation):
            raise ValueError(FAILURE)
        epoch = self.epoch
        self.flush()
        if (self.epoch != epoch or not self._owned(generation) or not self.is_current()
                or not self._binding_current()):
            raise ValueError(FAILURE)
        mark = DispatchMark(operation_id, self.epoch, self.last_sequence)
        on_dispatch(mark)
        return mark

    def _transport_report(self, generation, dps):
        """Worker: preserve an uncorrelated exchange frame as observation only."""
        with self._guard:
            if generation is not self._generation:
                return
            self._note_socket()
            at = datetime.now(timezone.utc)
            # Exchange frames are never acknowledgement-eligible.
            for actual, eligible in split_frame(dps, exchange=True):
                self._enqueue(("report", at, actual, eligible))

    async def async_wait_idle(self, timeout: float = 5.0) -> bool:
        """True once no claim/detach transition runs and deliveries are flushed."""
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
        """Drain/restore receive ownership, then remove observation hooks.

        Returns True once complete (legacy detach never refuses).
        """
        self._detaching = True
        self._abort_requested.set()  # an in-flight claim stops waiting promptly
        # Obsolete every queued or future callback before any await.
        with self._guard:
            self._generation = object()
        self._subscribers.clear()
        self.connected = False
        async with self._transitioning():
            release = asyncio.ensure_future(self._release_receive())
            try:
                await _settle(release)
            finally:
                async with self.lock:
                    with self._guard:
                        self._active = False
                        for name, hook in self._installed.items():
                            if self.api.__dict__.get(name) is hook:
                                delattr(self.api, name)
                            disown_attribute(self.api, name, hook)
                        self._installed.clear()
        return True


async def async_attach(hass, source_entry, registry_device):
    """Find the wrapper by registry identity; never read the entry's secrets."""
    domain_data = hass.data.get("tuya_local", {})
    candidates = []
    if source_entry is None or source_entry.entry_id != registry_device.config_entry_id:
        raise ValueError(FAILURE)
    for domain, identifier in registry_device.identifiers:
        data = domain_data.get(identifier) if domain == "tuya_local" else None
        if isinstance(data, dict) and data.get("device") is not None:
            candidates.append(data["device"])
    if len(candidates) != 1:
        raise ValueError(FAILURE)
    wrapper = candidates[0]
    api, lock = getattr(wrapper, "_api", None), getattr(wrapper, "_api_lock", None)
    if not await hass.async_add_executor_job(check_loaded_source, wrapper, api, lock):
        raise ValueError(FAILURE)
    try:
        # Read observation does not need it; receive ownership requires it.
        binding = bind_source(entry=source_entry, registry_device=registry_device,
                              domain_data=domain_data)
    except SourceBindingError:
        binding = None
    async with lock:
        if wrapper._api is not api or getattr(api, "parent", None) is not None:
            raise ValueError(FAILURE)
        bridge = TuyaBridge(hass, wrapper, api, lock, entry=source_entry,
                            registry_device=registry_device, binding=binding)
        bridge.install()
    return bridge
