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
* the session (sid/ecode/uid) lives only in a per-call
  ``profile.AccountSession`` and is dropped when ``discover`` returns.

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
    # The session, exchange and client are dropped here; only the result's
    # labels and the feeders leave.
    del account, session, exchange
    if result.outcome is Outcome.OK:
        return Discovery(Outcome.OK, feeders)
    return Discovery(result.outcome, (), stage=result.stage, reason=result.reason)
