"""Real HTTPS exchange for account discovery.

Ported from the private harness ``live_exchange.py``
(2026-11-17), which passed a real read-only login and discovery. Changes
from the harness: the fixed User-Agent is ``petsafe-freshfeed/<manifest
version>`` (``fixed_headers``), and with no supervisor worker to terminate,
the deadline is best-effort: every connect gets the remaining budget as its
timeout (``socket.create_connection`` inside ``http.client``), but DNS
resolution inside it is not preemptible, so a stalled resolver can outlast
the deadline. Everything else is unchanged.

One POST per call to one of the contract's fixed route URLs (or the one
allowlisted server-directed route ``boundary.directed_route`` issued, for a
request the boundary bound to it), over
``http.client.HTTPSConnection`` on port 443 with ``ssl.create_default_context()``
(hostname check on, certificate required). Fixed technical headers only
(``fixed_headers()`` plus ``Content-Length``; ``http.client`` adds ``Host``).

Refused by construction: redirects (HTTP 200 only, ``Location`` never read),
proxies and tunnels (``HTTPSConnection`` never consults the environment and
no CONNECT tunnel is configured), retries (one connect, one request, one
response), and HTTP-level content codings (any ``Content-Encoding`` other
than a single ``identity`` is rejected before the body is read).

The body is read with ``HTTPResponse.read1(65536)``-bounded calls; before
each call the absolute ``deadline`` (``time.monotonic()`` scale, via the
injected clock) and the byte ceiling are checked, and the socket timeout is
re-set to the time remaining. Below ``http.client`` every socket receive
(response head included) and every send is also preceded by a deadline
check and a socket-timeout re-arm (``_DeadlineSocket``), so a byte-by-byte
head cannot outlast the deadline either. Not bounded here: DNS resolution
and ``socket.create_connection``'s per-address connect timeout (each
address gets the remaining time); nothing bounds those further here. Deadline expiry and socket timeouts raise
``ExchangeTimeout`` (a ``TimeoutError``, which ``AccountClient`` maps to
``TIMEOUT``); every other failure raises one ``ExchangeFailed`` subclass
with a fixed class-level ``kind`` (``BIND``: local request binding or
validation refusal, the directed-route check included; ``CONNECT``: the
factory, connect, TLS or DNS, and any ``OSError`` before a response
exists; ``STATUS``: non-200; ``ENCODING``: Content-Encoding; ``LENGTH``:
declared or actual length, over-ceiling or truncation; ``IO``: any other
failure while sending or reading). The kind is chosen only by which check
or phase failed: exception text and the numeric status are never read.
All carry one fixed message and are raised outside any ``except`` block,
so neither ``__cause__`` nor ``__context__`` holds URL, header, body or
server text. The connection is closed on every path.
"""
import http.client
import io
import json
import math
import os
import re
import ssl
import time

from . import boundary
from . import client as account_client

__all__ = ["ExchangeBindFailed", "ExchangeConnectFailed", "ExchangeEncodingFailed",
           "ExchangeFailed", "ExchangeIOFailed", "ExchangeLengthFailed",
           "ExchangeStatusFailed", "ExchangeTimeout", "LiveExchange",
           "default_connection_factory", "fixed_headers"]

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "manifest.json")
_VERSION = re.compile(r"\A[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}\Z")


def _user_agent(version) -> str:
    """``petsafe-freshfeed/<version>`` for a plain ``X.Y.Z`` version; no host,
    user or platform data."""
    if type(version) is not str or not _VERSION.match(version):
        raise ValueError("invalid version")
    return "petsafe-freshfeed/" + version


def fixed_headers() -> tuple:
    """The fixed outbound headers (plan Global Constraints). Reads the
    integration manifest version: blocking file I/O, call it in the
    executor."""
    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        version = json.load(handle).get("version")
    return (
        ("Content-Type", boundary.CONTENT_TYPE),
        ("Accept-Encoding", "identity"),
        ("User-Agent", _user_agent(version)),
        ("Connection", "close"),
    )

HTTPS_PORT = 443
READ_CHUNK = 65536
_SCHEME = "https://"
_EXPECTED_WIRE_HEADERS = (("Content-Type", boundary.CONTENT_TYPE),)


class ExchangeFailed(Exception):
    """Fixed-message exchange failure; never carries wire or server text.
    ``kind`` is a fixed class attribute naming the failure class; the base
    class has none (``account_client`` maps it to plain ``TRANSPORT``)."""

    kind = None

    def __init__(self):
        super().__init__("live exchange failed")


class ExchangeBindFailed(ExchangeFailed):
    """Local request binding or validation refusal (directed route included)."""

    kind = "BIND"


class ExchangeConnectFailed(ExchangeFailed):
    """Factory, connect, TLS or DNS failure, or an OSError before a response."""

    kind = "CONNECT"


class ExchangeStatusFailed(ExchangeFailed):
    """A response status other than 200 (the status itself is never kept)."""

    kind = "STATUS"


class ExchangeEncodingFailed(ExchangeFailed):
    """A Content-Encoding other than a single identity."""

    kind = "ENCODING"


class ExchangeLengthFailed(ExchangeFailed):
    """Declared or actual length over the ceiling, mismatched or truncated."""

    kind = "LENGTH"


class ExchangeIOFailed(ExchangeFailed):
    """Any other failure while sending the request or reading the response."""

    kind = "IO"


_FAILURE_CLASSES = (ExchangeBindFailed, ExchangeConnectFailed, ExchangeStatusFailed,
                    ExchangeEncodingFailed, ExchangeLengthFailed, ExchangeIOFailed)


class ExchangeTimeout(TimeoutError):
    """Fixed-message deadline or socket timeout."""

    def __init__(self):
        super().__init__("live exchange timed out")


def default_connection_factory(host, port, *, timeout, context):
    """Build (do not connect) the one HTTPS connection."""
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)


def _route_urls(contract):
    """The exact route URLs of the loaded contract (country -> region -> URL).

    ``Contract`` exposes per-country accessors but no country list; the keys
    are read from its validated, frozen document (equal to boundary's closed
    route table by ``Contract._validate``)."""
    urls = set()
    for country_code in contract._document["country_routes"]:
        urls.add(contract.region_url(contract.country_region(country_code)))
    return frozenset(urls)


def _split_route(url):
    """Host and path of a contract or directed route URL (already checked)."""
    if not url.startswith(_SCHEME):
        raise ExchangeBindFailed()
    host, sep, rest = url[len(_SCHEME):].partition("/")
    if not sep or not host or ":" in host or "@" in host:
        raise ExchangeBindFailed()
    path = "/" + rest
    if path != boundary.API_PATH:
        raise ExchangeBindFailed()
    return host, path


class _DeadlineRaw(io.RawIOBase):
    """Raw stream over the socket's unbuffered reader: every receive is
    preceded by the deadline check and a socket-timeout re-arm."""

    def __init__(self, inner, arm):
        super().__init__()
        self._inner = inner
        self._arm = arm

    def readable(self):
        return True

    def readinto(self, buffer):
        self._arm()
        return self._inner.readinto(buffer)

    def close(self):
        try:
            self._inner.close()
        finally:
            super().close()


class _DeadlineSocket:
    """The connected socket as ``http.client`` sees it after ``connect``."""

    def __init__(self, sock, arm):
        self._sock = sock
        self._arm = arm

    def settimeout(self, value):
        self._sock.settimeout(value)

    def sendall(self, data):
        self._arm()
        return self._sock.sendall(data)

    def makefile(self, mode="rb"):
        if mode != "rb":
            raise ExchangeIOFailed()
        return io.BufferedReader(_DeadlineRaw(self._sock.makefile("rb", buffering=0),
                                              self._arm))

    def close(self):
        self._sock.close()


class LiveExchange(account_client.Exchange):
    """One injected wire call. No retries, redirects, proxies or decoding."""

    def __init__(self, contract, *, connection_factory=None, clock=time.monotonic,
                 max_response_bytes: int = 1_048_576):
        if not isinstance(contract, boundary.Contract):
            raise ValueError("contract required")
        if (type(max_response_bytes) is not int or max_response_bytes < 1
                or max_response_bytes > account_client.MAX_RESPONSE_BYTES):
            raise ValueError("invalid response ceiling")
        if connection_factory is None:
            connection_factory = default_connection_factory
        if not callable(connection_factory) or not callable(clock):
            raise ValueError("invalid seam")
        context = ssl.create_default_context()
        if context.check_hostname is not True or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("unsafe TLS context")
        self._route_urls = _route_urls(contract)
        self._headers = fixed_headers()
        self._factory = connection_factory
        self._clock = clock
        self._max = max_response_bytes
        self._context = context

    def __repr__(self):
        return "LiveExchange(<redacted>)"

    def __reduce__(self):
        raise TypeError("LiveExchange is not picklable")

    # -- binding ----------------------------------------------------------
    def _bind(self, request, deadline):
        if not isinstance(request, boundary.WireRequest):
            raise ExchangeBindFailed()
        if type(request.method) is not str or request.method != boundary.METHOD:
            raise ExchangeBindFailed()
        if type(request.url) is not str:
            raise ExchangeBindFailed()
        # A contract route URL, or the URL of a request boundary built for a
        # route issued by ``boundary.directed_route`` (allowlisted, bound and
        # unchanged). Never an arbitrary URL.
        if request.url not in self._route_urls and not boundary.is_directed_request(request):
            raise ExchangeBindFailed()
        if type(request.headers) is not tuple or request.headers != _EXPECTED_WIRE_HEADERS:
            raise ExchangeBindFailed()
        if type(request.body) is not bytes:
            raise ExchangeBindFailed()
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ExchangeBindFailed()
        host, path = _split_route(request.url)
        return host, path, request.body

    def _remaining(self, deadline):
        remaining = max(0.0, float(deadline - self._clock()))
        if remaining <= 0.0:
            raise ExchangeTimeout()
        return remaining

    def _arm(self, sock, deadline):
        remaining = self._remaining(deadline)
        if sock is not None:
            sock.settimeout(remaining)

    # -- the one call -----------------------------------------------------
    def exchange(self, request, *, deadline):
        host, path, body = self._bind(request, deadline)
        failure = None
        result = None
        opened = []  # the connection and, once received, the response
        connected = [False]  # set once connect() returned with a socket
        try:
            timeout = self._remaining(deadline)
            opened.append(self._factory(host, HTTPS_PORT, timeout=timeout,
                                        context=self._context))
            result = self._exchange(opened, path, body, deadline, connected)
        except TimeoutError:  # includes ExchangeTimeout and socket timeouts
            failure = ExchangeTimeout
        except ExchangeFailed as error:
            # Only the class is kept, and only a closed one; never the text.
            failure = type(error) if type(error) in _FAILURE_CLASSES else ExchangeIOFailed
            del error
        except http.client.IncompleteRead:
            failure = ExchangeLengthFailed  # truncated body
        except Exception as error:
            # Factory, connect, TLS or DNS, or any OSError (ssl.SSLError
            # included) before a response exists, is CONNECT; anything else
            # while sending or reading is IO. Only the class is tested.
            before_response = len(opened) < 2
            if before_response and (not connected[0] or isinstance(error, OSError)):
                failure = ExchangeConnectFailed
            else:
                failure = ExchangeIOFailed
            del error
        finally:
            for item in reversed(opened):
                try:
                    item.close()
                except Exception:
                    pass
            del opened
        # Raised outside the except blocks: no __cause__/__context__ chain.
        if failure is not None:
            raise failure() from None
        return result

    def _exchange(self, opened, path, body, deadline, connected):
        connection = opened[0]
        connection.timeout = self._remaining(deadline)
        connection.connect()
        sock = connection.sock
        if sock is None:
            raise ExchangeConnectFailed()
        connection.sock = _DeadlineSocket(sock, lambda: self._arm(sock, deadline))
        connected[0] = True

        headers = dict(self._headers)
        headers["Content-Length"] = str(len(body))
        self._arm(sock, deadline)
        connection.request(boundary.METHOD, path, body=body, headers=headers)
        self._arm(sock, deadline)
        response = connection.getresponse()
        opened.append(response)
        self._remaining(deadline)

        if response.status != 200:
            raise ExchangeStatusFailed()
        encodings = response.msg.get_all("Content-Encoding") or []
        if len(encodings) > 1:
            raise ExchangeEncodingFailed()
        if encodings and encodings[0].strip().lower() != "identity":
            raise ExchangeEncodingFailed()
        declared = response.length
        if declared is not None and declared > self._max:
            raise ExchangeLengthFailed()

        chunks = []
        total = 0
        while not response.isclosed():
            # isclosed(): http.client has released the socket after the
            # declared length or the final chunk; nothing left to arm.
            self._arm(sock, deadline)
            want = min(READ_CHUNK, self._max + 1 - total)
            chunk = response.read1(want)
            if type(chunk) is not bytes:
                raise ExchangeIOFailed()
            if not chunk:
                break
            total += len(chunk)
            if total > self._max:
                raise ExchangeLengthFailed()
            chunks.append(chunk)
        self._remaining(deadline)
        if declared is not None and total != declared:
            raise ExchangeLengthFailed()
        return b"".join(chunks)
