"""Final outbound wire boundary for the PetSafe (Thingclips) mobile account API.

Ported from the private harness ``boundary.py`` (2026-11-17), which passed
a real read-only login and discovery. Changes from the harness: the
contract ships inside this package and ``Contract.load`` no longer
re-verifies the pinned vendor source at runtime (the vendor source is not
shipped); the pinned-source check lives in the tests
(test_backend/test_account_boundary.py), which run it against a local
vendor copy when one is configured. Everything else is unchanged.

This module contains no networking, no DNS resolution, no credential
material and no signing constants: the single exchange is an injected
callable supplied by the caller, and every cryptographic transformation is
delegated to an opaque profile provider.

What this module does enforce:

* a closed set of four read operations with the exact action/version rows
  taken from the pinned public source (see ``protocol_contract.json``,
  ``SOURCE_REVIEW.md``);
* exactly one serialization of the final form bytes, matching the pinned
  source's handoff (vendor/tuya_mobile/client.py:149-154) through the
  installed aiohttp 3.11.16 urlencoded serializer semantics
  (formdata.py:123-139);
* an internal operation/business/envelope/route binding per prepared request,
  rechecked immediately before the exchange, so a dependency cannot swap the
  action, version, ciphertext, envelope or destination after validation;
* a fixed destination: HTTPS, exact lowercase contract host, ``/api.json``,
  no userinfo/query/fragment/port other than 443, no HTTP redirects;
* at most one server-directed API host per route (``directed_route``): the
  same destination checks plus a closed host allowlist
  (``_DIRECTED_API_HOSTS``), reported only as a fixed family label
  (``ApiDomain``), never as the host.

What it does NOT establish: actual signing correctness (no independent
PetSafe signer or envelope vectors exist), server acceptance, or runtime
provider/region selection. Those remain open qualifications recorded in
``protocol_contract.json`` and ``SOURCE_REVIEW.md``.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import enum
import hashlib
import ipaddress
import json
import os
import re
import threading
import urllib.parse
import weakref
from types import MappingProxyType

__all__ = [
    "ApiDomain",
    "BoundaryRejected",
    "Contract",
    "Operation",
    "Route",
    "WireRequest",
    "build_request",
    "directed_route",
    "is_directed_request",
    "select_route",
    "send",
    "validate_request",
]

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONTRACT_PATH = os.path.join(_HERE, "protocol_contract.json")
MAX_CONTRACT_BYTES = 64 * 1024

CONTENT_TYPE = "application/x-www-form-urlencoded"
METHOD = "POST"
API_PATH = "/api.json"
OWNED_KEYS = ("a", "v", "postData", "sign")
SEAL_OVERHEAD = 28  # 12-byte nonce + 16-byte GCM tag (client.py:51-54)
SIGN_HEX = re.compile(r"\A[0-9a-f]{64}\Z")
RSA_HEX = re.compile(r"\A[0-9a-f]+\Z")
GID_STR = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
TOKEN_STR = re.compile(r"\A[\x21-\x7e]{1,1024}\Z")
FORM_TOKEN = re.compile(r"\A[A-Za-z0-9_.~+%-]*\Z")
PERCENT = re.compile(r"%(?![0-9A-F]{2})")
ENVELOPE_VALUE = re.compile(r"\A[^\x00-\x1f\x7f]+\Z")
HOST = re.compile(r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+\Z")

# The closed enumerations this boundary will accept. A contract document that
# widens them (extra region, extra country, extra action, changed version) is
# not loadable.
_CLOSED_OPERATIONS = {
    "TOKEN": ("thing.m.user.username.token.get", "2.0"),
    "PASSWORD_LOGIN": ("thing.m.user.email.password.login", "3.0"),
    "HOMES": ("m.life.home.space.list", "1.0"),
    "DEVICES": ("m.life.my.group.device.list", "2.2"),
}
_CLOSED_REGIONS = {
    "AZ": "https://a1-us.iotbing.com/api.json",
    "EU": "https://a1.tuyaeu.com/api.json",
    "IN": "https://a1-in.iotbing.com/api.json",
    "RU": "https://a1.iot334.com/api.json",
}
_CLOSED_COUNTRY_ROUTES = {"1": "AZ", "44": "EU", "91": "IN", "7": "RU"}


class ApiDomain(enum.Enum):
    """Closed label naming only the API domain FAMILY a route uses. ``SAME``:
    the contract route, no server-directed host accepted. A label is chosen
    from ``_DIRECTED_API_HOSTS`` and never built from server text."""

    SAME = "SAME"
    IOTBING = "IOTBING"
    TUYAEU = "TUYAEU"
    TUYAUS = "TUYAUS"
    TUYACN = "TUYACN"
    TUYAIN = "TUYAIN"
    IOT334 = "IOT334"


# Closed allowlist for a server-directed API host (``mobileApiUrl`` in the
# login response; vendor/tuya_mobile/password_client.py:388-395 follows it
# without any check). Evidence for the domain families: the vendor endpoint
# list vendor/tuya_mobile/models.py:29-35 (a1.tuyaeu.com, a1.tuyaus.com,
# a1-sg.iotbing.com, a1.tuyacn.com, a1.tuyain.com) and protocol_contract.json
# ``regions``/``country_routes`` (a1-us.iotbing.com, a1.tuyaeu.com,
# a1-in.iotbing.com, a1.iot334.com). Entries:
#   ("exact", host, label)       the host must equal ``host``;
#   ("a1-label", domain, label)  the host must be exactly three labels: a
#                                first label matching ``a1[a-z0-9-]*`` (no
#                                trailing hyphen, at most 63 chars), then
#                                exactly ``domain``.
# No other wildcard; never caller-extensible.
_DIRECTED_API_HOSTS = (
    ("exact", "a1.tuyaeu.com", ApiDomain.TUYAEU),
    ("exact", "a1.tuyaus.com", ApiDomain.TUYAUS),
    ("exact", "a1.tuyacn.com", ApiDomain.TUYACN),
    ("exact", "a1.tuyain.com", ApiDomain.TUYAIN),
    ("a1-label", "iotbing.com", ApiDomain.IOTBING),
    ("a1-label", "tuyaeu.com", ApiDomain.TUYAEU),
    ("a1-label", "tuyaus.com", ApiDomain.TUYAUS),
    ("a1-label", "tuyacn.com", ApiDomain.TUYACN),
    ("a1-label", "tuyain.com", ApiDomain.TUYAIN),
    ("a1-label", "iot334.com", ApiDomain.IOT334),
)
_A1_LABEL = re.compile(r"\Aa1(?:[a-z0-9-]{0,60}[a-z0-9])?\Z")
MAX_DIRECTED_CHARS = 256
_SESSION_RULES = {"forbidden", "required"}
_VALUE_KINDS = {"country_code", "email", "rsa_hex", "login_token", "group_id"}
# Closed per-operation session rule and ordered business schema. Compared
# type-exactly (JSON text), so e.g. a literal false cannot become 0.
_CLOSED_SESSIONS = {
    "TOKEN": "forbidden",
    "PASSWORD_LOGIN": "forbidden",
    "HOMES": "required",
    "DEVICES": "required",
}
_CLOSED_BUSINESS = {
    "TOKEN": [
        ["countryCode", "country_code"],
        ["username", "email"],
        ["isUid", {"literal": False}],
    ],
    "PASSWORD_LOGIN": [
        ["countryCode", "country_code"],
        ["email", "email"],
        ["passwd", "rsa_hex"],
        ["options", {"literal": "{\"group\":1,\"mfaCode\":\"\"}"}],
        ["token", "login_token"],
        ["ifencrypt", {"literal": 1}],
    ],
    "HOMES": [],
    "DEVICES": [["gid", "group_id"]],
}
_CLOSED_ENVELOPE = {
    "order": [
        "a", "v", "clientId", "os", "appVersion", "channel", "osSystem",
        "sdkVersion", "deviceCoreVersion", "platform", "timeZoneId", "cp",
        "nd", "lang", "ttid", "et", "chKey", "deviceId", "time", "requestId",
        "postData", "appRnVersion", "bizDM", "sid", "sign",
    ],
    "literals": {"os": "Android", "cp": "gzip", "nd": "1"},
    "provider_supplied": [
        "clientId", "appVersion", "channel", "osSystem", "sdkVersion",
        "deviceCoreVersion", "platform", "timeZoneId", "lang", "ttid", "et",
        "chKey", "deviceId", "time", "requestId",
    ],
    "optional": ["appRnVersion", "bizDM"],
    "sign_keys": [
        "a", "appVersion", "chKey", "clientId", "deviceId", "et", "h5",
        "h5Token", "lang", "lat", "lon", "n4h5", "os", "postData",
        "requestId", "sid", "sp", "time", "ttid", "v",
    ],
    "session": "sid",
    "seal_request_id_field": "requestId",
    "boundary_owned": list(OWNED_KEYS),
}
_CLOSED_FORM_LIMITS = {
    "method": METHOD,
    "content_type": CONTENT_TYPE,
    "max_body_bytes": 8192,
    "max_business_bytes": 4096,
    "max_envelope_value_chars": 256,
}


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _same(actual, expected) -> bool:
    """Type-exact structural equality via canonical JSON text."""
    try:
        left = json.dumps(_thaw(actual), sort_keys=True)
    except (TypeError, ValueError):
        return False
    return left == json.dumps(expected, sort_keys=True)


class BoundaryRejected(Exception):
    """Fixed, context-free rejection. Carries no message, value or cause."""

    def __init__(self) -> None:
        super().__init__()

    def __str__(self) -> str:
        return "boundary rejected"

    def __repr__(self) -> str:
        return "BoundaryRejected()"


def _reject() -> "BoundaryRejected":
    """Return a rejection whose traceback chain carries no foreign context."""
    error = BoundaryRejected()
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    return error


class Operation(enum.Enum):
    TOKEN = "TOKEN"
    PASSWORD_LOGIN = "PASSWORD_LOGIN"
    HOMES = "HOMES"
    DEVICES = "DEVICES"


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


_contract_permit = threading.local()


class Contract:
    """Immutable public-evidence contract. Never caller-extensible.

    Constructed only by ``Contract.load``; direct construction rejects. The
    closed enumerations below (operations, regions, country routes, business
    schema, envelope, form limits) are compared type-exactly, so a widened
    or edited document is not loadable.
    """

    def __init__(self, document, source_digest):
        if getattr(_contract_permit, "load", False) is not True:
            raise _reject()
        if not isinstance(document, dict) or not isinstance(source_digest, str):
            raise _reject()
        if not source_digest:
            raise _reject()
        self._document = _freeze(json.loads(json.dumps(document)))
        self._source_digest = source_digest
        self._validate()

    # -- validation ------------------------------------------------------
    def _validate(self) -> None:
        doc = self._document
        if doc.get("schema") != "petsafe-offline-wire-contract/1":
            raise _reject()
        operations = doc.get("operations")
        if not isinstance(operations, MappingProxyType):
            raise _reject()
        if set(operations) != set(_CLOSED_OPERATIONS):
            raise _reject()
        for name, expected in _CLOSED_OPERATIONS.items():
            row = operations[name]
            if (row.get("action"), row.get("version")) != expected:
                raise _reject()
            if row.get("session") not in _SESSION_RULES:
                raise _reject()
            if row.get("session") != _CLOSED_SESSIONS[name]:
                raise _reject()
            if not _same(row.get("business"), _CLOSED_BUSINESS[name]):
                raise _reject()
            for entry in row.get("business", ()):  # ordered business schema
                if not isinstance(entry, tuple) or len(entry) != 2:
                    raise _reject()
                key, kind = entry
                if not isinstance(key, str) or not key:
                    raise _reject()
                if isinstance(kind, MappingProxyType):
                    if set(kind) != {"literal"}:
                        raise _reject()
                elif kind not in _VALUE_KINDS:
                    raise _reject()
        regions = doc.get("regions")
        routes = doc.get("country_routes")
        if not isinstance(regions, MappingProxyType) or not isinstance(routes, MappingProxyType):
            raise _reject()
        if dict(regions) != _CLOSED_REGIONS or dict(routes) != _CLOSED_COUNTRY_ROUTES:
            raise _reject()
        envelope = doc.get("envelope")
        if not isinstance(envelope, MappingProxyType):
            raise _reject()
        order = envelope.get("order")
        if not isinstance(order, tuple) or len(set(order)) != len(order):
            raise _reject()
        if tuple(order[:2]) != ("a", "v") or order[-1] != "sign" or "postData" not in order:
            raise _reject()
        literals = envelope.get("literals")
        if not isinstance(literals, MappingProxyType):
            raise _reject()
        for group in ("provider_supplied", "optional", "sign_keys"):
            if not isinstance(envelope.get(group), tuple):
                raise _reject()
        if envelope.get("session") != "sid" or envelope.get("seal_request_id_field") != "requestId":
            raise _reject()
        if tuple(envelope.get("boundary_owned", ())) != OWNED_KEYS:
            raise _reject()
        known = set(literals) | set(envelope["provider_supplied"]) | set(envelope["optional"])
        known |= {"sid"} | set(OWNED_KEYS)
        if set(order) != known:
            raise _reject()
        for field, expected in _CLOSED_ENVELOPE.items():
            if not _same(envelope.get(field), expected):
                raise _reject()
        form = doc.get("form")
        if not isinstance(form, MappingProxyType):
            raise _reject()
        for field, expected in _CLOSED_FORM_LIMITS.items():
            if not _same(form.get(field), expected):
                raise _reject()
        if form.get("method") != METHOD or form.get("content_type") != CONTENT_TYPE:
            raise _reject()
        for limit in ("max_body_bytes", "max_business_bytes", "max_envelope_value_chars"):
            value = form.get(limit)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise _reject()

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, *, contract_path=None):
        """Read and validate the shipped contract (blocking file I/O: call it
        in the executor). The digest names the pinned source revision and
        the contract bytes' SHA-256."""
        path = DEFAULT_CONTRACT_PATH if contract_path is None else contract_path
        if not isinstance(path, str):
            raise _reject()
        raw, document = _read_json(path)
        digest = _contract_digest(raw, document)
        _contract_permit.load = True
        try:
            contract = cls(document, digest)
        finally:
            _contract_permit.load = False
        return contract

    # -- accessors -------------------------------------------------------
    @property
    def source_digest(self) -> str:
        return self._source_digest

    def row(self, operation):
        entry = self._operation(operation)
        return (entry["action"], entry["version"])

    def business_schema(self, operation):
        return self._operation(operation)["business"]

    def session_rule(self, operation):
        return self._operation(operation)["session"]

    def _operation(self, operation):
        if not isinstance(operation, Operation):
            raise _reject()
        return self._document["operations"][operation.name]

    def country_region(self, country_code):
        if not isinstance(country_code, str):
            raise _reject()
        region = self._document["country_routes"].get(country_code)
        if region is None:
            raise _reject()
        return region

    def region_url(self, region):
        if not isinstance(region, str):
            raise _reject()
        url = self._document["regions"].get(region)
        if url is None:
            raise _reject()
        return url

    @property
    def envelope(self):
        return self._document["envelope"]

    @property
    def limits(self):
        return self._document["form"]


def _read_json(path):
    raw = None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONTRACT_BYTES + 1)
        document = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        document = None
    if not isinstance(document, dict) or len(raw) > MAX_CONTRACT_BYTES:
        raise _reject()
    return raw, document


def _contract_digest(raw, document) -> str:
    source = document.get("source")
    revision = source.get("revision") if isinstance(source, dict) else None
    if not isinstance(revision, str) or not revision:
        raise _reject()
    return revision + "|contract=" + hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------

_permit = threading.local()


@dataclasses.dataclass(frozen=True)
class Route:
    country_code: str
    region: str
    url: str
    api_domain: ApiDomain = ApiDomain.SAME

    def __post_init__(self) -> None:
        if getattr(_permit, "route", False) is not True:
            raise _reject()

    def __repr__(self) -> str:  # region and fixed label only; never the host
        if getattr(self, "api_domain", None) is ApiDomain.SAME:
            return f"<Route {self.region}>"
        return f"<Route {self.region} directed>"


# Identity registry of routes issued by ``directed_route``: id -> (weak
# reference, field snapshot). A copy or forgery made past the constructor
# gate is never one of them, and an issued route mutated in place no longer
# matches its snapshot.
_DIRECTED_ROUTES = {}
_DIRECTED_LOCK = threading.Lock()
_DIRECTED_OPERATIONS = ("HOMES", "DEVICES")


def _route_fields(route):
    return (route.country_code, route.region, route.url, route.api_domain)


def _register_directed(route) -> None:
    key = id(route)

    def forget(reference, _key=key):
        entry = _DIRECTED_ROUTES.get(_key)
        if entry is not None and entry[0] is reference:
            _DIRECTED_ROUTES.pop(_key, None)
    reference = weakref.ref(route, forget)
    with _DIRECTED_LOCK:
        _DIRECTED_ROUTES[key] = (reference, _route_fields(route))


def _is_issued_directed(route) -> bool:
    entry = _DIRECTED_ROUTES.get(id(route))
    if entry is None or entry[0]() is not route:
        return False
    return entry[1] == _route_fields(route)


def select_route(country_code, contract) -> Route:
    """The only constructor of a Route. Unknown countries reject; no fallback."""
    if not isinstance(contract, Contract) or not isinstance(country_code, str):
        raise _reject()
    region = contract.country_region(country_code)
    url = contract.region_url(region)
    _permit.route = True
    try:
        route = Route(country_code, region, url)
    finally:
        _permit.route = False
    return route


def _check_route(route, contract) -> None:
    if not isinstance(route, Route) or not isinstance(contract, Contract):
        raise _reject()
    if not isinstance(route.country_code, str) or not isinstance(route.region, str):
        raise _reject()
    if not isinstance(route.url, str):
        raise _reject()
    label = getattr(route, "api_domain", None)
    if type(label) is not ApiDomain:
        raise _reject()
    if contract.country_region(route.country_code) != route.region:
        raise _reject()
    if label is ApiDomain.SAME:
        if contract.region_url(route.region) != route.url:
            raise _reject()
    else:
        if type(route) is not Route or not _is_issued_directed(route):
            raise _reject()
        if _directed_destination(route.url) != (route.url, label):
            raise _reject()
    _check_url(route.url, route)


def _allowlisted_label(host):
    """The fixed family label of an allowlisted host, else a rejection."""
    first, _, rest = host.partition(".")
    matches = set()
    for kind, value, label in _DIRECTED_API_HOSTS:
        if kind == "exact":
            if host == value:
                matches.add(label)
        elif kind == "a1-label":
            if rest == value and _A1_LABEL.match(first):
                matches.add(label)
        else:
            raise _reject()
    if len(matches) != 1:
        raise _reject()
    return matches.pop()


def _directed_destination(value):
    """Normalise and validate one server-directed API URL.

    Accepts the vendor's two spellings (password_client.py:388-395): a bare
    base (no path, or a single "/") which becomes base + ``/api.json``, or
    the full ``/api.json`` URL; any other path rejects. The candidate then
    passes the same destination checks as a contract route, and the host
    must match ``_DIRECTED_API_HOSTS``. Returns ``(canonical_url, label)``;
    the canonical form never carries an explicit port."""
    url, host = _directed_shape(value)
    return url, _allowlisted_label(host)


def directed_route(value, route, contract) -> Route:
    """Validate a server-directed API URL against ``route``.

    Returns ``route`` itself when ``value`` names the route's own destination;
    otherwise a new frozen ``Route`` with the ORIGINAL country code and region,
    the canonical directed URL and the matched family label. Only a contract
    route (label ``SAME``) can be redirected, so a run follows at most one
    directed host. Anything else is the fixed ``BoundaryRejected``."""
    if not isinstance(contract, Contract) or not isinstance(route, Route):
        raise _reject()
    _check_route(route, contract)
    url, _host = _directed_shape(value)
    if url == route.url:
        return route
    if route.api_domain is not ApiDomain.SAME:
        raise _reject()
    url, label = _directed_destination(value)
    _permit.route = True
    try:
        moved = Route(route.country_code, route.region, url, label)
    finally:
        _permit.route = False
    _register_directed(moved)
    _check_route(moved, contract)
    return moved


def _directed_shape(value):
    """Shape normalisation and the shared destination checks, without the
    host allowlist. Returns ``(canonical_url, host)``."""
    if type(value) is not str or not value or len(value) > MAX_DIRECTED_CHARS:
        raise _reject()
    if not value.isascii() or not value.startswith("https://"):
        raise _reject()
    netloc, slash, rest = value[len("https://"):].partition("/")
    if (slash + rest) not in ("", "/", API_PATH):
        raise _reject()
    host = _check_destination("https://" + netloc + API_PATH)
    if netloc not in (host, host + ":443"):
        raise _reject()
    return "https://" + host + API_PATH, host


def is_directed_request(request) -> bool:
    """True only for a ``WireRequest`` this boundary built for a route issued
    by ``directed_route``, unchanged since: URL and body equal the owned
    binding and the URL still passes the directed allowlist with the bound
    label. Never true for a hand-made request or an arbitrary URL."""
    if type(request) is not WireRequest:
        return False
    binding = _BINDINGS.get(request)
    if binding is None or type(binding.api_domain) is not ApiDomain:
        return False
    if binding.api_domain is ApiDomain.SAME:
        return False
    if binding.operation.name not in _DIRECTED_OPERATIONS:
        return False
    if type(request.url) is not str or request.url != binding.url:
        return False
    if type(request.body) is not bytes or request.body != binding.body:
        return False
    if request.method != binding.method or request.headers != binding.headers:
        return False
    try:
        destination = _directed_destination(request.url)
    except BoundaryRejected:
        destination = None
    return destination == (binding.url, binding.api_domain)


def _accepted_urls(route):
    host, _, path = route.url[len("https://"):].partition("/")
    return (route.url, f"https://{host}:443/{path}")


def _check_url(url, route) -> None:
    _check_destination(url)
    if url not in _accepted_urls(route):
        raise _reject()


def _check_destination(url) -> str:
    """The destination checks shared by contract and directed routes.
    Returns the validated lowercase host."""
    if not isinstance(url, str) or url != url.strip():
        raise _reject()
    if any(ch in url for ch in "\r\n\t \\[]"):
        raise _reject()
    if not url.startswith("https://"):
        raise _reject()
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        parts = None  # rejected below, outside the handler (no __context__)
    if parts is None:
        raise _reject()
    if parts.scheme != "https" or parts.query or parts.fragment:
        raise _reject()
    if "?" in url or "#" in url or ";" in url:
        raise _reject()
    if "@" in parts.netloc or parts.username or parts.password:
        raise _reject()
    host = parts.hostname
    if not isinstance(host, str) or host != host.lower() or not HOST.match(host):
        raise _reject()
    if host.endswith(".") or ".." in host or host.startswith("xn--") or ".xn--" in host:
        raise _reject()
    if host.rsplit(".", 1)[-1].isdigit() or host == "localhost" or host.endswith(".localhost"):
        raise _reject()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise _reject()
    if parts.netloc.split(":")[0] != host:
        raise _reject()
    try:
        port = parts.port
    except ValueError:
        port = -1  # rejected below, outside the handler (no __context__)
    if port not in (None, 443):
        raise _reject()
    if port == 443 and not parts.netloc.endswith(":443"):
        raise _reject()
    if parts.path != API_PATH:
        raise _reject()
    if len(host) > 253 or any(len(label) > 63 for label in host.split(".")):
        raise _reject()
    return host


# --------------------------------------------------------------------------
# WireRequest and its owned binding
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, eq=False)
class WireRequest:
    """Private frozen wire value; repr exposes no values.

    Equality is identity. Only an instance produced by ``build_request`` has
    an owned binding; any copy (``dataclasses.replace``) or hand-made
    instance is rejected by ``validate_request``/``send``.
    """

    method: str
    url: str
    headers: tuple
    body: bytes

    def __repr__(self) -> str:
        return f"<WireRequest {len(self.body) if isinstance(self.body, bytes) else 0}B>"

    __str__ = __repr__


@dataclasses.dataclass(frozen=True)
class _Binding:
    operation: Operation
    business: str          # canonical JSON text of the validated business
    business_bytes: bytes
    envelope: tuple        # ordered (key, value) pairs
    country_code: str
    region: str
    url: str
    api_domain: ApiDomain
    post_data: str
    signature: str
    method: str
    headers: tuple
    body: bytes
    source_digest: str


_BINDINGS = weakref.WeakKeyDictionary()

# One-shot PASSWORD_LOGIN bindings: a prepared password request that has been
# handed to an exchange once (successfully or not) is never validated or sent
# again. Marked under a lock before the exchange is called, so an ambiguous
# or failed exchange still consumes it and concurrent sends admit one.
_SENT_PASSWORD = weakref.WeakKeyDictionary()
_SEND_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Business and envelope validation
# --------------------------------------------------------------------------


def _check_business(operation, business, route, contract):
    schema = contract.business_schema(operation)
    if type(business) is not dict:
        try:
            keys = set(business.keys())
        except Exception:
            keys = None
        if keys is None:
            raise _reject()
        business = dict(business)
    expected_keys = [key for key, _ in schema]
    if set(business) != set(expected_keys):
        raise _reject()
    ordered = {}
    for key, kind in schema:
        value = business[key]
        if isinstance(kind, MappingProxyType):
            literal = kind["literal"]
            if type(value) is not type(literal) or value != literal:
                raise _reject()
        elif kind == "country_code":
            if not isinstance(value, str) or value != route.country_code:
                raise _reject()
            contract.country_region(value)
        elif kind == "email":
            _check_email(value)
        elif kind == "rsa_hex":
            if not isinstance(value, str) or not RSA_HEX.match(value):
                raise _reject()
            if len(value) % 2 or not 128 <= len(value) <= 2048:
                raise _reject()
        elif kind == "login_token":
            if not isinstance(value, str) or not TOKEN_STR.match(value):
                raise _reject()
        elif kind == "group_id":
            _check_group_id(value)
        else:
            raise _reject()
        ordered[key] = value
    return ordered


def _check_email(value) -> None:
    if not isinstance(value, str) or not 3 <= len(value) <= 254:
        raise _reject()
    if value.count("@") != 1 or value != value.strip():
        raise _reject()
    local, _, domain = value.partition("@")
    if not local or not domain or "." not in domain:
        raise _reject()
    if not ENVELOPE_VALUE.match(value) or any(ch.isspace() for ch in value):
        raise _reject()


def _check_group_id(value) -> None:
    if isinstance(value, bool):
        raise _reject()
    if isinstance(value, int):
        if not 1 <= value <= 2 ** 63 - 1:
            raise _reject()
        return
    if isinstance(value, str):
        if not GID_STR.match(value):
            raise _reject()
        return
    raise _reject()


def _business_bytes(ordered, contract) -> bytes:
    raw = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > contract.limits["max_business_bytes"]:
        raise _reject()
    return raw


def _check_envelope(operation, envelope, contract):
    spec = contract.envelope
    if type(envelope) is not dict:
        if not hasattr(envelope, "keys"):
            raise _reject()
        envelope = dict(envelope)
    literals = spec["literals"]
    required = set(literals) | set(spec["provider_supplied"])
    optional = set(spec["optional"])
    session = spec["session"]
    keys = set(envelope)
    if keys & set(OWNED_KEYS):
        raise _reject()
    if not required <= keys:
        raise _reject()
    if not keys <= (required | optional | {session}):
        raise _reject()
    rule = contract.session_rule(operation)
    if rule == "required" and session not in keys:
        raise _reject()
    if rule == "forbidden" and session in keys:
        raise _reject()
    limit = contract.limits["max_envelope_value_chars"]
    for key, value in envelope.items():
        if not isinstance(value, str) or not value or len(value) > limit:
            raise _reject()
        if not ENVELOPE_VALUE.match(value):
            raise _reject()
        if key in literals and value != literals[key]:
            raise _reject()
    return tuple((key, envelope[key]) for key in spec["order"] if key in envelope)


# --------------------------------------------------------------------------
# Form serialization and canonical signing string
# --------------------------------------------------------------------------


def _canonical_sign_string(params, contract) -> str:
    """Independent reimplementation of client.py:35-48 (no source import)."""
    sign_keys = set(contract.envelope["sign_keys"])
    parts = []
    for key in sorted(params):
        value = params[key]
        if key in sign_keys and value:
            if key == "postData":
                digest = hashlib.md5(value.encode("utf-8")).hexdigest()
                value = digest[8:16] + digest[0:8] + digest[24:32] + digest[16:24]
            parts.append(f"{key}={value}")
    return "||".join(parts)


def _serialize(pairs) -> bytes:
    """Byte-exact match for aiohttp 3.11.16 formdata.py:123-139 handling of
    a str->str mapping: urlencode(items, doseq=True, encoding='utf-8')."""
    return urllib.parse.urlencode(list(pairs), doseq=True, encoding="utf-8").encode()


def _parse_form(body, contract):
    if type(body) is not bytes or not body:
        raise _reject()
    if len(body) > contract.limits["max_body_bytes"]:
        raise _reject()
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raise _reject() from None
    pairs = []
    for segment in text.split("&"):
        if not segment or "=" not in segment:
            raise _reject()
        raw_key, _, raw_value = segment.partition("=")
        if not raw_key:
            raise _reject()
        pairs.append((_decode_token(raw_key), _decode_token(raw_value)))
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _reject()
    order = list(contract.envelope["order"])
    index = -1
    for key in keys:
        if key not in order:
            raise _reject()
        position = order.index(key)
        if position <= index:
            raise _reject()
        index = position
    return pairs


def _decode_token(token) -> str:
    if not FORM_TOKEN.match(token) or PERCENT.search(token):
        raise _reject()
    try:
        value = urllib.parse.unquote_to_bytes(token.replace("+", " ")).decode("utf-8")
    except UnicodeDecodeError:
        raise _reject() from None
    if urllib.parse.quote_plus(value, encoding="utf-8") != token:
        raise _reject()
    return value


# --------------------------------------------------------------------------
# Build / validate / send
# --------------------------------------------------------------------------

_FAILED = object()


def _call_provider(function, *arguments):
    """Call an opaque provider without letting its context or values escape."""
    if not callable(function):
        return _FAILED
    try:
        return function(*arguments)
    except Exception:
        return _FAILED


def build_request(operation, business, *, route, envelope, contract, profile) -> WireRequest:
    """Validate the typed operation, then serialize exactly one wire request."""
    if not isinstance(contract, Contract) or not isinstance(operation, Operation):
        raise _reject()
    _check_route(route, contract)
    _check_operation_route(operation, route)
    action, version = contract.row(operation)
    ordered_business = _check_business(operation, business, route, contract)
    business_bytes = _business_bytes(ordered_business, contract)
    envelope_pairs = _check_envelope(operation, envelope, contract)
    envelope_map = dict(envelope_pairs)
    request_id = envelope_map[contract.envelope["seal_request_id_field"]]

    sealed = _call_provider(getattr(profile, "seal", None), request_id, business_bytes)
    if sealed is _FAILED:
        raise _reject()
    _check_sealed(sealed, business_bytes)

    params = {"a": action, "v": version}
    params.update(envelope_map)
    params["postData"] = sealed
    canonical = _canonical_sign_string(params, contract)
    signature = _call_provider(getattr(profile, "sign", None), canonical)
    if signature is _FAILED:
        raise _reject()
    if not isinstance(signature, str) or not SIGN_HEX.match(signature):
        raise _reject()
    params["sign"] = signature

    pairs = [(key, params[key]) for key in contract.envelope["order"] if key in params]
    body = _serialize(pairs)
    headers = (("Content-Type", CONTENT_TYPE),)
    request = WireRequest(METHOD, route.url, headers, body)
    _BINDINGS[request] = _Binding(
        operation=operation,
        business=json.dumps(ordered_business, separators=(",", ":"), sort_keys=True,
                            ensure_ascii=False),
        business_bytes=business_bytes,
        envelope=envelope_pairs,
        country_code=route.country_code,
        region=route.region,
        url=route.url,
        api_domain=route.api_domain,
        post_data=sealed,
        signature=signature,
        method=METHOD,
        headers=headers,
        body=body,
        source_digest=contract.source_digest,
    )
    validate_request(request, operation=operation, expected_business=ordered_business,
                     route=route, contract=contract)
    return request


def _check_operation_route(operation, route) -> None:
    """A directed route serves only the session list operations: TOKEN and
    PASSWORD_LOGIN always use the contract route."""
    if route.api_domain is not ApiDomain.SAME and operation.name not in _DIRECTED_OPERATIONS:
        raise _reject()


def _check_sealed(sealed, business_bytes) -> None:
    if not isinstance(sealed, str) or not sealed:
        raise _reject()
    try:
        raw = base64.b64decode(sealed.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise _reject() from None
    if base64.b64encode(raw).decode("ascii") != sealed:
        raise _reject()
    if len(raw) != SEAL_OVERHEAD + len(business_bytes):
        raise _reject()


def validate_request(request, *, operation, expected_business, route, contract) -> None:
    """Revalidate the final bytes against this boundary's owned construction."""
    if not isinstance(contract, Contract) or not isinstance(operation, Operation):
        raise _reject()
    if not isinstance(request, WireRequest):
        raise _reject()
    _check_route(route, contract)
    _check_operation_route(operation, route)
    binding = _BINDINGS.get(request)
    if binding is None:
        raise _reject()
    if binding.operation is Operation.PASSWORD_LOGIN and request in _SENT_PASSWORD:
        raise _reject()
    if binding.source_digest != contract.source_digest:
        raise _reject()
    if binding.operation is not operation:
        raise _reject()
    if (binding.country_code, binding.region, binding.url) != (
            route.country_code, route.region, route.url):
        raise _reject()
    if binding.api_domain is not route.api_domain:
        raise _reject()

    expected_ordered = _check_business(operation, expected_business, route, contract)
    expected_text = json.dumps(expected_ordered, separators=(",", ":"), sort_keys=True,
                               ensure_ascii=False)
    if expected_text != binding.business:
        raise _reject()

    if request.method != METHOD or binding.method != METHOD:
        raise _reject()
    if not isinstance(request.headers, tuple) or request.headers != binding.headers:
        raise _reject()
    if request.headers != (("Content-Type", CONTENT_TYPE),):
        raise _reject()
    _check_url(request.url, route)

    # Independent structural revalidation of the final bytes, then exact
    # equality with the owned construction.
    pairs = _parse_form(request.body, contract)
    fields = dict(pairs)
    action, version = contract.row(operation)
    if fields.get("a") != action or fields.get("v") != version:
        raise _reject()
    if fields.get("postData") != binding.post_data:
        raise _reject()
    if fields.get("sign") != binding.signature:
        raise _reject()
    _check_sealed(fields["postData"], binding.business_bytes)
    envelope_map = dict(binding.envelope)
    if {k: v for k, v in fields.items() if k not in OWNED_KEYS} != envelope_map:
        raise _reject()
    _check_envelope(operation, envelope_map, contract)
    rebuilt = {"a": action, "v": version}
    rebuilt.update(envelope_map)
    rebuilt["postData"] = binding.post_data
    if _canonical_sign_string(rebuilt, contract) != _canonical_sign_string(
            {k: v for k, v in fields.items() if k != "sign"}, contract):
        raise _reject()
    rebuilt["sign"] = binding.signature
    expected_pairs = [(k, rebuilt[k]) for k in contract.envelope["order"] if k in rebuilt]
    if pairs != expected_pairs or _serialize(expected_pairs) != request.body:
        raise _reject()
    if request.body != binding.body:
        raise _reject()


def send(request, *, operation, expected_business, route, contract, exchange):
    """Validate immediately before the only injected exchange, then call it.

    A PASSWORD_LOGIN request is one-shot: its binding is consumed atomically
    before the exchange is called, and any later ``send`` or
    ``validate_request`` of the same request is a fixed rejection."""
    if not callable(exchange):
        raise _reject()
    validate_request(request, operation=operation, expected_business=expected_business,
                     route=route, contract=contract)
    if operation is Operation.PASSWORD_LOGIN:
        with _SEND_LOCK:
            if request in _SENT_PASSWORD:
                raise _reject()
            _SENT_PASSWORD[request] = True
    return exchange(request)
