"""Synthetic receive-boundary tests: no network or credentials."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class Api:
    def __init__(self):
        self.socket = object()
        self.socketPersistent = True
        self.parent = None

    def _cache_response(self, response):
        return response

    def _get_socket(self, renew=False):
        if renew or self.socket is None:
            self.socket = object()
        return True

    def _check_socket_close(self, force=False):
        if force or not self.socketPersistent:
            self.socket = None

    def set_socketPersistent(self, persist):
        self.socketPersistent = persist
        self._check_socket_close()

    def close(self):
        self.socket = None


async def test_actual_reports_and_hidden_connections(hass):
    from custom_components.petsafe_freshfeed.tuya_bridge import TuyaBridge
    api = Api()
    bridge = TuyaBridge(hass, SimpleNamespace(_api=api), api, asyncio.Lock())
    bridge.install()
    reports, connections = [], []
    bridge.subscribe(reports.append, lambda *args: connections.append(args))
    api._cache_response({'dps': {'1': '/w==', '99': 2}})
    api._cache_response({'dps': {'1': '/w=='}})
    await hass.async_block_till_done()
    assert len(reports) == 2
    assert reports[0].dps == {'1': '/w=='}
    assert reports[1].sequence > reports[0].sequence
    epoch = bridge.epoch
    api.close()
    api._cache_response({'dps': {'1': '/w=='}})
    await hass.async_block_till_done()
    assert not bridge.connected
    assert bridge.epoch > epoch
    assert len(reports) == 3  # genuine close-before-cache report remains observable
    api._get_socket()
    await hass.async_block_till_done()
    assert bridge.connected
    api._get_socket(True)
    await hass.async_block_till_done()
    assert any(not connected for _, connected in connections)
    await bridge.async_detach()
    await bridge.async_detach()
    api._cache_response({'dps': {'1': '/w=='}})
    await hass.async_block_till_done()
    assert len(reports) == 3
    assert '_cache_response' not in api.__dict__


def test_allowlisted_files_and_bound_methods(tmp_path, monkeypatch):
    import hashlib
    import sys
    from types import ModuleType
    from custom_components.petsafe_freshfeed import tuya_compat as compat

    files = {}
    def module(name, suffix, text):
        path = tmp_path / suffix
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        files[suffix] = hashlib.sha256(path.read_bytes()).hexdigest()
        mod = ModuleType(name)
        mod.__file__ = str(path)
        monkeypatch.setitem(sys.modules, name, mod)
        exec(compile(text, str(path), 'exec'), mod.__dict__)
        return mod

    wrapper_mod = module('custom_components.tuya_local.device',
                         'custom_components/tuya_local/device.py', 'class Wrapper: pass\n')
    text = 'class Device:\n' + ''.join(
        f'    def {name}(self): pass\n' for name in compat._API_METHODS)
    api_mod = module('tinytuya.core.Device', 'tinytuya/core/Device.py', text)
    for suffix in ('tinytuya/__init__.py', 'tinytuya/core/__init__.py',
                   'tinytuya/core/XenonDevice.py', 'tinytuya/core/core.py',
                   'tinytuya/core/message_helper.py', 'tinytuya/core/header.py',
                   'tinytuya/core/crypto_helper.py'):
        name = suffix[:-3].replace('/', '.').removesuffix('.__init__')
        module(name, suffix, '')
    monkeypatch.setattr(compat.json, 'loads', lambda _: {'files': files})
    wrapper, api, lock = wrapper_mod.Wrapper(), api_mod.Device(), asyncio.Lock()
    wrapper._api, wrapper._api_lock = api, lock
    assert compat.check_loaded_source(wrapper, api, lock)
    assert not compat.check_loaded_source(wrapper, api, asyncio.Lock())
    api.close = lambda: None
    assert not compat.check_loaded_source(wrapper, api, lock)
    del api.close
    # Another allowlisted, genuinely bound method is still the wrong callsite.
    api.close = api._check_socket_close
    assert not compat.check_loaded_source(wrapper, api, lock)
    del api.close
    original_close = api_mod.Device.close
    monkeypatch.setattr(api_mod.Device, 'close', api_mod.Device._check_socket_close)
    assert not compat.check_loaded_source(wrapper, api, lock)
    monkeypatch.setattr(api_mod.Device, 'close', original_close)
    assert compat.check_loaded_source(wrapper, api, lock)
    (tmp_path / 'tinytuya/core/header.py').write_text('changed')
    assert not compat.check_loaded_source(wrapper, api, lock)


def _import_pinned(monkeypatch):
    """Import the pinned public tuya_local/TinyTuya layout, or skip if absent.

    PETSAFE_PINNED_TUYA_LOCAL names the pinned custom_components/tuya_local
    directory; PETSAFE_PINNED_TINYTUYA_SITE names a site-packages directory
    holding the pinned tinytuya package. Either unset skips the test.
    """
    import importlib
    import os
    import sys
    from pathlib import Path
    from types import ModuleType

    source_env = os.environ.get('PETSAFE_PINNED_TUYA_LOCAL', '')
    site_env = os.environ.get('PETSAFE_PINNED_TINYTUYA_SITE', '')
    if not source_env or not site_env:
        pytest.skip('pinned public source layout is not configured')
    source = Path(source_env)
    site = Path(site_env)
    if not source.is_dir() or not (site / 'tinytuya').is_dir():
        pytest.skip('pinned public source layout is not installed')
    monkeypatch.syspath_prepend(str(site))
    package = ModuleType('custom_components.tuya_local')
    package.__path__ = [str(source)]
    package.DOMAIN = 'tuya_local'
    monkeypatch.setitem(sys.modules, 'custom_components.tuya_local', package)
    for name in tuple(sys.modules):
        if name.startswith('tinytuya') or name.startswith('custom_components.tuya_local.'):
            monkeypatch.delitem(sys.modules, name)
    device = importlib.import_module('custom_components.tuya_local.device')
    tinytuya = importlib.import_module('tinytuya')
    return device, tinytuya


def test_pinned_public_layout_matches_checked_in_manifest(monkeypatch):
    import sys
    from pathlib import Path
    from custom_components.petsafe_freshfeed.tuya_compat import (
        check_loaded_source, check_receive_control_flow)

    device, tinytuya = _import_pinned(monkeypatch)
    wrapper = object.__new__(device.TuyaLocalDevice)
    api = object.__new__(tinytuya.Device)
    lock = asyncio.Lock()
    wrapper._api, wrapper._api_lock = api, lock
    from custom_components.petsafe_freshfeed import tuya_compat as compat
    import json
    files = json.loads(Path(compat.__file__).with_name('tuya_fingerprints.json').read_text())['files']
    missing = [suffix for suffix in files if suffix.startswith('tinytuya/') and
               suffix[:-3].replace('/', '.').removesuffix('.__init__') not in sys.modules]
    assert not missing
    assert all(hasattr(api, name) for name in compat._API_METHODS), [name for name in compat._API_METHODS if not hasattr(api, name)]
    assert check_loaded_source(wrapper, api, lock)
    # The exact monitor control flow the receive takeover relies on is pinned too.
    assert check_receive_control_flow(wrapper)


def test_unknown_implementation_fails_closed():
    from custom_components.petsafe_freshfeed.tuya_compat import check_loaded_source
    api = Api()
    lock = asyncio.Lock()
    wrapper = SimpleNamespace(_api=api, _api_lock=lock)
    before = dict(api.__dict__)
    assert not check_loaded_source(wrapper, api, lock)
    assert api.__dict__ == before


# ---------------------------------------------------------------------------
# Exclusive receive ownership, exercised against the REAL pinned tuya_local
# TuyaLocalDevice monitor (receive_loop/async_receive/_retry_on_failed_connection)
# with an instrumented fake client. No network, credentials or device.

import logging  # noqa: E402
import queue  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

_fake_log = logging.getLogger('petsafe_test.fake_tinytuya')
_fake_log.setLevel(logging.INFO)


class FakeSocket:
    closed = False
    timeout = 3.0

    def __init__(self):
        self.writes = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.writes.append(bytes(data))

    def close(self):
        self.closed = True


class IoApi(Api):
    """Instrumented client; each call opens/closes like TinyTuya's socket handling."""

    def __init__(self):
        super().__init__()
        self.socket = FakeSocket()
        self.id = 'fakeid'
        self.children = {}
        self.version = 3.5
        self.socketRetryLimit = 1
        self.inbox = queue.Queue()
        self.gate = None
        self.gate_name = 'receive'
        self.entered = threading.Event()
        self.fail_next = None
        self.persist_calls = []
        self._mutex = threading.Lock()
        self.active = 0
        self.reset()

    def reset(self):
        with self._mutex:
            self.calls, self.max_active = [], self.active

    def _get_socket(self, renew=False):
        if renew or self.socket is None:
            self.socket = FakeSocket()
        return True

    def _io(self, name, result=None):
        with self._mutex:
            self.calls.append(name)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self._get_socket(False)
            gate = self.gate
            if gate is not None and name == self.gate_name:
                self.entered.set()
                gate.wait(5)
            if self.fail_next == name:
                self.fail_next = None
                raise OSError('synthetic')
            if name == 'receive':
                try:
                    result = self.inbox.get(timeout=0.02)
                except queue.Empty:
                    result = None
            elif name == 'critical':
                time.sleep(0.01)
            if isinstance(result, dict) and 'dps' in result:
                self._cache_response(result)
            return result
        finally:
            self._check_socket_close()
            with self._mutex:
                self.active -= 1

    def receive(self):
        return self._io('receive')

    def heartbeat(self, nowait=True):
        return self._io('heartbeat')

    def status(self, nowait=False):
        return self._io('status', {'dps': {'4': 'standby'}})

    def updatedps(self, index=None, nowait=False):
        return self._io('updatedps')

    def critical(self):
        return self._io('critical')

    def set_version(self, version):
        self.version = version

    def set_socketRetryLimit(self, limit):
        self.socketRetryLimit = limit

    def set_socketPersistent(self, persist):
        self.persist_calls.append(persist)
        super().set_socketPersistent(persist)

    # Synthetic critical exchange surface: an undecoded response frame plus
    # the client's own decoder. Never the merged cache.
    response = None
    before_send = None
    decode_error = False

    def generate_payload(self, *args, **kwargs):
        if not hasattr(self, 'payload_calls'):
            self.payload_calls = []
        self.payload_calls.append((args, kwargs))
        _fake_log.debug('payload')
        if self.before_send is not None:
            self.before_send()
        return b'synthetic-control'

    def _send_receive(self, payload, minresponse=28, getresponse=True,
                      decode_response=True, from_child=None):
        _fake_log.debug('exchange')
        if payload is None:
            return None
        with self._mutex:
            self.calls.append('control')
        self._get_socket(False)
        try:
            self.socket.sendall(payload)
        except Exception:
            self._check_socket_close(True)
            return {'Err': '901'}
        self._check_socket_close()
        return self.response

    def _decode_payload(self, payload):
        _fake_log.debug('decode')
        if self.decode_error:
            raise RuntimeError('synthetic-private-decode')
        import json
        return json.loads(payload)


class Child:
    entity_id = 'sensor.synthetic_child'

    def __init__(self):
        self.received = []
        self._config = SimpleNamespace(dps=lambda: [])

    def on_receive(self, poll, full_poll):
        self.received.append((dict(poll), full_poll))

    def schedule_update_ha_state(self, force_refresh=False):
        pass

    def async_schedule_update_ha_state(self, force_refresh=False):
        pass


class _FastSleep:
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(seconds):
        await asyncio.sleep(min(seconds, 0.05))


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition not reached'
        await asyncio.sleep(0.01)


async def exchange(ctx):
    """Synthetic lock-holding critical exchange (not the future adapter)."""
    async with ctx.lock:
        await ctx.hass.async_add_executor_job(ctx.api.critical)


def _gate(ctx, name='receive'):
    gate = threading.Event()
    ctx.gates.append(gate)
    ctx.api.entered.clear()
    ctx.api.gate_name = name
    ctx.api.gate = gate
    return gate


def _open(ctx, gate):
    ctx.api.gate = None
    gate.set()


async def create_owned(hass, source_entry, monkeypatch, *, attach=True):
    """Create and attach the pinned shared-client fixture; caller must clean up bridge."""
    from homeassistant.helpers import device_registry as dr
    from custom_components.petsafe_freshfeed import tuya_bridge
    device_module, _ = _import_pinned(monkeypatch)
    registry_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id, identifiers={('tuya_local', 'fakeid')})
    hass.data.setdefault('tuya_local', {})
    wrapper = device_module.TuyaLocalDevice(
        'Test', 'fakeid', '192.0.2.10', '0123456789abcdef', 3.5, None, hass)
    api = IoApi()
    wrapper._api = api
    wrapper._api_protocol_version_index = 0
    wrapper._api_protocol_working = True
    wrapper._HEARTBEAT_INTERVAL = 0.05
    wrapper._cached_state = {'updated_at': time.time(), '4': 'standby'}
    child = Child()
    wrapper._children.append(child)
    lock = wrapper._api_lock
    hass.data['tuya_local']['fakeid'] = {
        'device': wrapper, 'tuyadevice': api, 'tuyadevicelock': lock}
    # Provenance of the fake client is covered separately; the control-flow
    # fingerprint of the REAL pinned wrapper class is NOT patched here.
    monkeypatch.setattr(tuya_bridge, 'check_loaded_source', lambda *args: True)
    monkeypatch.setattr(tuya_bridge.TuyaBridge, 'BACKOFF', 0.05)
    # Test-only: shorten the pinned module's 5 s backoff sleep (module-local proxy).
    monkeypatch.setattr(device_module, 'asyncio', _FastSleep())
    wrapper_class = type(wrapper)
    wrapper.actually_start()
    bridge = await tuya_bridge.async_attach(hass, source_entry, registry_device) if attach else None
    ctx = SimpleNamespace(hass=hass, wrapper=wrapper, api=api, lock=lock, bridge=bridge,
                          child=child, gates=[], registry_device=registry_device)
    ctx._wrapper_class = wrapper_class
    return ctx


async def async_def_cleanup(ctx):
    for gate in ctx.gates:
        gate.set()
    ctx.api.gate = None
    ctx.wrapper.__class__ = ctx._wrapper_class
    if ctx.bridge is not None and ctx.bridge._active:
        await ctx.bridge.async_detach()
    task = ctx.wrapper._refresh_task
    if task is not None and task.done():
        ctx.wrapper._refresh_task = None
    await ctx.wrapper.async_stop()


@pytest.fixture
async def owned(hass, source_entry, monkeypatch):
    ctx = await create_owned(hass, source_entry, monkeypatch)
    yield ctx
    await async_def_cleanup(ctx)


async def test_claim_owns_single_receiver_and_detach_restores_upstream(owned):
    from custom_components.petsafe_freshfeed import tuya_compat as compat
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    old = wrapper._refresh_task
    await until(lambda: api.calls.count('receive') >= 2)
    reports = []
    bridge.subscribe(reports.append, lambda *args: None)
    await bridge.async_claim_receive()
    assert old.done() and not old.cancelled()  # drained gracefully, never abandoned
    monitor = wrapper._refresh_task
    assert monitor is not old and not monitor.done() and wrapper._running is True
    adapter = vars(wrapper)['async_receive']
    assert compat.owned_original(wrapper, 'async_receive').__func__ is type(wrapper).async_receive
    assert compat.check_receive_control_flow(wrapper)
    await bridge.async_claim_receive()  # idempotent: no second receiver
    assert wrapper._refresh_task is monitor and vars(wrapper)['async_receive'] is adapter
    api.reset()
    # Ordinary polls and heartbeats are preserved under the takeover.
    await until(lambda: 'status' in api.calls and api.calls.count('heartbeat') >= 2
                and api.calls.count('receive') >= 2)
    ctx.child.received.clear()
    api.inbox.put({'dps': {'1': '/w==', '4': 'feeding'}})
    pushed = ({'1': '/w==', '4': 'feeding'}, False)
    await until(lambda: pushed in ctx.child.received)
    assert ctx.child.received.count(pushed) == 1
    assert wrapper._cached_state['1'] == '/w==' and wrapper._cached_state['4'] == 'feeding'
    await until(lambda: any(report.dps == pushed[0] for report in reports))
    sequences = [report.sequence for report in reports]
    assert sequences == sorted(set(sequences))
    for _ in range(5):
        await exchange(ctx)
    # Deterministic: an exchange cannot start while an owned receive is in flight.
    gate = _gate(ctx)
    await until(api.entered.is_set)
    critical = ctx.hass.async_create_task(exchange(ctx))
    await asyncio.sleep(0.05)
    assert api.calls.count('critical') == 5 and not critical.done()
    _open(ctx, gate)
    await critical
    assert api.calls.count('critical') == 6
    assert api.max_active == 1  # no monitor I/O ever overlapped a lock holder
    await bridge.async_detach()
    assert 'async_receive' not in vars(wrapper)
    assert compat.owned_original(wrapper, 'async_receive') is None
    assert monitor.done() and not monitor.cancelled()
    restarted = wrapper._refresh_task
    assert restarted not in (old, monitor) and not restarted.done() and wrapper._running
    count = api.calls.count('receive')
    await until(lambda: api.calls.count('receive') > count)


async def test_lock_held_until_receive_settles_even_when_monitor_cancelled(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await bridge.async_claim_receive()
    await until(lambda: api.calls.count('receive') >= 2)
    api.reset()
    gate = _gate(ctx)
    await until(api.entered.is_set)
    monitor = wrapper._refresh_task
    critical = ctx.hass.async_create_task(exchange(ctx))
    await asyncio.sleep(0.05)
    assert 'critical' not in api.calls and ctx.lock.locked()
    monitor.cancel()
    await asyncio.sleep(0.05)
    # Cancellation does not release the lock while the executor receive runs.
    assert not monitor.done() and 'critical' not in api.calls and ctx.lock.locked()
    _open(ctx, gate)
    await critical
    await until(monitor.done)
    assert monitor.cancelled()
    assert api.max_active == 1
    assert wrapper._running is False and api.persist_calls[-1] is False
    await bridge.async_detach()
    # A stopping source gets its method back but is not restarted.
    assert 'async_receive' not in vars(wrapper) and wrapper._refresh_task is monitor


async def test_cancelled_claim_while_original_receive_drains_restores_original(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await until(lambda: api.calls.count('receive') >= 1)
    gate = _gate(ctx)
    await until(api.entered.is_set)
    old = wrapper._refresh_task
    claim = ctx.hass.async_create_task(bridge.async_claim_receive())
    await until(lambda: wrapper._running is False)
    await asyncio.sleep(0.05)
    assert not claim.done() and not old.done()
    assert not await bridge.async_wait_idle(0.05)
    claim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await claim
    assert wrapper._running is True and wrapper._refresh_task is old and not old.done()
    assert 'async_receive' not in vars(wrapper) and not ctx.lock.locked()
    _open(ctx, gate)
    count = api.calls.count('receive')
    await until(lambda: api.calls.count('receive') > count + 1)
    assert not old.done()
    assert await bridge.async_wait_idle()


async def test_claim_waits_for_original_receive_before_owning(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await until(lambda: api.calls.count('receive') >= 1)
    gate = _gate(ctx)
    await until(api.entered.is_set)
    old = wrapper._refresh_task
    claim = ctx.hass.async_create_task(bridge.async_claim_receive())
    await until(lambda: wrapper._running is False)
    await asyncio.sleep(0.05)
    assert not claim.done() and 'async_receive' not in vars(wrapper)
    _open(ctx, gate)
    await claim
    assert old.done() and not old.cancelled() and 'async_receive' in vars(wrapper)
    assert await bridge.async_wait_idle()


async def test_monitor_exception_restores_socket_and_retry(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await bridge.async_claim_receive()
    await until(lambda: api.calls.count('receive') >= 2)
    epoch = bridge.epoch
    api.persist_calls.clear()
    api.fail_next = 'receive'
    await until(lambda: api.fail_next is None)
    await until(lambda: False in api.persist_calls and api.persist_calls[-1] is True
                and api.socket is not None)
    count = api.calls.count('receive')
    await until(lambda: api.calls.count('receive') > count)
    assert bridge.epoch > epoch  # the forced reconnect is a connection break
    assert api.socketRetryLimit == 1
    assert wrapper._running and not wrapper._refresh_task.done()
    assert 'async_receive' in vars(wrapper)


@pytest.mark.parametrize('change', ['code', 'control_flow', 'logger', 'socket', 'binding',
                                    'override', 'detached'])
async def test_claim_rejects_before_mutation(owned, monkeypatch, change):
    from custom_components.petsafe_freshfeed import tuya_bridge
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await until(lambda: api.calls.count('receive') >= 1)
    old = wrapper._refresh_task
    original_lock = ctx.hass.data['tuya_local']['fakeid']['tuyadevicelock']
    if change == 'code':
        monkeypatch.setattr(tuya_bridge, 'check_loaded_source', lambda *args: False)
    elif change == 'control_flow':
        class Patched(type(wrapper)):
            async def async_receive(self):
                yield {}
        wrapper.__class__ = Patched
    elif change == 'logger':
        _fake_log.setLevel(logging.DEBUG)
    elif change == 'socket':
        api.socket.closed = True
    elif change == 'binding':
        ctx.hass.data['tuya_local']['fakeid']['tuyadevicelock'] = asyncio.Lock()
    elif change == 'override':
        wrapper.async_receive = lambda: None
    elif change == 'detached':
        await bridge.async_detach()
    try:
        with pytest.raises(ValueError, match='tuya_source_incompatible'):
            await bridge.async_claim_receive()
        assert wrapper._running is True and wrapper._refresh_task is old and not old.done()
        assert ('async_receive' in vars(wrapper)) == (change == 'override')
        assert not ctx.lock.locked()
    finally:
        _fake_log.setLevel(logging.INFO)
        ctx.hass.data['tuya_local']['fakeid']['tuyadevicelock'] = original_lock
        if change == 'override':
            del wrapper.async_receive


async def test_obsolete_adapter_generation_does_nothing(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await bridge.async_claim_receive()
    adapter = vars(wrapper)['async_receive']
    await bridge.async_detach()
    persist = list(api.persist_calls)
    generator = adapter()
    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()  # returns without suspending: nothing can interleave
    assert api.persist_calls == persist


async def test_source_stop_while_claimed_restores_without_restart(owned):
    wrapper, bridge = owned.wrapper, owned.bridge
    await bridge.async_claim_receive()
    monitor = wrapper._refresh_task
    await wrapper.async_stop()
    assert monitor.done() and not monitor.cancelled()
    assert wrapper._refresh_task is None and wrapper._running is False
    await bridge.async_detach()
    assert 'async_receive' not in vars(wrapper)
    assert wrapper._refresh_task is None and wrapper._running is False


async def test_sequence_assigned_on_owning_loop_and_obsolete_queue_dropped(hass):
    from custom_components.petsafe_freshfeed.tuya_bridge import TuyaBridge
    api = Api()
    bridge = TuyaBridge(hass, SimpleNamespace(_api=api), api, asyncio.Lock())
    bridge.install()
    seen = []
    bridge.subscribe(lambda report: seen.append((report.sequence, threading.get_ident())),
                     lambda *args: None)
    worker = threading.Thread(target=api._cache_response, args=({'dps': {'1': '/w=='}},))
    worker.start()
    worker.join()  # the loop is blocked here: nothing may be numbered by the worker
    assert bridge.last_sequence == 0 and not seen
    await hass.async_block_till_done()
    assert seen == [(1, threading.get_ident())] and bridge.last_sequence == 1
    worker = threading.Thread(target=api._cache_response, args=({'dps': {'1': '/w=='}},))
    worker.start()
    worker.join()
    hass.async_create_task(bridge.async_detach(), eager_start=True)
    await hass.async_block_till_done()
    assert len(seen) == 1  # queued before detach, delivered to nobody after it
    assert await bridge.async_wait_idle()
    with pytest.raises(ValueError):
        await bridge.async_wait_idle(0)


async def test_obsolete_generation_token_alone_rejects_queued_callbacks(hass):
    from datetime import datetime, timezone
    from custom_components.petsafe_freshfeed.tuya_bridge import TuyaBridge
    api = Api()
    bridge = TuyaBridge(hass, SimpleNamespace(_api=api), api, asyncio.Lock())
    bridge.install()
    seen = []
    bridge.subscribe(seen.append, lambda *args: None)
    now = datetime.now(timezone.utc)
    # Live subscriber, active, not detaching: only the token differs.
    bridge._events.append((object(), ('report', now, {'1': '/w=='})))
    bridge._flush()
    assert seen == [] and bridge.last_sequence == 0
    bridge._events.append((bridge._generation, ('report', now, {'1': '/w=='})))
    bridge._flush()
    assert [report.sequence for report in seen] == [1]
    await bridge.async_detach()


async def test_detach_drains_monitor_upstream_restarted_over_adapter(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await bridge.async_claim_receive()
    await wrapper.async_stop()  # upstream stop clears children and the task
    wrapper.register_entity(ctx.child)  # upstream restart runs OUR adapter
    restarted = wrapper._refresh_task
    assert restarted is not None and not restarted.done() and wrapper._running
    await bridge.async_claim_receive()  # still owned: adopts, no second receiver
    await until(lambda: api.calls.count('receive') >= 1)
    await bridge.async_detach()
    assert restarted.done() and 'async_receive' not in vars(wrapper)
    ordinary = wrapper._refresh_task
    assert ordinary is not restarted and not ordinary.done() and wrapper._running
    count = api.calls.count('status') + api.calls.count('receive')
    await until(lambda: api.calls.count('status') + api.calls.count('receive') > count)


async def test_aborted_claim_never_resurrects_a_stopping_source(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await until(lambda: api.calls.count('receive') >= 1)
    gate = _gate(ctx)
    await until(api.entered.is_set)
    claim = ctx.hass.async_create_task(bridge.async_claim_receive())
    await until(lambda: wrapper._running is False)
    stop = ctx.hass.async_create_task(wrapper.async_stop())
    await until(lambda: not wrapper._children)
    claim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await claim
    assert wrapper._running is False  # upstream's stop is not undone
    _open(ctx, gate)
    await stop
    assert wrapper._refresh_task is None and wrapper._running is False
    assert 'async_receive' not in vars(wrapper)


async def test_detach_aborts_inflight_claim_promptly(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await until(lambda: api.calls.count('receive') >= 1)
    gate = _gate(ctx)
    await until(api.entered.is_set)
    old = wrapper._refresh_task
    claim = ctx.hass.async_create_task(bridge.async_claim_receive())
    await until(lambda: wrapper._running is False)
    detach = ctx.hass.async_create_task(bridge.async_detach())
    with pytest.raises(ValueError, match='tuya_source_incompatible'):
        await claim  # resolves while the original receive is still blocked
    await detach
    assert not old.done() and wrapper._running is True and wrapper._refresh_task is old
    assert 'async_receive' not in vars(wrapper)
    _open(ctx, gate)
    count = api.calls.count('receive')
    await until(lambda: api.calls.count('receive') > count)


async def test_upstream_restart_during_claim_wait_fails_promptly(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await until(lambda: api.calls.count('receive') >= 1)
    gate = _gate(ctx)
    await until(api.entered.is_set)
    old = wrapper._refresh_task
    claim = ctx.hass.async_create_task(bridge.async_claim_receive())
    await until(lambda: wrapper._running is False)
    wrapper.register_entity(Child())  # upstream start() sets _running again
    with pytest.raises(ValueError, match='tuya_source_incompatible'):
        await asyncio.wait_for(claim, 2)  # well before the 60 s stop bound
    assert wrapper._running is True and wrapper._refresh_task is old and not old.done()
    assert 'async_receive' not in vars(wrapper)
    _open(ctx, gate)


async def test_ownership_registration_failure_after_stop_restores_upstream(owned, monkeypatch):
    from custom_components.petsafe_freshfeed import tuya_bridge
    wrapper, api, bridge = owned.wrapper, owned.api, owned.bridge
    await until(lambda: api.calls.count('receive') >= 1)
    old = wrapper._refresh_task

    def refuse(*args):
        raise ValueError('tuya_source_incompatible')
    monkeypatch.setattr(tuya_bridge, 'own_attribute', refuse)
    with pytest.raises(ValueError, match='tuya_source_incompatible'):
        await bridge.async_claim_receive()
    assert old.done() and not old.cancelled()  # it was stopped by the claim
    restarted = wrapper._refresh_task
    assert restarted is not old and not restarted.done() and wrapper._running is True
    assert 'async_receive' not in vars(wrapper)
    count = api.calls.count('status') + api.calls.count('receive')
    await until(lambda: api.calls.count('status') + api.calls.count('receive') > count)


async def test_partial_install_failure_rolls_back_every_hook(hass, monkeypatch):
    from custom_components.petsafe_freshfeed import tuya_compat as compat
    from custom_components.petsafe_freshfeed.tuya_bridge import TuyaBridge, _HOOKS
    api = Api()
    bridge = TuyaBridge(hass, SimpleNamespace(_api=api), api, asyncio.Lock())
    original = TuyaBridge._install_hook

    def flaky(self, name):
        if name == _HOOKS[2]:
            raise RuntimeError('synthetic')
        original(self, name)
    monkeypatch.setattr(TuyaBridge, '_install_hook', flaky)
    with pytest.raises(ValueError, match='tuya_source_incompatible'):
        bridge.install()
    assert not any(name in api.__dict__ for name in _HOOKS)
    assert all(compat.owned_original(api, name) is None for name in _HOOKS)
    assert bridge._installed == {} and bridge._active is False


async def test_stop_timeout_aborts_claim_and_restores(owned, monkeypatch):
    from custom_components.petsafe_freshfeed import tuya_bridge
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    monkeypatch.setattr(tuya_bridge, '_STOP_TIMEOUT', 0.1)
    await until(lambda: api.calls.count('receive') >= 1)
    gate = _gate(ctx)
    await until(api.entered.is_set)
    old = wrapper._refresh_task
    with pytest.raises(ValueError, match='tuya_source_incompatible'):
        await bridge.async_claim_receive()
    assert wrapper._running is True and wrapper._refresh_task is old and not old.done()
    assert 'async_receive' not in vars(wrapper)
    _open(ctx, gate)


async def test_force_dps_polls_alternate_with_status(owned):
    wrapper, api, bridge = owned.wrapper, owned.api, owned.bridge
    wrapper._force_dps = [4]
    wrapper._POLLING_INTERVAL = 0.05
    await bridge.async_claim_receive()
    api.reset()
    await until(lambda: len([c for c in api.calls if c in ('status', 'updatedps')]) >= 6)
    polls = [c for c in api.calls if c in ('status', 'updatedps')][:6]
    assert all(a != b for a, b in zip(polls, polls[1:])), polls


async def test_error_replies_follow_pinned_semantics(owned):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    await bridge.async_claim_receive()
    await until(lambda: api.calls.count('receive') >= 2)
    failures = wrapper._api_working_protocol_failures
    api.inbox.put({'Err': '904', 'Error': 'synthetic'})
    await until(lambda: api.inbox.empty())
    count = api.calls.count('receive')
    await until(lambda: api.calls.count('receive') > count + 1)
    assert wrapper._api_working_protocol_failures == failures  # 904 means no data
    api.persist_calls.clear()
    ctx.child.received.clear()
    api.inbox.put({'Err': '901', 'Error': 'synthetic'})
    await until(lambda: wrapper._api_working_protocol_failures == failures + 1)
    await until(lambda: False in api.persist_calls and api.persist_calls[-1] is True)
    assert all('Err' not in poll for poll, _ in ctx.child.received)


@pytest.mark.parametrize('stage', ['heartbeat', 'status'])
async def test_lock_held_through_cancelled_heartbeat_or_poll(owned, stage):
    ctx, wrapper, api, bridge = owned, owned.wrapper, owned.api, owned.bridge
    # The first owned iteration is a status poll (the takeover reset the
    # cache). The original monitor polls status once at start and then not for
    # 30 s, so once that first poll is seen a status gate set before the claim
    # can only catch OUR poll. Heartbeats run in both monitors, so that gate is
    # set only once ownership is complete.
    if stage == 'status':
        await until(lambda: 'status' in api.calls and api.calls.count('receive') >= 1)
        gate = _gate(ctx, stage)
        await bridge.async_claim_receive()
    else:
        await bridge.async_claim_receive()
        gate = _gate(ctx, stage)
    await until(api.entered.is_set)
    api.reset()
    monitor = wrapper._refresh_task
    critical = ctx.hass.async_create_task(exchange(ctx))
    await asyncio.sleep(0.05)
    monitor.cancel()
    await asyncio.sleep(0.05)
    assert 'critical' not in api.calls and ctx.lock.locked() and not monitor.done()
    _open(ctx, gate)
    await critical
    await until(monitor.done)
    assert monitor.cancelled() and api.max_active == 1


# ---------------------------------------------------------------------------
# Task 5: send-boundary watermark and decoded critical responses, on the SAME
# owned client and bridge stream. Synthetic DPS only; no network.

class _Witness:
    def __init__(self):
        self.record = None
        self.gate = None
        self.entered = asyncio.Event()

    async def async_load_last_intent(self):
        return None if self.record is None else dict(self.record)

    async def async_prepare_intent(self, record):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        self.record = dict(record)


def _frame(dps):
    import json
    return SimpleNamespace(payload=json.dumps({'dps': dps}).encode(), crc_good=True)


async def _enabled(ctx):
    """Owned receive plus a transport whose marks and reports are recorded."""
    log = []
    ctx.log = log
    ctx.witness = _Witness()
    ctx.bridge.subscribe(lambda report: log.append(('report', report)), lambda *args: None)

    def on_dispatch(mark):
        assert ctx.lock.locked()  # send linearization point holds the API lock
        log.append(('mark', mark))
    await _owned_current(ctx)
    return await ctx.bridge.async_enable_transport(ctx.witness, on_dispatch=on_dispatch)


async def _owned_current(ctx):
    # Claiming ownership costs one ordinary reconnect; enabling requires the
    # owned connection to be current again.
    await until(lambda: ctx.api.calls.count('receive') >= 1 and ctx.bridge.is_current())
    await ctx.bridge.async_claim_receive()
    await until(ctx.bridge.is_current)


def _marks(ctx):
    return [item for kind, item in ctx.log if kind == 'mark']


def _reports(ctx, value):
    return [item for kind, item in ctx.log if kind == 'report' and item.dps.get('1') == value]


async def test_early_echo_is_post_mark_and_pre_dispatch_report_is_not(owned):
    from datetime import datetime, timezone
    from custom_components.petsafe_freshfeed.tuya_bridge import DispatchMark
    ctx, api, bridge = owned, owned.api, owned.bridge
    transport = await _enabled(ctx)
    epoch = bridge.epoch
    ctx.witness.gate = asyncio.Event()

    def before_send():
        # Worker-side push during payload generation, plus an owning-loop
        # backlog item that was never scheduled for a flush.
        api._cache_response({'dps': {'1': 'during-generation'}})
        with bridge._guard:
            bridge._events.append((bridge._generation, (
                'report', datetime.now(timezone.utc), {'1': 'backlog'})))
    api.before_send = before_send
    api.response = _frame({'1': 'echo', '4': 'standby'})
    returned = []
    holder = {}
    bridge.subscribe(lambda report: report.dps.get('1') == 'echo' and returned.append(
        holder['send'].done()), lambda *args: None)
    sock = api.socket
    send = holder['send'] = ctx.hass.async_create_task(
        transport.async_send('schedule', {'1': '/w=='}))
    await ctx.witness.entered.wait()
    # A matching report while preparation waits is pre-dispatch.
    threading.Thread(target=api._cache_response,
                     args=({'dps': {'1': 'during-prepare'}},)).start()
    await until(lambda: _reports(ctx, 'during-prepare'))
    ctx.witness.gate.set()
    receipt = await send
    marks = _marks(ctx)
    assert len(marks) == 1 and type(marks[0]) is DispatchMark
    mark = marks[0]
    assert mark.operation_id == receipt.operation_id and mark.epoch == epoch
    for value in ('during-prepare', 'during-generation', 'backlog'):
        assert len(_reports(ctx, value)) == 1, value
        (report,) = _reports(ctx, value)
        assert report.sequence <= mark.sequence and ctx.log.index(('report', report)) < \
            ctx.log.index(('mark', mark))
    (echo,) = _reports(ctx, 'echo')  # decoded exactly once, not also via the cache hook
    assert echo.epoch == mark.epoch and echo.sequence > mark.sequence
    assert echo.ack_eligible is False
    assert all(_reports(ctx, value)[0].ack_eligible is True
               for value in ('during-prepare', 'during-generation', 'backlog'))
    assert returned == [False]  # observable before the submit returned
    assert sock.writes == [b'synthetic-control'] and api.socket is sock
    assert bridge.epoch == epoch  # the transport's socket guard is not a reconnect
    assert receipt.outcome == 'unknown' and transport.needs_reconciliation
    assert await transport.async_wait_idle()


async def test_unflushed_backlog_is_numbered_before_the_mark(owned):
    from datetime import datetime, timezone
    ctx, api, bridge = owned, owned.api, owned.bridge
    transport = await _enabled(ctx)

    def backlog():
        # Decoded but no flush scheduled yet: the boundary itself must drain it.
        with bridge._guard:
            bridge._events.append((bridge._generation, (
                'report', datetime.now(timezone.utc), {'1': 'backlog'})))
    api.before_send = backlog
    api.response = None
    await transport.async_send('schedule', {'1': '/w=='})
    bridge._flush()
    (mark,) = _marks(ctx)
    (report,) = _reports(ctx, 'backlog')
    assert report.sequence <= mark.sequence


async def test_epoch_replacement_during_handoff_forbids_send(owned):
    ctx, api, bridge = owned, owned.api, owned.bridge
    transport = await _enabled(ctx)

    def replace():
        with bridge._guard:  # a break observed but not yet numbered on the loop
            bridge._events.append((bridge._generation, ('socket', True)))
            bridge._pending_sockets += 1
    api.before_send = replace
    api.response = _frame({'1': 'echo'})
    sock = api.socket
    receipt = await transport.async_send('schedule', {'1': '/w=='})
    assert _marks(ctx) == [] and sock.writes == [] and not _reports(ctx, 'echo')
    assert receipt.reason == 'dispatch_handoff_failed' and not receipt.attempted
    assert transport.needs_reconciliation


class _Captured(logging.Handler):
    """Root capture that never changes any logger's level (caplog may)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.text = ''

    def emit(self, record):
        self.text += record.getMessage() + repr(record.exc_info) + '\n'

    def __enter__(self):
        logging.getLogger().addHandler(self)
        return self

    def __exit__(self, *args):
        logging.getLogger().removeHandler(self)


async def test_on_dispatch_failure_forbids_send(owned):
    ctx, api, bridge = owned, owned.api, owned.bridge
    await _owned_current(ctx)

    def fail(mark):
        raise RuntimeError('synthetic-private-dispatch')
    transport = await bridge.async_enable_transport(_Witness(), on_dispatch=fail)
    api.response = _frame({'1': 'echo'})
    sock = api.socket
    with _Captured() as captured:
        receipt = await transport.async_send('schedule', {'1': '/w=='})
    assert sock.writes == [] and receipt.reason == 'dispatch_handoff_failed'
    assert 'synthetic-private' not in captured.text
    assert transport.needs_reconciliation


async def test_cancelled_caller_during_handoff_forbids_send(owned):
    ctx, api, bridge = owned, owned.api, owned.bridge
    transport = await _enabled(ctx)
    holder = {}
    api.before_send = lambda: ctx.hass.loop.call_soon_threadsafe(holder['task'].cancel)
    sock = api.socket
    holder['task'] = ctx.hass.async_create_task(transport.async_send('schedule', {'1': '/w=='}))
    with pytest.raises(asyncio.CancelledError):
        await holder['task']
    assert await transport.async_wait_idle()
    assert _marks(ctx) == [] and sock.writes == []
    assert transport.needs_reconciliation


async def test_decoder_failure_keeps_uncertainty_without_private_text(owned):
    ctx, api, bridge = owned, owned.api, owned.bridge
    transport = await _enabled(ctx)
    api.decode_error = True
    api.response = _frame({'1': 'echo'})
    with _Captured() as captured:
        receipt = await transport.async_send('schedule', {'1': '/w=='})
    assert len(_marks(ctx)) == 1 and not _reports(ctx, 'echo')
    assert receipt.outcome == 'unknown' and receipt.reason == 'response_undecodable'
    assert transport.needs_reconciliation and 'synthetic-private' not in captured.text


async def test_enable_requires_owned_current_source(owned):
    ctx, api, bridge = owned, owned.api, owned.bridge
    await bridge.async_detach()
    with pytest.raises(ValueError, match='tuya_source_incompatible'):
        await bridge.async_enable_transport(_Witness(), on_dispatch=lambda mark: None)


async def test_detached_bridge_forbids_send(owned):
    ctx, api, bridge = owned, owned.api, owned.bridge
    transport = await _enabled(ctx)
    sock = api.socket
    await bridge.async_detach()
    receipt = await transport.async_send('schedule', {'1': '/w=='})
    assert sock.writes == []
    assert receipt.outcome == 'not_sent' and _marks(ctx) == []
