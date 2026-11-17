"""Offline tests for account/exchange.py (port of the harness live_exchange.py
and its tests, plan 2026-11-17 Task 2).

No socket is ever opened. Every connection is an injected fake: either a
real ``http.client.HTTPSConnection`` subclass whose ``connect`` installs an
in-memory socket (so request serialization and response parsing are the
real ``http.client`` code), or, for the default-factory test, a patched
constructor. setUpModule installs the fail-closed offline guards of
``synthetic_fixtures`` (socket connect/DNS, ``HTTPConnection.connect``,
urllib, subprocess) and every test asserts zero guard hits in tearDown; a
real connect attempt raises ``NetworkAttempt`` (a ``BaseException``) and
fails the test. In-process monkeypatches only, not universal isolation.
"""
import datetime
import email.parser
import http.client
import io
import os
import secrets
import socket
import ssl
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from custom_components.petsafe_freshfeed.account import client as ac
from custom_components.petsafe_freshfeed.account import boundary
from custom_components.petsafe_freshfeed.account import exchange as live_exchange
import account_fixtures as sf

from custom_components.petsafe_freshfeed.account.exchange import ExchangeFailed, ExchangeTimeout, LiveExchange

# The closed failure subclasses by kind (getattr: a missing class makes the
# assertions fail, not the import).
FAILURE_CLASSES = {kind: getattr(live_exchange, name, None) for kind, name in (
    ("BIND", "ExchangeBindFailed"), ("CONNECT", "ExchangeConnectFailed"),
    ("STATUS", "ExchangeStatusFailed"), ("ENCODING", "ExchangeEncodingFailed"),
    ("LENGTH", "ExchangeLengthFailed"), ("IO", "ExchangeIOFailed"))}

AZ_URL = "https://a1-us.iotbing.com/api.json"
ROUTE_URLS = {
    "https://a1-us.iotbing.com/api.json",
    "https://a1.tuyaeu.com/api.json",
    "https://a1-in.iotbing.com/api.json",
    "https://a1.iot334.com/api.json",
}
BODY = b"a=thing.m.user.username.token.get&v=2.0&postData=abc&sign=00"
MANIFEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "custom_components", "petsafe_freshfeed", "manifest.json")


def manifest_version():
    import json
    with open(MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)["version"]
SENTINEL = "SENTINEL-7f3a-live-exchange"


def setUpModule():
    sf.install_path_guards()


def tearDownModule():
    sf.remove_path_guards()


_CONTRACT = None


def contract():
    global _CONTRACT
    if _CONTRACT is None:
        _CONTRACT = boundary.Contract.load()
    return _CONTRACT


def wire(url=AZ_URL, method="POST", body=BODY,
         headers=(("Content-Type", boundary.CONTENT_TYPE),)):
    return boundary.WireRequest(method, url, headers, body)


def http_response(body=b'{"ok":1}', status="200 OK", headers=None):
    if headers is None:
        headers = [("Content-Type", "application/json"),
                   ("Content-Length", str(len(body)))]
    head = "HTTP/1.1 %s\r\n" % status
    head += "".join("%s: %s\r\n" % pair for pair in headers)
    head += "Connection: close\r\n\r\n"
    return head.encode("latin-1"), body


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class _Raw(io.RawIOBase):
    """In-memory socket stream: serves the response head in one read, then
    the body ``body_step`` bytes per read. ``on_body_read`` runs before
    each body read (it may advance a fake clock or raise)."""

    def __init__(self, head, body, body_step=None, on_body_read=None, head_step=None,
                 on_head_read=None):
        self._head_step = head_step
        self._head_hook = on_head_read
        self._head_pos = 0
        self._head = head
        self._body = body
        self._head_done = False
        self._pos = 0
        self._step = body_step
        self._hook = on_body_read
        self.body_reads = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self._head_done and self._head_step is not None:
            if self._head_hook is not None:
                self._head_hook()
            data = self._head[self._head_pos:self._head_pos + self._head_step]
            self._head_pos += len(data)
            if self._head_pos >= len(self._head):
                self._head_done = True
        elif not self._head_done:
            self._head_done = True
            data = self._head
        else:
            if self._hook is not None:
                self._hook()
            remaining = self._body[self._pos:]
            if not remaining:
                return 0
            step = len(buffer) if self._step is None else self._step
            data = remaining[:min(step, len(buffer))]
            self._pos += len(data)
            self.body_reads += 1
        buffer[:len(data)] = data
        return len(data)


class MemSocket:
    def __init__(self, raw, clock):
        self.raw = raw
        self.clock = clock
        self.sent = bytearray()
        self.timeouts = []
        self.makefile_calls = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append((self.clock(), value))

    def sendall(self, data):
        self.sent += bytes(data)

    def makefile(self, mode, buffering=None):
        # socket.socket.makefile signature: buffering=0 gives the raw stream.
        assert mode == "rb"
        self.makefile_calls.append(buffering)
        return self.raw if buffering == 0 else io.BufferedReader(self.raw)

    def close(self):
        self.closed = True


class CountingResponse(http.client.HTTPResponse):
    returned = None

    def read1(self, n=-1):
        data = super().read1(n)
        CountingResponse.returned.append((n, len(data)))
        return data


class Factory:
    """Injected connection factory. Each call builds a real
    ``HTTPSConnection`` subclass whose ``connect`` installs an in-memory
    socket instead of opening one."""

    def __init__(self, head=None, body=None, *, clock=None, body_step=None,
                 on_body_read=None, connect_error=None, respond=None, head_step=None,
                 on_head_read=None):
        self.head_step = head_step
        self.on_head_read = on_head_read
        if head is None:
            head, body = http_response()
        self.head, self.body = head, body
        self.clock = clock or FakeClock()
        self.body_step = body_step
        self.on_body_read = on_body_read
        self.connect_error = connect_error
        self.respond = respond
        self.calls = []
        self.connections = []
        self.connect_timeouts = []
        self.closed = []
        CountingResponse.returned = []

    @property
    def reads(self):
        return CountingResponse.returned

    def __call__(self, host, port, *, timeout, context):
        self.calls.append((host, port, timeout, context))
        factory = self

        class Conn(http.client.HTTPSConnection):
            response_class = CountingResponse

            def connect(self):
                factory.connect_timeouts.append((factory.clock(), self.timeout))
                if factory.connect_error is not None:
                    raise factory.connect_error
                raw = _Raw(factory.head, factory.body, factory.body_step,
                           factory.on_body_read, factory.head_step, factory.on_head_read)
                self.mem = MemSocket(raw, factory.clock)
                self.sock = self.mem

            def send(self, data):
                super().send(data)
                if factory.respond is not None and self.mem.sent.count(b"\r\n\r\n") >= 1:
                    head_end = self.mem.sent.index(b"\r\n\r\n") + 4
                    sent_body = bytes(self.mem.sent[head_end:])
                    length = int(parse_sent(bytes(self.mem.sent))[2]["Content-Length"])
                    if len(sent_body) == length:
                        out = factory.respond(sent_body)
                        self.mem.raw._head, self.mem.raw._body = http_response(out)

            def close(self):
                factory.closed.append(id(self))
                super().close()

        conn = Conn(host, port, timeout=timeout, context=context)
        self.connections.append(conn)
        return conn


def parse_sent(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    request_line = lines[0]
    names = [line.split(":", 1)[0] for line in lines[1:]]
    message = email.parser.Parser().parsestr("\r\n".join(lines[1:]) + "\r\n\r\n")
    return request_line, names, message, body


def exchange_with(factory, request=None, *, deadline=None, max_bytes=1_048_576):
    ex = LiveExchange(contract(), connection_factory=factory, clock=factory.clock,
                      max_response_bytes=max_bytes)
    if deadline is None:
        deadline = factory.clock() + 20.0
    return ex.exchange(request or wire(), deadline=deadline)


class GuardedTestCase(unittest.TestCase):
    def setUp(self):
        sf.take_guard_hits()

    def tearDown(self):
        self.assertEqual(sf.take_guard_hits(), 0, "an offline guard fired")

    def assertClosed(self, factory):
        """Every connection the factory built was closed at least once, and
        every in-memory socket it opened is closed."""
        self.assertTrue(factory.connections)
        for conn in factory.connections:
            self.assertIn(id(conn), factory.closed)
            mem = getattr(conn, "mem", None)
            if mem is not None:
                self.assertTrue(mem.closed)

    def assertFixedFailure(self, exc, cls=ExchangeFailed, kind=None):
        """A fixed-message failure of ``cls``: for ExchangeFailed, exactly one
        of the closed subclasses (the one of ``kind`` when given)."""
        if cls is ExchangeFailed:
            self.assertIsInstance(exc, ExchangeFailed)
            self.assertIn(type(exc), FAILURE_CLASSES.values())
            if kind is not None:
                self.assertIs(type(exc), FAILURE_CLASSES[kind])
                self.assertEqual(type(exc).kind, kind)
        else:
            self.assertIs(type(exc), cls)
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        self.assertEqual(exc.args, cls().args)
        for text in (str(exc), repr(exc)):
            self.assertNotIn("iotbing", text)
            self.assertNotIn("http", text.lower())
            self.assertNotIn(SENTINEL, text)


class GuardTests(GuardedTestCase):
    def test_offline_guards_active(self):
        with self.assertRaises(sf.NetworkAttempt):
            socket.getaddrinfo("a1-us.iotbing.com", 443)
        with self.assertRaises(sf.NetworkAttempt):
            socket.create_connection(("a1-us.iotbing.com", 443))
        with self.assertRaises(sf.NetworkAttempt):
            http.client.HTTPSConnection("a1-us.iotbing.com", 443).connect()
        self.assertEqual(sf.take_guard_hits(), 3)


class ExactRequestTests(GuardedTestCase):
    def test_exchange_sends_exact_request(self):
        factory = Factory()
        result = exchange_with(factory)
        self.assertIs(type(result), bytes)
        self.assertEqual(result, b'{"ok":1}')
        self.assertEqual(len(factory.calls), 1)
        host, port, _timeout, context = factory.calls[0]
        self.assertEqual((host, port), ("a1-us.iotbing.com", 443))
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertIs(context.check_hostname, True)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(len(factory.connect_timeouts), 1)

        request_line, names, message, body = parse_sent(bytes(factory.connections[0].mem.sent))
        self.assertEqual(request_line, "POST /api.json HTTP/1.1")
        self.assertEqual(body, BODY)
        expected = dict(live_exchange.fixed_headers())
        expected["Content-Length"] = str(len(BODY))
        expected["Host"] = "a1-us.iotbing.com"
        self.assertEqual(sorted(names), sorted(expected))
        self.assertEqual(len(names), len(set(n.lower() for n in names)))
        for name, value in expected.items():
            self.assertEqual(message[name], value)
        self.assertClosed(factory)
        self.assertTrue(factory.connections[0].mem.closed)

    def test_fixed_headers_exact_values(self):
        self.assertEqual(dict(live_exchange.fixed_headers()), {
            "Content-Type": boundary.CONTENT_TYPE,
            "Accept-Encoding": "identity",
            "User-Agent": "petsafe-freshfeed/" + manifest_version(),
            "Connection": "close",
        })
        self.assertIs(type(live_exchange.fixed_headers()), tuple)
        self.assertEqual([name for name, _ in live_exchange.fixed_headers()],
                         ["Content-Type", "Accept-Encoding", "User-Agent", "Connection"])

    def test_every_contract_route_accepted_with_its_own_host(self):
        for url in sorted(ROUTE_URLS):
            factory = Factory()
            exchange_with(factory, wire(url=url))
            host = url[len("https://"):].split("/", 1)[0]
            self.assertEqual(factory.calls[0][:2], (host, 443))

    def test_route_set_matches_contract(self):
        c = contract()
        urls = {c.region_url(c.country_region(cc)) for cc in ("1", "44", "91", "7")}
        self.assertEqual(urls, ROUTE_URLS)
        self.assertEqual(LiveExchange(c)._route_urls, frozenset(ROUTE_URLS))

    def test_chunked_response_accepted(self):
        payload = b'{"result":"x"}'
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(payload), payload)
        head, _ = http_response(b"", headers=[("Transfer-Encoding", "chunked")])
        self.assertEqual(exchange_with(Factory(head, chunked)), payload)

    def test_identity_content_encoding_accepted(self):
        head, body = http_response(b"{}", headers=[("Content-Encoding", "identity"),
                                                   ("Content-Length", "2")])
        self.assertEqual(exchange_with(Factory(head, body)), b"{}")

    def test_constructor_validation(self):
        c = contract()
        for bad in (0, -1, True, 1.5, "1"):
            with self.assertRaises(ValueError):
                LiveExchange(c, max_response_bytes=bad)
        with self.assertRaises(ValueError):
            LiveExchange(object())
        with self.assertRaises(ValueError):
            LiveExchange(c, connection_factory=42)
        self.assertIsInstance(LiveExchange(c), ac.Exchange)


class UnboundRequestTests(GuardedTestCase):
    def test_exchange_refuses_unbound_request(self):
        cases = [
            wire(url="https://evil.example/api.json"),
            wire(url="http://a1-us.iotbing.com/api.json"),
            wire(url="https://a1-us.iotbing.com:8443/api.json"),
            wire(url="https://a1-us.iotbing.com/api.json?x=1"),
            wire(url="https://A1-US.iotbing.com/api.json"),
            wire(url="https://a1-us.iotbing.com/other.json"),
            wire(method="GET"),
            wire(method="post"),
            wire(headers=(("Content-Type", "application/json"),)),
            wire(headers=(("Content-Type", boundary.CONTENT_TYPE), ("X-Extra", "1"))),
            wire(body="not-bytes"),
            wire(body=bytearray(BODY)),
            types.SimpleNamespace(method="POST", url=AZ_URL,
                                  headers=(("Content-Type", boundary.CONTENT_TYPE),),
                                  body=BODY),
        ]
        for request in cases:
            factory = Factory()
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory, request)
            self.assertFixedFailure(caught.exception, kind="BIND")
            self.assertEqual(factory.calls, [], repr(request))

    def test_bad_deadline_refused_before_factory(self):
        for deadline in (float("nan"), float("inf"), "10", None, True):
            factory = Factory()
            ex = LiveExchange(contract(), connection_factory=factory, clock=factory.clock)
            with self.assertRaises(ExchangeFailed) as caught:
                ex.exchange(wire(), deadline=deadline)
            self.assertFixedFailure(caught.exception, kind="BIND")
            self.assertEqual(factory.calls, [])

    def test_expired_deadline_times_out_before_factory(self):
        factory = Factory()
        with self.assertRaises(ExchangeTimeout) as caught:
            exchange_with(factory, deadline=factory.clock() - 1)
        self.assertFixedFailure(caught.exception, ExchangeTimeout)
        self.assertEqual(factory.calls, [])


class RejectionTests(GuardedTestCase):
    def test_exchange_rejects_encoding_redirect_and_oversize(self):
        # Redirects: never followed, no second request.
        for status in ("301 Moved Permanently", "302 Found", "307 Temporary Redirect",
                       "308 Permanent Redirect"):
            head, body = http_response(b"", status=status, headers=[
                ("Location", "https://evil.example/api.json"), ("Content-Length", "0")])
            factory = Factory(head, body)
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory)
            self.assertFixedFailure(caught.exception, kind="STATUS")
            self.assertEqual(len(factory.calls), 1)
            self.assertEqual(len(factory.connections), 1)
            self.assertClosed(factory)

        # Other non-200 statuses.
        for status in ("500 Internal Server Error", "204 No Content", "201 Created",
                       "404 Not Found"):
            head, body = http_response(SENTINEL.encode(), status=status)
            factory = Factory(head, body)
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory)
            self.assertFixedFailure(caught.exception, kind="STATUS")
            self.assertEqual(factory.reads, [])

        # Content-Encoding other than identity: rejected without reading the body.
        gz = b"\x1f\x8b\x08\x00" + b"\x00" * 20
        for encodings in (["gzip"], ["deflate"], ["br"], ["GZIP"], ["identity", "gzip"],
                          ["identity", "identity"], [""], ["x-" + SENTINEL]):
            headers = [("Content-Encoding", e) for e in encodings]
            headers.append(("Content-Length", str(len(gz))))
            head, body = http_response(gz, headers=headers)
            factory = Factory(head, body)
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory)
            self.assertFixedFailure(caught.exception, kind="ENCODING")
            self.assertEqual(factory.reads, [], encodings)
            self.assertClosed(factory)

        limit = 100
        # Declared Content-Length over the ceiling: rejected without reading.
        big = b"x" * (limit + 1)
        factory = Factory(*http_response(big))
        with self.assertRaises(ExchangeFailed) as caught:
            exchange_with(factory, max_bytes=limit)
        self.assertFixedFailure(caught.exception, kind="LENGTH")
        self.assertEqual(factory.reads, [])

        # Undeclared length (EOF-delimited) and chunked: the loop ceiling.
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(big), big)
        for head, body in (
                http_response(big, headers=[("Content-Type", "application/json")]),
                (http_response(b"", headers=[("Transfer-Encoding", "chunked")])[0], chunked),
                http_response(b"x" * 5000, headers=[]),
        ):
            factory = Factory(head, body, body_step=7)
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory, max_bytes=limit)
            self.assertFixedFailure(caught.exception, kind="LENGTH")
            total = sum(n for _req, n in factory.reads)
            self.assertLessEqual(total, limit + 1)
            self.assertGreater(total, limit)
            for requested, _got in factory.reads:
                self.assertGreater(requested, 0)
                self.assertLessEqual(requested, 65536)
            self.assertClosed(factory)

        # Exactly at the ceiling passes.
        exact = b"y" * limit
        factory = Factory(*http_response(exact, headers=[]), body_step=9)
        self.assertEqual(exchange_with(factory, max_bytes=limit), exact)

    def test_reads_use_read1_in_bounded_chunks(self):
        payload = b"z" * 200_000
        factory = Factory(*http_response(payload))
        self.assertEqual(exchange_with(factory, max_bytes=1_048_576), payload)
        self.assertGreater(len(factory.reads), 1)
        for requested, got in factory.reads:
            self.assertLessEqual(requested, 65536)
            self.assertLessEqual(got, requested)

    def test_malformed_http_is_fixed_failure(self):
        # A malformed status line is IO; a peer that closes before any
        # response head (an OSError before a response exists) is CONNECT.
        for head, kind in ((b"garbage\r\n\r\n", "IO"), (b"HTTP/1.1 2OO OK\r\n\r\n", "IO"),
                           (b"", "CONNECT")):
            factory = Factory(head, b"")
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory)
            self.assertFixedFailure(caught.exception, kind=kind)
            self.assertClosed(factory)

    def test_truncated_body_is_fixed_failure(self):
        head, _ = http_response(b"0123456789")
        factory = Factory(head, b"01234")
        with self.assertRaises(ExchangeFailed) as caught:
            exchange_with(factory)
        self.assertFixedFailure(caught.exception, kind="LENGTH")
        # A truncated chunked body is LENGTH as well.
        chunked = b"a\r\n01234"
        head, _ = http_response(b"", headers=[("Transfer-Encoding", "chunked")])
        factory = Factory(head, chunked)
        with self.assertRaises(ExchangeFailed) as caught:
            exchange_with(factory)
        self.assertFixedFailure(caught.exception, kind="LENGTH")

    def test_no_proxy_or_tunnel(self):
        factory = Factory()
        with mock.patch.dict("os.environ", {"https_proxy": "http://127.0.0.1:9",
                                            "HTTPS_PROXY": "http://127.0.0.1:9"}):
            exchange_with(factory)
        conn = factory.connections[0]
        self.assertIsNone(conn._tunnel_host)
        self.assertEqual((conn.host, conn.port), ("a1-us.iotbing.com", 443))


class DeadlineTests(GuardedTestCase):
    def test_exchange_absolute_deadline_slow_drip(self):
        clock = FakeClock()
        deadline = clock() + 5.0
        payload = b"d" * 1000

        def drip():
            clock.now += 1.0

        factory = Factory(*http_response(payload), clock=clock, body_step=1, on_body_read=drip)
        with self.assertRaises(ExchangeTimeout) as caught:
            exchange_with(factory, deadline=deadline)
        self.assertFixedFailure(caught.exception, ExchangeTimeout)
        self.assertIsInstance(caught.exception, TimeoutError)
        self.assertNotIsInstance(caught.exception, ExchangeFailed)
        self.assertLessEqual(len(factory.reads), 6)
        self.assertClosed(factory)

        # Socket timeout: max(0.0, deadline - now) before connect and
        # re-set before each read.
        self.assertEqual(factory.calls[0][2], 5.0)
        self.assertEqual(factory.connect_timeouts, [(1000.0, 5.0)])
        timeouts = factory.connections[0].mem.timeouts
        self.assertGreaterEqual(len(timeouts), len(factory.reads))
        for now, value in timeouts:
            self.assertEqual(value, max(0.0, deadline - now))
            self.assertGreater(value, 0.0)
        read_times = [now for now, _ in timeouts]
        self.assertEqual(read_times, sorted(read_times))
        # Distinct timeouts as the clock moved: re-set before each read.
        self.assertGreaterEqual(len({v for _, v in timeouts}), 4)

    def test_slow_drip_response_head_stops_at_deadline(self):
        clock = FakeClock()
        deadline = clock() + 5.0
        head, body = http_response(b"{}", headers=[("X-Pad", "p" * 2000),
                                                   ("Content-Length", "2")])

        def drip():
            clock.now += 1.0

        factory = Factory(head, body, clock=clock, head_step=1, on_head_read=drip)
        with self.assertRaises(ExchangeTimeout) as caught:
            exchange_with(factory, deadline=deadline)
        self.assertFixedFailure(caught.exception, ExchangeTimeout)
        mem = factory.connections[0].mem
        self.assertLessEqual(mem.raw._head_pos, 6)
        self.assertEqual(mem.makefile_calls, [0])
        for now, value in mem.timeouts:
            self.assertEqual(value, deadline - now)
        self.assertClosed(factory)

    def test_socket_timeout_during_connect_and_read(self):
        factory = Factory(connect_error=TimeoutError(SENTINEL))
        with self.assertRaises(ExchangeTimeout) as caught:
            exchange_with(factory)
        self.assertFixedFailure(caught.exception, ExchangeTimeout)
        self.assertClosed(factory)

        factory = Factory(connect_error=socket.timeout(SENTINEL))
        with self.assertRaises(ExchangeTimeout):
            exchange_with(factory)

        def stall():
            raise TimeoutError(SENTINEL)

        factory = Factory(*http_response(b"x" * 50), on_body_read=stall)
        with self.assertRaises(ExchangeTimeout) as caught:
            exchange_with(factory)
        self.assertFixedFailure(caught.exception, ExchangeTimeout)
        self.assertClosed(factory)

    def test_deadline_passing_during_request_times_out(self):
        clock = FakeClock()

        def respond(_sent):
            clock.now += 100.0
            return b"{}"

        factory = Factory(clock=clock, respond=respond)
        with self.assertRaises(ExchangeTimeout):
            exchange_with(factory, deadline=clock() + 10.0)
        self.assertClosed(factory)

    def test_account_client_maps_deadline_to_timeout(self):
        # Fake clock tied to the real monotonic clock so the client's own
        # deadline is real; each body read jumps the exchange clock 60 s.
        offset = [0.0]

        def clock():
            return time.monotonic() + offset[0]

        def drip():
            offset[0] += 60.0

        server = sf.FakeServer()
        factory = Factory(clock=clock, body_step=1, on_body_read=drip,
                          respond=lambda body: server.respond(types.SimpleNamespace(body=body)))
        ex = LiveExchange(contract(), connection_factory=factory, clock=clock)
        client = ac.AccountClient(sf.SyntheticProfile(), ex, contract(), permit=granted_permit())
        result = client.run(sf.credentials(), password_authorized=True,
                            deadline=time.monotonic() + 30.0)
        self.assertEqual(result, ac.SafeResult(ac.Outcome.TIMEOUT, 0, 0, 0))
        self.assertEqual(len(factory.calls), 1)

    def test_account_client_end_to_end_through_wire(self):
        server = sf.FakeServer()
        factory = Factory(clock=time.monotonic,
                          respond=lambda body: server.respond(types.SimpleNamespace(body=body)))
        ex = LiveExchange(contract(), connection_factory=factory, clock=time.monotonic)
        client = ac.AccountClient(sf.SyntheticProfile(), ex, contract(), permit=granted_permit())
        result = client.run(sf.credentials(), password_authorized=True,
                            deadline=time.monotonic() + 30.0)
        self.assertEqual(result.outcome, ac.Outcome.OK)
        self.assertEqual([op for op, _ in server.business],
                         ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"])
        self.assertEqual(len(factory.calls), 4)
        self.assertClosed(factory)
        for conn in factory.connections:
            self.assertNotIn(sf.EMAIL.encode(), bytes(conn.mem.sent).split(b"\r\n\r\n")[0])


class FailureMessageTests(GuardedTestCase):
    def test_exchange_failure_messages_fixed(self):
        errors = [
            OSError(SENTINEL),
            ConnectionRefusedError(SENTINEL),
            ConnectionResetError(SENTINEL),
            ssl.SSLError(1, SENTINEL),
            ssl.SSLCertVerificationError(1, SENTINEL),
            socket.gaierror(-2, SENTINEL),
            http.client.HTTPException(SENTINEL),
            ValueError(SENTINEL),
        ]
        for error in errors:
            factory = Factory(connect_error=error)
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory)
            exc = caught.exception
            self.assertFixedFailure(exc, kind="CONNECT")
            self.assertIsNone(exc.__context__)
            self.assertTrue(exc.__suppress_context__)
            self.assertClosed(factory)
            tb = exc.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_filename == live_exchange.__file__:
                    for value in tb.tb_frame.f_locals.values():
                        self.assertIsNot(value, error)
                tb = tb.tb_next

        def boom():
            raise ConnectionResetError(SENTINEL)

        factory = Factory(*http_response(b"x" * 10), on_body_read=boom)
        with self.assertRaises(ExchangeFailed) as caught:
            exchange_with(factory)
        # An OSError once the response exists is IO, not CONNECT.
        self.assertFixedFailure(caught.exception, kind="IO")

    def test_failure_kind_per_phase(self):
        clock = FakeClock()
        # The factory itself fails before any connection object exists.
        for error in (ConnectionRefusedError(SENTINEL), ssl.SSLError(1, SENTINEL),
                      ValueError(SENTINEL)):
            calls = []

            def factory(host, port, *, timeout, context, _e=error):
                calls.append(host)
                raise _e
            ex = LiveExchange(contract(), connection_factory=factory, clock=clock)
            with self.assertRaises(ExchangeFailed) as caught:
                ex.exchange(wire(), deadline=clock() + 5.0)
            self.assertFixedFailure(caught.exception, kind="CONNECT")
            self.assertEqual(calls, ["a1-us.iotbing.com"])

        # connect() that leaves no socket is CONNECT.
        class NoSock:
            sock = None
            timeout = None

            def connect(self):
                return None

            def close(self):
                return None
        ex = LiveExchange(contract(), connection_factory=lambda *a, **k: NoSock(), clock=clock)
        with self.assertRaises(ExchangeFailed) as caught:
            ex.exchange(wire(), deadline=clock() + 5.0)
        self.assertFixedFailure(caught.exception, kind="CONNECT")

        # A non-OSError while sending (after connect, before a response) is IO.
        factory = Factory()
        with mock.patch.object(http.client.HTTPConnection, "request",
                               side_effect=ValueError(SENTINEL)):
            with self.assertRaises(ExchangeFailed) as caught:
                exchange_with(factory)
        self.assertFixedFailure(caught.exception, kind="IO")
        self.assertClosed(factory)

    def test_status_reason_and_headers_never_echoed(self):
        head, body = http_response(SENTINEL.encode(), status="500 " + SENTINEL)
        factory = Factory(head, body)
        with self.assertRaises(ExchangeFailed) as caught:
            exchange_with(factory)
        self.assertFixedFailure(caught.exception, kind="STATUS")

    def test_base_exceptions_propagate_and_close(self):
        factory = Factory(connect_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            exchange_with(factory)
        self.assertClosed(factory)

    def test_exception_classes(self):
        self.assertTrue(issubclass(ExchangeTimeout, TimeoutError))
        self.assertFalse(issubclass(ExchangeFailed, OSError))
        self.assertFalse(issubclass(ExchangeTimeout, ExchangeFailed))
        self.assertEqual(str(ExchangeFailed()), str(ExchangeFailed()))
        self.assertTrue(str(ExchangeFailed()))
        self.assertTrue(str(ExchangeTimeout()))
        # One subclass per failure class, each with a fixed class-level kind
        # and the same fixed message; none is an OSError or a TimeoutError.
        self.assertIsNone(ExchangeFailed.kind)
        self.assertEqual(sorted(FAILURE_CLASSES), ["BIND", "CONNECT", "ENCODING", "IO",
                                                   "LENGTH", "STATUS"])
        for kind, cls in FAILURE_CLASSES.items():
            with self.subTest(kind=kind):
                self.assertTrue(issubclass(cls, ExchangeFailed))
                self.assertFalse(issubclass(cls, OSError))
                self.assertFalse(issubclass(cls, TimeoutError))
                self.assertEqual(cls.__dict__["kind"], kind)
                self.assertEqual(cls().args, ExchangeFailed().args)
                self.assertIn(cls.__name__, live_exchange.__all__)
        self.assertEqual(len(set(FAILURE_CLASSES.values())), 6)


class RealSocketTests(GuardedTestCase):
    def test_deadline_socket_over_real_local_socketpair(self):
        """Real kernel socket (``socket.socketpair``, no connect, no DNS):
        the deadline wrapper works with real ``socket.makefile(buffering=0)``
        and ``settimeout``, and a silent peer ends at the socket timeout as
        ``ExchangeTimeout``."""
        for respond, expect in ((True, b'{"ok":1}'), (False, None)):
            client_end, server_end = socket.socketpair()
            try:
                if respond:
                    head, body = http_response(b'{"ok":1}')
                    server_end.sendall(head + body)
                    server_end.shutdown(socket.SHUT_WR)

                def factory(host, port, *, timeout, context, _sock=client_end):
                    class Conn(http.client.HTTPSConnection):
                        def connect(self):
                            self.sock = _sock
                    return Conn(host, port, timeout=timeout, context=context)

                clock = time.monotonic
                ex = LiveExchange(contract(), connection_factory=factory, clock=clock)
                if expect is not None:
                    self.assertEqual(ex.exchange(wire(), deadline=clock() + 5.0), expect)
                    sent = server_end.recv(65536)
                    self.assertTrue(sent.startswith(b"POST /api.json HTTP/1.1\r\n"))
                    self.assertTrue(sent.endswith(BODY))
                else:
                    started = clock()
                    with self.assertRaises(ExchangeTimeout):
                        ex.exchange(wire(), deadline=started + 0.3)
                    self.assertLess(clock() - started, 2.0)
                self.assertEqual(client_end.fileno(), -1)
            finally:
                client_end.close()
                server_end.close()


class DefaultFactoryTests(GuardedTestCase):
    def test_default_factory_is_https_connection(self):
        ex = LiveExchange(contract())
        self.assertIs(ex._factory, live_exchange.default_connection_factory)
        context = ex._context
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertIs(context.check_hostname, True)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(context.protocol, ssl.create_default_context().protocol)

        # Inspect a real constructed connection: construction opens no socket.
        conn = live_exchange.default_connection_factory(
            "a1-us.iotbing.com", 443, timeout=3.0, context=context)
        try:
            self.assertIs(type(conn), http.client.HTTPSConnection)
            self.assertEqual((conn.host, conn.port, conn.timeout), ("a1-us.iotbing.com", 443, 3.0))
            self.assertIs(conn._context, context)
            self.assertIsNone(conn.sock)
            self.assertIsNone(conn._tunnel_host)
        finally:
            conn.close()

        # The unpatched default path builds HTTPSConnection with the expected
        # arguments; the patched class records and refuses to connect.
        built = []

        class Refusing:
            def __init__(self, host, port=None, *, timeout, context):
                built.append((host, port, timeout, context))
                self.sock = None

            def connect(self):
                raise OSError(SENTINEL)

            def close(self):
                built.append("closed")

        clock = FakeClock()
        ex = LiveExchange(contract(), clock=clock)
        with mock.patch.object(live_exchange.http.client, "HTTPSConnection", Refusing):
            with self.assertRaises(ExchangeFailed) as caught:
                ex.exchange(wire(), deadline=clock() + 7.0)
        self.assertFixedFailure(caught.exception, kind="CONNECT")
        self.assertEqual(built, [("a1-us.iotbing.com", 443, 7.0, ex._context), "closed"])

    def test_default_path_real_connect_hits_guard(self):
        clock = FakeClock()
        ex = LiveExchange(contract(), clock=clock)
        with self.assertRaises(sf.NetworkAttempt):
            ex.exchange(wire(), deadline=clock() + 7.0)
        self.assertEqual(sf.take_guard_hits(), 1)


# --------------------------------------------------------------------------
# Loopback TLS: the real ssl handshake and real http.client over a real
# kernel socket to 127.0.0.1. The offline guards stay installed; for these
# tests only, four guarded attributes are replaced by loopback-only wrappers
# (everything else still calls the guard) and the guard is put back in
# cleanup. "localhost" is answered numerically as 127.0.0.1, so no resolver
# is consulted.
# --------------------------------------------------------------------------

_LOOPBACK_V4 = "127.0.0.1"
_LOOPBACK_NAMES = ("localhost", _LOOPBACK_V4)
_EXEMPTED = (
    (socket, "getaddrinfo"),
    (socket, "create_connection"),
    (socket.socket, "connect"),
    (http.client.HTTPConnection, "connect"),
)


def permit_loopback_only():
    """Replace the four guarded attributes the real connect path needs with
    wrappers that pass loopback through to the saved originals and send
    anything else to the guard. Returns an idempotent restore function that
    reinstalls the guard itself. Refuses to run if the guard is not in place."""
    original = {}
    for owner, name in _EXEMPTED:
        if getattr(owner, name) is not sf._deny or (owner, name) not in sf._SAVED:
            raise AssertionError("offline guard not installed")
        original[(owner, name)] = sf._SAVED[(owner, name)]

    real_getaddrinfo = original[(socket, "getaddrinfo")]
    real_create_connection = original[(socket, "create_connection")]
    real_socket_connect = original[(socket.socket, "connect")]
    real_http_connect = original[(http.client.HTTPConnection, "connect")]

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host not in _LOOPBACK_NAMES or family not in (0, socket.AF_INET):
            return sf._deny()
        return real_getaddrinfo(_LOOPBACK_V4, port, socket.AF_INET, type, proto,
                                flags | socket.AI_NUMERICHOST)

    def create_connection(address, *args, **kwargs):
        if type(address) is not tuple or address[0] not in _LOOPBACK_NAMES:
            return sf._deny()
        return real_create_connection(address, *args, **kwargs)

    def socket_connect(self, address):
        if (self.family != socket.AF_INET or type(address) is not tuple
                or address[0] != _LOOPBACK_V4):
            return sf._deny()
        return real_socket_connect(self, address)

    def http_connect(self):
        if self.host not in _LOOPBACK_NAMES or self._tunnel_host is not None:
            return sf._deny()
        return real_http_connect(self)

    wrappers = {
        (socket, "getaddrinfo"): getaddrinfo,
        (socket, "create_connection"): create_connection,
        (socket.socket, "connect"): socket_connect,
        (http.client.HTTPConnection, "connect"): http_connect,
    }
    for (owner, name), wrapper in wrappers.items():
        setattr(owner, name, wrapper)

    def restore():
        for owner, name in _EXEMPTED:
            setattr(owner, name, sf._deny)

    return restore


def write_self_signed_localhost(directory):
    """In-memory self-signed EC certificate for DNS name ``localhost``,
    written as PEM key and cert into ``directory`` (a temporary directory)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False,
                                     decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(public),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path = os.path.join(directory, "key.pem")
    cert_path = os.path.join(directory, "cert.pem")
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class LoopbackTlsServer:
    """One-connection TLS HTTP server on 127.0.0.1 in a daemon thread. It
    reads the request head and exactly Content-Length body bytes, records
    them, then calls ``respond(server, tls_socket)``."""

    def __init__(self, cert_path, key_path, respond):
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(cert_path, key_path)
        self._respond = respond
        self.received = None
        self.errors = []
        self.release = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.bind((_LOOPBACK_V4, 0))
            self._listener.listen(1)
            self._listener.settimeout(10.0)
        except BaseException:
            self._listener.close()
            raise
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        conn = None
        tls = None
        try:
            conn, _peer = self._listener.accept()
            conn.settimeout(10.0)
            tls = self._context.wrap_socket(conn, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = tls.recv(65536)
                if not chunk:
                    raise ConnectionError("client closed before request head")
                data += chunk
            head, _sep, body = data.partition(b"\r\n\r\n")
            length = None
            for line in head.split(b"\r\n")[1:]:
                key, _colon, value = line.partition(b":")
                if key.strip().lower() == b"content-length":
                    length = int(value.strip())
            if length is None:
                raise ValueError("request without Content-Length")
            while len(body) < length:
                chunk = tls.recv(65536)
                if not chunk:
                    raise ConnectionError("client closed before request body")
                body += chunk
            self.received = head + b"\r\n\r\n" + body
            self._respond(self, tls)
        except Exception as exc:
            self.errors.append(type(exc).__name__)
        finally:
            for item in (tls, conn):
                if item is not None:
                    try:
                        item.close()
                    except Exception:
                        pass

    def close(self):
        self.release.set()
        self._listener.close()
        self._thread.join(10.0)
        if self._thread.is_alive():
            raise AssertionError("loopback server thread did not stop")


def fixed_response(body, *, declared=None, content_length=True):
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    if content_length:
        head += b"Content-Length: %d\r\n" % (len(body) if declared is None else declared)
    head += b"Connection: close\r\n\r\n"

    def respond(_server, tls):
        tls.sendall(head + body)
        try:
            tls.unwrap()  # clean close_notify so EOF is a TLS-level EOF
        except (OSError, ValueError):
            pass

    return respond


def stall(server, _tls):
    server.release.wait(10.0)


class LoopbackTlsTests(GuardedTestCase):
    """Real TLS handshake, real ssl socket under ``_DeadlineSocket`` and
    ``_DeadlineRaw``, real ``http.client`` reading with ``read1``/``isclosed``,
    through the production ``default_connection_factory``, to 127.0.0.1."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.cert_path, cls.key_path = write_self_signed_localhost(tmp.name)

    def setUp(self):
        super().setUp()
        self.addCleanup(permit_loopback_only())

    def serve(self, respond):
        server = LoopbackTlsServer(self.cert_path, self.key_path, respond)
        self.addCleanup(server.close)
        return server

    def factory_for(self, server, *, host="localhost", trust_test_ca=True):
        """Thin wrapper over the PRODUCTION default_connection_factory that
        points it at the loopback server and (by default) a context trusting
        only the test certificate."""
        test_context = ssl.create_default_context(cafile=self.cert_path)
        self.assertIs(test_context.check_hostname, True)
        self.assertEqual(test_context.verify_mode, ssl.CERT_REQUIRED)
        record = types.SimpleNamespace(calls=[], connections=[])

        def factory(route_host, port, *, timeout, context):
            record.calls.append((route_host, port, timeout, context))
            conn = live_exchange.default_connection_factory(
                host, server.port, timeout=timeout,
                context=test_context if trust_test_ca else context)
            record.connections.append(conn)
            return conn

        return factory, record

    def run_exchange(self, server, *, deadline_in=10.0, max_bytes=1_048_576, **kwargs):
        factory, record = self.factory_for(server, **kwargs)
        ex = LiveExchange(contract(), connection_factory=factory, clock=time.monotonic,
                          max_response_bytes=max_bytes)
        self.addCleanup(lambda: [conn.close() for conn in record.connections])
        try:
            return ex.exchange(wire(), deadline=time.monotonic() + deadline_in), record, ex
        finally:
            self.assertEqual(len(record.calls), 1)
            self.assertEqual(record.calls[0][:2], ("a1-us.iotbing.com", 443))
            self.assertIs(record.calls[0][3], ex._context)
            self.assertIs(type(record.connections[0]), http.client.HTTPSConnection)
            self.assertIsNone(record.connections[0].sock)  # closed on every path

    def assertExactRequest(self, server):
        self.assertIsNotNone(server.received)
        request_line, names, message, body = parse_sent(server.received)
        self.assertEqual(request_line, "POST /api.json HTTP/1.1")
        self.assertEqual("/api.json", boundary.API_PATH)
        self.assertEqual(body, BODY)
        expected = dict(live_exchange.fixed_headers())
        expected["Content-Length"] = str(len(BODY))
        expected["Host"] = "localhost:%d" % server.port
        self.assertEqual(sorted(names), sorted(expected))
        self.assertEqual(len(names), len(set(n.lower() for n in names)))
        for name, value in expected.items():
            self.assertEqual(message[name], value)

    def test_real_tls_exchange_round_trip(self):
        payload = b'{"success":true,"result":"loopback"}'
        server = self.serve(fixed_response(payload))
        result, _record, _ex = self.run_exchange(server)
        self.assertIs(type(result), bytes)
        self.assertEqual(result, payload)
        self.assertExactRequest(server)
        server.close()
        self.assertEqual(server.errors, [])

    def test_real_tls_multi_chunk_body(self):
        payload = bytes(range(256)) * 1200  # 307200 bytes: several read1 calls
        server = self.serve(fixed_response(payload))
        result, _record, _ex = self.run_exchange(server)
        self.assertEqual(result, payload)
        self.assertExactRequest(server)
        server.close()
        self.assertEqual(server.errors, [])

    def test_real_tls_oversize_response_fails(self):
        limit = 100
        for respond in (fixed_response(b"x" * (limit + 1)),
                        fixed_response(b"x" * 5000, content_length=False)):
            server = self.serve(respond)
            with self.assertRaises(ExchangeFailed) as caught:
                self.run_exchange(server, max_bytes=limit)
            self.assertFixedFailure(caught.exception, kind="LENGTH")
            self.assertExactRequest(server)
            server.close()

    def test_real_tls_stalled_server_times_out(self):
        server = self.serve(stall)
        deadline_in = 0.75
        started = time.monotonic()
        with self.assertRaises(ExchangeTimeout) as caught:
            self.run_exchange(server, deadline_in=deadline_in)
        elapsed = time.monotonic() - started
        self.assertFixedFailure(caught.exception, ExchangeTimeout)
        self.assertGreaterEqual(elapsed, deadline_in - 0.1)
        self.assertLess(elapsed, deadline_in + 1.5)
        self.assertExactRequest(server)
        server.close()
        self.assertEqual(server.errors, [])

    def test_real_tls_truncated_body_fails(self):
        server = self.serve(fixed_response(b"01234", declared=10))
        with self.assertRaises(ExchangeFailed) as caught:
            self.run_exchange(server)
        self.assertFixedFailure(caught.exception, kind="LENGTH")
        self.assertExactRequest(server)

    def test_real_tls_rejects_untrusted_and_mismatched_certificate(self):
        # The production context (system CAs only) does not trust the
        # self-signed certificate; the test CA with host 127.0.0.1 fails the
        # hostname check (the certificate names only DNS:localhost).
        for kwargs in ({"trust_test_ca": False}, {"host": _LOOPBACK_V4}):
            server = self.serve(fixed_response(b"{}"))
            with self.assertRaises(ExchangeFailed) as caught:
                self.run_exchange(server, **kwargs)
            self.assertFixedFailure(caught.exception, kind="CONNECT")
            server.close()
            self.assertIsNone(server.received)

    def test_loopback_exemption_is_narrow_and_restorable(self):
        for call in (lambda: socket.getaddrinfo("a1-us.iotbing.com", 443),
                     lambda: socket.getaddrinfo("localhost", 443, socket.AF_INET6),
                     lambda: socket.create_connection(("192.0.2.1", 443), 0.1),
                     lambda: http.client.HTTPSConnection("a1-us.iotbing.com", 443).connect()):
            with self.assertRaises(sf.NetworkAttempt):
                call()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(sf.NetworkAttempt):
                probe.connect(("192.0.2.1", 443))
        finally:
            probe.close()
        self.assertEqual(sf.take_guard_hits(), 5)
        # Restoring reinstalls the guard; restoring again is harmless.
        for owner, name in _EXEMPTED:
            self.assertIsNot(getattr(owner, name), sf._deny)
        self.doCleanups()
        for owner, name in _EXEMPTED:
            self.assertIs(getattr(owner, name), sf._deny)
        with self.assertRaises(sf.NetworkAttempt):
            socket.getaddrinfo("localhost", 443)
        self.assertEqual(sf.take_guard_hits(), 1)


def directed_request(directed="https://a1-sg.iotbing.com", country="1"):
    """A HOMES request built by the boundary for a directed route."""
    c = contract()
    route = boundary.directed_route(directed, boundary.select_route(country, c), c)
    profile = sf.SyntheticProfile()
    envelope = dict(profile.prepare(boundary.Operation.HOMES, {}), sid="synthetic-sid")
    request = boundary.build_request(boundary.Operation.HOMES, {}, route=route, envelope=envelope,
                                     contract=c, profile=profile)
    return route, request


class DirectedRouteTests(GuardedTestCase):
    def test_allowlisted_directed_request_accepted(self):
        for directed, host in (("https://a1-sg.iotbing.com", "a1-sg.iotbing.com"),
                               ("https://a1.tuyaus.com/api.json", "a1.tuyaus.com"),
                               ("https://a1-ueaz.tuyaus.com/", "a1-ueaz.tuyaus.com")):
            with self.subTest(host=host):
                route, request = directed_request(directed)
                self.assertNotIn(request.url, ROUTE_URLS)
                factory = Factory()
                self.assertEqual(exchange_with(factory, request), b'{"ok":1}')
                self.assertEqual(factory.calls[0][:2], (host, 443))
                request_line, _names, message, body = parse_sent(
                    bytes(factory.connections[0].mem.sent))
                self.assertEqual(request_line, "POST /api.json HTTP/1.1")
                self.assertEqual(message["Host"], host)
                self.assertEqual(body, request.body)
                self.assertClosed(factory)

    def test_unbound_or_altered_directed_url_refused(self):
        _route, request = directed_request()
        cases = [
            wire(url="https://a1-sg.iotbing.com/api.json", body=request.body),
            wire(url="https://evil.example/api.json", body=request.body),
        ]
        altered = []
        for url in ("https://evil.example/api.json", "https://a1-xx.iotbing.com/api.json",
                    "https://a1.tuya.com/api.json", "https://a1-sg.iotbing.com:443/api.json",
                    "http://a1-sg.iotbing.com/api.json"):
            _r, req = directed_request()
            object.__setattr__(req, "url", url)
            altered.append(req)
        _r, req = directed_request()
        object.__setattr__(req, "body", req.body + b"&x=1")
        altered.append(req)
        for candidate in cases + altered:
            with self.subTest(url=candidate.url):
                factory = Factory()
                with self.assertRaises(ExchangeFailed) as caught:
                    exchange_with(factory, candidate)
                self.assertFixedFailure(caught.exception, kind="BIND")
                self.assertEqual(factory.calls, [])

    def test_route_set_unchanged_by_directed_routes(self):
        directed_request()
        self.assertEqual(LiveExchange(contract())._route_urls, frozenset(ROUTE_URLS))


class StaticTests(unittest.TestCase):
    def test_no_redirect_proxy_retry_machinery(self):
        with open(live_exchange.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("set_tunnel", "urllib", "urlopen", "import requests", "aiohttp",
                          "import gzip", "import zlib", ".read(", "getenv", "os.environ",
                          "print("):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertIn("read1(", source)


def granted_permit():
    return ac.PasswordPermit._issue()



class ProductionHeaderAndBudgetTests(GuardedTestCase):
    """Plan 2026-11-17 Task 2: the fixed version User-Agent, and every
    connect timeout is the remaining budget (best-effort deadline; DNS
    inside ``socket.create_connection`` is not preemptible)."""

    def test_user_agent_is_fixed_version_header(self):
        version = manifest_version()
        self.assertRegex(version, r"\A[0-9]+\.[0-9]+\.[0-9]+\Z")
        factory = Factory()
        exchange_with(factory)
        _line, names, message, _body = parse_sent(bytes(factory.connections[0].mem.sent))
        self.assertEqual(message.get_all("User-Agent"), ["petsafe-freshfeed/" + version])
        self.assertEqual(sorted(names), sorted(["Host", "Content-Length", "Content-Type",
                                                "Accept-Encoding", "User-Agent", "Connection"]))
        self.assertEqual(message["Accept-Encoding"], "identity")
        self.assertEqual(message["Connection"], "close")
        self.assertEqual(message["Content-Type"], boundary.CONTENT_TYPE)
        # The value is derived from the manifest only: no host, user or
        # platform data, and the same for every call.
        self.assertEqual(live_exchange.fixed_headers(), live_exchange.fixed_headers())
        for bad in ("", "0.3", "1.2.3 extra", "1.2.3\r\nX: y", None, 3):
            with self.subTest(version=repr(bad)):
                with self.assertRaises(ValueError):
                    live_exchange._user_agent(bad)
        self.assertEqual(live_exchange._user_agent("0.3.0"), "petsafe-freshfeed/0.3.0")

    def test_connect_uses_remaining_budget_timeout(self):
        clock = FakeClock(1000.0)
        built = []

        def factory(host, port, *, timeout, context):
            conn = live_exchange.default_connection_factory(host, port, timeout=timeout,
                                                            context=context)
            built.append((timeout, conn))
            clock.now += 7.0  # time spent before the connect
            return conn

        seen = []

        def create_connection(address, timeout=None, source_address=None, *args, **kwargs):
            seen.append((address, timeout))
            raise OSError("synthetic refused")

        ex = LiveExchange(contract(), connection_factory=factory, clock=clock)
        # The real http.client connect (the offline guard replaces it), over a
        # recording socket.create_connection that refuses: no socket, no DNS.
        real_connect = sf._SAVED[(http.client.HTTPConnection, "connect")]
        with mock.patch.object(socket, "create_connection", create_connection), \
                mock.patch.object(http.client.HTTPConnection, "connect", real_connect):
            with self.assertRaises(ExchangeFailed) as caught:
                ex.exchange(wire(), deadline=clock() + 20.0)
        self.assertFixedFailure(caught.exception, kind="CONNECT")
        self.assertEqual([timeout for timeout, _conn in built], [20.0])
        self.assertEqual(seen, [(("a1-us.iotbing.com", 443), 13.0)])
        self.assertIsNone(built[0][1].sock)

    def test_connect_after_deadline_times_out_without_socket(self):
        clock = FakeClock(1000.0)

        def factory(host, port, *, timeout, context):
            conn = live_exchange.default_connection_factory(host, port, timeout=timeout,
                                                            context=context)
            clock.now += 30.0
            return conn

        seen = []
        ex = LiveExchange(contract(), connection_factory=factory, clock=clock)
        with mock.patch.object(socket, "create_connection",
                               lambda *a, **k: seen.append(a) or None):
            with self.assertRaises(ExchangeTimeout):
                ex.exchange(wire(), deadline=clock() + 20.0)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
