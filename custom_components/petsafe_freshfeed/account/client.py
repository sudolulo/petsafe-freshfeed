"""Minimal PetSafe (Thingclips) account client: token -> encrypted
email/password login -> homes -> per-home device lists, reduced at once to
fixed outcomes, counts and the supported feeders' setup material.

Ported from the private harness ``account_client.py``
(2026-11-17), which passed a real read-only login and discovery (1 home,
1 FreshFeed, a 16-character local key present). Changes from the harness:
the harness success outcome is renamed ``OK``; the supervisor's
per-run nonce and process ``AttemptBudget`` are replaced by a per-call
``PasswordPermit`` that ``account.discover`` issues (Home Assistant cannot
spawn a supervised worker; the config flow adds a per-``hass`` cooldown);
``TransientCredentials`` is ``SignIn``; and a supported device record's
``localKey`` and ``name`` are now returned as ``Feeder`` values instead of
only being counted.

Every request is built and revalidated by ``boundary.py`` and handed to
exactly one injected ``Exchange``; every cryptographic step (envelope
preparation, sealing, signing, password encryption, response decoding) is
delegated to an injected ``OpaqueProfile``.

Response shapes enforced here narrow the pinned library's tolerant selectors
(vendor/tuya_mobile/client.py:177-221, vendor/tuya_mobile/password_client.py:
113-171): unknown sibling keys are ignored and nested ``result`` dicts are
unwrapped (at most MAX_UNWRAP levels, ``unwrap_result``), but the required
fields keep primary names only and strict type checks. Every level is
checked for errorCode, ``success: false`` and a non-empty ``errorMsg``.
Home and device records are found by a bounded walk of every nested object
(``_walk_records``, after vendor client.py:67-74 and 197-215), not at one
fixed level; every non-record dict rejects explicit more-pages signals
(``hasMore`` true, or an int ``total``/``totalCount`` above the records in
its whole subtree). HOMES and DEVICES plaintexts may also be a bare JSON
array, read as ``{"result": <array>}`` by the profile. A HOMES body with no
list and no record is NO_LIST; a DEVICES body like that is an empty home.
Token and login keep their exact-level rules. None of this is a proven
server schema.

A device record must carry a non-null string id alias
(``devId``/``id``/``deviceId``) AND a product alias
(``productId``/``product_id``); an object with only an id, or with a
non-string id (a group, mesh or room), is walked into, not an error. A null
alias value counts as absent. If a DEVICES response yields no record but
skipped at least one object with a string id and no product alias, the run
stops with RECORD_PRODUCT instead of reporting 0 devices. Several alias
keys on one record are accepted only when their non-null values are equal
with the same type; any disagreement is RECORD_SHAPE.

An exchange failure is labelled by the closed ``kind`` of its
``exchange.ExchangeFailed`` subclass (``TRANSPORT_BIND`` ...
``TRANSPORT_IO``), or plain ``TRANSPORT``.

Every failure also carries two closed labels chosen only by this code
(``Stage``, ``Reason``): which step failed and which category of check.
No server text, host, email or credential can reach them.
"""
from __future__ import annotations

import base64 as _b64
import dataclasses as _dataclasses
import enum as _enum
import json as _json
import math as _math
import re as _re
import sys as _sys
import threading as _threading
import time as _time
import unicodedata as _unicodedata

from . import boundary as _boundary

__all__ = [
    "AccountClient",
    "ApiDomain",
    "Cancelled",
    "DEFAULT_FEEDER_NAME",
    "Discovery",
    "Exchange",
    "Feeder",
    "OpaqueProfile",
    "Outcome",
    "PasswordPermit",
    "PreparedCall",
    "Reason",
    "SafeResult",
    "SignIn",
    "Stage",
    "unwrap_result",
]

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_HOMES = 16
MAX_DEVICES = 128
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_UNWRAP = 4
# Bounds of the home/device record walk (on top of MAX_DEPTH/MAX_NODES):
# containers deeper than MAX_WALK_DEPTH (root = 1) or more than
# MAX_WALK_NODES visited containers are LIMIT_EXCEEDED.
MAX_WALK_DEPTH = 10
MAX_WALK_NODES = 2048

OBSERVED_PRODUCT_ID = "knjcty4hcaby0qaa"

UNRESOLVED_DEPENDENCIES = (
    "No independent PetSafe signer/envelope vector exists: signing and response "
    "decoding are verified only by one live read-only check (2026-11-17); "
    "synthetic providers prove plumbing only.",
    "Runtime password charset and independent RSA/MD5 encryption vectors are "
    "unverified.",
    "Live response shapes for token, login, homes and device lists are "
    "synthetic-only: unknown keys are ignored, nested result dicts are "
    "unwrapped up to 4 descents with error checks at every level "
    "(errorCode, success false, non-empty errorMsg), required keys stay "
    "strict, home/device records are found by a bounded walk of nested "
    "objects and every non-record level rejects explicit more-pages "
    "signals counted over its subtree; no server schema is proven.",
    "The observed product id knjcty4hcaby0qaa is counted by product id only; "
    "server-side product metadata is not verified.",
    "Server error codes mapped to terminal outcomes are a fixed synthetic "
    "table, not a proven server vocabulary.",
)


# Closed API domain family label (boundary.ApiDomain); never a host.
ApiDomain = _boundary.ApiDomain

_DEVICE_ID = _re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_PRODUCT_ID = _re.compile(r"\A[A-Za-z0-9]{1,64}\Z")
_DIGITS = _re.compile(r"\A[0-9]{1,1300}\Z")
_LOWER_HEX = _re.compile(r"\A[0-9a-f]+\Z")


class Outcome(_enum.Enum):
    """Fixed outcome of one discovery (the harness outcomes, with the
    harness success name renamed ``OK``). ``CLEANUP_FAILED`` is kept for parity and never produced:
    there is no worker to reap."""

    OK = "OK"
    REJECTED = "REJECTED"
    CHALLENGE = "CHALLENGE"
    UNSUPPORTED_PROFILE = "UNSUPPORTED_PROFILE"
    UNSUPPORTED_ROUTE = "UNSUPPORTED_ROUTE"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    STARTUP_FAILED = "STARTUP_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class Stage(_enum.Enum):
    """Closed label: which step a failure happened in. Chosen only by this
    harness's own code, never derived from server data."""

    NONE = "NONE"
    SETUP = "SETUP"
    TOKEN = "TOKEN"
    LOGIN = "LOGIN"
    HOMES = "HOMES"
    DEVICES = "DEVICES"
    RESULT = "RESULT"


class Reason(_enum.Enum):
    """Closed label: which category of check failed. Chosen only by this
    harness's own code, never derived from server data.

    ``SESSION_REJECTED``: the recognized invalid-session error code.
    ``SERVER_ERROR_CODE``: any other non-empty string ``errorCode``.
    ``SERVER_ERROR_FLAG``: ``success`` false or a non-empty ``errorMsg``.
    ``TRANSPORT_BIND``/``_CONNECT``/``_STATUS``/``_ENCODING``/``_LENGTH``/
    ``_IO``: the exchange raised a ``exchange.ExchangeFailed`` whose
    fixed class-level ``kind`` is that closed value (local request binding;
    connect, TLS or DNS before a response; non-200 status; content
    encoding; length or truncation; any other send/read failure).
    ``TRANSPORT``: an ``ExchangeFailed`` without a recognised kind. Only the
    class attribute is read, validated against a closed table; the
    exception's text, instance attributes and status are never read.
    ``NONE`` on a failure with a non-NONE stage means any other unexpected
    exception (from the exchange or internal)."""

    NONE = "NONE"
    SERVER_ERROR_CODE = "SERVER_ERROR_CODE"
    SESSION_REJECTED = "SESSION_REJECTED"
    SERVER_ERROR_FLAG = "SERVER_ERROR_FLAG"
    OUTER_SHAPE = "OUTER_SHAPE"
    SEAL = "SEAL"
    DECODE = "DECODE"
    UNWRAP_DEPTH = "UNWRAP_DEPTH"
    MISSING_FIELD = "MISSING_FIELD"
    FIELD_TYPE = "FIELD_TYPE"
    NO_LIST = "NO_LIST"
    RECORD_SHAPE = "RECORD_SHAPE"
    RECORD_ID = "RECORD_ID"
    RECORD_PRODUCT = "RECORD_PRODUCT"
    PAGINATION = "PAGINATION"
    DIRECTED_URL = "DIRECTED_URL"
    DIRECTED_CONFLICT = "DIRECTED_CONFLICT"
    LIMIT = "LIMIT"
    BUDGET = "BUDGET"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    PROFILE = "PROFILE"
    WALK_BOUNDS = "WALK_BOUNDS"
    TRANSPORT = "TRANSPORT"
    TRANSPORT_BIND = "TRANSPORT_BIND"
    TRANSPORT_CONNECT = "TRANSPORT_CONNECT"
    TRANSPORT_STATUS = "TRANSPORT_STATUS"
    TRANSPORT_ENCODING = "TRANSPORT_ENCODING"
    TRANSPORT_LENGTH = "TRANSPORT_LENGTH"
    TRANSPORT_IO = "TRANSPORT_IO"


# Name of the sibling exchange module (harness ``live_exchange``).
_EXCHANGE_MODULE = __package__ + ".exchange"

# Closed table: exchange.ExchangeFailed class-level ``kind`` -> Reason.
# Any other kind (absent, unknown, non-str) is plain TRANSPORT.
_TRANSPORT_REASONS = {
    "BIND": Reason.TRANSPORT_BIND,
    "CONNECT": Reason.TRANSPORT_CONNECT,
    "STATUS": Reason.TRANSPORT_STATUS,
    "ENCODING": Reason.TRANSPORT_ENCODING,
    "LENGTH": Reason.TRANSPORT_LENGTH,
    "IO": Reason.TRANSPORT_IO,
}


def _transport_reason(error_type):
    """The closed Reason for an ExchangeFailed class: its class-level
    ``kind`` looked up in ``_TRANSPORT_REASONS`` (exact ``str`` only)."""
    try:
        kind = getattr(error_type, "kind", None)
    except Exception:
        kind = None
    if type(kind) is not str:
        return Reason.TRANSPORT
    return _TRANSPORT_REASONS.get(kind, Reason.TRANSPORT)


def _transport_stop(error_type):
    """The terminal stop for an ExchangeFailed class (literal reasons only)."""
    reason = _transport_reason(error_type)
    if reason is Reason.TRANSPORT_BIND:
        return _Stop(Outcome.PROTOCOL_ERROR, Reason.TRANSPORT_BIND)
    if reason is Reason.TRANSPORT_CONNECT:
        return _Stop(Outcome.PROTOCOL_ERROR, Reason.TRANSPORT_CONNECT)
    if reason is Reason.TRANSPORT_STATUS:
        return _Stop(Outcome.PROTOCOL_ERROR, Reason.TRANSPORT_STATUS)
    if reason is Reason.TRANSPORT_ENCODING:
        return _Stop(Outcome.PROTOCOL_ERROR, Reason.TRANSPORT_ENCODING)
    if reason is Reason.TRANSPORT_LENGTH:
        return _Stop(Outcome.PROTOCOL_ERROR, Reason.TRANSPORT_LENGTH)
    if reason is Reason.TRANSPORT_IO:
        return _Stop(Outcome.PROTOCOL_ERROR, Reason.TRANSPORT_IO)
    return _Stop(Outcome.PROTOCOL_ERROR, Reason.TRANSPORT)


# Fixed server error-code table (synthetic vocabulary). Unknown codes are
# PROTOCOL_ERROR. Error text is never read.
_ERROR_OUTCOMES = {
    "USER_CAPTCHA_REQUIRED": Outcome.CHALLENGE,
    "NEED_VERIFYCODE": Outcome.CHALLENGE,
    "ACCOUNT_LOCKED": Outcome.REJECTED,
    "USER_PASSWD_WRONG": Outcome.REJECTED,
    "ILLEGAL_CLIENT_ID": Outcome.UNSUPPORTED_PROFILE,
    "SIGN_INVALID": Outcome.UNSUPPORTED_PROFILE,
    "ENCRYPT_NOT_SUPPORTED": Outcome.UNSUPPORTED_PROFILE,
    "PASSWORD_DECRYPT_FAILED_IFENCRYPT": Outcome.UNSUPPORTED_PROFILE,
    "API_NOT_EXIST": Outcome.PROTOCOL_ERROR,
}


@_dataclasses.dataclass(frozen=True)
class SafeResult:
    """Only a fixed outcome, bounded nonnegative counts, the fixed API
    domain family label (``SAME`` unless a directed host was accepted) and
    the closed diagnostic ``stage``/``reason`` labels.

    ``stage`` and ``reason`` are diagnostics only: they are excluded from
    equality and hashing (``compare=False``), so equality keeps comparing
    outcome, counts and api_domain exactly as before. Success carries
    NONE/NONE; a NONE stage never carries a non-NONE reason.

    ``key_count``/``name_count`` (keyword, default 0) count the counted
    supported devices whose record carries a plausible ``localKey`` / a
    usable ``name`` (``_local_key``/``_name``). Only the counts exist here
    (the values travel separately as ``Feeder`` objects), so a SafeResult is
    safe to log. Each is at most ``supported_count``; a failure carries 0."""

    outcome: Outcome
    home_count: int
    device_count: int
    supported_count: int
    api_domain: ApiDomain = ApiDomain.SAME
    stage: Stage = _dataclasses.field(default=Stage.NONE, compare=False)
    reason: Reason = _dataclasses.field(default=Reason.NONE, compare=False)
    key_count: int = 0
    name_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, Outcome):
            raise ValueError("invalid outcome")
        if type(self.api_domain) is not ApiDomain:
            raise ValueError("invalid label")
        if type(self.stage) is not Stage or type(self.reason) is not Reason:
            raise ValueError("invalid label")
        if self.stage is Stage.NONE and self.reason is not Reason.NONE:
            raise ValueError("invalid label")
        if self.outcome is Outcome.OK and (
                self.stage is not Stage.NONE or self.reason is not Reason.NONE):
            raise ValueError("invalid label")
        counts = (self.home_count, self.device_count, self.supported_count,
                  self.key_count, self.name_count)
        for value in counts:
            if type(value) is not int or value < 0:
                raise ValueError("invalid count")
        if self.home_count > MAX_HOMES or self.device_count > MAX_DEVICES:
            raise ValueError("invalid count")
        if self.supported_count > self.device_count:
            raise ValueError("invalid count")
        if self.key_count > self.supported_count or self.name_count > self.supported_count:
            raise ValueError("invalid count")
        if self.outcome is not Outcome.OK and any(counts):
            raise ValueError("failure carries counts")


def _failure(outcome, api_domain=ApiDomain.SAME, stage=Stage.NONE,
             reason=Reason.NONE) -> SafeResult:
    return SafeResult(outcome, 0, 0, 0, api_domain, stage, reason)


class _Stop(Exception):
    """Internal terminal signal; carries only a fixed outcome and the closed
    ``Reason``/``Stage`` labels. ``stage`` None means "the stage the flow is
    in" (filled in by ``AccountClient._run``)."""

    def __init__(self, outcome, reason, stage=None) -> None:
        super().__init__()
        if type(reason) is not Reason or (stage is not None and type(stage) is not Stage):
            # A programming error: never let a non-enum value become a label.
            reason, stage = Reason.NONE, None
        self.outcome = outcome
        self.reason = reason
        self.stage = stage

    def __str__(self) -> str:
        return self.outcome.name

    __repr__ = __str__


class Cancelled(Exception):
    """Raised by an Exchange when its caller cancelled the call (kept from the
    harness; the production exchange never raises it)."""

    def __init__(self, *_args) -> None:
        super().__init__()

    def __str__(self) -> str:
        return "cancelled"

    __repr__ = __str__


class _Sealed:
    """Shared redaction for values that carry credentials or key material:
    fixed ``repr``/``str``/``format``, immutable, no ``__dict__``, never
    pickled or copied, not a dataclass (no ``asdict``)."""

    __slots__ = ()
    _LABEL = "<redacted>"

    def __setattr__(self, name, value):
        raise AttributeError("immutable")

    def __delattr__(self, name):
        raise AttributeError("immutable")

    def __repr__(self) -> str:
        return self._LABEL

    def __str__(self) -> str:
        return repr(self)

    def __format__(self, spec) -> str:
        return repr(self)

    def __reduce_ex__(self, protocol):
        raise TypeError("not serializable")

    def __reduce__(self):
        raise TypeError("not serializable")

    def __copy__(self):
        raise TypeError("not copyable")

    def __deepcopy__(self, memo):
        raise TypeError("not copyable")


class SignIn(_Sealed):
    """In-memory sign-in request (harness ``TransientCredentials``). Redacted
    repr; not picklable or copyable. Pass it straight to ``account.discover``
    and drop it; only ``country_code`` is readable.

    Single use: the first ``account.discover`` given this SignIn spends it
    (whatever the outcome) and clears the stored password when it returns;
    any later ``discover`` with the same SignIn sends nothing and returns
    ``Outcome.BUDGET_EXHAUSTED`` (stage ``SETUP``, reason ``BUDGET``). Build
    a new SignIn for every attempt."""

    __slots__ = ("_email", "_password", "_country_code", "_spent")
    _LABEL = "<SignIn redacted>"

    def __init__(self, email: str, password: str, country_code: str) -> None:
        object.__setattr__(self, "_email", email)
        object.__setattr__(self, "_password", password)
        object.__setattr__(self, "_country_code", country_code)
        object.__setattr__(self, "_spent", False)

    @property
    def country_code(self):
        return self._country_code


DEFAULT_FEEDER_NAME = "PetSafe FreshFeed"


class Feeder(_Sealed):
    """One supported feeder's setup material: device ID, LAN local key and a
    display name. Built only by the client; redacted repr; not picklable."""

    __slots__ = ("_device_id", "_local_key", "_name")
    _LABEL = "<Feeder redacted>"

    def __init__(self, *_args, **_kwargs) -> None:
        raise TypeError("Feeder is not constructible")

    @classmethod
    def _make(cls, device_id, local_key, name):
        feeder = object.__new__(cls)
        object.__setattr__(feeder, "_device_id", device_id)
        object.__setattr__(feeder, "_local_key", local_key)
        object.__setattr__(feeder, "_name", name)
        return feeder

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def local_key(self) -> str:
        return self._local_key

    @property
    def name(self) -> str:
        return self._name


class Discovery(_Sealed):
    """Result of ``account.discover``: a fixed ``Outcome``, the supported
    feeders (empty on every failure) and the closed ``Stage``/``Reason``
    labels, which are for debug logging only."""

    __slots__ = ("_outcome", "_feeders", "_stage", "_reason", "_session")

    def __init__(self, outcome, feeders=(), *, stage=Stage.NONE, reason=Reason.NONE, session=None) -> None:
        if type(outcome) is not Outcome or type(stage) is not Stage or type(reason) is not Reason:
            raise ValueError("invalid label")
        feeders = tuple(feeders)
        if any(type(item) is not Feeder for item in feeders):
            raise ValueError("invalid feeder")
        if outcome is not Outcome.OK and feeders:
            raise ValueError("failure carries feeders")
        if outcome is Outcome.OK and (stage is not Stage.NONE or reason is not Reason.NONE):
            raise ValueError("invalid label")
        object.__setattr__(self, "_outcome", outcome)
        object.__setattr__(self, "_feeders", feeders)
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_stage", stage)
        object.__setattr__(self, "_reason", reason)

    @property
    def outcome(self):
        return self._outcome

    @property
    def session(self):
        """Opaque optional login-to-storage handoff; never in SafeResult."""
        return self._session

    @property
    def feeders(self):
        return self._feeders

    @property
    def stage(self):
        return self._stage

    @property
    def reason(self):
        return self._reason

    def __repr__(self) -> str:
        return f"<Discovery {self._outcome.name} feeders={len(self._feeders)}>"


class OpaqueProfile:
    """Opaque provider of envelope, seal, sign, password encryption and
    response decoding. The client never inspects its inputs or state."""

    def prepare(self, operation, business):
        raise NotImplementedError

    def seal(self, request_id, plaintext):
        raise NotImplementedError

    def sign(self, canonical):
        raise NotImplementedError

    def encrypt_password(self, password, token):
        raise NotImplementedError

    def decode_response(self, prepared, response):
        raise NotImplementedError


class Exchange:
    """One injected wire call. No retries or redirects."""

    def exchange(self, request, *, deadline):
        raise NotImplementedError


class PreparedCall:
    """Private per-request value: immutable WireRequest, operation/business/
    envelope binding and the opaque decoding context. Not constructible from
    outside; the decoding context is readable only while its own decode is in
    progress."""

    __slots__ = ("_operation", "_wire", "_business", "_request_id", "_active")

    def __init__(self, *_args, **_kwargs) -> None:
        raise TypeError("PreparedCall is not constructible")

    @classmethod
    def _make(cls, operation, wire, business, request_id):
        call = object.__new__(cls)
        object.__setattr__(call, "_operation", operation)
        object.__setattr__(call, "_wire", wire)
        object.__setattr__(call, "_business", business)
        object.__setattr__(call, "_request_id", request_id)
        object.__setattr__(call, "_active", False)
        return call

    def __setattr__(self, name, value):
        raise AttributeError("immutable")

    @property
    def operation(self):
        return self._operation

    @property
    def request_id(self):
        if self._active is not True:
            raise _boundary.BoundaryRejected()
        return self._request_id

    def _set_active(self, value) -> None:
        object.__setattr__(self, "_active", value)

    def __repr__(self) -> str:
        return f"<PreparedCall {self._operation.name}>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("not serializable")


class PasswordPermit:
    """One-use password permission, plus a separate one-use send latch bound
    to a single prepared call. Replaces the harness supervisor's per-run
    nonce: ``account.discover`` issues one granted permit per call through
    the private ``_issue``; a directly constructed permit grants nothing.
    Neither latch is ever recreated. Account-lock protection across calls
    (the harness process budget) is the config flow's per-``hass`` cooldown."""

    __slots__ = ("_lock", "_granted", "_claimed", "_sent")

    def __init__(self) -> None:
        self._lock = _threading.Lock()
        self._granted = False
        self._claimed = False
        self._sent = False

    @classmethod
    def _issue(cls):
        permit = cls()
        permit._granted = True
        return permit

    def claim(self) -> bool:
        with self._lock:
            if not self._granted or self._claimed:
                return False
            self._claimed = True
            return True

    def admit_send(self, call) -> bool:
        with self._lock:
            if call is None or not self._claimed or self._sent:
                return False
            self._sent = True
            return True

    def __repr__(self) -> str:
        return "<PasswordPermit>"

    def __reduce_ex__(self, protocol):
        raise TypeError("not serializable")


# --------------------------------------------------------------------------
# Bounded parsing and validation
# --------------------------------------------------------------------------


def _check_request_size(body) -> None:
    if type(body) is not bytes or len(body) > MAX_REQUEST_BYTES:
        raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)


def _scan_depth(text) -> None:
    """Bound JSON nesting before parsing (string-aware bracket scan)."""
    depth = 0
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)
        elif ch in "]}":
            depth -= 1


class _Malformed(Exception):
    pass


def _pairs_hook(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _Malformed()
        out[key] = value
    return out


def _bad_constant(_name):
    raise _Malformed()


def _finite_float(text):
    value = float(text)
    if not _math.isfinite(value):
        raise _Malformed()
    return value


def _parse_outer(response):
    if type(response) is not bytes:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE)
    if len(response) > MAX_RESPONSE_BYTES:
        raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)
    try:
        text = response.decode("utf-8")
    except UnicodeDecodeError:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE) from None
    _scan_depth(text)
    try:
        parsed = _json.loads(text, object_pairs_hook=_pairs_hook,
                             parse_constant=_bad_constant, parse_float=_finite_float)
    except Exception:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE) from None
    return parsed


_OUTER_KEYS = {"result": str, "t": int, "success": bool, "errorCode": str, "errorMsg": str}


def _check_outer(parsed) -> None:
    """Validate the outer envelope; terminal on any server error. Unknown
    keys are ignored; known keys keep their type checks."""
    if type(parsed) is not dict:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE)
    for key, kind in _OUTER_KEYS.items():
        if key in parsed and type(parsed[key]) is not kind:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE)
    if "errorCode" in parsed:
        if "result" in parsed or parsed.get("success") is True:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE)
        code = parsed["errorCode"]
        if not code:
            # An empty string code is not a server error code (Reason doc).
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE)
        if code == "USER_SESSION_INVALID":
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.SESSION_REJECTED)
        raise _Stop(_ERROR_OUTCOMES.get(code, Outcome.PROTOCOL_ERROR),
                    Reason.SERVER_ERROR_CODE)
    if parsed.get("success") is False or ("errorMsg" in parsed and parsed["errorMsg"]):
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.SERVER_ERROR_FLAG)
    if "errorMsg" in parsed:
        # An empty outer errorMsg stays terminal, as before; it is not a flag.
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.OUTER_SHAPE)
    if "result" not in parsed:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.MISSING_FIELD)
    sealed = parsed["result"]
    try:
        raw = _b64.b64decode(sealed.encode("ascii"), validate=True)
    except Exception:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.SEAL) from None
    if len(raw) <= _boundary.SEAL_OVERHEAD:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.SEAL)


def _check_decoded(decoded) -> None:
    """Bounded iterative structure check (depth, nodes, types, size) before
    any traversal. Uses only items()/iteration, never selector lookups."""
    if not isinstance(decoded, dict):
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.DECODE)
    stack = [(decoded, 1)]
    nodes = 0
    size = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)
        if isinstance(value, dict):
            for key, item in dict.items(value):
                if type(key) is not str:
                    raise _Stop(Outcome.PROTOCOL_ERROR, Reason.DECODE)
                size += len(key.encode("utf-8", "surrogatepass")) + 4
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            for item in list.__iter__(value):
                stack.append((item, depth + 1))
            size += 2
        elif value is None or type(value) is bool:
            size += 5
        elif type(value) is int:
            size += 20
        elif type(value) is float:
            if not _math.isfinite(value):
                raise _Stop(Outcome.PROTOCOL_ERROR, Reason.DECODE)
            size += 24
        elif type(value) is str:
            size += len(value.encode("utf-8", "surrogatepass")) + 2
        else:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.DECODE)
        if size > MAX_RESPONSE_BYTES:
            raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)


def _check_level_error(level) -> None:
    """Server error signalled inside a decoded payload level: mapped exactly
    like the outer envelope's errorCode (unknown code -> PROTOCOL_ERROR);
    ``success: false`` -> PROTOCOL_ERROR; a non-empty string ``errorMsg`` ->
    PROTOCOL_ERROR (vendor client.py:182). Every falsy errorCode (``None``,
    ``""``, ``0``, ``False``, ``[]``, ``{}``) is treated as absent, as the
    vendor does (client.py:182 tests truthiness); a truthy non-string
    errorCode is PROTOCOL_ERROR (FIELD_TYPE). Error text is only type- and
    emptiness-tested, never stored or emitted."""
    if "errorCode" in level:
        code = level["errorCode"]
        # Truthiness of a decoded JSON value only (never a custom object:
        # _check_decoded admits plain JSON types only).
        if code:
            if type(code) is str:
                if code == "USER_SESSION_INVALID":
                    raise _Stop(Outcome.PROTOCOL_ERROR, Reason.SESSION_REJECTED)
                raise _Stop(_ERROR_OUTCOMES.get(code, Outcome.PROTOCOL_ERROR),
                            Reason.SERVER_ERROR_CODE)
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
    if "success" in level and level["success"] is False:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.SERVER_ERROR_FLAG)
    if "errorMsg" in level and type(level["errorMsg"]) is str and level["errorMsg"]:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.SERVER_ERROR_FLAG)


def unwrap_result(decoded):
    """Shared unwrap of a decoded payload (vendor client.py:177-178): check
    the level for a server error, then while ``result`` is a dict descend,
    at most MAX_UNWRAP levels (deeper -> PROTOCOL_ERROR), checking each level.
    Returns the innermost dict level; unknown sibling keys are ignored. Uses
    only plain key tests and subscripts on dicts; raises only ``_Stop``."""
    if not isinstance(decoded, dict):
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.DECODE)
    level = decoded
    descents = 0
    while True:
        _check_level_error(level)
        if "result" not in level:
            return level
        inner = level["result"]
        if not isinstance(inner, dict):
            return level
        if descents >= MAX_UNWRAP:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.UNWRAP_DEPTH)
        descents += 1
        level = inner


def _alias_values(record, names):
    """The non-null values of the alias keys present in ``record``, in
    ``names`` order. A key whose value is ``null`` counts as absent (the
    vendor's selectors skip falsy values; only ``None`` is skipped here:
    falsy-but-non-null values such as ``0``, ``""`` and ``False`` stay
    values and fail the caller's id validation)."""
    values = []
    for name in names:
        if name in record:
            value = record[name]
            if value is not None:
                values.append(value)
    return values


def _alias(record, names):
    """The value of the alias keys present in ``record`` with a non-null
    value (``_alias_values``); with none, the alias is not found. Several
    such keys are accepted only when every value is equal with the same type
    (no coercion: ``"1"`` and ``1``, or ``1`` and ``True``, disagree); any
    disagreement is RECORD_SHAPE."""
    values = _alias_values(record, names)
    if not values:
        return None, False
    value = values[0]
    for other in values[1:]:
        if type(other) is not type(value) or other != value:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_SHAPE)
    return value, True


_HOME_ID_KEYS = ("gid", "groupId", "homeId")
# ``deviceId`` after vendor client.py:212.
_DEVICE_ID_KEYS = ("devId", "id", "deviceId")
_PRODUCT_KEYS = ("productId", "product_id")
# Keys that make a skipped dict an entity (room, group, mesh, home) rather
# than a page wrapper, for the pagination ``total`` rule in _walk_records.
_ENTITY_ID_KEYS = _HOME_ID_KEYS + _DEVICE_ID_KEYS + ("meshId", "roomId")
# Order in which a device's id is picked from its string aliases: the
# vendor's ``devId or deviceId or id`` (vendor client.py:212).
_DEVICE_PICK_ORDER = ("devId", "deviceId", "id")


def _walk_records(decoded, id_keys, require_keys=(), *, string_ids=False, empty_ok=False):
    """Bounded walk of a decoded HOMES/DEVICES payload, modelled on the
    vendor's ``_walk`` (vendor/tuya_mobile/client.py:67-74, 197-215): every
    nested dict and list is visited, pre-order, in document order.

    - ``unwrap_result`` runs first, unchanged: its error checks and the
      MAX_UNWRAP limit on nested ``result`` dicts still apply.
    - Containers deeper than MAX_WALK_DEPTH (root = 1), or more than
      MAX_WALK_NODES visited containers, are LIMIT_EXCEEDED (WALK_BOUNDS).
    - ``_check_level_error`` runs on every visited dict, records included.
    - Record test, for a non-root dict: it must carry a NON-NULL value for
      at least one of ``id_keys`` (``_alias_values``: a ``null`` id is no
      id, so a null-id object is never a record and is walked into like any
      other object; ``0``, ``""`` and ``False`` are values and fail the
      caller's id validation with RECORD_ID). When ``require_keys`` is
      given, one of them must also be present (by key: a ``null`` product
      still makes a record, which the caller rejects as RECORD_PRODUCT);
      an id-bearing dict without one is skipped and, when one of its
      non-null id values is a string, counted in ``skipped``. With
      ``string_ids`` an id that is not a string (a group's int ``id``)
      makes the dict an ordinary object, not a record; for devices the id
      is the first string alias in _DEVICE_PICK_ORDER and other aliases are
      ignored.
      Disagreeing HOME id aliases are RECORD_SHAPE via ``_alias``. Records are leaves: they are
      not descended into, so a nested object inside a record is never read.
      Every non-record dict is walked into like any other object.
    - Scalars are ignored. Dict values are reached only by iteration; the
      only keys ever subscripted are the fixed error, id and pagination
      names.
    - Pagination, on every visited non-record dict (the root included):
      ``hasMore`` true, or (only on a dict that is not an id-bearing entity,
      ``_ENTITY_ID_KEYS``) an int (non-bool) ``total``/``totalCount`` above
      the number of records in that dict's whole subtree (every record is
      counted for each enclosing non-record dict), is PAGINATION.
    - If no record is found and no list was seen anywhere, there is no list
      to read: NO_LIST, unless ``empty_ok`` (DEVICES: an empty home). An
      empty list, or lists without records, is a legitimate empty result.

    Returns ``([(record, id_value)], skipped)``, records in document order."""
    unwrap_result(decoded)
    records = []
    skipped = 0
    saw_list = False
    nodes = 0
    # Per visited non-record dict: [dict, parent index, records in subtree,
    # entity flag (id-bearing: its own total is not a page size)].
    # Each work item: (container, depth, index of the nearest enclosing
    # non-record dict; None for the root dict itself).
    levels = []
    stack = [(decoded, 1, None)]
    while stack:
        value, depth, parent = stack.pop()
        nodes += 1
        if depth > MAX_WALK_DEPTH or nodes > MAX_WALK_NODES:
            raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.WALK_BOUNDS)
        if isinstance(value, dict):
            _check_level_error(value)
            entity = False
            if depth > 1:
                ids = _alias_values(value, id_keys)
                if string_ids:
                    # Devices: the id is the first STRING alias in
                    # _DEVICE_PICK_ORDER (vendor client.py:212, ``devId or
                    # deviceId or id``); non-string aliases (an int row id, a
                    # group's int id) are ignored, never compared.
                    ids = [item for item in _alias_values(value, _DEVICE_PICK_ORDER)
                           if type(item) is str]
                entity = bool(ids) or bool(_alias_values(value, _ENTITY_ID_KEYS))
                if ids and require_keys and not any(name in value for name in require_keys):
                    if any(type(item) is str for item in ids):
                        skipped += 1
                elif ids:
                    if string_ids:
                        id_value = ids[0]
                    else:
                        id_value, _found = _alias(value, id_keys)
                    records.append((value, id_value))
                    index = parent
                    while index is not None:
                        levels[index][2] += 1
                        index = levels[index][1]
                    continue
            levels.append([value, parent, 0, entity])
            index = len(levels) - 1
            children = [(item, depth + 1, index) for _key, item in dict.items(value)
                        if isinstance(item, (dict, list))]
            stack.extend(reversed(children))
        elif isinstance(value, list):
            saw_list = True
            children = [(item, depth + 1, parent) for item in list.__iter__(value)
                        if isinstance(item, (dict, list))]
            stack.extend(reversed(children))
    for level, _parent, collected, entity in levels:
        if "hasMore" in level and level["hasMore"] is True:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.PAGINATION)
        if entity:
            # An id-bearing room, group or mesh object: its total is the
            # entity's own count, not a page size.
            continue
        for count_key in ("total", "totalCount"):
            if count_key in level:
                count = level[count_key]
                if type(count) is int and count > collected:
                    raise _Stop(Outcome.PROTOCOL_ERROR, Reason.PAGINATION)
    if not records and not saw_list and not empty_ok:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.NO_LIST)
    return records, skipped


def _home_ids(decoded):
    unique = []
    seen = set()
    records, _skipped = _walk_records(decoded, _HOME_ID_KEYS)
    for _record, gid in records:
        if type(gid) is int:
            if not 1 <= gid <= 2 ** 63 - 1:
                raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_ID)
        elif type(gid) is str:
            if not _boundary.GID_STR.match(gid):
                raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_ID)
        else:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_ID)
        key = (type(gid).__name__, gid)
        if key not in seen:
            seen.add(key)
            unique.append(gid)
            if len(unique) > MAX_HOMES:
                raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)
    return unique


# Tuya LAN local key shape: exactly 16 printable ASCII characters, no space.
# The pinned vendor validates the same length (vendor/tuya_mobile/
# password_client.py:51 DEFAULT_KEY_LENGTH = 16); it is the AES-128 key the
# LAN protocol uses. Shape only: a stale or wrong key of this shape passes.
LOCAL_KEY_CHARS = 16
MAX_NAME_CHARS = 64


def _local_key(record):
    """One lookup of ``localKey`` on a SUPPORTED device record (harness
    ``_has_local_key``). Returns the value only when it is a string of exactly
    LOCAL_KEY_CHARS printable ASCII characters (0x21-0x7E), else None. The
    value is never compared with anything, logged or put in a label."""
    if "localKey" not in record:
        return None
    value = record["localKey"]
    if (type(value) is str
            and len(value) == LOCAL_KEY_CHARS
            and all(0x21 <= ord(char) <= 0x7E for char in value)):
        return value
    return None


def _name_char_ok(char) -> bool:
    # No control, format, surrogate, private-use, unassigned or line/paragraph
    # separator characters inside a display name.
    category = _unicodedata.category(char)
    return category[0] != "C" and category not in ("Zl", "Zp")


def _name(record):
    """One lookup of ``name`` on a SUPPORTED device record (harness
    ``_has_name``): a string that is 1 to MAX_NAME_CHARS characters after
    stripping whitespace and contains no control characters (``_name_char_ok``).
    Returns the stripped name, else None (the caller uses
    DEFAULT_FEEDER_NAME)."""
    if "name" not in record:
        return None
    value = record["name"]
    if type(value) is not str:
        return None
    stripped = value.strip()
    if not 1 <= len(stripped) <= MAX_NAME_CHARS:
        return None
    if not all(_name_char_ok(char) for char in stripped):
        return None
    return stripped


def _device_products(decoded):
    # A device record needs a string id alias AND a product alias (vendor
    # client.py:197-215 keeps only objects carrying a device key). A group,
    # mesh or room object with only an id, or with a non-string id, is not
    # a device. A body with no list and no record at all is an empty home
    # (NO_LIST is a HOMES-only rule). If no device record was found but at
    # least one object with a string device id was skipped for lacking a
    # product alias, the shape is not understood: RECORD_PRODUCT, never a
    # silent 0.
    records, skipped = _walk_records(decoded, _DEVICE_ID_KEYS, _PRODUCT_KEYS,
                                     string_ids=True, empty_ok=True)
    if not records and skipped:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_PRODUCT)
    if len(records) > MAX_DEVICES:
        raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)
    out = []
    for record, dev_id in records:
        if type(dev_id) is not str or not _DEVICE_ID.match(dev_id):
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_ID)
        product, found = _alias(record, _PRODUCT_KEYS)
        # Not found: every present product alias is null.
        if not found or type(product) is not str or not _PRODUCT_ID.match(product):
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_PRODUCT)
        supported = product == OBSERVED_PRODUCT_ID
        # localKey and name are looked up only on a supported record, once
        # each; an unsupported record is never read beyond id and product.
        local_key = _local_key(record) if supported else None
        name = _name(record) if supported else None
        out.append((dev_id, supported, local_key, name))
    return out


def _token_material(decoded):
    level = unwrap_result(decoded)
    for key in ("publicKey", "exponent", "token"):
        if key not in level:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.MISSING_FIELD)
    public_key, exponent, token = level["publicKey"], level["exponent"], level["token"]
    if type(public_key) is not str or not _DIGITS.match(public_key):
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
    if type(exponent) is not str or not _DIGITS.match(exponent):
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
    if type(token) is not str or not _boundary.TOKEN_STR.match(token):
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
    return {"publicKey": public_key, "exponent": exponent, "token": token}


_LOGIN_REQUIRED = ("sid", "ecode", "uid")


def _session_secret_valid(value):
    return (type(value) is str and 1 <= len(value) <= 1024
            and all(33 <= ord(char) <= 126 for char in value))


def _check_login(decoded, route, contract):
    """Validate the session response shape and any server-directed route.
    The session values stay with the opaque profile; nothing is retained.
    Validates the same unwrapped level the profile reads the session from.

    Returns the route for the session operations: ``route`` itself, or the
    one directed route ``boundary.directed_route`` accepts. A top-level and a
    ``domain`` value must name the same destination after normalisation.
    Any rejection is UNSUPPORTED_ROUTE."""
    decoded = unwrap_result(decoded)
    for key in _LOGIN_REQUIRED:
        if key not in decoded:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.MISSING_FIELD)
        value = decoded[key]
        if (type(value) is not str or not value
                or (key in ("sid", "ecode") and not _session_secret_valid(value))):
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
    urls = _directed_values(decoded)
    routes = [_directed(url, route, contract) for url in urls]
    if not routes:
        return route
    for other in routes[1:]:
        if other.url != routes[0].url:
            raise _Stop(Outcome.UNSUPPORTED_ROUTE, Reason.DIRECTED_CONFLICT)
    return routes[0]


def _directed_values(level):
    """The non-empty server-directed values on one level, top level first.

    ``null`` and ``""`` mean absent, as in the vendor
    (password_client.py:388-395 skips falsy values); a ``null`` ``domain``
    is absent too. Any other non-string value, or a non-object ``domain``,
    is PROTOCOL_ERROR."""
    values = []
    if "mobileApiUrl" in level:
        values.append(level["mobileApiUrl"])
    domain = level["domain"] if "domain" in level else None
    if domain is not None:
        if not isinstance(domain, dict):
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
        if "mobileApiUrl" in domain:
            values.append(domain["mobileApiUrl"])
    urls = []
    for value in values:
        if value is None or value == "":
            continue
        if type(value) is not str:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
        urls.append(value)
    return urls


def _directed(url, route, contract):
    """One boundary allowlist decision; any rejection is UNSUPPORTED_ROUTE."""
    accepted = None
    try:
        accepted = _boundary.directed_route(url, route, contract)
    except Exception:
        accepted = None
    if accepted is None:
        raise _Stop(Outcome.UNSUPPORTED_ROUTE, Reason.DIRECTED_URL)
    return accepted


def _check_no_new_direction(decoded, route, contract) -> None:
    """HOMES/DEVICES: a directed value (``mobileApiUrl`` or
    ``domain.mobileApiUrl``) on any unwrapped level may only restate the
    current route; any change, a second redirect included, is
    UNSUPPORTED_ROUTE. Runs after the bounded list parse."""
    level = decoded
    for _ in range(MAX_UNWRAP + 1):
        if not isinstance(level, dict):
            return
        domain = level["domain"] if "domain" in level else None
        if domain is not None and not isinstance(domain, dict):
            domain = None
        values = []
        if "mobileApiUrl" in level:
            values.append(level["mobileApiUrl"])
        if domain is not None and "mobileApiUrl" in domain:
            values.append(domain["mobileApiUrl"])
        for value in values:
            if value is None or value == "":
                continue
            if _directed(value, route, contract) is not route:
                # Allowlisted, but a different destination than the route
                # in use: conflicts with the one accepted direction.
                raise _Stop(Outcome.UNSUPPORTED_ROUTE, Reason.DIRECTED_CONFLICT)
        level = level["result"] if "result" in level else None


def _check_password_ciphertext(value, public_key) -> None:
    if type(value) is not str or not _LOWER_HEX.match(value):
        raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE)
    modulus_bytes = (int(public_key).bit_length() + 7) // 8
    if modulus_bytes < 64 or len(value) != 2 * modulus_bytes:
        raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE)


def _check_email(value) -> None:
    try:
        _boundary._check_email(value)
    except Exception:
        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE, Stage.SETUP) from None


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class AccountClient:
    """Fixed read-only state machine. Public surface: ``run`` only."""

    def __init__(self, profile, exchange, contract, *, permit=None) -> None:
        self._profile = profile
        self._exchange = exchange
        self._contract = contract
        self._permit = permit
        self._final = None
        self._session_route = None

    def __repr__(self) -> str:
        return "<AccountClient>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("not serializable")

    def run(self, credentials, *, password_authorized, deadline) -> SafeResult:
        """The harness entry point: the fixed result only."""
        return self._discover(credentials, password_authorized=password_authorized,
                              deadline=deadline)[0]

    def _discover(self, credentials, *, password_authorized, deadline):
        """``(SafeResult, feeders)``. The feeders are returned once, from the
        run that produced them, and never kept on the client; a repeated call
        returns the sticky result with no feeders."""
        if self._final is not None:
            return self._final, ()
        if password_authorized is not True or not isinstance(self._permit, PasswordPermit):
            return _failure(Outcome.BUDGET_EXHAUSTED, stage=Stage.SETUP, reason=Reason.BUDGET), ()
        try:
            outcome = self._run(credentials, deadline)
        except _Stop as stop:
            # Before the permit is claimed every stop is a SETUP stop.
            return _failure(stop.outcome, stage=stop.stage or Stage.SETUP, reason=stop.reason), ()
        except Exception:
            return _failure(Outcome.PROTOCOL_ERROR, stage=Stage.SETUP, reason=Reason.NONE), ()
        if (type(outcome) is tuple and len(outcome) == 2
                and isinstance(outcome[0], SafeResult) and type(outcome[1]) is tuple):
            return outcome
        return _failure(Outcome.PROTOCOL_ERROR, stage=Stage.SETUP, reason=Reason.NONE), ()

    def _run(self, credentials, deadline):
        contract = self._contract
        if not isinstance(contract, _boundary.Contract):
            raise _Stop(Outcome.STARTUP_FAILED, Reason.FIELD_TYPE, Stage.SETUP)
        if not isinstance(credentials, SignIn):
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE, Stage.SETUP)
        if type(deadline) not in (int, float) or not _math.isfinite(deadline):
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE, Stage.SETUP)
        try:
            route = _boundary.select_route(credentials._country_code, contract)
        except Exception:
            raise _Stop(Outcome.UNSUPPORTED_ROUTE, Reason.FIELD_TYPE, Stage.SETUP) from None
        username = credentials._email
        _check_email(username)
        if type(credentials._password) is not str or not credentials._password:
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE, Stage.SETUP)
        if _time.monotonic() >= deadline:
            raise _Stop(Outcome.TIMEOUT, Reason.TIMEOUT, Stage.SETUP)
        if not self._permit.claim():
            return _failure(Outcome.BUDGET_EXHAUSTED, stage=Stage.SETUP, reason=Reason.BUDGET), ()
        # From here on every outcome is sticky for this client; the permit is
        # spent for every client sharing it. ``state`` holds only the fixed
        # label of an accepted directed route and the closed stage the flow
        # is in, for failure results.
        state = [ApiDomain.SAME, Stage.TOKEN]
        feeders = ()
        try:
            result, feeders = self._flow(credentials, username, route, deadline, state)
        except _Stop as stop:
            result = _failure(stop.outcome, state[0], stop.stage or state[1], stop.reason)
        except Exception:
            # Unexpected internal exception: reason NONE at the current stage.
            result = _failure(Outcome.PROTOCOL_ERROR, state[0], state[1], Reason.NONE)
        self._final = result
        return result, feeders

    def _flow(self, credentials, username, route, deadline, state):
        Op = _boundary.Operation
        country = route.country_code

        state[1] = Stage.TOKEN
        token_business = {"countryCode": country, "username": username, "isUid": False}
        decoded = self._call(Op.TOKEN, token_business, route, deadline)
        token = _token_material(decoded)
        del decoded

        # Password encryption and the login call are the LOGIN stage.
        state[1] = Stage.LOGIN
        try:
            passwd = self._profile.encrypt_password(credentials._password, dict(token))
        except Exception:
            raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE) from None
        _check_password_ciphertext(passwd, token["publicKey"])
        login_business = {
            "countryCode": country,
            "email": username,
            "passwd": passwd,
            "options": '{"group":1,"mfaCode":""}',
            "token": token["token"],
            "ifencrypt": 1,
        }
        del passwd, token
        decoded = self._call(Op.PASSWORD_LOGIN, login_business, route, deadline, password=True)
        del login_business
        # The only point a directed route can be adopted; TOKEN and
        # PASSWORD_LOGIN are never re-sent. HOMES and DEVICES use ``route``.
        route = _check_login(decoded, route, self._contract)
        state[0] = route.api_domain
        del decoded

        state[1] = Stage.HOMES
        decoded = self._call(Op.HOMES, {}, route, deadline)
        homes = _home_ids(decoded)
        _check_no_new_direction(decoded, route, self._contract)
        del decoded

        state[1] = Stage.DEVICES
        products = {}
        # Per supported device id, in first-seen order: the local key and the
        # name from any of its records (a feeder shared by two homes is one
        # device). Two different valid keys for one device are not
        # understood: RECORD_SHAPE. The first valid name wins.
        keyed = {}
        named = {}
        for gid in homes:
            decoded = self._call(Op.DEVICES, {"gid": gid}, route, deadline)
            device_records = _device_products(decoded)
            _check_no_new_direction(decoded, route, self._contract)
            for dev_id, supported, local_key, name in device_records:
                if supported:
                    known = keyed.get(dev_id)
                    if known is not None and local_key is not None and local_key != known:
                        raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_SHAPE)
                    keyed[dev_id] = known if known is not None else local_key
                    if named.get(dev_id) is None:
                        named[dev_id] = name
                previous = products.get(dev_id)
                if previous is None:
                    products[dev_id] = supported
                    if len(products) > MAX_DEVICES:
                        raise _Stop(Outcome.LIMIT_EXCEEDED, Reason.LIMIT)
                elif previous != supported:
                    # One physical device id reported with two products.
                    raise _Stop(Outcome.PROTOCOL_ERROR, Reason.RECORD_PRODUCT)
            del decoded
        supported_count = sum(1 for value in products.values() if value)
        key_count = sum(1 for dev_id, value in keyed.items()
                        if value is not None and products[dev_id])
        name_count = sum(1 for dev_id, value in named.items()
                         if value is not None and products[dev_id])
        # Only supported devices with a usable local key are offered.
        feeders = tuple(Feeder._make(dev_id, local_key,
                                     named.get(dev_id) or DEFAULT_FEEDER_NAME)
                        for dev_id, local_key in keyed.items()
                        if local_key is not None and products[dev_id])
        result = SafeResult(Outcome.OK, len(homes), len(products), supported_count,
                            route.api_domain, key_count=key_count, name_count=name_count)
        self._session_route = route
        return result, feeders

    def _call(self, operation, business, route, deadline, *, password=False):
        contract = self._contract
        profile = self._profile
        try:
            envelope = profile.prepare(operation, dict(business))
            envelope = dict(envelope)
        except Exception:
            raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE) from None
        try:
            wire = _boundary.build_request(operation, business, route=route, envelope=envelope,
                                           contract=contract, profile=profile)
            request_id = dict(envelope)[contract.envelope["seal_request_id_field"]]
        except Exception:
            raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE) from None
        _check_request_size(wire.body)
        call = PreparedCall._make(operation, wire, business, request_id)
        try:
            _boundary.validate_request(wire, operation=operation, expected_business=business,
                                       route=route, contract=contract)
        except Exception:
            raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE) from None
        if _time.monotonic() >= deadline:
            raise _Stop(Outcome.TIMEOUT, Reason.TIMEOUT)
        if password and not self._permit.admit_send(call):
            raise _Stop(Outcome.BUDGET_EXHAUSTED, Reason.BUDGET)
        send = getattr(self._exchange, "exchange", None)
        if not callable(send):
            raise _Stop(Outcome.STARTUP_FAILED, Reason.FIELD_TYPE)
        try:
            # The final validation and the one-shot PASSWORD_LOGIN latch live in
            # boundary.send; the client never calls the exchange directly.
            response = _boundary.send(
                wire, operation=operation, expected_business=business, route=route,
                contract=contract, exchange=lambda request: send(request, deadline=deadline))
        except _boundary.BoundaryRejected:
            raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE) from None
        except Cancelled:
            raise _Stop(Outcome.CANCELLED, Reason.CANCELLED) from None
        except TimeoutError:
            raise _Stop(Outcome.TIMEOUT, Reason.TIMEOUT) from None
        except Exception as error:
            # The exchange module imports this module, so its failure class
            # is looked up, not imported; if it was never loaded, no
            # ExchangeFailed can exist. Only the class test is made: the
            # exception's text, type name and status are never read.
            exchange_module = _sys.modules.get(_EXCHANGE_MODULE)
            failed = getattr(exchange_module, "ExchangeFailed", None)
            transport = isinstance(failed, type) and isinstance(error, failed)
            error_type = type(error)
            del error
            if transport:
                # Only the class-level ``kind`` is read, through the closed
                # table: bind, connect, status, encoding, length or IO.
                raise _transport_stop(error_type) from None
            # Any other exception from the exchange: reason NONE.
            raise _Stop(Outcome.PROTOCOL_ERROR, Reason.NONE) from None
        if _time.monotonic() > deadline:
            raise _Stop(Outcome.TIMEOUT, Reason.TIMEOUT)
        _check_outer(_parse_outer(response))
        call._set_active(True)
        try:
            decoded = profile.decode_response(call, response)
        except Exception:
            raise _Stop(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE) from None
        finally:
            call._set_active(False)
        del response
        _check_decoded(decoded)
        return decoded
