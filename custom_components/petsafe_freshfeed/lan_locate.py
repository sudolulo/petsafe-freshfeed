"""Passive LAN locator: find a Tuya device's IP and protocol version by device ID.

Listens for the periodic UDP broadcasts Tuya devices send to ports 6666 (3.1, plain) and
6667 (3.3 and later, encrypted with the public UDP key), the same ports Tuya Local's
TinyTuya scanner reads. The locator only listens: it never sends a packet, never sets
``SO_BROADCAST`` and never sends the port 7000 discovery request, so a device that stays
silent until asked is not found and the user types its address instead.

Synchronous and blocking; call it in the executor. The decode function defaults to
``tinytuya.decrypt_udp``, imported lazily so this module imports without TinyTuya; a
missing TinyTuya returns ``None`` before any socket is opened.

Bounds: at most ``MAX_PACKETS`` packets in total are read, each at most ``MAX_PACKET_BYTES``
bytes (a larger datagram is dropped before decode), and the decoded text is at most
``MAX_TEXT_CHARS`` characters before ``json.loads``. The whole listen ends at the deadline.
Any ``OSError`` while opening, configuring, binding or receiving returns ``None`` (Tuya
Local's own scanner may already hold the ports); every socket is closed on every exit.

Acceptance rule: a packet is accepted only when its source is an RFC 1918 IPv4 address,
its decoded payload is a JSON object whose string ``gwId`` equals the requested device ID
exactly, whose string ``ip`` equals the packet source address exactly, and whose string
``version`` is one of ``SUPPORTED_VERSIONS``.

Forgery limit: this is not authentication. The UDP key is public, so any host on the same
LAN can forge a valid broadcast for any device ID with its own address. A forged result
only directs Tuya Local's connection test to that host. This module never sends or reveals
the local key, but the forged host does receive Tuya Local's handshake, which is ciphertext
encrypted with the local key; recovering a random 16-character key from it by brute force
is infeasible. Without the key the host cannot complete the handshake, so it fails the
connection test, which the setup flow reports as a connection failure.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import socket
import time

LISTEN_PORTS = (6666, 6667)
SUPPORTED_VERSIONS = frozenset({"3.1", "3.3", "3.4", "3.5"})
MAX_PACKETS = 256
MAX_PACKET_BYTES = 2048
MAX_TEXT_CHARS = 4096
MAX_TIMEOUT_S = 60.0
# Per-receive wait, so both sockets are polled in turn without select() on fakes.
_SLICE_S = 0.25

_RFC1918 = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)

# Indirection so tests can drive the deadline with a fake clock.
_monotonic = time.monotonic


@dataclass(frozen=True, slots=True)
class Located:
    """Where the device answered from. Holds no secret."""

    host: str
    version: str


def _default_decrypt():
    try:
        import tinytuya  # noqa: PLC0415 - lazy: the backend interpreter has no TinyTuya

        return tinytuya.decrypt_udp
    except Exception:  # noqa: BLE001 - any import failure means "cannot decode"
        return None


def _rfc1918_source(addr) -> str | None:
    if not isinstance(addr, tuple) or len(addr) != 2 or not isinstance(addr[0], str):
        return None
    try:
        parsed = ipaddress.IPv4Address(addr[0])
    except ValueError:
        return None
    if not any(parsed in net for net in _RFC1918):
        return None
    return addr[0]


def _accept(device_id: str, data, addr, decrypt) -> Located | None:
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PACKET_BYTES:
        return None
    source = _rfc1918_source(addr)
    if source is None:
        return None
    try:
        text = decrypt(bytes(data))
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8")
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            return None
        body = json.loads(text)
    except Exception:  # noqa: BLE001 - any decode or parse failure drops the packet
        return None
    if not isinstance(body, dict):
        return None
    gw_id = body.get("gwId")
    ip = body.get("ip")
    version = body.get("version")
    if not (isinstance(gw_id, str) and isinstance(ip, str) and isinstance(version, str)):
        return None
    if gw_id != device_id or ip != source or version not in SUPPORTED_VERSIONS:
        return None
    return Located(host=source, version=version)


def _open(factory, port: int):
    sock = factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reuseport = getattr(socket, "SO_REUSEPORT", None)
        if reuseport is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, reuseport, 1)
            except OSError:
                pass  # optional: kernels without it still share via SO_REUSEADDR
        sock.bind(("", port))
    except BaseException:
        sock.close()
        raise
    return sock


def locate(
    device_id: str,
    *,
    timeout_s: float = 8.0,
    socket_factory=None,
    decrypt=None,
) -> Located | None:
    """Listen for the device's broadcast; return its address and version, else ``None``."""
    if not isinstance(device_id, str) or not device_id:
        raise ValueError("device_id must be a non-empty string")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or not 0 < timeout_s <= MAX_TIMEOUT_S
    ):
        raise ValueError("timeout_s must be in (0, 60]")
    if decrypt is None:
        decrypt = _default_decrypt()
        if decrypt is None:
            return None
    factory = socket.socket if socket_factory is None else socket_factory

    deadline = _monotonic() + float(timeout_s)
    socks = []
    try:
        try:
            for port in LISTEN_PORTS:
                socks.append(_open(factory, port))
        except OSError:
            return None
        received = 0
        while received < MAX_PACKETS:
            for sock in socks:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    return None
                if received >= MAX_PACKETS:
                    return None
                try:
                    sock.settimeout(min(_SLICE_S, remaining))
                    data, addr = sock.recvfrom(MAX_PACKET_BYTES + 1)
                except TimeoutError:
                    continue
                except OSError:
                    return None
                received += 1
                located = _accept(device_id, data, addr, decrypt)
                if located is not None:
                    return located
        return None
    finally:
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
