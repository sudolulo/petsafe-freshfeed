"""PetSafe account sign-in for setup: discover the supported feeders.

``discover`` signs in once with the PetSafe app email and password, lists the
homes and their devices, and returns only supported FreshFeed feeders
(product ``knjcty4hcaby0qaa``) with their device ID, LAN local key and name.
It is synchronous and blocking: call it through
``hass.async_add_executor_job(account.discover, request, ...)``.

Ported from the private harness (2026-11-17), whose
worker process and supervisor Home Assistant cannot use. Replacements:

* exactly one password-bearing request per call through a per-call
  ``client.PasswordPermit`` (no retry, no fallback region, no alias host);
  the account-lock protection across calls (the harness process budget) is
  the config flow's per-``hass`` cooldown, not this package's;
* the deadline is best-effort: each connect uses the remaining budget as its
  timeout, but DNS resolution is not preemptible (``exchange``);
* uid lives only in a per-call ``profile.AccountSession``; successful
  discovery offers a redacted sid+ecode handoff for explicit HA storage.
  Restored sessions perform only on-demand DP_GET reads.

This package imports only the standard library, ``cryptography`` and its own
modules; it writes no files and keeps no global state except the immutable
contract file it reads.
"""
from __future__ import annotations

import math as _math
import threading as _threading
import time as _time

from . import boundary as _boundary
from . import client as _client
from . import exchange as _exchange_mod
from . import profile as _profile
from .client import Discovery, Feeder, Outcome, Reason, SignIn, Stage
from .profile import AppProfile, ProfileUnavailable, load_app_profile

__all__ = [
    "AppProfile",
    "Discovery",
    "Feeder",
    "Outcome",
    "ProfileUnavailable",
    "Reason",
    "SUPPORTED_COUNTRIES",
    "SignIn",
    "Stage",
    "discover",
    "load_app_profile",
    "restore_session", "read_dps", "SessionHandoff", "DPRead",
]

# Calling code -> region label shown to the user. Exactly the contract's
# country routes (boundary._CLOSED_COUNTRY_ROUTES: 1 AZ, 44 EU, 91 IN, 7 RU).
SUPPORTED_COUNTRIES = (("1", "Americas"), ("44", "Europe"), ("91", "India"), ("7", "Russia"))

MAX_DEADLINE_S = 120.0

# Guards the check-and-set of ``SignIn._spent`` so two concurrent calls with
# one SignIn spend it once. Holds no data.
_SPEND_LOCK = _threading.Lock()


def _setup_failure(outcome, reason) -> Discovery:
    return Discovery(outcome, (), stage=Stage.SETUP, reason=reason)


def discover(request, *, profile, deadline_s: float = 30.0, _exchange=None, _session=None,
             _connection_factory=None) -> Discovery:
    """Sign in once and return the supported feeders.

    ``request`` is a ``SignIn``; ``profile`` the ``AppProfile`` from
    ``load_app_profile``. ``deadline_s`` (0 < s <= 120) bounds the whole
    exchange, best-effort. Every failure is a ``Discovery`` with a fixed
    ``Outcome``, no feeders and closed ``Stage``/``Reason`` debug labels;
    this function does not raise for account, network or protocol
    failures. ``_exchange``, ``_session`` and ``_connection_factory`` are test
    seams only.

    ``request`` is single use: this call spends it whatever the outcome and
    clears its stored password before returning. A spent ``SignIn`` sends
    nothing and returns ``BUDGET_EXHAUSTED`` (``SETUP``/``BUDGET``): the
    same closed labels the client returns when its one password permit is
    already spent, which is what a reused sign-in request is."""
    if type(request) is not SignIn:
        return _setup_failure(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
    with _SPEND_LOCK:
        spent = request._spent
        object.__setattr__(request, "_spent", True)
    if spent is not False:
        # The call that spent it clears the password in its own ``finally``;
        # clearing here would break that call while it is still running.
        return _setup_failure(Outcome.BUDGET_EXHAUSTED, Reason.BUDGET)
    try:
        return _discover_once(request, profile, deadline_s, _exchange, _session,
                              _connection_factory)
    finally:
        object.__setattr__(request, "_password", "")


def _discover_once(request, profile, deadline_s, _exchange, _session,
                   _connection_factory) -> Discovery:
    if (type(deadline_s) not in (int, float) or not _math.isfinite(deadline_s)
            or not 0 < deadline_s <= MAX_DEADLINE_S):
        return _setup_failure(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
    if type(profile) is not AppProfile:
        return _setup_failure(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE)
    deadline = _time.monotonic() + float(deadline_s)
    try:
        contract = _boundary.Contract.load()
    except Exception:
        contract = None
    if contract is None:
        return _setup_failure(Outcome.STARTUP_FAILED, Reason.NONE)
    session = _session
    if session is None:
        try:
            session = _profile.new_session(profile)
        except Exception:
            session = None
        if session is None:
            return _setup_failure(Outcome.UNSUPPORTED_PROFILE, Reason.PROFILE)
    exchange = _exchange
    if exchange is None:
        try:
            exchange = _exchange_mod.LiveExchange(
                contract, connection_factory=_connection_factory)
        except Exception:
            exchange = None
        if exchange is None:
            return _setup_failure(Outcome.STARTUP_FAILED, Reason.NONE)
    account = _client.AccountClient(session, exchange, contract,
                                    permit=_client.PasswordPermit._issue())
    result, feeders = account._discover(request, password_authorized=True, deadline=deadline)
    handoff = None
    if result.outcome is Outcome.OK:
        handoff = _handoff(session, account._session_route)
    del account, session, exchange
    if result.outcome is Outcome.OK:
        if handoff is None:
            return _setup_failure(Outcome.PROTOCOL_ERROR, Reason.FIELD_TYPE)
        return Discovery(Outcome.OK, feeders, session=handoff)
    return Discovery(result.outcome, (), stage=result.stage, reason=result.reason)


class SessionHandoff(_client._Sealed):
    """Secrets leave only through explicit storage_data(), never formatting.

    HA's config-entry storage is plain JSON, not encryption at rest. Callers
    must never log the returned mapping. No password, email or uid is kept.
    """
    __slots__ = ("_sid", "_ecode", "_route")

    def __init__(self, *args, **kwargs):
        raise TypeError("use restore_session")

    def storage_data(self):
        return {"sid": self._sid, "ecode": self._ecode}

    def storage_route(self):
        route = self._route
        return {"country_code": route.country_code, "region": route.region,
                "url": route.url, "api_domain": route.api_domain.value}


def _handoff(session, route):
    if route is None:
        return None
    return restore_session({"sid": session._sid, "ecode": session._ecode}, {
        "country_code": route.country_code, "region": route.region,
        "url": route.url, "api_domain": route.api_domain.value})


def stored_session_shape_valid(stored, route):
    """Structural check only, with no I/O; restore_session also checks the route."""
    try:
        return (type(stored) is dict and set(stored) == {"sid", "ecode"}
                and all(_client._session_secret_valid(stored[k]) for k in ("sid", "ecode"))
                and type(route) is dict
                and set(route) == {"country_code", "region", "url", "api_domain"}
                and all(type(v) is str for v in route.values()))
    except Exception:
        return False


def restore_session(stored, route):
    """Validate stored sid+ecode and canonical route; loads the contract file.

    Blocking file I/O: call it in the executor. None is the fixed signed-out
    result. Structural corruption is detectable; an otherwise valid but wrong
    secret can only be rejected by the server.
    """
    try:
        if not stored_session_shape_valid(stored, route):
            return None
        contract = _boundary.Contract.load()
        base = _boundary.select_route(route["country_code"], contract)
        accepted = _boundary.directed_route(route["url"], base, contract)
        if (accepted.url != route["url"] or accepted.region != route["region"]
                or accepted.api_domain.value != route["api_domain"]):
            return None
        value = object.__new__(SessionHandoff)
        for name, item in (("_sid", stored["sid"]), ("_ecode", stored["ecode"]),
                           ("_route", accepted)):
            object.__setattr__(value, name, item)
        return value
    except Exception:
        return None


class DPRead(_client._Sealed):
    """Only bounded DP1 and decoded DP140; payloads never enter formatting."""
    __slots__ = ("outcome", "dp1", "dp140", "dp140_present")

    def __init__(self, outcome, dp1=None, dp140=None, dp140_present=False):
        for name, value in (("outcome", outcome), ("dp1", dp1),
                            ("dp140", dp140), ("dp140_present", dp140_present)):
            object.__setattr__(self, name, value)


def read_dps(session, device_id, *, profile, deadline_s=10.0,
             _session=None, _exchange=None):
    """One closed DP_GET, synchronous; executor/DNS must be drained by caller.

    No retries, discovery, password, control or route changes. The caller binds
    device_id to its selected config entry. DP140 malformed is returned as an
    unknown decoded value with dp140_present=True so safety can latch a veto.
    """
    if type(session) is not SessionHandoff:
        return DPRead("session_rejected")
    try:
        if (type(device_id) is not str or not _client._DEVICE_ID.fullmatch(device_id)
                or type(deadline_s) not in (int, float)
                or not _math.isfinite(deadline_s) or not 0 < deadline_s <= 10):
            return DPRead("malformed")
        contract = _boundary.Contract.load()
        provider = _session if _session is not None else _profile.new_session(profile)
        object.__setattr__(provider, "_sid", session._sid)
        object.__setattr__(provider, "_ecode", session._ecode)
        exchange = _exchange if _exchange is not None else _exchange_mod.LiveExchange(contract)
        client = _client.AccountClient(provider, exchange, contract)
        route = session._route
        decoded = client._call(_boundary.Operation.DP_GET,
                               {"gwId": device_id, "devId": device_id}, route,
                               _time.monotonic() + float(deadline_s))
        _client._check_no_new_direction(decoded, route, contract)
        level = _client.unwrap_result(decoded)
        # The observed endpoint returns a direct DP mapping, not DEVICES data.
        if not any(key in level for key in ("1", "140")):
            return DPRead("malformed")
        dp1 = level.get("1")
        invalid_dp1 = "1" in level and (type(dp1) is not str or len(dp1) > 4096)
        from ..quick_feed import decode_quick_feed
        quick = decode_quick_feed(level.get("140")) if "140" in level else None
        return DPRead("malformed" if invalid_dp1 else "ok",
                      None if invalid_dp1 else dp1, quick, "140" in level)
    except _client._Stop as stop:
        # Only the explicitly recognized session rejection calls for reauth.
        if stop.reason is Reason.SESSION_REJECTED:
            return DPRead("session_rejected")
        if stop.reason in (Reason.NONE, Reason.TIMEOUT, Reason.CANCELLED) or stop.reason.name.startswith("TRANSPORT"):
            return DPRead("unavailable")
        return DPRead("malformed")
    except Exception:
        return DPRead("unavailable")
