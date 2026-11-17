"""Offline tests for account/profile.py (port of the harness live_profile.py
and its tests, plan 2026-11-17 Task 2).

Synthetic app profile files only, created in a system temporary directory. No real profile file is opened, no network is used, no
credential exists. setUpModule installs the offline network guards and the
real-directory refusal (account_fixtures) and every test asserts zero guard
hits.

Agreement with the pinned public vendor signer/client (vendor/tuya_mobile/
signer.py, client.py) is a SAME-ALGORITHM PORT CHECK, NOT AN INDEPENDENT
VECTOR: it shows the port reproduces the pinned source, not that the PetSafe
server accepts the result. The vendor modules are loaded as a namespace
package from ``$PETSAFE_ACCOUNT_HARNESS/vendor`` when that is set and present
(skipped otherwise); vendor/tuya_mobile/profiles.py is never imported.
"""
import base64
import contextlib
import copy
import gzip
import hashlib
import hmac
import importlib
import json
import os
import pickle
import secrets
import sys
import tempfile
import time
import traceback
import unittest
import unittest.mock
import urllib.parse
import uuid

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from custom_components.petsafe_freshfeed.account import client as ac
from custom_components.petsafe_freshfeed.account import boundary
import account_fixtures as sf


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# Local harness checkout named by PETSAFE_ACCOUNT_HARNESS (never committed);
# the vendor agreement checks skip when it is unset or missing.
_HARNESS = os.environ.get("PETSAFE_ACCOUNT_HARNESS", "")
VENDOR_ROOT = os.path.join(_HARNESS, "vendor") if _HARNESS else ""
HAVE_VENDOR = bool(_HARNESS) and os.path.isdir(os.path.join(VENDOR_ROOT, "tuya_mobile"))
NO_VENDOR = ("PETSAFE_ACCOUNT_HARNESS is unset or has no vendor/tuya_mobile: "
             "pinned vendor agreement skipped")
MIB = 1024 * 1024

APP_PROFILE_NAME = "app_profile.json"

# Obviously fake synthetic values; each carries a sentinel for leak checks.
FAKE_PROFILE = {
    "app_id": "FAKEAPPID-sentA1",
    "app_secret": "FAKESECRET-sentA2",
    "app_key": "FAKEKEY-sentA3",
    "cert_sha256_hex": "0123456789abcdef" * 4,
    "package": "invalid.fake.sentA4",
}
FAKE_ENVELOPE = {
    "app_version": "0.0.0-FAKE-sentB1",
    "ttid": "FAKETTID-sentB2",
    "sdk_version": "0.0.1-FAKE-sentB3",
    "device_core_version": "0.0.2-FAKE-sentB4",
}
SENTINELS = tuple(v for v in list(FAKE_PROFILE.values()) + list(FAKE_ENVELOPE.values())
                  if "sent" in v) + ("sentA", "sentB", "sentC")
# The nine-key bundled app profile schema (plan Global Constraints).
FAKE_APP = dict(FAKE_PROFILE, **FAKE_ENVELOPE)

VENDOR = {}
_ABSENT = object()


def _hash(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def load_vendor():
    """Load only tuya_mobile.signer and tuya_mobile.client (which imports
    .models) from the pinned public source as a namespace package. Never
    imports tuya_mobile.profiles or password_client."""
    if VENDOR:
        return VENDOR
    pinned = {}
    for name in ("SOURCE.json", "PUBLIC_EVIDENCE_SOURCE.json", "MODELS_SOURCE.json"):
        with open(os.path.join(VENDOR_ROOT, name), "rb") as handle:
            for file, digest in json.load(handle)["files_sha256"].items():
                pinned[os.path.basename(file)] = digest
    for file in ("signer.py", "client.py", "models.py"):
        actual = _hash(os.path.join(VENDOR_ROOT, "tuya_mobile", file))
        assert actual == pinned[file], "vendor source is not the pinned revision"
    saved_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, VENDOR_ROOT)
    try:
        signer = importlib.import_module("tuya_mobile.signer")
        client = importlib.import_module("tuya_mobile.client")
    finally:
        sys.path.remove(VENDOR_ROOT)
        sys.dont_write_bytecode = saved_bytecode
    package = sys.modules["tuya_mobile"]
    assert getattr(package, "__file__", None) is None, "expected a namespace package"
    assert "tuya_mobile.profiles" not in sys.modules
    assert "tuya_mobile.password_client" not in sys.modules
    VENDOR.update(signer=signer, client=client)
    return VENDOR


def vendor_signer(values=FAKE_PROFILE):
    return load_vendor()["signer"].PurePythonTuyaSigner(
        app_id=values["app_id"], app_secret=values["app_secret"],
        cert_sha256_hex=values["cert_sha256_hex"], app_key=values["app_key"],
        package=values["package"])


def setUpModule():
    sf.install_path_guards()
    sf.take_guard_hits()


def tearDownModule():
    sf.remove_path_guards()


CONTRACT = []


def contract():
    if not CONTRACT:
        CONTRACT.append(boundary.Contract.load())
    return CONTRACT[0]


def write_file(directory, content, mode=0o644, name=APP_PROFILE_NAME):
    path = os.path.join(directory, name)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)
    return path


def granted_permit():
    return ac.PasswordPermit._issue()


class GuardedCase(unittest.TestCase):
    def setUp(self):
        from custom_components.petsafe_freshfeed.account import profile as live_profile
        self.lp = live_profile
        # System temp directory: no test needs a same-filesystem location.
        self.tmp = tempfile.TemporaryDirectory(prefix="petsafe-test-profile-")
        self.addCleanup(self.tmp.cleanup)
        sf.take_guard_hits()

    def tearDown(self):
        self.assertEqual(sf.take_guard_hits(), 0, "offline guard was hit")

    def app_profile(self, values=None):
        directory = os.path.join(self.tmp.name, "p" + uuid.uuid4().hex[:8])
        os.mkdir(directory, 0o700)
        path = write_file(directory, json.dumps(FAKE_APP if values is None else values))
        return self.lp.load_app_profile(path=path)

    def profile(self, **seams):
        return self.lp.new_session(self.app_profile(), **seams)

    def assert_no_leak(self, exc, extra=(), cls=None):
        self.assertIsInstance(exc, cls or self.lp.ProfileRejected)
        self.assertEqual(exc.args, ())
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        texts = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
        for text in texts:
            for value in tuple(SENTINELS) + tuple(extra):
                self.assertNotIn(value, text)


def ref_material(values=FAKE_PROFILE):
    """Independent restatement of vendor signer.py:65-70 (global material)."""
    colon = ":".join(values["cert_sha256_hex"][i:i + 2] for i in range(0, 64, 2)).upper()
    return f"{values['package']}_{colon}_{values['app_key']}_{values['app_secret']}"


def ref_key(request_id, ecode, values=FAKE_PROFILE):
    """Independent restatement of vendor signer.py:82-89 (per-request AES key).
    The pinned vendor signer itself is compared in VendorAgreement when the
    port source is present."""
    material = ref_material(values)
    message = material if not ecode else f"{material}_{ecode}"
    return hmac.new(request_id.encode(), message.encode(), hashlib.sha256).hexdigest()[:16].encode()


def seal_response(key, plaintext, nonce=None):
    nonce = nonce or os.urandom(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plaintext, None)).decode()


def outer(result_b64):
    return json.dumps({"result": result_b64, "t": 1700000000, "success": True}).encode()


def prepared(operation, request_id, business=None):
    call = ac.PreparedCall._make(operation, None, business or {}, request_id)
    call._set_active(True)
    return call


def sealed_call(profile, operation, business=None):
    """prepare + seal through the profile, as build_request does; returns rid."""
    env = profile.prepare(operation, dict(business or {}))
    rid = env["requestId"]
    profile.seal(rid, b"{}")
    return rid


def login(profile, *, sid="FAKE-sid-sentC1", ecode="FAKE-ecode-sentC2", uid="FAKE-uid-sentC3"):
    Op = boundary.Operation
    rid = sealed_call(profile, Op.PASSWORD_LOGIN)
    body = json.dumps({"sid": sid, "ecode": ecode, "uid": uid}).encode()
    response = outer(seal_response(ref_key(rid, None), body))
    return profile.decode_response(prepared(Op.PASSWORD_LOGIN, rid), response)


# --------------------------------------------------------------------------


class AppProfileFileValidation(GuardedCase):
    """``load_app_profile``: one JSON object with exactly the nine keys, each a
    non-empty string of at most 256 printable ASCII characters,
    ``cert_sha256_hex`` 64 lowercase hex; anything else is the fixed
    ``ProfileUnavailable``."""

    def _case_dir(self, label):
        directory = os.path.join(self.tmp.name, label)
        os.mkdir(directory, 0o700)
        return directory

    def _expect_unavailable(self, path, extra=()):
        with self.assertRaises(self.lp.ProfileUnavailable) as caught:
            self.lp.load_app_profile(path=path)
        self.assert_no_leak(caught.exception, extra, cls=self.lp.ProfileUnavailable)
        self.assertEqual(str(caught.exception), str(self.lp.ProfileUnavailable()))

    def test_schema_keys(self):
        self.assertEqual(set(self.lp.APP_PROFILE_KEYS), {
            "app_id", "app_secret", "cert_sha256_hex", "app_key", "package",
            "app_version", "ttid", "sdk_version", "device_core_version"})
        self.assertEqual(len(self.lp.APP_PROFILE_KEYS), 9)
        self.assertEqual(os.path.basename(self.lp.APP_PROFILE_PATH), "app_profile.json")
        self.assertEqual(os.path.dirname(self.lp.APP_PROFILE_PATH),
                         os.path.dirname(os.path.abspath(self.lp.__file__)))

    def test_valid_file_loads_with_any_read_mode(self):
        for mode in (0o600, 0o644, 0o444):
            with self.subTest(mode=oct(mode)):
                directory = self._case_dir("good%o" % mode)
                path = write_file(directory, json.dumps(FAKE_APP), mode)
                self.assertIsInstance(self.lp.load_app_profile(path=path), self.lp.AppProfile)
        import pathlib
        directory = self._case_dir("pathlib")
        path = write_file(directory, json.dumps(FAKE_APP))
        self.assertIsInstance(self.lp.load_app_profile(path=pathlib.Path(path)),
                              self.lp.AppProfile)

    def test_valid_file_loads_without_optional_open_flags(self):
        """A platform without O_NOFOLLOW/O_NONBLOCK/O_CLOEXEC still loads a
        valid profile: a missing flag must not raise AttributeError inside
        the loader and so silently turn into ProfileUnavailable (which would
        hide account sign-in)."""
        directory = self._case_dir("noflags")
        path = write_file(directory, json.dumps(FAKE_APP))
        for missing in (("O_NOFOLLOW",), ("O_NONBLOCK",), ("O_CLOEXEC",),
                        ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")):
            with self.subTest(missing=missing):
                with contextlib.ExitStack() as stack:
                    for name in missing:
                        if hasattr(self.lp._os, name):
                            stack.enter_context(unittest.mock.patch.object(
                                self.lp._os, name, new=_ABSENT))
                            delattr(self.lp._os, name)
                    for name in missing:
                        self.assertFalse(hasattr(self.lp._os, name))
                    self.assertIsInstance(self.lp.load_app_profile(path=path),
                                          self.lp.AppProfile)
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
            self.assertTrue(hasattr(os, name))

    def test_default_path_absent_is_unavailable(self):
        """The repository never contains the bundled file (Task 3 keeps it out
        of the tree), so the default path is absent here."""
        self.assertFalse(os.path.lexists(self.lp.APP_PROFILE_PATH))
        with self.assertRaises(self.lp.ProfileUnavailable):
            self.lp.load_app_profile()

    def test_profile_file_validation(self):
        good_text = json.dumps(FAKE_APP)
        first_key = sorted(FAKE_APP)[0]
        cases = {}

        def build(label, content):
            directory = self._case_dir(label)
            if content is None:
                return os.path.join(directory, APP_PROFILE_NAME)
            return write_file(directory, content)

        target = build("target", good_text)
        link_dir = self._case_dir("symlink")
        os.symlink(target, os.path.join(link_dir, APP_PROFILE_NAME))
        cases["symlink"] = os.path.join(link_dir, APP_PROFILE_NAME)
        cases["missing"] = build("missing", None)
        sub = self._case_dir("directory")
        os.mkdir(os.path.join(sub, APP_PROFILE_NAME), 0o700)
        cases["directory"] = os.path.join(sub, APP_PROFILE_NAME)
        padded = dict(FAKE_APP)
        padded[first_key] = FAKE_APP[first_key] + "x" * 4200
        cases["oversize"] = build("oversize", json.dumps(padded))
        cases["invalid_json"] = build("invalid", good_text[:-1])
        cases["not_utf8"] = build("notutf8", b"\xff\xfe" + good_text.encode())
        cases["nan"] = build("nan", good_text[:-1] + ', "x": NaN}')
        cases["top_list"] = build("toplist", json.dumps([FAKE_APP]))
        cases["empty"] = build("emptyfile", b"")
        dup = good_text[:-1] + ", " + json.dumps(first_key) + ": " + json.dumps(FAKE_APP[first_key]) + "}"
        cases["duplicate"] = build("duplicate", dup)
        for key in sorted(FAKE_APP):
            missing = {k: v for k, v in FAKE_APP.items() if k != key}
            cases[f"missing_{key}"] = build(f"missing-{key}", json.dumps(missing))
            for label, bad in (("empty", ""), ("int", 1), ("null", None),
                               ("list", ["FAKE-sentC9"]), ("bool", True),
                               ("long", "FAKE-sentC9" + "x" * 250),
                               ("newline", "FAKE-sentC9\n"), ("tab", "FAKE\tsentC9"),
                               ("del", "FAKE-sentC9\x7f"), ("nonascii", "FAKE-sentC9\u00e9")):
                changed = dict(FAKE_APP)
                changed[key] = bad
                cases[f"{label}_{key}"] = build(f"{label}-{key}", json.dumps(changed))
        extra = dict(FAKE_APP)
        extra["extra_key"] = "FAKE-extra-sentC8"
        cases["extra"] = build("extra", json.dumps(extra))
        for label, bad in (("short", "0123456789abcdef" * 3 + "0123456789abcde"),
                           ("long", "0123456789abcdef" * 4 + "0"),
                           ("nonhex", "FAKECERT-sentC7" + "0" * 49),
                           ("colons", ":".join(["ab"] * 32)),
                           ("space", " " + "0123456789abcdef" * 4),
                           ("upper", "0123456789ABCDEF" * 4)):
            changed = dict(FAKE_APP)
            changed["cert_sha256_hex"] = bad
            cases[f"cert_{label}"] = build(f"cert-{label}", json.dumps(changed))
        for label, path in cases.items():
            with self.subTest(case=label):
                self._expect_unavailable(path, extra=("FAKE-sentC9", "FAKE-extra-sentC8",
                                                      "FAKECERT-sentC7"))

    def test_unavailable_is_fixed_and_context_free(self):
        exc = self.lp.ProfileUnavailable()
        again = self.lp.ProfileUnavailable("FAKE-sentC9")
        self.assertEqual(str(exc), str(again))
        self.assertEqual(repr(exc), "ProfileUnavailable()")
        self.assertNotIn("sentC9", str(again) + repr(again))
        self.assertEqual(again.args, ())
        self.assertIsInstance(exc, Exception)


class Redaction(GuardedCase):
    def test_repr_and_pickle_redacted(self):
        app = self.app_profile()
        profile = self.lp.new_session(app)
        login(profile)
        for text in (repr(profile), str(profile), format(profile), f"{profile!r}"):
            self.assertEqual(text, "AccountSession(<redacted>)")
        for text in (repr(app), str(app), format(app), f"{app!r}"):
            self.assertEqual(text, "AppProfile(<redacted>)")
        for obj in (profile, app):
            for dump in (lambda: pickle.dumps(obj),
                         lambda: pickle.dumps(obj, protocol=0),
                         lambda: copy.copy(obj),
                         lambda: copy.deepcopy(obj)):
                with self.assertRaises(TypeError):
                    dump()
        with self.assertRaises(AttributeError):
            app.app_id = "x"
        exc = self.lp.ProfileRejected()
        self.assertEqual(str(exc), "profile rejected")
        self.assertEqual(repr(exc), "ProfileRejected()")
        self.assertEqual(str(self.lp.ProfileRejected("FAKE-sentC9")), "profile rejected")
        self.assertEqual(exc.args, ())
        self.assertIsInstance(exc, Exception)
        self.assertTrue(issubclass(self.lp.AccountSession, ac.OpaqueProfile))

    def test_session_not_constructible_directly(self):
        with self.assertRaises(TypeError):
            self.lp.AccountSession()
        with self.assertRaises(TypeError):
            self.lp.AppProfile()
        with self.assertRaises(self.lp.ProfileRejected):
            self.lp.new_session(object())

    def test_sessions_are_independent(self):
        """The loaded AppProfile holds no session: each discovery builds a
        fresh session, so a cached AppProfile never retains sid/ecode."""
        app = self.app_profile()
        first = self.lp.new_session(app)
        login(first)
        second = self.lp.new_session(app)
        self.assertTrue(first._has_session())
        self.assertFalse(second._has_session())
        state = repr([getattr(app, name, None) for name in type(app).__slots__])
        self.assertNotIn("FAKE-sid-sentC1", state)


class Envelope(GuardedCase):
    def test_envelope_matches_source_table(self):
        Op = boundary.Operation
        spec = contract().envelope
        expected_keys = set(spec["literals"]) | set(spec["provider_supplied"])
        self.assertEqual(len(expected_keys), 18)
        counter = iter(range(1000))
        rids = []

        def next_rid():
            rid = f"00000000-0000-4000-8000-{next(counter):012d}"
            rids.append(rid)
            return rid

        profile = self.profile(clock=lambda: 1700000123.9, request_id=next_rid,
                               device_id=lambda: "ab" * 22)
        g = (f"{FAKE_PROFILE['package']}_"
             + ":".join(FAKE_PROFILE["cert_sha256_hex"][i:i + 2]
                        for i in range(0, 64, 2)).upper())
        chkey = hmac.new(FAKE_PROFILE["app_id"].encode(), g.encode(), hashlib.sha256).hexdigest()[8:16]
        fixed = {
            "os": "Android", "cp": "gzip", "nd": "1",
            "clientId": FAKE_PROFILE["app_id"],
            "chKey": chkey,
            "appVersion": FAKE_ENVELOPE["app_version"],
            "ttid": FAKE_ENVELOPE["ttid"],
            "sdkVersion": FAKE_ENVELOPE["sdk_version"],
            "deviceCoreVersion": FAKE_ENVELOPE["device_core_version"],
            "channel": "sdk", "osSystem": "14", "platform": "Android", "et": "3",
            "timeZoneId": "UTC", "lang": "en",
            "deviceId": "ab" * 22,
            "time": "1700000123",
        }
        self.assertEqual(set(fixed) | {"requestId"}, expected_keys)
        for key, value in spec["literals"].items():
            self.assertEqual(fixed[key], value)

        def check(operation, business, with_sid):
            env = dict(profile.prepare(operation, business))
            want = set(expected_keys) | ({"sid"} if with_sid else set())
            self.assertEqual(set(env), want)
            for key, value in fixed.items():
                self.assertEqual(env[key], value, key)
            self.assertEqual(env["requestId"], rids[-1])
            if with_sid:
                self.assertEqual(env["sid"], "FAKE-sid-sentC1")
            self.assertNotIn("appRnVersion", env)
            self.assertNotIn("bizDM", env)
            return env

        check(Op.TOKEN, {"countryCode": "1", "username": "a@example.invalid", "isUid": False}, False)
        check(Op.PASSWORD_LOGIN, {"email": "b@example.invalid"}, False)
        # before login: no sid on any operation
        check(Op.HOMES, {}, False)
        check(Op.DEVICES, {"gid": 1}, False)
        login(profile)
        check(Op.TOKEN, {"username": "c@example.invalid"}, False)
        check(Op.PASSWORD_LOGIN, {}, False)
        check(Op.HOMES, {}, True)
        check(Op.DEVICES, {"gid": 7}, True)
        self.assertEqual(len(set(rids)), len(rids))
        with self.assertRaises(self.lp.ProfileRejected):
            profile.prepare("HOMES", {})

    def test_default_seams_and_device_id(self):
        Op = boundary.Operation
        first = self.profile()
        second = self.profile()
        envs = [dict(first.prepare(Op.TOKEN, {"username": name}))
                for name in ("x@example.invalid", "y@example.invalid")]
        device = envs[0]["deviceId"]
        self.assertRegex(device, r"\A[0-9a-f]{44}\Z")
        self.assertEqual(envs[1]["deviceId"], device, "deviceId must not depend on the username")
        for name in ("x@example.invalid", "y@example.invalid"):
            material = f"{FAKE_PROFILE['package']}|{FAKE_PROFILE['app_id']}|{name}".encode()
            self.assertNotEqual(device, hashlib.sha256(material).hexdigest()[:44])
        other = dict(second.prepare(Op.TOKEN, {}))["deviceId"]
        self.assertNotEqual(other, device, "deviceId must be fresh per profile instance")
        uuid.UUID(envs[0]["requestId"], version=4)
        self.assertNotEqual(envs[0]["requestId"], envs[1]["requestId"])
        self.assertLessEqual(abs(int(envs[0]["time"]) - int(time.time())), 5)

    def test_bad_seam_values_rejected(self):
        Op = boundary.Operation
        for seams in ({"device_id": lambda: "AB" * 22}, {"device_id": lambda: "ab" * 21},
                      {"request_id": lambda: ""}, {"request_id": lambda: "x\ny"},
                      {"clock": lambda: float("nan")}, {"clock": lambda: -1}):
            with self.subTest(seams=sorted(seams)):
                with self.assertRaises(self.lp.ProfileRejected):
                    profile = self.profile(**seams)
                    profile.prepare(Op.TOKEN, {})
        fixed = self.profile(request_id=lambda: "same-id")
        fixed.prepare(Op.TOKEN, {})
        with self.assertRaises(self.lp.ProfileRejected):
            fixed.prepare(Op.TOKEN, {})


@unittest.skipUnless(HAVE_VENDOR, NO_VENDOR)
class VendorAgreement(GuardedCase):
    def _requests(self, profile):
        Op = boundary.Operation
        route = boundary.select_route("1", contract())
        email = "synthetic.user@example.invalid"
        token = {"publicKey": str(rsa.generate_private_key(public_exponent=65537, key_size=1024)
                                  .public_key().public_numbers().n), "exponent": "65537"}
        out = []
        cases = [
            (Op.TOKEN, {"countryCode": "1", "username": email, "isUid": False}),
            (Op.PASSWORD_LOGIN, {"countryCode": "1", "email": email,
                                 "passwd": profile.encrypt_password("synthetic-pass", token),
                                 "options": '{"group":1,"mfaCode":""}',
                                 "token": "FAKE-login-token", "ifencrypt": 1}),
        ]
        for operation, business in cases:
            env = profile.prepare(operation, business)
            out.append((operation, boundary.build_request(
                operation, business, route=route, envelope=env, contract=contract(),
                profile=profile)))
        login(profile)
        for operation, business in ((Op.HOMES, {}), (Op.DEVICES, {"gid": 100001})):
            env = profile.prepare(operation, business)
            out.append((operation, boundary.build_request(
                operation, business, route=route, envelope=env, contract=contract(),
                profile=profile)))
        return out

    def test_signature_matches_pinned_vendor_signer(self):
        """Same-algorithm port check, not an independent vector: the port's
        sign() over boundary.build_request's canonical string equals the
        pinned vendor PurePythonTuyaSigner.sign(canonical_string(params))
        (signer.py:76-80, client.py:35-48). Server acceptance is unverified."""
        vendor = load_vendor()
        signer = vendor_signer()
        profile = self.profile()
        built = self._requests(profile)
        self.assertEqual([op.name for op, _ in built], ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"])
        for operation, request in built:
            with self.subTest(op=operation.name):
                fields = dict(urllib.parse.parse_qsl(request.body.decode("ascii"),
                                                     keep_blank_values=True, strict_parsing=True))
                signature = fields.pop("sign")
                expected = signer.sign(vendor["client"].canonical_string(fields))
                self.assertEqual(signature, expected)
                self.assertEqual(fields["chKey"], signer.channel_key())
                self.assertEqual(("sid" in fields), operation.name in ("HOMES", "DEVICES"))
        canonical = "a=x||v=1.0"
        self.assertEqual(profile.sign(canonical), signer.sign(canonical))
        for bad in (b"a=x", None, 5):
            with self.assertRaises(self.lp.ProfileRejected):
                profile.sign(bad)

    def test_request_encryption_roundtrip_and_binding(self):
        """Same-algorithm port check, not an independent vector: seal() with a
        fixed nonce equals vendor client._encrypt (client.py:51-54) with
        os.urandom patched to that nonce and the vendor derive_key
        (signer.py:82-89); the port's decryption returns the exact bytes."""
        vendor = load_vendor()
        nonce = bytes(range(12))
        Op = boundary.Operation
        profile = self.profile(nonce=lambda: nonce)
        payloads = [
            {"countryCode": "1", "username": "synthetic.user@example.invalid", "isUid": False},
            {"gid": 100001},
            {"note": "caf\u00e9 \u2603"},
        ]
        for payload in payloads:
            with self.subTest(payload=sorted(payload)):
                env = profile.prepare(Op.TOKEN, {})
                rid = env["requestId"]
                plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
                sealed = profile.seal(rid, plaintext)
                key = ref_key(rid, None)
                with unittest.mock.patch.object(vendor["client"].os, "urandom", lambda n: nonce):
                    self.assertEqual(sealed, vendor["client"]._encrypt(key, payload))
                self.assertEqual(self.lp._aes_open(key, sealed), plaintext)
                self.assertEqual(vendor["client"]._decrypt(key, sealed), payload)
        # binding: seal only for an issued, not-yet-sealed request id
        with self.assertRaises(self.lp.ProfileRejected):
            profile.seal("never-issued", b"{}")
        env = profile.prepare(Op.TOKEN, {})
        profile.seal(env["requestId"], b"{}")
        with self.assertRaises(self.lp.ProfileRejected):
            profile.seal(env["requestId"], b"{}")
        env = profile.prepare(Op.TOKEN, {})
        for bad in ("{}", None):
            with self.assertRaises(self.lp.ProfileRejected):
                profile.seal(env["requestId"], bad)
        # a wrong key or tampered ciphertext does not open
        rid = profile.prepare(Op.TOKEN, {})["requestId"]
        good = profile.seal(rid, b'{"a":1}')
        self.assertEqual(self.lp._aes_open(ref_key(rid, None), good), b'{"a":1}')
        raw = bytearray(base64.b64decode(good))
        raw[-1] ^= 1
        with self.assertRaises(self.lp.ProfileRejected):
            self.lp._aes_open(ref_key("other", None), good)
        with self.assertRaises(self.lp.ProfileRejected):
            self.lp._aes_open(ref_key(rid, None), base64.b64encode(bytes(raw)).decode())

    def test_reference_key_matches_vendor(self):
        signer = vendor_signer()
        for ecode in (None, "FAKE-ecode-sentC2"):
            self.assertEqual(ref_key("rid-1", ecode), signer.derive_key("rid-1", ecode))

    def test_derive_key_ecode_semantics(self):
        """Null and non-empty ecode match vendor signer.py:82-89; an empty
        string ecode is rejected (native semantics differ; plan constraint)."""
        signer = vendor_signer()
        for ecode in (None, "FAKE-ecode-sentC2"):
            self.assertEqual(self.lp._derive_key(self.lp._global_material(FAKE_PROFILE),
                                                 "rid-1", ecode),
                             signer.derive_key("rid-1", ecode))
        with self.assertRaises(self.lp.ProfileRejected):
            self.lp._derive_key(self.lp._global_material(FAKE_PROFILE), "rid-1", "")


class SessionState(GuardedCase):
    """Drive the real AccountClient with a fake Exchange. The fake server
    opens requests and seals responses with the pinned vendor derive_key
    (same algorithm, not an independent vector)."""

    class Server(ac.Exchange):
        def __init__(self, case, ecode, wrap=None):
            self.case = case
            self.wrap = wrap
            self.login_ecode = ecode
            self.server_ecode = None
            self.ops = []
            self.private = rsa.generate_private_key(public_exponent=65537, key_size=1024)

        def exchange(self, request, *, deadline):
            fields = dict(urllib.parse.parse_qsl(request.body.decode("ascii"), strict_parsing=True))
            op = sf.ACTIONS[fields["a"]]
            rid = fields["requestId"]
            key = ref_key(rid, self.server_ecode)
            business = json.loads(AESGCM(key).decrypt(
                base64.b64decode(fields["postData"])[:12],
                base64.b64decode(fields["postData"])[12:], None))
            self.ops.append((op, fields, business))
            if op == "TOKEN":
                numbers = self.private.public_key().public_numbers()
                reply = {"publicKey": str(numbers.n), "exponent": str(numbers.e),
                         "token": "FAKE-login-token"}
            elif op == "PASSWORD_LOGIN":
                reply = {"sid": "FAKE-sid-sentC1", "ecode": self.login_ecode, "uid": "FAKE-uid-sentC3"}
                if self.wrap is not None:
                    reply = self.wrap(reply)
            elif op == "HOMES":
                reply = {"result": [{"gid": 100001}]}
            else:
                reply = {"result": [{"devId": "synthdev0001", "productId": "knjcty4hcaby0qaa"}]}
            sealed = seal_response(key, json.dumps(reply).encode())
            if op == "PASSWORD_LOGIN" and isinstance(self.login_ecode, str) and self.login_ecode:
                self.server_ecode = self.login_ecode
            return outer(sealed)

    def _run(self, ecode, wrap=None):
        profile = self.profile()
        server = self.Server(self, ecode, wrap)
        client = ac.AccountClient(profile, server, contract(), permit=granted_permit())
        creds = ac.SignIn("synthetic.user@example.invalid", "synthetic-pass", "1")
        result = client.run(creds, password_authorized=True, deadline=time.monotonic() + 30)
        return result, server, profile

    def test_session_state_and_empty_ecode(self):
        result, server, profile = self._run("")
        self.assertIsNot(result.outcome, ac.Outcome.OK)
        self.assertEqual((result.home_count, result.device_count, result.supported_count), (0, 0, 0))
        self.assertEqual([op for op, _, _ in server.ops], ["TOKEN", "PASSWORD_LOGIN"])
        self.assertFalse(profile._has_session())
        with self.assertRaises(Exception):
            boundary.build_request(boundary.Operation.HOMES, {},
                                   route=boundary.select_route("1", contract()),
                                   envelope=profile.prepare(boundary.Operation.HOMES, {}),
                                   contract=contract(), profile=profile)

        result, server, profile = self._run("FAKE-ecode-sentC2")
        self.assertIs(result.outcome, ac.Outcome.OK)
        self.assertEqual((result.home_count, result.device_count, result.supported_count), (1, 1, 1))
        self.assertEqual([op for op, _, _ in server.ops], ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"])
        self.assertTrue(profile._has_session())
        for op, fields, _ in server.ops:
            self.assertEqual("sid" in fields, op in ("HOMES", "DEVICES"))
            if op in ("HOMES", "DEVICES"):
                self.assertEqual(fields["sid"], "FAKE-sid-sentC1")
                raw = base64.b64decode(fields["postData"])
                # sealed with the derived post-login key, not the pre-login key
                with self.assertRaises(Exception):
                    AESGCM(ref_key(fields["requestId"], None)).decrypt(raw[:12], raw[12:], None)
                AESGCM(ref_key(fields["requestId"], "FAKE-ecode-sentC2")).decrypt(raw[:12], raw[12:], None)
        _, login_fields, login_business = server.ops[1]
        self.assertEqual(login_business["email"], "synthetic.user@example.invalid")
        self.assertNotIn("synthetic.user@example.invalid", json.dumps(login_fields))

    def _assert_session_used(self, server, profile):
        self.assertEqual([op for op, _, _ in server.ops], ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"])
        self.assertTrue(profile._has_session())
        for op, fields, _ in server.ops:
            self.assertEqual(fields.get("sid"), "FAKE-sid-sentC1" if op in ("HOMES", "DEVICES") else None)
            if op in ("HOMES", "DEVICES"):
                raw = base64.b64decode(fields["postData"])
                AESGCM(ref_key(fields["requestId"], "FAKE-ecode-sentC2")).decrypt(raw[:12], raw[12:], None)

    def test_wrapped_login_with_extra_fields_captures_session(self):
        wraps = {
            "flat_extras": lambda r: dict(r, nickname="FAKE-nick", userType=1,
                                          domain={"mobileApiUrl": "https://a1-us.iotbing.com",
                                                  "regionCode": "AZ", "mobileMqttUrl": "m.invalid"}),
            "wrapped_once": lambda r: {"result": dict(r, extra="x"), "success": True, "status": "ok"},
            "wrapped_four": lambda r: {"result": {"result": {"result": {"result": r}}}, "t": 1},
        }
        for name, wrap in wraps.items():
            with self.subTest(name):
                result, server, profile = self._run("FAKE-ecode-sentC2", wrap)
                self.assertIs(result.outcome, ac.Outcome.OK)
                self.assertEqual((result.home_count, result.device_count, result.supported_count),
                                 (1, 1, 1))
                self._assert_session_used(server, profile)

    def test_wrapped_login_errors_keep_mapping_and_store_nothing(self):
        O = ac.Outcome
        wraps = [
            ("nested_code", lambda r: {"result": {"errorCode": "USER_PASSWD_WRONG"}, "success": True},
             O.REJECTED),
            ("code_beside_session", lambda r: {"result": dict(r, errorCode="NEED_VERIFYCODE")}, O.CHALLENGE),
            ("nested_success_false", lambda r: {"result": dict(r, success=False)}, O.PROTOCOL_ERROR),
            ("top_error_msg", lambda r: dict(r, errorMsg="x"), O.PROTOCOL_ERROR),
            ("nested_error_msg", lambda r: {"result": dict(r, errorMsg="x"), "success": True},
             O.PROTOCOL_ERROR),
            ("too_deep", lambda r: {"result": {"result": {"result": {"result": {"result": r}}}}},
             O.PROTOCOL_ERROR),
        ]
        for name, wrap, outcome in wraps:
            with self.subTest(name):
                result, server, profile = self._run("FAKE-ecode-sentC2", wrap)
                self.assertIs(result.outcome, outcome)
                self.assertEqual([op for op, _, _ in server.ops], ["TOKEN", "PASSWORD_LOGIN"])
                self.assertFalse(profile._has_session())

    def test_wrapped_empty_ecode_rejected_and_stores_nothing(self):
        result, server, profile = self._run("", lambda r: {"result": dict(r, extra=1), "status": "ok"})
        self.assertIsNot(result.outcome, ac.Outcome.OK)
        self.assertEqual([op for op, _, _ in server.ops], ["TOKEN", "PASSWORD_LOGIN"])
        self.assertFalse(profile._has_session())

    def test_session_captured_once_even_when_wrapped(self):
        profile = self.profile()
        Op = boundary.Operation
        for attempt in range(2):
            rid = sealed_call(profile, Op.PASSWORD_LOGIN)
            body = json.dumps({"result": {"sid": "FAKE-sid-sentC1", "ecode": "FAKE-ecode-sentC2",
                                          "uid": "FAKE-uid-sentC3"}, "status": "ok"}).encode()
            ecode = None if attempt == 0 else "FAKE-ecode-sentC2"
            response = outer(seal_response(ref_key(rid, ecode), body))
            if attempt == 0:
                profile.decode_response(prepared(Op.PASSWORD_LOGIN, rid), response)
                self.assertTrue(profile._has_session())
            else:
                with self.assertRaises(self.lp.ProfileRejected) as caught:
                    profile.decode_response(prepared(Op.PASSWORD_LOGIN, rid), response)
                self.assert_no_leak(caught.exception)

    def test_login_without_session_fields_stores_nothing(self):
        for reply in ({"sid": "x"}, {"sid": "x", "ecode": None, "uid": "u"},
                      {"sid": "", "ecode": "e", "uid": "u"}, {"sid": "s", "ecode": 3, "uid": "u"}):
            with self.subTest(keys=sorted(reply)):
                profile = self.profile()
                rid = sealed_call(profile, boundary.Operation.PASSWORD_LOGIN)
                response = outer(seal_response(ref_key(rid, None), json.dumps(reply).encode()))
                try:
                    profile.decode_response(prepared(boundary.Operation.PASSWORD_LOGIN, rid), response)
                except self.lp.ProfileRejected:
                    pass
                self.assertFalse(profile._has_session())
                env = profile.prepare(boundary.Operation.HOMES, {})
                self.assertNotIn("sid", env)

    def test_empty_ecode_login_rejected_directly(self):
        profile = self.profile()
        with self.assertRaises(self.lp.ProfileRejected) as caught:
            login(profile, ecode="")
        self.assert_no_leak(caught.exception)
        self.assertFalse(profile._has_session())


class PasswordEncryption(GuardedCase):
    def test_password_encryption(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private.public_key().public_numbers()
        token = {"publicKey": str(numbers.n), "exponent": str(numbers.e), "token": "FAKE-login-token"}
        profile = self.profile()
        password = "synthetic-p\u00e4ss-sentC5"
        value = profile.encrypt_password(password, dict(token))
        self.assertRegex(value, r"\A[0-9a-f]+\Z")
        self.assertEqual(len(value), 2 * 256)
        digest = hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()
        self.assertEqual(private.decrypt(bytes.fromhex(value), padding.PKCS1v15()),
                         digest.encode("ascii"))
        self.assertNotEqual(profile.encrypt_password(password, dict(token)), value,
                            "PKCS1v15 must be randomized")

        bad_tokens = [
            {"exponent": "65537"},
            {"publicKey": str(numbers.n)},
            {"publicKey": "not-a-number", "exponent": "65537"},
            {"publicKey": str(numbers.n), "exponent": "2"},
            {"publicKey": "1" * 5000, "exponent": "65537"},
            {"publicKey": str(numbers.n), "exponent": 65537.5},
            {"publicKey": str(2 ** 255 + 1), "exponent": "65537"},  # too small for PKCS1 of 32 bytes
            None,
        ]
        for bad in bad_tokens:
            with self.subTest(token=None if bad is None else sorted(bad)):
                with self.assertRaises(self.lp.ProfileRejected) as caught:
                    profile.encrypt_password(password, bad)
                self.assert_no_leak(caught.exception, (password, digest))
        for bad_password in ("", None, "bad-\udc80-sentC6", b"bytes"):
            with self.subTest(password=type(bad_password).__name__):
                with self.assertRaises(self.lp.ProfileRejected) as caught:
                    profile.encrypt_password(bad_password, dict(token))
                self.assert_no_leak(caught.exception, ("bad-\udc80-sentC6", digest))


class DecodeBounds(GuardedCase):
    def _decode(self, profile, plaintext=None, *, response=None, op=boundary.Operation.HOMES,
                key=None):
        rid = sealed_call(profile, op)
        if response is None:
            response = outer(seal_response(key or ref_key(rid, None), plaintext))
        return profile.decode_response(prepared(op, rid), response)

    def test_decode_response_bounds(self):
        profile = self.profile()
        body = {"result": [{"gid": 100001, "name": "caf\u00e9"}]}
        text = json.dumps(body).encode()
        self.assertEqual(self._decode(profile, text), body)
        self.assertEqual(self._decode(profile, gzip.compress(text)), body)

        # exactly 1 MiB of decoded gzip plaintext is accepted
        filler = MIB - len(b'{"result":""}')
        exact = b'{"result":"' + b"a" * filler + b'"}'
        self.assertEqual(len(exact), MIB)
        self.assertEqual(len(self._decode(profile, gzip.compress(exact))["result"]), filler)

        over = b'{"result":"' + b"a" * (filler + 1) + b'"}'
        bomb = gzip.compress(b'{"result":"' + b"a" * (64 * MIB) + b'"}')
        self.assertLess(len(bomb), MIB)
        truncated = gzip.compress(text)[:-6]
        trailing = gzip.compress(text) + b"trailing"
        two_members = gzip.compress(text) + gzip.compress(text)
        bad_magic_gzip = b"\x1f\x8b" + b"garbage-not-gzip"
        sealed_ok = seal_response(ref_key("x", None), text)
        oversized_outer = json.dumps({"result": sealed_ok, "t": 1, "success": True,
                                      "pad": "x" * MIB}).encode()
        cases = {
            "decoded_over_1MiB": dict(plaintext=gzip.compress(over)),
            "gzip_bomb": dict(plaintext=bomb),
            "truncated_gzip": dict(plaintext=truncated),
            "trailing_data": dict(plaintext=trailing),
            "two_members": dict(plaintext=two_members),
            "bad_gzip": dict(plaintext=bad_magic_gzip),
            "outer_over_1MiB": dict(response=oversized_outer),
            "invalid_aes_wrong_key": dict(plaintext=text, key=ref_key("other", None)),
            "invalid_aes_post_login_key": dict(plaintext=text, key=ref_key("x", "FAKE-ecode-sentC2")),
            "outer_not_json": dict(response=b"not json"),
            "outer_no_result": dict(response=b'{"success":true}'),
            "outer_result_not_b64": dict(response=b'{"result":"***"}'),
            "outer_short": dict(response=outer(base64.b64encode(b"x" * 28).decode())),
            "outer_bytes_type": dict(response="{}"),
            "plain_not_json": dict(plaintext=b"\x00\x01"),
            "plain_not_utf8": dict(plaintext=b'{"a":"\xff"}'),
            # A bare array is accepted only for HOMES/DEVICES (BareArrays).
            "plain_list_token": dict(plaintext=b"[1]", op=boundary.Operation.TOKEN),
            "plain_list_login": dict(plaintext=b"[1]", op=boundary.Operation.PASSWORD_LOGIN),
            "plain_string": dict(plaintext=b'"x"'),
            "plain_number": dict(plaintext=b"1"),
            "plain_duplicate": dict(plaintext=b'{"a":1,"a":2}'),
            "plain_nan": dict(plaintext=b'{"a":NaN}'),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(self.lp.ProfileRejected) as caught:
                    self._decode(profile, **kwargs)
                self.assert_no_leak(caught.exception)

    def test_bare_array_wrapped_for_homes_and_devices_only(self):
        Op = boundary.Operation
        profile = self.profile()
        homes = [{"gid": 100001}, {"gid": "h-2"}]
        text = json.dumps(homes).encode()
        self.assertEqual(self._decode(profile, text, op=Op.HOMES), {"result": homes})
        self.assertEqual(self._decode(profile, gzip.compress(text), op=Op.HOMES),
                         {"result": homes})
        devices = [{"devId": "d1", "productId": "p1"}]
        self.assertEqual(self._decode(profile, json.dumps(devices).encode(), op=Op.DEVICES),
                         {"result": devices})
        self.assertEqual(self._decode(profile, b"[]", op=Op.DEVICES), {"result": []})
        # TOKEN and PASSWORD_LOGIN still reject it; no session is stored.
        for op in (Op.TOKEN, Op.PASSWORD_LOGIN):
            with self.subTest(op=op.name):
                body = json.dumps([{"sid": "FAKE-sid", "ecode": "FAKE-ecode",
                                    "uid": "FAKE-uid"}]).encode()
                with self.assertRaises(self.lp.ProfileRejected) as caught:
                    self._decode(profile, body, op=op)
                self.assert_no_leak(caught.exception)
        self.assertFalse(profile._has_session())
        # Byte ceiling, gzip bounds, duplicates and NaN still apply to arrays.
        filler = MIB - len(b'[""]')
        exact = b'["' + b"a" * filler + b'"]'
        self.assertEqual(len(exact), MIB)
        self.assertEqual(len(self._decode(profile, gzip.compress(exact))["result"][0]), filler)
        over = b'["' + b"a" * (filler + 1) + b'"]'
        for label, plaintext in (("over_1MiB", gzip.compress(over)),
                                 ("bomb", gzip.compress(b"[" + b"0," * (8 * MIB) + b"0]")),
                                 ("duplicate", b'[{"gid":1,"gid":2}]'),
                                 ("nan", b"[NaN]"),
                                 ("truncated", gzip.compress(text)[:-6])):
            for op in (Op.HOMES, Op.DEVICES):
                with self.subTest(case=label, op=op.name):
                    with self.assertRaises(self.lp.ProfileRejected):
                        self._decode(profile, plaintext, op=op)

    def test_gzip_inflate_bounded_by_max_length(self):
        calls = []
        real = self.lp.zlib.decompressobj

        def spy(*args, **kwargs):
            obj = real(*args, **kwargs)
            calls.append((args, kwargs))

            class Wrapped:
                def decompress(self_inner, data, max_length=0):
                    calls.append(("max_length", max_length))
                    return obj.decompress(data, max_length)

                def __getattr__(self_inner, name):
                    return getattr(obj, name)
            return Wrapped()

        profile = self.profile()
        with unittest.mock.patch.object(self.lp.zlib, "decompressobj", spy):
            with self.assertRaises(self.lp.ProfileRejected):
                self._decode(profile, gzip.compress(b"0" * (8 * MIB)))
        self.assertIn(((), {"wbits": 31}), calls)
        self.assertIn(("max_length", MIB + 1), calls)

    def test_decode_uses_ecode_recorded_at_seal(self):
        """The login response is decoded with the pre-login (null ecode) key
        even though decoding it installs the session; a call sealed before
        login keeps the pre-login key; one sealed after uses the new key."""
        Op = boundary.Operation
        profile = self.profile()
        early = sealed_call(profile, Op.HOMES)
        login(profile)
        late = sealed_call(profile, Op.HOMES)
        body = b'{"result":[]}'
        self.assertEqual(profile.decode_response(
            prepared(Op.HOMES, early), outer(seal_response(ref_key(early, None), body))),
            {"result": []})
        self.assertEqual(profile.decode_response(
            prepared(Op.HOMES, late),
            outer(seal_response(ref_key(late, "FAKE-ecode-sentC2"), body))), {"result": []})
        # each sealed request decodes at most once; unknown ids reject
        for rid in (early, late, "never-issued"):
            with self.assertRaises(self.lp.ProfileRejected):
                profile.decode_response(prepared(Op.HOMES, rid),
                                        outer(seal_response(ref_key(rid, None), body)))
        # a second (correctly sealed) login on the same profile is refused and
        # does not replace the session
        rid = sealed_call(profile, Op.PASSWORD_LOGIN)
        again = json.dumps({"sid": "FAKE-sid2", "ecode": "FAKE-ecode2", "uid": "FAKE-uid2"}).encode()
        with self.assertRaises(self.lp.ProfileRejected):
            profile.decode_response(prepared(Op.PASSWORD_LOGIN, rid),
                                    outer(seal_response(ref_key(rid, "FAKE-ecode-sentC2"), again)))
        self.assertEqual(profile.prepare(Op.HOMES, {})["sid"], "FAKE-sid-sentC1")


class StaticImports(unittest.TestCase):
    def test_no_network_or_vendor_imports(self):
        import ast
        with open(os.path.join(REPO, "custom_components", "petsafe_freshfeed", "account",
                               "profile.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        banned_roots = {"socket", "subprocess", "http", "requests", "aiohttp", "ssl",
                        "tuya_mobile", "pickle", "synthetic_fixtures", "account_fixtures",
                        "homeassistant", "multiprocessing"}
        for name in names:
            self.assertNotIn(name.split(".")[0], banned_roots, name)
            self.assertNotEqual(name, "urllib.request")


if __name__ == "__main__":
    unittest.main()
