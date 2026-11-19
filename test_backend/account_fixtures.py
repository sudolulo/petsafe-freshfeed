"""Synthetic-only fixtures for the account package tests (test_account_*.py).

Ported from the private harness ``synthetic_fixtures.py`` and
``live_test_guards.py`` (2026-11-17); the path guard below refuses every
directory listed in ``PETSAFE_REFUSED_DIRS`` in this process.

Everything here is SYNTHETIC. No credential, profile artifact, signer input,
app state or signing constant is read or reproduced. The synthetic profile
uses a fixed synthetic AES key and a process-local generated RSA key; it
demonstrates plumbing through the real client and boundary, not actual
PetSafe/Thingclips signing or encryption correctness.

Response shapes produced by ``FakeServer`` are SYNTHETIC SHAPES. They follow
the strict schemas the client enforces (account_client.py), which are this
gate's fail-closed narrowing of the pinned library's tolerant recursive
selectors (vendor/tuya_mobile/client.py:199-221,
vendor/tuya_mobile/password_client.py:113-171). They are not a proven server
response schema.
"""
from __future__ import annotations

import base64
import builtins
import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import threading
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from custom_components.petsafe_freshfeed.account import client as account_client
from custom_components.petsafe_freshfeed.account.client import Exchange, OpaqueProfile

# --------------------------------------------------------------------------
# Offline guards (the calling test process only; not universal isolation)
# --------------------------------------------------------------------------


class NetworkAttempt(BaseException):
    """Deliberately NOT an ``Exception``: the client's and the boundary's
    ``except Exception`` handling cannot turn a denied network or subprocess
    call into an ordinary failure outcome. Every hit is also counted
    (``guard_hits``), so even code that catches ``BaseException`` cannot hide
    one: parent-side tests assert zero hits in tearDown, and the worker
    reports a fixed boolean flag through the validated IPC result."""


_HITS_LOCK = threading.Lock()
_HITS = [0]


def guard_hits():
    """Number of offline-guard hits in this process since the last take."""
    with _HITS_LOCK:
        return _HITS[0]


def take_guard_hits():
    """Return and reset the offline-guard hit counter of this process."""
    with _HITS_LOCK:
        hits = _HITS[0]
        _HITS[0] = 0
        return hits


def _deny(*_args, **_kwargs):
    with _HITS_LOCK:
        _HITS[0] += 1
    raise NetworkAttempt("network/subprocess use is forbidden in offline tests")


_GUARDED = (
    (socket.socket, "connect"),
    (socket.socket, "connect_ex"),
    (socket.socket, "sendto"),
    (socket, "create_connection"),
    (socket, "getaddrinfo"),
    (socket, "gethostbyname"),
    (socket, "gethostbyname_ex"),
    (socket, "gethostbyaddr"),
    (http.client.HTTPConnection, "connect"),
    (urllib.request, "urlopen"),
    (subprocess, "Popen"),
    (subprocess, "run"),
    (os, "system"),
    (socket.socket, "sendmsg"),
) + tuple((os, name) for name in (
    "posix_spawn", "posix_spawnp",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
) if hasattr(os, name))
# In-process monkeypatches only: code that reaches the OS without these
# Python attributes (C extensions, _posixsubprocess, ctypes, an already-bound
# reference) is not stopped. multiprocessing's spawn worker creation uses
# _posixsubprocess directly and is unaffected; the one documented self-test
# subprocess lifts the guards around its single subprocess.run call.
_SAVED = {}


def install_offline_guards():
    for owner, name in _GUARDED:
        if (owner, name) not in _SAVED:
            _SAVED[(owner, name)] = getattr(owner, name)
            setattr(owner, name, _deny)


def remove_offline_guards():
    for (owner, name), value in _SAVED.items():
        setattr(owner, name, value)
    _SAVED.clear()


# --------------------------------------------------------------------------
# Synthetic secrets and sentinels (never real values)
# --------------------------------------------------------------------------

SENTINEL = "SENTINEL-7f3a"
EMAIL = "synthetic.user+SENTINEL-7f3a@example.invalid"
PASSWORD = "SENTINEL-7f3a-synthetic-password"
COUNTRY = "1"
LOGIN_TOKEN = "SENTINEL-7f3a-login-token"
SID = "SENTINEL-7f3a-sid"
ECODE = "SENTINEL-7f3a-ecode"
UID = "SENTINEL-7f3a-uid"
OBSERVED_PRODUCT = "knjcty4hcaby0qaa"
OTHER_PRODUCT = "l0opxehxjnmzyree"
SECRET_STRINGS = (SENTINEL, EMAIL, PASSWORD, LOGIN_TOKEN, SID, ECODE, UID)

_SYNTH_AES = b"synthetic-aes-16"
_RSA_LOCK = threading.Lock()
_RSA = []


def synthetic_rsa_key():
    """Process-local synthetic RSA key (deterministic test seam: the fixture
    holds the private key so the encrypted password can be independently
    decrypted and compared with the expected plaintext)."""
    with _RSA_LOCK:
        if not _RSA:
            _RSA.append(rsa.generate_private_key(public_exponent=65537, key_size=1024))  # noqa: S505
        return _RSA[0]


def token_fixture():
    numbers = synthetic_rsa_key().public_key().public_numbers()
    return {"publicKey": str(numbers.n), "exponent": str(numbers.e), "token": LOGIN_TOKEN}


def synth_key(request_id, ecode):
    material = b"synthetic|" + request_id.encode() + b"|" + (ecode or "").encode()
    return hashlib.sha256(material + _SYNTH_AES).digest()[:16]


def synth_nonce(request_id, label):
    return hashlib.sha256(label + request_id.encode()).digest()[:12]


def seal_with(request_id, ecode, plaintext, label=b"req|"):
    nonce = synth_nonce(request_id, label)
    return base64.b64encode(nonce + AESGCM(synth_key(request_id, ecode)).encrypt(nonce, plaintext, None)).decode()


def open_with(request_id, ecode, value):
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    return AESGCM(synth_key(request_id, ecode)).decrypt(raw[:12], raw[12:], None)


def md5_hex_rsa_hex(password, public_key):
    """Observed successful branch: lowercase MD5 digest hex, then
    RSA/ECB/PKCS1Padding, then lowercase ciphertext hex (research spec;
    pinned password_client.py:174-190)."""
    digest = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest().encode("ascii")
    return public_key.encrypt(digest, padding.PKCS1v15()).hex()


class RecordingDict(dict):
    """Decoded-record stand-in that records key lookups (privacy probe)."""

    lookups = None
    # When a list: (owner, key) per lookup, where owner is the record's own
    # ``devId`` or ``gid`` (read without recording), else None.
    owned = None

    def _note(self, key):
        if RecordingDict.lookups is not None:
            RecordingDict.lookups.append(key)
        if RecordingDict.owned is not None:
            owner = dict.get(self, "devId", dict.get(self, "gid"))
            RecordingDict.owned.append((owner, key))

    def get(self, key, default=None):
        self._note(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self._note(key)
        return super().__getitem__(key)


def _wrap(value):
    if isinstance(value, dict):
        return RecordingDict({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


# --------------------------------------------------------------------------
# Synthetic opaque profile
# --------------------------------------------------------------------------


class SyntheticProfile(OpaqueProfile):
    """Synthetic provider. Holds the session it observes in decoded login
    responses, as the real opaque provider would; the client never does."""

    def __init__(self, *, fail=None, record_access=False, password_transform=None):
        self._lock = threading.Lock()
        self._ecode = None
        self._sid = None
        self._counter = 0
        self.fail = set(fail or ())
        self.record_access = record_access
        self.encrypt_calls = 0
        self.decoded = 0
        self._transform = password_transform or md5_hex_rsa_hex

    def prepare(self, operation, business):
        if "prepare" in self.fail:
            raise RuntimeError(SENTINEL + " prepare failed")
        with self._lock:
            self._counter += 1
            request_id = f"synthetic-req-{self._counter:04d}"
        env = {
            "clientId": "synthetic-client-id",
            "os": "Android",
            "appVersion": "0.0.0-synthetic",
            "channel": "synthetic-channel",
            "osSystem": "14",
            "sdkVersion": "0.0.0",
            "deviceCoreVersion": "0.0.0",
            "platform": "synthetic platform",
            "timeZoneId": "UTC",
            "cp": "gzip",
            "nd": "1",
            "lang": "en",
            "ttid": "synthetic-ttid",
            "et": "3",
            "chKey": "synthchk",
            "deviceId": "synthetic-device-id",
            "time": "1700000000",
            "requestId": request_id,
        }
        if operation.name in ("HOMES", "DEVICES", "DP_GET") and self._sid is not None:
            env["sid"] = self._sid
        return env

    def seal(self, request_id, plaintext):
        if "seal" in self.fail:
            raise RuntimeError(SENTINEL + " seal failed")
        return seal_with(request_id, self._ecode, plaintext)

    def sign(self, canonical):
        if "sign" in self.fail:
            raise RuntimeError(SENTINEL + " sign failed")
        return hashlib.sha256(b"synthetic-sign|" + canonical.encode()).hexdigest()

    def encrypt_password(self, password, token):
        self.encrypt_calls += 1
        if "encrypt" in self.fail:
            raise RuntimeError(SENTINEL + " encryption failed")
        public_key = rsa.RSAPublicNumbers(int(token["exponent"]), int(token["publicKey"])).public_key()
        return self._transform(password, public_key)

    def decode_response(self, prepared, response):
        if "decode" in self.fail:
            raise RuntimeError(SENTINEL + " decode failed")
        envelope = json.loads(response.decode("utf-8"))
        plaintext = open_with(prepared.request_id, self._ecode, envelope["result"])
        decoded = json.loads(plaintext.decode("utf-8"))
        if type(decoded) is list and prepared.operation.name in ("HOMES", "DEVICES"):
            # Mirrors live_profile._decode_plaintext: a bare HOMES/DEVICES
            # array is read as if wrapped; TOKEN/PASSWORD_LOGIN keep the list
            # (the client rejects it as DECODE).
            decoded = {"result": decoded}
        self.decoded += 1
        if prepared.operation.name == "PASSWORD_LOGIN" and isinstance(decoded, dict):
            level = _session_level(decoded)
            sid, ecode = level.get("sid"), level.get("ecode")
            if isinstance(sid, str) and isinstance(ecode, str):
                self._sid, self._ecode = sid, ecode
        return _wrap(decoded) if self.record_access else decoded

    def __repr__(self):
        return "SyntheticProfile()"


# --------------------------------------------------------------------------
# Fake server and exchange
# --------------------------------------------------------------------------


def _session_level(decoded):
    """The level the client validates and the live profile reads the
    session from (account_client.unwrap_result); {} on any server error."""
    try:
        return account_client.unwrap_result(decoded)
    except account_client._Stop:
        return {}


class Raw(bytes):
    """Return these exact bytes from the fake wire."""


class Envelope(dict):
    """Return this outer JSON envelope verbatim (unencrypted)."""


ACTIONS = {
    "thing.m.user.username.token.get": "TOKEN",
    "thing.m.user.email.password.login": "PASSWORD_LOGIN",
    "m.life.home.space.list": "HOMES",
    "m.life.my.group.device.list": "DEVICES",
    "s.m.dev.dp.get": "DP_GET",
}


def default_homes():
    return {"result": [{"gid": 100001, "name": SENTINEL + "-home"}]}


def default_devices(gid):
    return {"result": [{
        "devId": "synthdev0001",
        "productId": OBSERVED_PRODUCT,
        "name": SENTINEL + "-display-name",
        "localKey": SENTINEL + "-lk",
        "ip": "192.0.2.10",
        "uuid": SENTINEL + "-uuid",
    }]}


class FakeServer:
    """Complete synthetic server for the four read operations.

    Each response slot is a decoded business mapping (sealed into
    ``{"result": ...}``), a ``Raw`` byte string, an ``Envelope`` returned
    verbatim, an exception instance to raise, or a callable returning one of
    those (DEVICES callables receive the requested gid).
    """

    def __init__(self, *, token=None, login=None, homes=None, devices=None, dp_get=None):
        self.responses = {
            "TOKEN": token if token is not None else token_fixture,
            "PASSWORD_LOGIN": login if login is not None else (
                lambda: {"sid": SID, "ecode": ECODE, "uid": UID}),
            "HOMES": homes if homes is not None else default_homes,
            "DEVICES": devices if devices is not None else default_devices,
            "DP_GET": dp_get if dp_get is not None else {"1": "AAAA", "140": "IAAA"},
        }
        self.ecode = None
        self.business = []

    def respond(self, request):
        pairs = urllib.parse.parse_qsl(request.body.decode("ascii"), strict_parsing=True)
        fields = dict(pairs)
        op = ACTIONS[fields["a"]]
        request_id = fields["requestId"]
        business = json.loads(open_with(request_id, self.ecode, fields["postData"]))
        self.business.append((op, business))
        slot = self.responses[op]
        if callable(slot) and not isinstance(slot, (dict, bytes)):
            slot = (slot(business) if op == "DP_GET" else
                    slot(business["gid"]) if op == "DEVICES" else slot())
        if isinstance(slot, BaseException):
            raise slot
        if isinstance(slot, Raw):
            return bytes(slot)
        if isinstance(slot, Envelope):
            return json.dumps(dict(slot)).encode()
        plaintext = json.dumps(slot, separators=(",", ":")).encode()
        sealed = seal_with(request_id, self.ecode, plaintext, label=b"resp|")
        if op == "PASSWORD_LOGIN" and isinstance(slot, dict):
            ecode = _session_level(slot).get("ecode")
            if isinstance(ecode, str):
                self.ecode = ecode
        return json.dumps({"result": sealed, "t": 1700000000, "success": True}).encode()


class FakeExchange(Exchange):
    """Injected fake wire: records every request; never retries/redirects."""

    def __init__(self, server=None, *, before=None):
        self.server = server or FakeServer()
        self.requests = []
        self.before = before

    def exchange(self, request, *, deadline):
        self.requests.append(request)
        if self.before is not None:
            self.before(request, deadline)
        return self.server.respond(request)

    def operations(self):
        out = []
        for request in self.requests:
            fields = dict(urllib.parse.parse_qsl(request.body.decode("ascii")))
            out.append((ACTIONS.get(fields["a"], fields["a"]), fields["a"], fields["v"]))
        return out

    def password_requests(self):
        return [op for op in self.operations() if op[0] == "PASSWORD_LOGIN"]


def credentials(country=COUNTRY, username=EMAIL, password=PASSWORD):
    return account_client.SignIn(email=username, password=password, country_code=country)


# --------------------------------------------------------------------------
# Real-directory refusal (port of the harness live_test_guards.py)
# --------------------------------------------------------------------------
# In-process monkeypatches of Python attributes only: a test discipline
# guard, not a sandbox. ``install_path_guards`` also installs the offline
# network guards above; ``remove_path_guards`` undoes both (every test module
# that installs them calls it in ``tearDownModule``).
#
# The refused directories come from the PETSAFE_REFUSED_DIRS environment
# variable (``os.pathsep``-separated absolute paths, default none), so no
# private path is written into the repository. Every listed directory is
# refused; nothing is refused when it is unset.

REFUSED_DIRS_ENV = "PETSAFE_REFUSED_DIRS"


def refused_roots_from_env(environ=None):
    """The normalised refused directories named by PETSAFE_REFUSED_DIRS."""
    value = (os.environ if environ is None else environ).get(REFUSED_DIRS_ENV, "")
    return tuple(os.path.normpath(os.path.abspath(part))
                 for part in value.split(os.pathsep) if part.strip())


REFUSED_ROOTS = refused_roots_from_env()

_LOCK = threading.Lock()
_STATE = {"installed": False, "roots": REFUSED_ROOTS}
_ORIGINAL = {}
# The genuine functions, captured when this module is first imported (test
# modules import it before any setUpModule runs), for restore checks.
PRISTINE = {"os." + name: getattr(os, name) for name in (
    "open", "stat", "lstat", "mkdir", "listdir", "scandir", "chmod", "unlink", "remove",
    "rmdir", "rename", "replace", "access", "utime", "link", "symlink", "readlink")
    if hasattr(os, name)}
PRISTINE["builtins.open"] = builtins.open
PRISTINE["io.open"] = io.open


class RealPathAccess(BaseException):
    """A test touched a refused (PETSAFE_REFUSED_DIRS) directory."""

    def __init__(self, *_args) -> None:
        super().__init__("access to a real private live directory is refused under test")


def _alias_roots():
    """The refused roots plus their parent-resolved forms. Only the PARENT
    directory of each refused directory is resolved, never the refused
    directories themselves."""
    roots = set(REFUSED_ROOTS)
    for root in REFUSED_ROOTS:
        parent, name = os.path.split(root)
        roots.add(os.path.join(os.path.realpath(parent), name))
    return tuple(sorted(roots))


def _under(path, roots) -> bool:
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def _text_path(path, dir_fd=None):
    if isinstance(path, int):
        return None  # an already-open descriptor, not a path
    try:
        text = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    if dir_fd is not None and not os.path.isabs(text):
        base = _ORIGINAL.get("readlink", os.readlink)("/proc/self/fd/%d" % dir_fd)
        text = os.path.join(base, text)
    return os.path.abspath(text)


def is_refused(path, *, dir_fd=None, resolve=True) -> bool:
    """Pure path check (no call into the refused directories): is ``path``
    under one of the real directories, textually or, when ``resolve`` is
    True, after resolving symlinks of the path itself."""
    text = _text_path(path, dir_fd)
    if text is None:
        return False
    roots = _STATE["roots"]
    if _under(os.path.normpath(text), roots):
        return True
    if resolve:
        return _under(os.path.realpath(text), roots)
    return False


def _refuse():
    with _HITS_LOCK:
        _HITS[0] += 1
    raise RealPathAccess()


def _wrap_path_call(name, *, path_args=1, resolve=False):
    original = getattr(os, name)
    _ORIGINAL[name] = original

    def guarded(*args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        candidates = list(args[:path_args])
        for key in ("path", "src", "dst"):
            if key in kwargs:
                candidates.append(kwargs[key])
        for candidate in candidates:
            if is_refused(candidate, dir_fd=dir_fd, resolve=resolve):
                _refuse()
        return original(*args, **kwargs)

    guarded.__name__ = name
    guarded.__wrapped__ = original
    return guarded


_PATH_CALLS = (
    # (name, number of leading path arguments, resolve symlinks)
    ("open", 1, True),
    ("stat", 1, False),
    ("lstat", 1, False),
    ("mkdir", 1, False),
    ("listdir", 1, False),
    ("scandir", 1, False),
    ("chmod", 1, False),
    ("unlink", 1, False),
    ("remove", 1, False),
    ("rmdir", 1, False),
    ("rename", 2, False),
    ("replace", 2, False),
    ("access", 1, False),
    ("utime", 1, False),
    ("link", 2, False),
    ("symlink", 2, False),
    ("readlink", 1, False),
)


def install_path_guards() -> None:
    """Install the offline network guards and the refused-directory guard.
    Idempotent; undone by ``remove_path_guards``."""
    install_offline_guards()
    with _LOCK:
        if _STATE["installed"]:
            return
        _ORIGINAL.clear()
        _ORIGINAL["readlink"] = os.readlink
        _STATE["roots"] = _alias_roots()
        wrappers = {}
        for name, count, resolve in _PATH_CALLS:
            if hasattr(os, name):
                wrappers[name] = _wrap_path_call(name, path_args=count, resolve=resolve)
        builtin_open = builtins.open
        _ORIGINAL["builtins.open"] = builtin_open
        _ORIGINAL["io.open"] = io.open

        def guarded_open(file, *args, **kwargs):
            if is_refused(file):
                _refuse()
            return builtin_open(file, *args, **kwargs)

        guarded_open.__wrapped__ = builtin_open
        for name, wrapper in wrappers.items():
            setattr(os, name, wrapper)
        builtins.open = guarded_open
        io.open = guarded_open
        _STATE["installed"] = True


def remove_path_guards() -> None:
    """Restore the original ``os`` path functions, ``builtins.open`` and
    ``io.open`` and remove the offline network guards. Idempotent."""
    with _LOCK:
        if _STATE["installed"]:
            for name, _count, _resolve in _PATH_CALLS:
                if name in _ORIGINAL:
                    setattr(os, name, _ORIGINAL[name])
            builtins.open = _ORIGINAL["builtins.open"]
            io.open = _ORIGINAL["io.open"]
            _ORIGINAL.clear()
            _STATE["installed"] = False
    remove_offline_guards()


def path_guards_installed() -> bool:
    return _STATE["installed"] and getattr(builtins.open, "__wrapped__", None) is not None \
        and getattr(os.open, "__wrapped__", None) is not None
