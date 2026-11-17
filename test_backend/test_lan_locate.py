"""Passive LAN locator: fake sockets and an injected decrypt only.

No real socket is opened, nothing is broadcast and no packet is sent. The fake socket has
no send methods at all, so any send attempt fails the test.
"""

import json
import socket
import sys
import unittest
from unittest import mock

from custom_components.petsafe_freshfeed import lan_locate

DEVICE_ID = "synthdevice0000000001"
OTHER_ID = "synthdevice0000000002"
SRC = "192.168.200.77"


def payload(gw_id=DEVICE_ID, ip=SRC, version="3.3", **extra):
    body = {"gwId": gw_id, "ip": ip, "version": version}
    body.update(extra)
    return json.dumps(body)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeSocket:
    """Receive-only fake. Packets are (data, addr) pairs or exceptions to raise."""

    def __init__(self, registry, clock, packets=(), bind_error=None, opt_error=None):
        self.registry = registry
        self.clock = clock
        self.packets = list(packets)
        self.bind_error = bind_error
        self.opt_error = opt_error
        self.bound = []
        self.opts = []
        self.timeouts = []
        self.recv_sizes = []
        self.closed = False
        self.events = []

    def setsockopt(self, level, name, value):
        if self.opt_error is not None and name == socket.SO_REUSEADDR:
            raise self.opt_error
        self.opts.append((level, name, value))
        self.events.append(("opt", name))

    def bind(self, addr):
        if self.bind_error is not None:
            raise self.bind_error
        self.bound.append(addr)
        self.events.append(("bind", addr))

    def settimeout(self, value):
        self.timeouts.append(value)
        self._timeout = value

    def recvfrom(self, size):
        self.recv_sizes.append(size)
        if self.packets:
            item = self.packets.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.clock.now += self._timeout
        raise socket.timeout("synthetic")

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self, clock, per_port=None, bind_error=None, opt_error=None):
        self.clock = clock
        self.per_port = list(per_port or [[], []])
        self.bind_error = bind_error
        self.opt_error = opt_error
        self.sockets = []
        self.calls = []

    def __call__(self, family, kind):
        self.calls.append((family, kind))
        packets = self.per_port[len(self.sockets)] if len(self.sockets) < len(self.per_port) else []
        sock = FakeSocket(self.sockets, self.clock, packets, self.bind_error, self.opt_error)
        self.sockets.append(sock)
        return sock


def identity_decrypt(data):
    return data.decode("utf-8")


class LocatorTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(lan_locate, "_monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_locate(self, per_port, decrypt=identity_decrypt, timeout_s=8.0, **kw):
        factory = FakeFactory(self.clock, per_port, **kw)
        result = lan_locate.locate(
            DEVICE_ID, timeout_s=timeout_s, socket_factory=factory, decrypt=decrypt
        )
        return result, factory

    def pkt(self, text, src=SRC, port=6667):
        data = text.encode("utf-8") if isinstance(text, str) else text
        return (data, (src, port))

    def assert_all_closed(self, factory):
        self.assertTrue(factory.sockets)
        for sock in factory.sockets:
            self.assertTrue(sock.closed)

    def test_locator_matches_device_id(self):
        for version in ("3.1", "3.3", "3.4", "3.5"):
            with self.subTest(version=version):
                result, factory = self.run_locate(
                    [[], [self.pkt(payload(version=version))]]
                )
                self.assertEqual(result, lan_locate.Located(host=SRC, version=version))
                self.assert_all_closed(factory)
        # A match on the 6666 socket works the same way.
        result, _ = self.run_locate([[self.pkt(payload(version="3.1"), port=6666)], []])
        self.assertEqual(result, lan_locate.Located(host=SRC, version="3.1"))

    def test_locator_skips_other_device_then_matches(self):
        result, _ = self.run_locate(
            [[], [self.pkt(payload(gw_id=OTHER_ID, ip="192.168.200.5"), src="192.168.200.5"),
                  self.pkt(payload())]]
        )
        self.assertEqual(result, lan_locate.Located(host=SRC, version="3.3"))

    def test_locator_rejects_mismatch_and_public_ip(self):
        cases = {
            "other device": self.pkt(payload(gw_id=OTHER_ID)),
            "id prefix": self.pkt(payload(gw_id=DEVICE_ID[:-1])),
            "id suffix": self.pkt(payload(gw_id=DEVICE_ID + "0")),
            "id case": self.pkt(payload(gw_id=DEVICE_ID.upper())),
            "public source": self.pkt(payload(ip="8.8.8.8"), src="8.8.8.8"),
            "cgnat source": self.pkt(payload(ip="100.64.1.2"), src="100.64.1.2"),
            "loopback source": self.pkt(payload(ip="127.0.0.1"), src="127.0.0.1"),
            "link-local source": self.pkt(payload(ip="169.254.1.2"), src="169.254.1.2"),
            "172.32 not rfc1918": self.pkt(payload(ip="172.32.0.1"), src="172.32.0.1"),
            "broadcast source": self.pkt(payload(ip="255.255.255.255"), src="255.255.255.255"),
            "payload ip differs": self.pkt(payload(ip="192.168.200.78")),
            "payload ip public": self.pkt(payload(ip="8.8.8.8")),
            "payload ip not ip": self.pkt(payload(ip="feeder.local")),
            "payload ip padded": self.pkt(payload(ip=" " + SRC)),
            "ipv6 source": (payload().encode(), ("fe80::1", 6667, 0, 0)),
            "bad addr": (payload().encode(), "192.168.200.77"),
        }
        for name, item in cases.items():
            with self.subTest(case=name):
                result, factory = self.run_locate([[], [item]], timeout_s=1.0)
                self.assertIsNone(result)
                self.assert_all_closed(factory)
        for src in ("10.0.0.9", "172.16.0.9", "172.31.255.9", "192.168.0.9"):
            with self.subTest(rfc1918=src):
                result, _ = self.run_locate([[], [self.pkt(payload(ip=src), src=src)]])
                self.assertEqual(result, lan_locate.Located(host=src, version="3.3"))

    def test_locator_rejects_bad_json_and_unknown_version(self):
        deep = "[" * 1500 + "]" * 1500
        cases = {
            "not json": self.pkt("not json"),
            "json list": self.pkt(json.dumps([DEVICE_ID, SRC, "3.3"])),
            "json string": self.pkt(json.dumps(DEVICE_ID)),
            "deep nesting": self.pkt(deep),
            "missing gwId": self.pkt(json.dumps({"ip": SRC, "version": "3.3"})),
            "missing ip": self.pkt(json.dumps({"gwId": DEVICE_ID, "version": "3.3"})),
            "missing version": self.pkt(json.dumps({"gwId": DEVICE_ID, "ip": SRC})),
            "non-string gwId": self.pkt(json.dumps({"gwId": 5, "ip": SRC, "version": "3.3"})),
            "non-string ip": self.pkt(json.dumps({"gwId": DEVICE_ID, "ip": 5, "version": "3.3"})),
            "float version": self.pkt(json.dumps({"gwId": DEVICE_ID, "ip": SRC, "version": 3.3})),
            "version 3.2": self.pkt(payload(version="3.2")),
            "version 3.6": self.pkt(payload(version="3.6")),
            "version padded": self.pkt(payload(version="3.3 ")),
            "invalid utf8": self.pkt(b"\xff\xfe{}"),
        }
        for name, item in cases.items():
            with self.subTest(case=name):
                result, _ = self.run_locate([[], [item]], timeout_s=1.0)
                self.assertIsNone(result)

    def test_locator_decrypt_failures_are_skipped(self):
        def raising(data):
            raise ValueError("synthetic")

        def returns_none(data):
            return None

        def returns_bytes(data):
            return data

        for decrypt in (raising, returns_none):
            with self.subTest(decrypt=decrypt.__name__):
                result, _ = self.run_locate([[], [self.pkt(payload())]], decrypt=decrypt, timeout_s=1.0)
                self.assertIsNone(result)
        # tinytuya may hand back bytes; those are decoded as UTF-8.
        result, _ = self.run_locate([[], [self.pkt(payload())]], decrypt=returns_bytes)
        self.assertEqual(result, lan_locate.Located(host=SRC, version="3.3"))

    def test_locator_times_out_to_none(self):
        result, factory = self.run_locate([[], []], timeout_s=2.0)
        self.assertIsNone(result)
        self.assertGreaterEqual(self.clock.now - 1000.0, 2.0)
        self.assertLess(self.clock.now - 1000.0, 2.0 + 1.0)
        for sock in factory.sockets:
            for value in sock.timeouts:
                self.assertGreater(value, 0)
                self.assertLessEqual(value, 2.0)
        self.assert_all_closed(factory)

    def test_locator_rejects_bad_arguments(self):
        factory = FakeFactory(self.clock)
        for device_id in ("", None, 5, b"x"):
            with self.subTest(device_id=device_id):
                with self.assertRaises(ValueError):
                    lan_locate.locate(device_id, socket_factory=factory, decrypt=identity_decrypt)
        for timeout_s in (0, -1, float("nan"), float("inf"), 61, "8", True):
            with self.subTest(timeout_s=timeout_s):
                with self.assertRaises(ValueError):
                    lan_locate.locate(
                        DEVICE_ID, timeout_s=timeout_s, socket_factory=factory,
                        decrypt=identity_decrypt,
                    )
        self.assertEqual(factory.calls, [])

    def test_locator_bounds_packet_size_and_count(self):
        seen = []

        def counting(data):
            seen.append(len(data))
            return data.decode("utf-8")

        # Oversized packet carrying a valid match is dropped before decrypt.
        big = payload(pad="x" * 2100)
        self.assertGreater(len(big), 2048)
        result, factory = self.run_locate([[], [self.pkt(big)]], decrypt=counting, timeout_s=1.0)
        self.assertIsNone(result)
        self.assertEqual(seen, [])
        for sock in factory.sockets:
            for size in sock.recv_sizes:
                self.assertEqual(size, 2049)

        # Exactly 2048 bytes is accepted.
        body = payload(pad="")
        exact = payload(pad="x" * (2048 - len(body)))
        self.assertEqual(len(exact.encode()), 2048)
        result, _ = self.run_locate([[], [self.pkt(exact)]], decrypt=counting)
        self.assertEqual(result, lan_locate.Located(host=SRC, version="3.3"))

        # At most 256 packets in total across both sockets; a match after that is never read.
        seen.clear()
        junk = [self.pkt(payload(gw_id=OTHER_ID)) for _ in range(200)]
        junk2 = [self.pkt(payload(gw_id=OTHER_ID)) for _ in range(200)]
        result, factory = self.run_locate(
            [junk + [self.pkt(payload())], junk2 + [self.pkt(payload())]], decrypt=counting
        )
        self.assertIsNone(result)
        self.assertEqual(len(seen), 256)
        received = sum(len(sock.recv_sizes) for sock in factory.sockets)
        self.assertEqual(received, 256)

    def test_locator_binds_only_6666_6667_receive(self):
        result, factory = self.run_locate([[], []], timeout_s=1.0)
        self.assertIsNone(result)
        self.assertEqual(factory.calls, [(socket.AF_INET, socket.SOCK_DGRAM)] * 2)
        bound = [addr for sock in factory.sockets for addr in sock.bound]
        self.assertEqual(sorted(bound), [("", 6666), ("", 6667)])
        for sock in factory.sockets:
            for _level, name, _value in sock.opts:
                self.assertNotEqual(name, socket.SO_BROADCAST)
            # The fake has no send methods; the locator must not need any.
            for attr in ("send", "sendto", "sendall", "connect"):
                self.assertFalse(hasattr(sock, attr))
        with open(lan_locate.__file__, encoding="ascii") as handle:
            self.assertNotIn("sendto", handle.read())

    def test_locator_sets_reuseaddr(self):
        result, factory = self.run_locate([[], []], timeout_s=1.0)
        self.assertIsNone(result)
        for sock in factory.sockets:
            names = [name for level, name, value in sock.opts if level == socket.SOL_SOCKET and value == 1]
            self.assertIn(socket.SO_REUSEADDR, names)
            if hasattr(socket, "SO_REUSEPORT"):
                self.assertIn(socket.SO_REUSEPORT, names)
            # Options are set before bind.
            self.assertEqual(sock.events[-1][0], "bind")
            self.assertIn(("opt", socket.SO_REUSEADDR), sock.events[:-1])

    def test_locator_without_reuseport_platform(self):
        saved = getattr(socket, "SO_REUSEPORT", None)
        if saved is not None:
            delattr(socket, "SO_REUSEPORT")
        try:
            result, factory = self.run_locate([[], [self.pkt(payload())]])
        finally:
            if saved is not None:
                socket.SO_REUSEPORT = saved
        self.assertEqual(result, lan_locate.Located(host=SRC, version="3.3"))

    def test_locator_bind_or_recv_oserror_returns_none(self):
        result, factory = self.run_locate(
            [[], [self.pkt(payload())]], bind_error=OSError(98, "Address in use")
        )
        self.assertIsNone(result)
        self.assert_all_closed(factory)

        result, factory = self.run_locate(
            [[], [self.pkt(payload())]], opt_error=OSError(22, "synthetic")
        )
        self.assertIsNone(result)
        self.assert_all_closed(factory)

        result, factory = self.run_locate(
            [[OSError(100, "Network down")], [self.pkt(payload())]]
        )
        self.assertIsNone(result)
        self.assert_all_closed(factory)

        def factory_fails(family, kind):
            raise OSError(24, "Too many open files")

        self.assertIsNone(
            lan_locate.locate(DEVICE_ID, socket_factory=factory_fails, decrypt=identity_decrypt)
        )

        # Second socket fails to open: the first is still closed.
        opened = []

        def second_fails(family, kind):
            if opened:
                raise OSError(24, "Too many open files")
            sock = FakeSocket(opened, self.clock)
            opened.append(sock)
            return sock

        self.assertIsNone(
            lan_locate.locate(DEVICE_ID, socket_factory=second_fails, decrypt=identity_decrypt)
        )
        self.assertTrue(opened[0].closed)

    def test_locator_uses_injected_decrypt(self):
        calls = []

        def decrypt(data):
            calls.append(data)
            return payload()

        blocked = {"tinytuya": None}
        with mock.patch.dict(sys.modules, blocked):
            result, _ = self.run_locate([[], [self.pkt(b"\x00opaque\x01")]], decrypt=decrypt)
        self.assertEqual(result, lan_locate.Located(host=SRC, version="3.3"))
        self.assertEqual(calls, [b"\x00opaque\x01"])

    def test_locator_missing_tinytuya_returns_none(self):
        factory = FakeFactory(self.clock, [[], [self.pkt(payload())]])
        with mock.patch.dict(sys.modules, {"tinytuya": None}):
            self.assertIsNone(lan_locate.locate(DEVICE_ID, socket_factory=factory))
        self.assertEqual(factory.calls, [])

    def test_located_holds_only_host_and_version(self):
        located = lan_locate.Located(host=SRC, version="3.3")
        self.assertEqual(repr(located), "Located(host='192.168.200.77', version='3.3')")
        with self.assertRaises(Exception):
            located.host = "10.0.0.1"


if __name__ == "__main__":
    unittest.main()
