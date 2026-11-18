"""PetSafe-owned TinyTuya source: injected sockets and encrypted v3.5 frames.

No network, device or real credential. Tuya Local is never imported: the
pinned TinyTuya library alone is loaded from PETSAFE_PINNED_TINYTUYA_SITE with
scoped sys.path/sys.modules restoration, and a meta-path blocker refuses any
``custom_components.tuya_local`` import for the duration of each test.
"""
import asyncio
from hashlib import sha256
import hmac
import importlib
import importlib.abc
import json
import logging
import os
from pathlib import Path
import socket as real_socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from custom_components.petsafe_freshfeed import owned_compat, owned_source
from custom_components.petsafe_freshfeed.critical_transport import CompatibilityError
from custom_components.petsafe_freshfeed.local_source import OBSERVED_DPS
from custom_components.petsafe_freshfeed.owned_source import (
    LocalConnection, LocalConnectionError, OwnedSource, OwnedSourceError,
    UNSUPPORTED_CONFIGURATION,
)

# Synthetic values only, built so no literal resembles a credential.
KEY = "".join(chr(0x61 + i) for i in range(16))
OTHER_KEY = "".join(chr(0x41 + i) for i in range(16))
DEVICE = "bfsynthetic0123456789"
HOST = "192.168.210.77"
CONTROL_CMDS = {7, 13}
READ_ONLY_CMDS = {3, 5, 9, 16}
HEADER_35 = b"3.5" + 12 * b"\x00"
BASE_DPS = {"1": "AAAA", "4": "standby", "14": 9, "110": True, "111": 10, "125": True,
            "130": "2.5.18", "131": "7.1.0.0", "140": 0, "142": 0, "145": 6, "150": 0}
# Longest a fake socket read blocks, regardless of the client's 3 s timeout.
MAX_WAIT = 0.05


def connection(**overrides):
    values = {"device_id": DEVICE, "host": HOST, "local_key": KEY, "version": "3.5"}
    values.update(overrides)
    return LocalConnection(**values)


class _BlockTuyaLocal(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "custom_components.tuya_local" or name.startswith("custom_components.tuya_local."):
            raise ImportError("tuya_local is blocked in standalone tests")
        return None


def _vendor_modules():
    return {name for name in sys.modules if name == "tinytuya" or name.startswith("tinytuya.")}


@pytest.fixture
def no_tuya_local(monkeypatch):
    for name in tuple(sys.modules):
        if name == "custom_components.tuya_local" or name.startswith("custom_components.tuya_local."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BlockTuyaLocal(), *sys.meta_path])


@pytest.fixture
def pinned(monkeypatch, no_tuya_local):
    """The exact pinned TinyTuya, scoped to this test; never Tuya Local."""
    site_env = os.environ.get("PETSAFE_PINNED_TINYTUYA_SITE", "")
    if not site_env:
        pytest.skip("PETSAFE_PINNED_TINYTUYA_SITE is not configured")
    site = Path(site_env)
    # Configured but absent is a broken verification run, never a skip.
    assert (site / "tinytuya").is_dir()
    saved = {name: sys.modules[name] for name in _vendor_modules()}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(site))
    try:
        tinytuya = importlib.import_module("tinytuya")
        assert Path(tinytuya.__file__).resolve().is_relative_to(site.resolve())
        yield tinytuya
    finally:
        for name in _vendor_modules():
            del sys.modules[name]
        sys.modules.update(saved)


class FakeSocket:
    """One injected TCP connection to a FakeDevice; thread-safe."""

    def __init__(self, device):
        self.device = device
        self.timeout = None
        self.closed = False
        self.peer_closed = False
        self.buffer = bytearray()
        self.cond = threading.Condition()
        self.session_key = None
        self.local_nonce = self.remote_nonce = None

    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def fileno(self):
        return -1 if self.closed else 1000

    def connect(self, address):
        self.device.addresses.append(address)
        if self.device.refuse:
            raise ConnectionRefusedError("synthetic refusal")

    def sendall(self, data):
        with self.device.io():
            if self.closed or self.peer_closed:
                raise BrokenPipeError("synthetic closed")
            self.device.from_client(self, bytes(data))

    def recv(self, size):
        with self.device.io():
            gate = self.device.take_gate()
            if gate is not None:
                self.device.entered.set()
                gate.wait(10)
            error = self.device.recv_error
            if error is not None:
                self.device.recv_error = None
                raise error
            deadline = time.monotonic() + min(self.timeout or MAX_WAIT, MAX_WAIT)
            with self.cond:
                while not self.buffer:
                    if self.closed:
                        raise OSError("synthetic closed")
                    if self.peer_closed:
                        return b""
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise real_socket.timeout("timed out")
                    self.cond.wait(remaining)
                chunk = bytes(self.buffer[:size])
                del self.buffer[:size]
                return chunk

    def push(self, data):
        with self.cond:
            self.buffer.extend(data)
            self.cond.notify_all()

    def drop(self):
        with self.cond:
            self.peer_closed = True
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()


class FakeDevice:
    """Device side of TinyTuya protocol 3.5 (session negotiation, GCM frames)."""

    def __init__(self, tinytuya, *, key=KEY, protocol="3.5", status_extra=None, dps=None):
        self.mh = importlib.import_module("tinytuya.core.message_helper")
        self.cipher = importlib.import_module("tinytuya.core.crypto_helper").AESCipher
        self.key = key.encode("latin1")
        self.protocol = protocol
        self.status_extra = dict(status_extra or {})
        self.dps = dict(BASE_DPS if dps is None else dps)
        self.commands, self.queries, self.controls = [], [], []
        self.sockets, self.addresses = [], []
        self.refuse = False
        self.on_control = None
        self.recv_error = None
        self.entered = threading.Event()
        self._gate = None
        self._seq = 100
        self._guard = threading.Lock()
        self.active = self.max_active = 0

    def install(self, monkeypatch):
        xenon = sys.modules["tinytuya.core.XenonDevice"]
        monkeypatch.setattr(xenon, "socket", SimpleNamespace(
            socket=self.socket_factory, timeout=real_socket.timeout,
            AF_INET=real_socket.AF_INET, AF_INET6=real_socket.AF_INET6,
            SOCK_STREAM=real_socket.SOCK_STREAM, IPPROTO_TCP=real_socket.IPPROTO_TCP,
            TCP_NODELAY=real_socket.TCP_NODELAY))
        return self

    def socket_factory(self, family, kind):
        sock = FakeSocket(self)
        self.sockets.append(sock)
        return sock

    def io(self):
        device = self

        class _Io:
            def __enter__(self):
                with device._guard:
                    device.active += 1
                    device.max_active = max(device.max_active, device.active)

            def __exit__(self, *exc):
                with device._guard:
                    device.active -= 1
        return _Io()

    def gate_next_recv(self):
        self.entered.clear()
        self._gate = threading.Event()
        return self._gate

    def take_gate(self):
        with self._guard:
            gate, self._gate = self._gate, None
        return gate

    def control_frames(self):
        return [cmd for cmd in self.commands if cmd in CONTROL_CMDS]

    def frame(self, cmd, payload, key):
        self._seq += 1
        message = self.mh.TuyaMessage(self._seq, cmd, 0, payload, 0, True, 0x6699, True)
        return self.mh.pack_message(message, hmac_key=key)

    def push_status(self, dps):
        body = HEADER_35 + json.dumps({"dps": dps}).encode()
        for sock in self.sockets:
            if sock.session_key is not None and not sock.closed and not sock.peer_closed:
                sock.push(self.frame(8, body, sock.session_key))

    def from_client(self, sock, data):
        while data:
            header = self.mh.parse_header(data)
            raw, data = data[:header.total_length], data[header.total_length:]
            if self.protocol != "3.5" or header.prefix != 0x6699:
                self.commands.append(header.cmd)
                sock.drop()  # an older-protocol device cannot answer 3.5 frames
                return
            key = sock.session_key or self.key
            message = self.mh.unpack_message(raw, hmac_key=key, header=header, no_retcode=True)
            self.commands.append(message.cmd)
            if not message.crc_good:
                sock.drop()
                return
            if message.cmd == 3:
                sock.local_nonce = message.payload[:16]
                sock.remote_nonce = os.urandom(16)
                proof = hmac.new(self.key, sock.local_nonce, sha256).digest()
                sock.push(self.frame(4, sock.remote_nonce + proof, self.key))
            elif message.cmd == 5:
                if message.payload != hmac.new(self.key, sock.remote_nonce, sha256).digest():
                    sock.drop()
                    return
                mixed = bytes(a ^ b for a, b in zip(sock.local_nonce, sock.remote_nonce))
                sock.session_key = self.cipher(self.key).encrypt(
                    mixed, use_base64=False, pad=False, iv=sock.local_nonce[:12])[12:28]
            elif message.cmd == 16:
                self.queries.append(message.payload)
                body = {"dps": dict(self.dps), **self.status_extra}
                sock.push(self.frame(16, json.dumps(body).encode(), sock.session_key))
            elif message.cmd == 9:
                sock.push(self.frame(9, b"", sock.session_key))
            elif message.cmd in CONTROL_CMDS:
                payload = json.loads(message.payload[len(HEADER_35):])
                self.controls.append(payload)
                if self.on_control is not None:
                    self.on_control(sock, payload)
                else:
                    sock.push(self.frame(13, b"", sock.session_key))


class Witness:
    def __init__(self, record=None):
        self.record, self.prepared = record, []

    async def async_load_last_intent(self):
        return None if self.record is None else dict(self.record)

    async def async_prepare_intent(self, record):
        self.prepared.append(dict(record))
        self.record = dict(record)


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def fast(source):
    source.BACKOFF, source.IDLE = 0.05, 0.005
    return source


async def start(hass, device, **kwargs):
    source = fast(OwnedSource(hass, connection(**kwargs)))
    await source.async_start()
    return source


# --------------------------------------------------------------------------
# Standalone startup and read-only scope


async def test_start_without_tuya_local(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    reports = []
    source = fast(OwnedSource(hass, connection()))
    source.subscribe(reports.append, lambda *args: None)
    await source.async_start()
    try:
        assert not any(name == "custom_components.tuya_local"
                       or name.startswith("custom_components.tuya_local.") for name in sys.modules)
        assert "tuya_local" not in hass.data
        assert device.addresses == [(HOST, 6668)]
        assert source.is_current() and source.connected
        assert source.api.version == 3.5
        assert source.api.socketRetryLimit == 1 and source.api.socketRetryDelay == 0
        assert source.api.connection_timeout == 3 and source.api.socketPersistent is True
        assert source.snapshot_dps() == {k: v for k, v in BASE_DPS.items() if k in OBSERVED_DPS}
        assert reports and all(report.epoch == source.epoch for report in reports)
        assert await hass.async_add_executor_job(source.code_ok) is True
    finally:
        assert await source.async_stop() is True
    assert not source.is_current()
    assert all(sock.closed for sock in device.sockets)


async def test_start_sends_no_control(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = fast(OwnedSource(hass, connection()))
    source.HEARTBEAT_INTERVAL = 0.02
    await source.async_start()
    try:
        await until(lambda: device.commands.count(9) >= 2)
        device.push_status({"4": "feeding"})
        await until(lambda: source.snapshot_dps().get("4") == "feeding")
    finally:
        assert await source.async_stop() is True
    # Audited wire commands: session negotiation, DP_QUERY_NEW and HEART_BEAT.
    assert set(device.commands) <= READ_ONLY_CMDS
    assert {3, 5, 16, 9} <= set(device.commands)
    assert device.control_frames() == [] and device.controls == []


async def test_no_dps_enumeration(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    try:
        await asyncio.sleep(0.1)
        api = source.api
        assert api.dps_to_request == {} and api.dev_type == "default"
    finally:
        assert await source.async_stop() is True
    assert 18 not in device.commands  # UPDATEDPS never sent
    # status() is the generic read-only query: no request-level DP filter.
    assert device.queries and all(payload == b"{}" for payload in device.queries)
    text = Path(owned_source.__file__).read_text()
    for forbidden in ("detect_available_dps", "updatedps", "add_dps_to_request", "set_dpsUsed"):
        assert forbidden not in text


async def test_authenticated_device_id_mismatch_refused(hass, pinned, monkeypatch):
    device = FakeDevice(pinned, status_extra={"devId": "bfsomeoneelse0000000"}).install(monkeypatch)
    reports = []
    source = fast(OwnedSource(hass, connection()))
    source.subscribe(reports.append, lambda *args: None)
    with pytest.raises(OwnedSourceError) as raised:
        await source.async_start()
    assert raised.value.args == ("local_admission_failed",)
    await hass.async_block_till_done()
    assert reports == [] and source.snapshot_dps() == {}
    assert not source.is_current()
    assert device.control_frames() == []
    assert all(sock.closed or sock.peer_closed for sock in device.sockets)
    # The refused source released its claim; a matching identity is admitted.
    good = FakeDevice(pinned, status_extra={"devId": DEVICE}).install(monkeypatch)
    source = await start(hass, good)
    assert source.is_current()
    assert await source.async_stop() is True


async def test_wrong_key_refused_without_control(hass, pinned, monkeypatch):
    device = FakeDevice(pinned, key=OTHER_KEY).install(monkeypatch)
    source = fast(OwnedSource(hass, connection()))
    with pytest.raises(OwnedSourceError) as raised:
        await source.async_start()
    assert raised.value.args == ("local_admission_failed",)
    assert device.commands and set(device.commands) <= {3}
    assert len(device.addresses) == 1  # one bounded attempt, no retry loop


async def test_owned_refuses_protocol_31_33_34(hass, pinned, monkeypatch):
    for version in ("3.1", "3.3", "3.4"):
        with pytest.raises(LocalConnectionError) as raised:
            connection(version=version)
        assert raised.value.args == (UNSUPPORTED_CONFIGURATION,)
    with pytest.raises(OwnedSourceError):
        OwnedSource(hass, SimpleNamespace(device_id=DEVICE, host=HOST, local_key=KEY,
                                          version="3.3"))
    # A device that does not speak 3.5 fails admission; never auto-downgrade.
    device = FakeDevice(pinned, protocol="3.4").install(monkeypatch)
    built = []

    def factory(conn):
        api = owned_source.default_client_factory(conn)
        built.append(api)
        return api
    source = fast(OwnedSource(hass, connection(), client_factory=factory))
    with pytest.raises(OwnedSourceError):
        await source.async_start()
    assert len(built) == 1 and built[0].version == 3.5
    assert len(device.addresses) == 1
    assert device.control_frames() == []


# --------------------------------------------------------------------------
# Ownership, lock and drain


async def test_one_monitor_and_shared_lock(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    try:
        monitor = source._monitor
        await source.async_claim_receive()
        await source.async_claim_receive()
        assert source._monitor is monitor and not monitor.done()
        with pytest.raises(OwnedSourceError):
            await source.async_start()
        # One connection per physical feeder, process-wide.
        second = fast(OwnedSource(hass, connection()))
        with pytest.raises(OwnedSourceError):
            await second.async_start()
        assert len(device.addresses) == 1
        marks = []
        transport = await source.async_enable_transport(Witness(), on_dispatch=marks.append)
        assert transport._lock is source.lock and transport._api is source.api
        receipt = await transport.async_send("feed", {"3": 1})
        assert receipt.outcome == "unknown" and receipt.reason == "client_returned"
        assert len(marks) == 1 and marks[0].epoch == source.epoch
        await asyncio.sleep(0.1)
    finally:
        assert await source.async_stop() is True
    assert device.max_active == 1  # monitor and critical I/O never overlap
    assert len(device.control_frames()) == 1
    assert device.controls[0]["data"]["dps"] == {"3": 1}


async def test_receive_drain_survives_cancel(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    gate = device.gate_next_recv()
    await hass.async_add_executor_job(device.entered.wait, 5)
    monitor = source._monitor
    monitor.cancel()
    await asyncio.sleep(0.1)
    # The cancelled task keeps the lock until the actual worker completes.
    assert not monitor.done() and source.lock.locked()
    gate.set()
    await asyncio.wait({monitor}, timeout=5)
    assert monitor.cancelled()
    assert not source.lock.locked()
    assert await source.async_stop() is True


async def test_stop_refuses_live_executor(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    gate = device.gate_next_recv()
    await hass.async_add_executor_job(device.entered.wait, 5)
    assert await source.async_stop(timeout=0.1) is False
    assert source.lock.locked() and not device.sockets[0].closed
    assert not source.is_current()
    # Ownership retained: no new client may connect to this feeder.
    replacement = fast(OwnedSource(hass, connection()))
    with pytest.raises(OwnedSourceError):
        await replacement.async_start()
    assert len(device.addresses) == 1
    gate.set()
    assert await source.async_stop() is True
    assert device.sockets[0].closed
    source = await start(hass, device)
    assert await source.async_stop() is True


async def test_reconnect_advances_epoch_without_control_retry(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    connections = []
    source.subscribe(lambda report: None, lambda *args: connections.append(args))
    try:
        transport = await source.async_enable_transport(Witness(), on_dispatch=lambda mark: None)
        device.on_control = lambda sock, payload: sock.drop()
        epoch = source.epoch
        receipt = await transport.async_send("feed", {"3": 1})
        assert receipt.outcome == "unknown"
        await until(lambda: len(device.addresses) == 2 and source.is_current())
        assert source.epoch > epoch
        assert (epoch + 1, False) in connections and connections[-1][1] is True
        assert transport.needs_reconciliation
        again = await transport.async_send("feed", {"3": 1})
        assert again.outcome == "not_sent" and again.reason == "blocked"
        await asyncio.sleep(0.1)
    finally:
        assert await source.async_stop() is True
    assert len(device.control_frames()) == 1  # never resent after reconnect
    assert set(device.commands) - CONTROL_CMDS <= READ_ONLY_CMDS


async def test_exchange_report_not_ack_eligible(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    reports, marks = [], []
    source.subscribe(reports.append, lambda *args: None)

    def respond(sock, payload):
        body = HEADER_35 + json.dumps({"dps": {"4": "feeding", "130": "2.5.19"}}).encode()
        sock.push(device.frame(13, body, sock.session_key))
    device.on_control = respond
    try:
        transport = await source.async_enable_transport(Witness(), on_dispatch=marks.append)
        receipt = await transport.async_send("feed", {"3": 1})
        assert receipt.reason == "client_returned"
        await until(lambda: any(r.dps.get("4") == "feeding" for r in reports))
    finally:
        assert await source.async_stop() is True
    exchange = [r for r in reports if r.sequence > marks[0].sequence
                and ("4" in r.dps or "130" in r.dps)]
    assert [(r.dps, r.ack_eligible) for r in exchange] == [
        ({"4": "feeding"}, False), ({"130": "2.5.19"}, False)]


async def test_pending_socket_break_refuses_dispatch(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    try:
        transport = await source.async_enable_transport(Witness(), on_dispatch=lambda mark: None)
        epoch = source.epoch
        async with source.lock:  # the monitor cannot observe or flush meanwhile
            # Replaced connection, seen before any flush: a pending break.
            original = source.api.socket
            source.api.socket = device.socket_factory(None, None)
            assert not source.is_current()
            with pytest.raises(CompatibilityError):
                transport._compatible()
            called = []
            with pytest.raises(ValueError):
                source._dispatch_mark(source._generation, called.append, "0" * 32)
            assert called == [] and source.epoch == epoch + 1
            # Never write on the unauthenticated replacement: drop it.
            source.api.socket.close()
            source.api.socket = None
            original.close()
        await until(lambda: source.is_current() and len(device.addresses) == 2)
        assert source.epoch > epoch + 1
    finally:
        assert await source.async_stop() is True
    assert device.control_frames() == []


async def test_owned_frame_split_no_double_delivery_obs_only_never_acks(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    reports = []
    source = fast(OwnedSource(hass, connection()))
    source.subscribe(reports.append, lambda *args: None)
    await source.async_start()
    try:
        # The admission status() frame is delivered once, from the hook only.
        assert [(set(r.dps), r.ack_eligible) for r in reports] == [
            ({"1", "4", "14", "110", "111", "125", "140", "142", "145", "150"}, True),
            ({"130", "131"}, False)]
        del reports[:]
        device.push_status({"1": "BBBB", "130": "2.5.19", "145": 2, "999": 5})
        await until(lambda: len(reports) >= 2)
        await asyncio.sleep(0.05)
    finally:
        assert await source.async_stop() is True
    assert [(r.dps, r.ack_eligible) for r in reports] == [
        ({"1": "BBBB", "145": 2}, True), ({"130": "2.5.19"}, False)]
    assert reports[1].sequence == reports[0].sequence + 1
    snapshot = source.snapshot_dps()
    assert snapshot["1"] == "BBBB" and snapshot["130"] == "2.5.19" and "999" not in snapshot


# --------------------------------------------------------------------------
# Loaded-code admission


async def test_loaded_code_mismatch_blocks_controls(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    # Not a TinyTuya client at all: refused before any connection I/O.
    source = fast(OwnedSource(hass, connection(), client_factory=lambda conn: SynthClient()))
    with pytest.raises(OwnedSourceError):
        await source.async_start()
    assert device.addresses == []
    # A genuine client whose callsite is replaced after start blocks controls.
    source = await start(hass, device)
    try:
        transport = await source.async_enable_transport(Witness(), on_dispatch=lambda mark: None)
        source.api.status = lambda *args, **kwargs: None
        assert await hass.async_add_executor_job(source.code_ok) is False
        assert not owned_compat.check_runtime_owner(source, source.api, source.lock)
        with pytest.raises(ValueError):
            await source.async_enable_transport(Witness(), on_dispatch=lambda mark: None)
        receipt = await transport.async_send("feed", {"3": 1})
        assert receipt.outcome == "not_sent" and receipt.reason == "incompatible"
        del source.api.status
    finally:
        assert await source.async_stop() is True
    assert device.control_frames() == []
    # Class-level patch of a fingerprinted method is unknown code.
    xenon = sys.modules["tinytuya.core.XenonDevice"].XenonDevice
    monkeypatch.setattr(xenon, "receive", lambda self: None)
    source = fast(OwnedSource(hass, connection()))
    with pytest.raises(OwnedSourceError):
        await source.async_start()
    assert len(device.addresses) == 1


async def test_loaded_tinytuya_version_mismatch_refuses(hass, pinned, monkeypatch):
    device = FakeDevice(pinned).install(monkeypatch)
    monkeypatch.setattr(pinned, "__version__", "1.20.1")
    source = fast(OwnedSource(hass, connection()))
    with pytest.raises(OwnedSourceError) as raised:
        await source.async_start()
    assert raised.value.args == ("owned_source_incompatible",)
    assert device.addresses == []


def test_fingerprints_are_tinytuya_rows_only():
    manifest = json.loads(Path(owned_compat.__file__).with_name("owned_fingerprints.json").read_text())
    legacy = json.loads(Path(owned_compat.__file__).with_name("tuya_fingerprints.json").read_text())
    assert manifest["tinytuya_version"] == "1.20.0"
    assert manifest["files"] == {key: value for key, value in legacy["files"].items()
                                 if key.startswith("tinytuya/")}
    text = Path(owned_compat.__file__).read_text()
    assert "tuya_fingerprints" not in text and "tuya_local" not in text
    # The synchronous logger gate covers every fingerprinted module, with no file I/O.
    modules = {key[:-3].replace("/", ".").removesuffix(".__init__") for key in manifest["files"]}
    assert set(owned_compat.VENDOR_MODULES) == modules


def test_runtime_gates_do_no_file_io(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("file I/O in a synchronous gate")
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    monkeypatch.setattr("builtins.open", refuse)
    assert owned_compat.vendor_logging_safe() is True
    owner = SimpleNamespace(api=None, lock=None, is_current=lambda: True)
    assert owned_compat.check_runtime_owner(owner, None, asyncio.Lock()) is False


# --------------------------------------------------------------------------
# Logging and secret hygiene


async def test_debug_logging_refuses_before_client_construction(hass, monkeypatch, no_tuya_local, caplog):
    built = []

    def factory(conn):
        built.append(conn)
        return SynthClient()
    monkeypatch.setattr(owned_source, "check_loaded_client", lambda api: True)
    # This test deliberately exercises inheritance from root, unlike the
    # suite's safe vendor baseline under pytest DEBUG capture.
    caplog.set_level(logging.NOTSET, logger="tinytuya")
    for name in ("tinytuya", "tinytuya.core.XenonDevice", ""):  # "" is the root logger
        logger = logging.getLogger(name)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            source = fast(OwnedSource(hass, connection(), client_factory=factory))
            with pytest.raises(OwnedSourceError) as raised:
                await source.async_start()
            assert raised.value.args == ("vendor_logging_unsafe",)
            assert logger.level == logging.DEBUG  # never silenced behind the user's back
        finally:
            logger.setLevel(previous)
    assert built == []
    # Debug logging enabled after construction also fails closed before I/O.
    client = SynthClient()

    def late(conn):
        logging.getLogger("tinytuya").setLevel(logging.DEBUG)
        return client
    try:
        source = fast(OwnedSource(hass, connection(), client_factory=late))
        with pytest.raises(OwnedSourceError):
            await source.async_start()
    finally:
        logging.getLogger("tinytuya").setLevel(logging.NOTSET)
    assert client.calls == [] and client.socket is None


async def test_debug_logging_enabled_while_running_pauses_client_io(hass, pinned, monkeypatch):
    """A runtime DEBUG level on TinyTuya stops all monitor I/O until it is lowered."""
    device = FakeDevice(pinned).install(monkeypatch)
    source = await start(hass, device)
    source.HEARTBEAT_INTERVAL = 0.02
    logger = logging.getLogger("tinytuya")
    try:
        await until(lambda: device.commands.count(9) >= 1)
        logger.setLevel(logging.DEBUG)
        await asyncio.sleep(0.1)  # let any in-flight iteration finish
        before = len(device.commands)
        await asyncio.sleep(0.3)
        assert len(device.commands) == before
        assert not source.is_current()  # frozen cache is not reported as current
        assert logger.level == logging.DEBUG  # never silenced behind the user's back
        logger.setLevel(logging.NOTSET)
        await until(lambda: len(device.commands) > before)
        await until(source.is_current)
    finally:
        logger.setLevel(logging.NOTSET)
        assert await source.async_stop() is True
    assert device.control_frames() == [] and device.controls == []



async def test_vendor_error_has_no_secret_context(hass, monkeypatch, caplog, no_tuya_local):
    caplog.set_level(logging.DEBUG, logger="custom_components.petsafe_freshfeed")
    # Capture may leave the root at DEBUG; TinyTuya is explicitly admitted at
    # WARNING here (an inherited DEBUG root would correctly refuse admission).
    caplog.set_level(logging.WARNING, logger="tinytuya")

    def factory(conn):
        raise RuntimeError(f"vendor failure for {conn.local_key} at {conn.host}")
    source = fast(OwnedSource(hass, connection(), client_factory=factory))
    with pytest.raises(OwnedSourceError) as raised:
        await source.async_start()
    error = raised.value
    assert error.__cause__ is None and error.__context__ is None
    assert KEY not in str(error) and KEY not in repr(error)
    # Runtime vendor exceptions are logged by type only.
    monkeypatch.setattr(owned_source, "check_loaded_client", lambda api: True)
    client = SynthClient(receive_error=OSError(f"socket error with key {KEY}"))
    source = fast(OwnedSource(hass, connection(), client_factory=lambda conn: client))
    await source.async_start()
    try:
        await until(lambda: client.receive_error is None)
        await asyncio.sleep(0.05)
    finally:
        assert await source.async_stop() is True
    assert "OSError" in caplog.text
    assert KEY not in caplog.text and HOST not in caplog.text
    assert KEY not in repr(source) and KEY not in repr(connection())


# --------------------------------------------------------------------------
# Socket hook parity with the legacy bridge


class SynthSocket:
    closed = False

    def close(self):
        self.closed = True


class SynthClient:
    """Duck-typed client for hook-parity and hygiene tests (provenance patched)."""

    def __init__(self, receive_error=None):
        self.socket = None
        self.socketPersistent = True
        self.parent = None
        self.children = {}
        self.version = 3.5
        self.receive_error = receive_error
        self.calls = []

    def _get_socket(self, renew=False):
        if renew and self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.socket is None:
            self.socket = SynthSocket()
        return True

    def _check_socket_close(self, force=False):
        if (force or not self.socketPersistent) and self.socket:
            self.socket.close()
            self.socket = None

    def set_socketPersistent(self, persist):
        self.socketPersistent = persist
        self._check_socket_close()

    def close(self):
        self.socket = None

    def _cache_response(self, response):
        return response

    def status(self, nowait=False):
        self.calls.append("status")
        self._get_socket(False)
        response = {"dps": {"4": "standby", "110": True}}
        self._cache_response(response)
        return response

    def receive(self):
        self.calls.append("receive")
        error, self.receive_error = self.receive_error, None
        if error is not None:
            raise error
        time.sleep(0.005)
        return None

    def heartbeat(self, nowait=True):
        self.calls.append("heartbeat")


def _drive(api):
    api._cache_response({"dps": {"1": "x", "110": True, "99": 1}})
    api.close()
    api._cache_response({"dps": {"4": "a"}})
    api._get_socket(False)
    api._get_socket(True)
    api.set_socketPersistent(False)
    api._get_socket(False)
    api._check_socket_close(True)


async def test_socket_hook_parity_with_legacy(hass, monkeypatch, no_tuya_local):
    from custom_components.petsafe_freshfeed import tuya_compat
    from custom_components.petsafe_freshfeed.tuya_bridge import TuyaBridge
    monkeypatch.setattr(owned_source, "check_loaded_client", lambda api: True)
    owned_api = SynthClient()
    source = fast(OwnedSource(hass, connection(), client_factory=lambda conn: owned_api))
    owned_events = []
    source.subscribe(lambda r: owned_events.append(("report", r.epoch, r.dps, r.ack_eligible)),
                     lambda *args: owned_events.append(("connection", *args)))
    await source.async_start()
    # Explicit owned-hook provenance, separate from the legacy registry.
    for name in owned_compat.OWNED_HOOKS:
        assert owned_compat.hook_original(owned_api, name) is not None
        assert tuya_compat.owned_original(owned_api, name) is None
    # Align the legacy side on the same post-admission state.
    legacy_api = SynthClient()
    legacy_api.status()
    bridge = TuyaBridge(hass, SimpleNamespace(_api=legacy_api), legacy_api, asyncio.Lock())
    bridge.epoch = source.epoch
    bridge.install()
    legacy_events = []
    bridge.subscribe(lambda r: legacy_events.append(("report", r.epoch, r.dps, r.ack_eligible)),
                     lambda *args: legacy_events.append(("connection", *args)))
    del owned_events[:]
    async with source.lock:  # keep the owned monitor out of the comparison
        _drive(owned_api)
        _drive(legacy_api)
        await hass.async_block_till_done()
        source.flush()
        bridge.flush()
    assert owned_events == legacy_events
    assert ("connection", source.epoch, False) in owned_events
    assert source.epoch == bridge.epoch
    await bridge.async_detach()
    assert await source.async_stop() is True
    for name in owned_compat.OWNED_HOOKS:
        assert name not in vars(owned_api)
        assert owned_compat.hook_original(owned_api, name) is None


async def test_stop_is_idempotent_and_detach_returns_stop_result(hass, monkeypatch, no_tuya_local):
    monkeypatch.setattr(owned_source, "check_loaded_client", lambda api: True)
    source = fast(OwnedSource(hass, connection(), client_factory=lambda conn: SynthClient()))
    assert await source.async_stop() is True  # never started
    source = fast(OwnedSource(hass, connection(), client_factory=lambda conn: SynthClient()))
    await source.async_start()
    assert await source.async_wait_idle(1.0) is True
    assert await source.async_detach() is True
    assert await source.async_stop() is True
    with pytest.raises(ValueError):
        await source.async_claim_receive()
