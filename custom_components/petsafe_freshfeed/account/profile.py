"""Bundled app profile and the per-discovery account session.

Ported from the private harness ``live_profile.py``
(2026-11-17), which passed a real read-only login and discovery. Changes
from the harness: the nine app profile values come from one bundled file,
``account/app_profile.json``, written only into a built package by
``tools/build-hacs.py`` (never present in the source repository); the file
is not required to be owner-only (the values are public APK values, the
same for every user, plan Decision record); ``cert_sha256_hex`` must be 64
lowercase hex; and the loaded ``AppProfile`` holds no session: each
discovery builds a fresh ``AccountSession`` (harness ``LiveProfile``),
which is discarded afterwards.

``AccountSession`` implements ``client.OpaqueProfile``: envelope
preparation, request sealing, signing, password encryption and bounded
response decoding. Every algorithm step is a port of the pinned public
source (vendor/tuya_mobile/, revision
ade70eff248764757aa177acf9bdd10440ae7394). Vendor modules are never
imported and vendor/tuya_mobile/profiles.py is never used.

What this does NOT establish: that the PetSafe server accepts these
signatures, envelope values or ciphertexts beyond the one live check.

No value read from the profile file, the session or the password ever
appears in an exception, ``repr`` or ``str``. Every session failure is the
fixed ``ProfileRejected`` and every file failure the fixed
``ProfileUnavailable``, both with no args, cause or context.
"""
from __future__ import annotations

import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac
import json as _json
import math as _math
import os as _os
import re as _re
import secrets as _secrets
import stat as _stat
import threading as _threading
import time as _time
import uuid as _uuid
import zlib

from cryptography.hazmat.primitives.asymmetric import padding as _padding
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM

from . import boundary as _boundary
from . import client as _account_client

__all__ = ["APP_PROFILE_KEYS", "APP_PROFILE_PATH", "AccountSession", "AppProfile",
           "ProfileRejected", "ProfileUnavailable", "load_app_profile", "new_session"]

APP_PROFILE_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "app_profile.json")
# The nine-key schema: the harness profile file keys, then its envelope file keys.
_PROFILE_KEYS = ("app_id", "app_secret", "cert_sha256_hex", "app_key", "package")
_ENVELOPE_KEYS = ("app_version", "ttid", "sdk_version", "device_core_version")
APP_PROFILE_KEYS = _PROFILE_KEYS + _ENVELOPE_KEYS
MAX_FILE_BYTES = 4096
MAX_VALUE_CHARS = 256
MAX_RESPONSE_BYTES = 1024 * 1024  # same ceiling as client.MAX_RESPONSE_BYTES
MAX_PENDING = 256
NONCE_BYTES = 12  # vendor/tuya_mobile/client.py:52

# Fixed envelope values (plan "Envelope field sources"; vendor defaults,
# not PetSafe-specific evidence).
_CHANNEL = "sdk"        # vendor/tuya_mobile/models.py:25 (TuyaMobileAppProfile.channel)
_OS_SYSTEM = "14"       # vendor/tuya_mobile/models.py:23 (os_system)
_PLATFORM = "Android"   # vendor/tuya_mobile/models.py:24 (platform)
_ET = "3"               # vendor/tuya_mobile/models.py:27 (et)
_TIME_ZONE = "UTC"      # vendor/tuya_mobile/client.py:125 default
_LANG = "en"            # vendor/tuya_mobile/client.py:128 default

_CERT_HEX = _re.compile(r"\A[0-9a-f]{64}\Z")
_PRINTABLE = _re.compile(r"\A[\x20-\x7e]+\Z")
_DEVICE_ID = _re.compile(r"\A[0-9a-f]{44}\Z")
_REQUEST_ID = _re.compile(r"\A[\x21-\x7e]{1,64}\Z")
_DIGITS = _re.compile(r"\A[0-9]{1,1300}\Z")
_SESSION_OPS = ("HOMES", "DEVICES", "DP_GET")


class ProfileRejected(Exception):
    """Fixed rejection; carries no value, cause or context."""

    def __init__(self, *_args) -> None:
        super().__init__()

    def __str__(self) -> str:
        return "profile rejected"

    def __repr__(self) -> str:
        return "ProfileRejected()"


class ProfileUnavailable(Exception):
    """The bundled app profile is absent or malformed. Fixed message; carries
    no path, value, cause or context."""

    def __init__(self, *_args) -> None:
        super().__init__()

    def __str__(self) -> str:
        return "app profile unavailable"

    def __repr__(self) -> str:
        return "ProfileUnavailable()"


class _Fail(Exception):
    """Internal failure marker; converted to a fresh ProfileRejected outside
    any ``except`` block so no ``__context__`` is attached."""


def _guarded(function, *args):
    failed = False
    result = None
    try:
        result = function(*args)
    except Exception:
        failed = True
    if failed:
        raise ProfileRejected()
    return result


# --------------------------------------------------------------------------
# Bundled file loading
# --------------------------------------------------------------------------


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise _Fail()
        out[key] = value
    return out


def _bad_constant(_name):
    raise _Fail()


def _strict_json(raw: bytes):
    return _json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates,
                       parse_constant=_bad_constant)


def _read_bounded(path) -> bytes:
    # No symlink, regular file, bounded size (harness _read_private without
    # the owner-only rule). A platform without one of the optional flags
    # still loads the file (the fstat regular-file and size checks below
    # still apply) instead of failing as an unavailable profile, which would
    # hide account sign-in.
    flags = (_os.O_RDONLY | getattr(_os, "O_NOFOLLOW", 0) | getattr(_os, "O_CLOEXEC", 0)
             | getattr(_os, "O_NONBLOCK", 0))
    fd = _os.open(path, flags)
    try:
        info = _os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise _Fail()
        chunks = []
        total = 0
        while True:
            chunk = _os.read(fd, MAX_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise _Fail()
    finally:
        _os.close(fd)
    return b"".join(chunks)


def _load_values(path):
    path = _os.fspath(path)
    if not isinstance(path, str) or not path:
        raise _Fail()
    document = _strict_json(_read_bounded(path))
    if type(document) is not dict or set(document) != set(APP_PROFILE_KEYS):
        raise _Fail()
    for value in document.values():
        if type(value) is not str or not 1 <= len(value) <= MAX_VALUE_CHARS:
            raise _Fail()
        # Printable ASCII also satisfies the boundary's envelope value rule
        # (boundary.ENVELOPE_VALUE, max_envelope_value_chars).
        if not _PRINTABLE.match(value) or not _boundary.ENVELOPE_VALUE.match(value):
            raise _Fail()
    if not _CERT_HEX.match(document["cert_sha256_hex"]):
        raise _Fail()
    return {key: document[key] for key in APP_PROFILE_KEYS}


_PERMIT = object()


class AppProfile:
    """The nine validated app profile values. Immutable, redacted, never
    pickled or copied; holds no session. Built only by ``load_app_profile``."""

    __slots__ = ("_values",)

    def __init__(self, *_args, **_kwargs) -> None:
        raise TypeError("use load_app_profile")

    @classmethod
    def _make(cls, values):
        profile = object.__new__(cls)
        object.__setattr__(profile, "_values", tuple(values[key] for key in APP_PROFILE_KEYS))
        return profile

    def _mapping(self):
        return dict(zip(APP_PROFILE_KEYS, self._values))

    def __setattr__(self, name, value):
        raise AttributeError("immutable")

    def __delattr__(self, name):
        raise AttributeError("immutable")

    def __repr__(self) -> str:
        return "AppProfile(<redacted>)"

    __str__ = __repr__

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


def load_app_profile(*, path=None) -> AppProfile:
    """Read the bundled ``account/app_profile.json`` (or ``path``). Blocking
    file I/O: call it in the executor. Any absence or schema failure raises
    the fixed ``ProfileUnavailable``."""
    failed = False
    values = None
    try:
        values = _load_values(APP_PROFILE_PATH if path is None else path)
    except Exception:
        failed = True
    if failed:
        raise ProfileUnavailable()
    return AppProfile._make(values)


def new_session(app_profile, **seams) -> "AccountSession":
    """A fresh per-discovery session over ``app_profile``. ``seams`` (clock,
    request_id, nonce, device_id) are test seams only."""
    if type(app_profile) is not AppProfile:
        raise ProfileRejected()
    values = app_profile._mapping()
    profile = {key: values[key] for key in _PROFILE_KEYS}
    envelope = {key: values[key] for key in _ENVELOPE_KEYS}
    return AccountSession(_permit=_PERMIT, _profile=profile, _envelope=envelope, **seams)


# --------------------------------------------------------------------------
# Ported primitives (vendor/tuya_mobile/signer.py, client.py)
# --------------------------------------------------------------------------


def _colon_hex(cert_sha256_hex: str) -> str:
    # vendor/tuya_mobile/signer.py:42-45 (input already validated as 64 hex).
    h = cert_sha256_hex.replace(":", "").strip()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2)).upper()


def _cert_msg(values) -> str:
    # vendor/tuya_mobile/signer.py:65-66
    return f"{values['package']}_{_colon_hex(values['cert_sha256_hex'])}"


def _global_material(values) -> str:
    # vendor/tuya_mobile/signer.py:68-70: G = cert_msg + "_" + app_key + "_" + app_secret
    return f"{_cert_msg(values)}_{values['app_key']}_{values['app_secret']}"


def _signing_key(material: str) -> bytes:
    # vendor/tuya_mobile/signer.py:72-73
    return _hashlib.sha256(material.encode()).digest()


def _sign(signing_key: bytes, canonical: str) -> str:
    # vendor/tuya_mobile/signer.py:76-80
    return _hmac.new(signing_key, canonical.encode(), _hashlib.sha256).hexdigest()


def _derive_key(material: str, request_id: str, ecode) -> bytes:
    """vendor/tuya_mobile/signer.py:82-89. The vendor treats any falsy ecode
    as absent; this port accepts only None (pre-login) or a non-empty string
    and rejects an empty string, whose native semantics differ."""
    if ecode is not None and (type(ecode) is not str or not ecode):
        raise ProfileRejected()
    if type(request_id) is not str or not request_id:
        raise ProfileRejected()
    message = material if ecode is None else f"{material}_{ecode}"
    digest = _hmac.new(request_id.encode(), message.encode(), _hashlib.sha256).hexdigest()
    return digest[:16].encode()


def _channel_key(values) -> str:
    # vendor/tuya_mobile/signer.py:91-95
    return _hmac.new(values["app_id"].encode(), _cert_msg(values).encode(),
                     _hashlib.sha256).hexdigest()[8:16]


def _aes_seal(key: bytes, nonce: bytes, plaintext: bytes) -> str:
    # vendor/tuya_mobile/client.py:51-54: base64(nonce || AESGCM(key).encrypt(nonce, body, None)).
    # The vendor serializes the payload dict itself (client.py:53); here the
    # boundary passes the identical json.dumps(separators=(",", ":"),
    # ensure_ascii=False) bytes (boundary._business_bytes).
    return _b64.b64encode(nonce + _AESGCM(key).encrypt(nonce, plaintext, None)).decode()


def _aes_open_raw(key: bytes, value: str) -> bytes:
    # vendor/tuya_mobile/client.py:57-59 with strict base64.
    raw = _b64.b64decode(value.encode("ascii"), validate=True)
    if len(raw) <= _boundary.SEAL_OVERHEAD:
        raise _Fail()
    return _AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], None)


def _aes_open(key: bytes, value: str) -> bytes:
    return _guarded(_aes_open_raw, key, value)


def _gunzip_bounded(data: bytes) -> bytes:
    """Bounded replacement for vendor/tuya_mobile/client.py:60-63
    (``gzip.decompress`` with fallback). Exactly one gzip member; output
    over MAX_RESPONSE_BYTES, truncation or trailing data reject."""
    inflater = zlib.decompressobj(wbits=31)
    out = inflater.decompress(data, MAX_RESPONSE_BYTES + 1)
    if len(out) > MAX_RESPONSE_BYTES or inflater.unconsumed_tail:
        raise _Fail()
    if not inflater.eof or inflater.unused_data:
        raise _Fail()
    return out


# Operations whose decoded plaintext may be a bare JSON array (a list of
# homes or devices); it is read as if wrapped as ``{"result": <array>}``.
_ARRAY_OPS = ("HOMES", "DEVICES")


def _decode_plaintext(plain: bytes, operation):
    """Bounded gzip inflation and strict JSON (duplicate keys and NaN
    reject). The result must be an object; for HOMES and DEVICES only, a
    top-level array is accepted and wrapped as ``{"result": <array>}``.
    The byte ceiling, gzip bounds and strict parsing apply before the
    wrap, and the client's depth/node/size check runs on the wrapped
    value. TOKEN and PASSWORD_LOGIN still reject an array."""
    if len(plain) > MAX_RESPONSE_BYTES:
        raise _Fail()
    if plain[:2] == b"\x1f\x8b":
        plain = _gunzip_bounded(plain)
    decoded = _strict_json(plain)  # vendor/tuya_mobile/client.py:64
    if type(decoded) is list and isinstance(operation, _boundary.Operation) \
            and operation.name in _ARRAY_OPS:
        return {"result": decoded}
    if type(decoded) is not dict:
        raise _Fail()
    return decoded


def _open_response(key: bytes, response: bytes, operation):
    if type(response) is not bytes or len(response) > MAX_RESPONSE_BYTES:
        raise _Fail()
    envelope = _strict_json(response)
    if type(envelope) is not dict or type(envelope.get("result")) is not str:
        raise _Fail()
    return _decode_plaintext(_aes_open_raw(key, envelope["result"]), operation)


def _encrypt_password(password, token) -> str:
    # vendor/tuya_mobile/password_client.py:174-190: lowercase MD5 hex of the
    # UTF-8 password, RSA/PKCS1v15 with (exponent, publicKey), lowercase hex.
    if type(password) is not str or not password:
        raise _Fail()
    if not isinstance(token, dict):
        raise _Fail()
    modulus, exponent = token.get("publicKey"), token.get("exponent")
    if type(modulus) is not str or type(exponent) is not str:
        raise _Fail()
    if not _DIGITS.match(modulus) or not _DIGITS.match(exponent):
        raise _Fail()
    public_key = _rsa.RSAPublicNumbers(int(exponent), int(modulus)).public_key()
    digest = _hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()
    value = public_key.encrypt(digest.encode("ascii"), _padding.PKCS1v15()).hex()
    del digest
    if len(value) != 2 * ((public_key.key_size + 7) // 8):
        raise _Fail()
    return value


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

def _default_device_id() -> str:
    return _secrets.token_hex(22)


def _default_request_id() -> str:
    return str(_uuid.uuid4())


def _default_nonce() -> bytes:
    return _os.urandom(NONCE_BYTES)


class AccountSession(_account_client.OpaqueProfile):
    """Real opaque provider. Holds signing material and the session
    (sid/ecode/uid) privately; the client never passes either on.

    Built only by ``new_session``, once per discovery, and discarded after it. Key derivation for each call uses the
    ecode in effect when that call was sealed, recorded per request id, so
    the login response is decoded with the pre-login (null ecode) key."""

    __slots__ = ("_lock", "_values", "_material", "_signing_key", "_channel_key",
                 "_envelope", "_device_id", "_clock", "_request_id", "_nonce",
                 "_issued", "_sealed", "_sid", "_ecode", "_uid")

    def __init__(self, *, _permit=None, _profile=None, _envelope=None,
                 clock=_time.time, request_id=_default_request_id,
                 nonce=_default_nonce, device_id=_default_device_id) -> None:
        if _permit is not _PERMIT:
            raise TypeError("use new_session")
        for seam in (clock, request_id, nonce, device_id):
            if not callable(seam):
                raise ProfileRejected()
        material = _global_material(_profile)
        setter = object.__setattr__
        setter(self, "_lock", _threading.Lock())
        setter(self, "_values", dict(_profile))
        setter(self, "_material", material)
        setter(self, "_signing_key", _signing_key(material))
        setter(self, "_channel_key", _channel_key(_profile))
        setter(self, "_envelope", dict(_envelope))
        setter(self, "_clock", clock)
        setter(self, "_request_id", request_id)
        setter(self, "_nonce", nonce)
        setter(self, "_issued", set())
        setter(self, "_sealed", {})
        setter(self, "_sid", None)
        setter(self, "_ecode", None)
        setter(self, "_uid", None)
        # Fresh per invocation; deliberately not the vendor's email-derived
        # stable_device_id (vendor/tuya_mobile/models.py:37-40).
        setter(self, "_device_id", _guarded(device_id))
        if type(self._device_id) is not str or not _DEVICE_ID.match(self._device_id):
            raise ProfileRejected()

    def __setattr__(self, name, value):
        raise AttributeError("immutable")

    def __repr__(self) -> str:
        return "AccountSession(<redacted>)"

    __str__ = __repr__

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

    def _has_session(self) -> bool:
        return self._sid is not None

    # -- OpaqueProfile ---------------------------------------------------

    def prepare(self, operation, business):
        """Envelope only, exactly the contract's literals + provider_supplied
        keys (plan "Envelope field sources"), plus ``sid`` for HOMES/DEVICES
        once a session exists (vendor/tuya_mobile/client.py:110-145)."""
        if not isinstance(operation, _boundary.Operation):
            raise ProfileRejected()
        now = _guarded(self._clock)
        if type(now) not in (int, float) or not _math.isfinite(now) or now < 0:
            raise ProfileRejected()
        request_id = _guarded(self._request_id)
        if type(request_id) is not str or not _REQUEST_ID.match(request_id):
            raise ProfileRejected()
        with self._lock:
            if request_id in self._issued or len(self._issued) >= MAX_PENDING:
                raise ProfileRejected()
            self._issued.add(request_id)
            sid = self._sid
        envelope = self._envelope
        env = {
            "clientId": self._values["app_id"],          # client.py:117
            "os": "Android",                             # client.py:118 (contract literal)
            "appVersion": envelope["app_version"],       # client.py:119 (profile.app_version)
            "channel": _CHANNEL,                         # client.py:120
            "osSystem": _OS_SYSTEM,                      # client.py:121
            "sdkVersion": envelope["sdk_version"],       # client.py:122
            "deviceCoreVersion": envelope["device_core_version"],  # client.py:123
            "platform": _PLATFORM,                       # client.py:124
            "timeZoneId": _TIME_ZONE,                    # client.py:125
            "cp": "gzip",                                # client.py:126 (contract literal)
            "nd": "1",                                   # client.py:127 (contract literal)
            "lang": _LANG,                               # client.py:128
            "ttid": envelope["ttid"],                    # client.py:129
            "et": _ET,                                   # client.py:130
            "chKey": self._channel_key,                  # client.py:131, signer.py:91-95
            "deviceId": self._device_id,                 # client.py:132 (fresh, see __init__)
            "time": str(int(now)),                       # client.py:133
            "requestId": request_id,                     # client.py:110,134
        }
        if operation.name in _SESSION_OPS and sid is not None:
            env["sid"] = sid                             # client.py:144-145
        return env

    def seal(self, request_id, plaintext):
        """vendor/tuya_mobile/client.py:111-112 (derive_key with the current
        ecode) and 51-54 (AES-GCM, random 12-byte nonce)."""
        if type(request_id) is not str or type(plaintext) is not bytes:
            raise ProfileRejected()
        nonce = _guarded(self._nonce)
        if type(nonce) is not bytes or len(nonce) != NONCE_BYTES:
            raise ProfileRejected()
        with self._lock:
            if request_id not in self._issued or request_id in self._sealed:
                raise ProfileRejected()
            ecode = self._ecode
            self._sealed[request_id] = ecode
        key = _derive_key(self._material, request_id, ecode)
        return _guarded(_aes_seal, key, nonce, plaintext)

    def sign(self, canonical):
        # vendor/tuya_mobile/client.py:146-148, signer.py:76-80
        if type(canonical) is not str:
            raise ProfileRejected()
        return _guarded(_sign, self._signing_key, canonical)

    def encrypt_password(self, password, token):
        # vendor/tuya_mobile/password_client.py:174-190
        return _guarded(_encrypt_password, password, token)

    def decode_response(self, prepared, response):
        """Open the sealed ``result`` with the key of the ecode recorded when
        this call was sealed (vendor/tuya_mobile/client.py:154-161, 57-64),
        with bounded gzip inflation. For PASSWORD_LOGIN, store sid/ecode/uid
        privately (strict narrowing of vendor client.py:179-181 and
        password_client.py:147-171); an empty-string ecode
        is rejected."""
        if not isinstance(prepared, _account_client.PreparedCall):
            raise ProfileRejected()
        operation = prepared.operation
        request_id = _guarded(lambda: prepared.request_id)
        with self._lock:
            if type(request_id) is not str or request_id not in self._sealed:
                raise ProfileRejected()
            ecode = self._sealed.pop(request_id)
        key = _derive_key(self._material, request_id, ecode)
        decoded = _guarded(_open_response, key, response, operation)
        if operation is _boundary.Operation.PASSWORD_LOGIN:
            self._take_session(decoded)
        return decoded

    def _take_session(self, decoded) -> None:
        """Read sid/ecode/uid from the same unwrapped level the client
        validates (account_client.unwrap_result). A level that signals a
        server error, or nests too deeply, stores nothing and returns: the
        client then maps it to its fixed outcome."""
        with self._lock:
            if self._sid is not None:
                raise ProfileRejected()
        try:
            level = _account_client.unwrap_result(decoded)
        except _account_client._Stop:
            return
        if level.get("ecode") == "":
            raise ProfileRejected()
        sid, ecode, uid = level.get("sid"), level.get("ecode"), level.get("uid")
        with self._lock:
            if self._sid is not None:
                raise ProfileRejected()
            if all(type(v) is str and v for v in (sid, ecode, uid)):
                setter = object.__setattr__
                setter(self, "_sid", sid)
                setter(self, "_ecode", ecode)
                setter(self, "_uid", uid)
