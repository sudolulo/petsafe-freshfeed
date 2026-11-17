"""Offline tests for the final PetSafe/Tuya mobile wire boundary.

Synthetic values only. No credentials, profile/signing inputs, DNS, sockets,
HTTP clients or subprocesses: setUpModule installs fail-closed guards and a
test asserts they are active. These guards cover this test process only; they
are not universal network isolation.

Expected wire fixtures are derived independently in this file from the pinned
public source (vendor/tuya_mobile/client.py:40-54,107-154 at revision
ade70eff248764757aa177acf9bdd10440ae7394) and the installed aiohttp 3.11.16
form serializer (formdata.py:123-139); they do not call boundary helpers.
"""
import base64
import dataclasses
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CONTRACT_PATH = os.path.join(REPO, "custom_components", "petsafe_freshfeed", "account",
                             "protocol_contract.json")
# Read-only port source, named by the PETSAFE_ACCOUNT_HARNESS environment
# variable (a local harness checkout; never committed or shipped). The checks
# that need it skip when it is unset or missing.
HARNESS = os.environ.get("PETSAFE_ACCOUNT_HARNESS", "")
VENDOR = os.path.join(HARNESS, "vendor", "tuya_mobile") if HARNESS else ""
HAVE_HARNESS = bool(HARNESS) and os.path.isdir(VENDOR)
NO_HARNESS = ("PETSAFE_ACCOUNT_HARNESS is unset or has no vendor/tuya_mobile: "
              "pinned public source check skipped")


def verify_pinned_source(document, directory):
    """Require the exact pinned public source bytes and line anchors.

    Test-only (moved out of boundary.py: the vendor source is not shipped,
    so nothing at runtime can run it). Returns the source digest string;
    raises ``boundary.BoundaryRejected`` on any mismatch."""
    from custom_components.petsafe_freshfeed.account import boundary
    reject = boundary._reject
    source = document.get("source")
    anchors = document.get("source_anchors")
    if not isinstance(source, dict) or not isinstance(anchors, list) or not anchors:
        raise reject()
    expected = source.get("files_sha256")
    revision = source.get("revision")
    if not isinstance(expected, dict) or not expected or not isinstance(revision, str):
        raise reject()
    texts = {}
    digests = []
    for name in sorted(expected):
        if not isinstance(name, str) or "/" in name or "\\" in name:
            raise reject()
        try:
            with open(os.path.join(directory, name), "rb") as handle:
                raw = handle.read()
        except OSError:
            raise reject() from None
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected[name]:
            raise reject()
        try:
            texts[name] = raw.decode("utf-8").split("\n")
        except UnicodeDecodeError:
            raise reject() from None
        digests.append(f"{name}={digest}")
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise reject()
        name = anchor.get("file")
        start = anchor.get("start")
        end = anchor.get("end")
        want = anchor.get("sha256")
        if name not in texts or not isinstance(want, str) or not want:
            raise reject()
        if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool):
            raise reject()
        lines = texts[name]
        if start < 1 or end < start or end > len(lines):
            raise reject()
        text = "\n".join(lines[start - 1:end])
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != want:
            raise reject()
    return revision + "|" + "|".join(digests)

# --------------------------------------------------------------------------
# Offline guards (this process only)
# --------------------------------------------------------------------------

_SAVED = {}


class NetworkAttempt(BaseException):
    """Not an Exception: provider/client ``except Exception`` handling cannot
    absorb a denied network or subprocess call into an ordinary rejection."""


_HITS = [0]


def take_guard_hits():
    """Return and reset the number of guard hits in this test process."""
    hits = _HITS[0]
    _HITS[0] = 0
    return hits


def _deny(*_args, **_kwargs):
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


def setUpModule():
    for owner, name in _GUARDED:
        _SAVED[(owner, name)] = getattr(owner, name)
        setattr(owner, name, _deny)


def tearDownModule():
    for (owner, name), value in _SAVED.items():
        setattr(owner, name, value)
    _SAVED.clear()


# --------------------------------------------------------------------------
# Synthetic providers and independent fixture derivation
# --------------------------------------------------------------------------

SYNTH_KEY = b"synthetic-key-16"
SENTINEL = "SENTINEL-secret-7f3a"
SIGN_KEYS = {
    "a", "appVersion", "chKey", "clientId", "deviceId", "et", "h5",
    "h5Token", "lang", "lat", "lon", "n4h5", "os", "postData", "requestId",
    "sid", "sp", "time", "ttid", "v",
}
# Insertion order of client.py:114-145 (conditional keys in source order).
SOURCE_ORDER = [
    "a", "v", "clientId", "os", "appVersion", "channel", "osSystem",
    "sdkVersion", "deviceCoreVersion", "platform", "timeZoneId", "cp", "nd",
    "lang", "ttid", "et", "chKey", "deviceId", "time", "requestId",
    "postData", "appRnVersion", "bizDM", "sid", "sign",
]


def synth_nonce(request_id):
    return hashlib.sha256(b"nonce|" + request_id.encode()).digest()[:12]


def synth_seal(request_id, plaintext):
    nonce = synth_nonce(request_id)
    return base64.b64encode(nonce + AESGCM(SYNTH_KEY).encrypt(nonce, plaintext, None)).decode()


def synth_open(value):
    raw = base64.b64decode(value)
    return AESGCM(SYNTH_KEY).decrypt(raw[:12], raw[12:], None)


def synth_sign(canonical):
    return hashlib.sha256(b"synthetic-sign|" + canonical.encode()).hexdigest()


def independent_canonical(params):
    parts = []
    for key in sorted(params):
        value = params[key]
        if key in SIGN_KEYS and value:
            if key == "postData":
                d = hashlib.md5(value.encode()).hexdigest()
                value = d[8:16] + d[0:8] + d[24:32] + d[16:24]
            parts.append(f"{key}={value}")
    return "||".join(parts)


class SyntheticProfile:
    """Opaque provider stand-in: trusted only for its declared transformation."""

    def __init__(self, seal=None, sign=None):
        self.sealed = []
        self.canonicals = []
        self._seal = seal or synth_seal
        self._sign = sign or synth_sign

    def seal(self, request_id, plaintext):
        self.sealed.append((request_id, plaintext))
        return self._seal(request_id, plaintext)

    def sign(self, canonical):
        self.canonicals.append(canonical)
        return self._sign(canonical)

    def __repr__(self):
        return "SyntheticProfile()"


def synth_envelope(request_id="req-0001", sid=None, extra=None):
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
    if sid is not None:
        env["sid"] = sid
    if extra:
        env.update(extra)
    return env


EMAIL = "synthetic.user+tag@example.invalid"
TOKEN_BIZ = {"countryCode": "1", "username": EMAIL, "isUid": False}
LOGIN_BIZ = {
    "countryCode": "1",
    "email": EMAIL,
    "passwd": "ab" * 128,
    "options": '{"group":1,"mfaCode":""}',
    "token": "synthetic-login-token-0001",
    "ifencrypt": 1,
}
HOMES_BIZ = {}
DEVICES_BIZ = {"gid": 123456}


def independent_body(action, version, business, envelope):
    plaintext = json.dumps(business, separators=(",", ":"), ensure_ascii=False).encode()
    params = {"a": action, "v": version}
    params.update(envelope)
    params["postData"] = synth_seal(envelope["requestId"], plaintext)
    ordered = {k: params[k] for k in SOURCE_ORDER if k in params}
    ordered["sign"] = synth_sign(independent_canonical(ordered))
    return plaintext, ordered, urllib.parse.urlencode(list(ordered.items()), doseq=True).encode()


def load():
    from custom_components.petsafe_freshfeed.account import boundary
    return boundary, boundary.Contract.load()


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        return b"synthetic-response"


def mutate(request, **changes):
    """Simulate a dependency mutating a prepared request in place."""
    for name, value in changes.items():
        object.__setattr__(request, name, value)
    return request


def form_pairs(body):
    return urllib.parse.parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)


def reencode(pairs):
    return urllib.parse.urlencode(pairs, doseq=True).encode()


def replace_field(body, key, value):
    return reencode([(k, value if k == key else v) for k, v in form_pairs(body)])


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class GuardedTestCase(unittest.TestCase):
    """Every test must end with zero offline-guard hits."""

    def setUp(self):
        take_guard_hits()

    def tearDown(self):
        self.assertEqual(take_guard_hits(), 0, "an offline guard fired")


class OfflineGuardTests(GuardedTestCase):
    def test_network_attempt_escapes_provider_exception_handling(self):
        """A provider that dials out is not absorbed into BoundaryRejected."""
        self.assertFalse(issubclass(NetworkAttempt, Exception))
        boundary, contract = load()
        route = boundary.select_route("1", contract)

        def dial(*_args):
            socket.getaddrinfo("a1.tuyaeu.com", 443)
        for kwargs in (dict(seal=dial), dict(sign=dial)):
            with self.subTest(sorted(kwargs)):
                with self.assertRaises(NetworkAttempt):
                    boundary.build_request(boundary.Operation.TOKEN, TOKEN_BIZ, route=route,
                                           envelope=synth_envelope(), contract=contract,
                                           profile=SyntheticProfile(**kwargs))
                self.assertEqual(take_guard_hits(), 1)

    def test_offline_guard_active(self):
        with self.assertRaises(NetworkAttempt):
            socket.create_connection(("a1.tuyaeu.com", 443))
        with self.assertRaises(NetworkAttempt):
            socket.getaddrinfo("a1.tuyaeu.com", 443)
        with self.assertRaises(NetworkAttempt):
            urllib.request.urlopen("https://a1.tuyaeu.com/api.json")
        with self.assertRaises(NetworkAttempt):
            subprocess.run(["true"])
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(NetworkAttempt):
                s.connect(("192.0.2.1", 443))
        finally:
            s.close()
        self.assertEqual(take_guard_hits(), 5)


class ContractTests(GuardedTestCase):
    def test_boundary_module_and_closed_operations(self):
        from custom_components.petsafe_freshfeed.account.boundary import Operation
        self.assertEqual({m.name for m in Operation}, {"TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"})

    def test_contract_rows_are_the_pinned_rows(self):
        boundary, contract = load()
        op = boundary.Operation
        self.assertEqual(contract.row(op.TOKEN), ("thing.m.user.username.token.get", "2.0"))
        self.assertEqual(contract.row(op.PASSWORD_LOGIN), ("thing.m.user.email.password.login", "3.0"))
        self.assertEqual(contract.row(op.HOMES), ("m.life.home.space.list", "1.0"))
        self.assertEqual(contract.row(op.DEVICES), ("m.life.my.group.device.list", "2.2"))
        routes = {c: (r.region, r.url) for c in ("1", "44", "91", "7")
                  for r in [boundary.select_route(c, contract)]}
        self.assertEqual(routes, {
            "1": ("AZ", "https://a1-us.iotbing.com/api.json"),
            "44": ("EU", "https://a1.tuyaeu.com/api.json"),
            "91": ("IN", "https://a1-in.iotbing.com/api.json"),
            "7": ("RU", "https://a1.iot334.com/api.json"),
        })

    @unittest.skipUnless(HAVE_HARNESS, NO_HARNESS)
    def test_contract_matches_pinned_source_anchors(self):
        """The shipped contract is the harness contract, byte for byte, and its
        source hashes and line anchors still match the pinned public source.
        At runtime the package ships no vendor source, so this check is a
        development-time test, not a load-time gate."""
        from custom_components.petsafe_freshfeed.account import boundary
        with open(CONTRACT_PATH, "rb") as fh:
            shipped = fh.read()
        with open(os.path.join(HARNESS, "protocol_contract.json"), "rb") as fh:
            self.assertEqual(shipped, fh.read())
        document = json.loads(shipped)
        self.assertTrue(verify_pinned_source(document, VENDOR))
        self.assertFalse(hasattr(boundary, "_verify_pinned_source"))
        tmp = tempfile.mkdtemp(prefix="petsafe-test-src-")
        try:
            src = os.path.join(tmp, "src")
            shutil.copytree(VENDOR, src)
            path = os.path.join(src, "client.py")
            with open(path, "rb") as fh:
                data = fh.read()
            with open(path, "wb") as fh:
                fh.write(data.replace(b'version: str = "1.0"', b'version: str = "1.1"'))
            with self.assertRaises(boundary.BoundaryRejected):
                verify_pinned_source(document, src)
        finally:
            shutil.rmtree(tmp)

    def test_contract_document_is_closed(self):
        from custom_components.petsafe_freshfeed.account import boundary
        R = boundary.BoundaryRejected
        tmp = tempfile.mkdtemp(prefix="petsafe-test-src-")
        try:
            # A widened contract (extra host or action) is not loadable.
            with open(CONTRACT_PATH, encoding="utf-8") as fh:
                doc = json.load(fh)
            for widen in (
                lambda d: d["regions"].__setitem__("AY", "https://a1.example.invalid/api.json"),
                lambda d: d["country_routes"].__setitem__("86", "EU"),
                lambda d: d["operations"]["HOMES"].__setitem__("version", "1.1"),
                lambda d: d["operations"].__setitem__("PUBLISH", d["operations"]["HOMES"]),
                # Widened business schema, envelope and form limits.
                lambda d: d["operations"]["HOMES"]["business"].append(["extra", "email"]),
                lambda d: d["operations"]["DEVICES"]["business"].append(["gid2", "group_id"]),
                lambda d: d["operations"]["TOKEN"]["business"][1].__setitem__(1, "login_token"),
                lambda d: d["operations"]["TOKEN"]["business"][2].__setitem__(1, {"literal": 0}),
                lambda d: d["operations"]["PASSWORD_LOGIN"]["business"][5].__setitem__(1, {"literal": True}),
                lambda d: d["operations"]["PASSWORD_LOGIN"]["business"].reverse(),
                lambda d: d["operations"]["HOMES"].__setitem__("session", "forbidden"),
                lambda d: d["envelope"]["sign_keys"].append("extraKey"),
                lambda d: d["envelope"]["sign_keys"].remove("sid"),
                lambda d: d["envelope"]["literals"].__setitem__("os", "iOS"),
                lambda d: (d["envelope"]["provider_supplied"].remove("lang"),
                           d["envelope"]["literals"].__setitem__("lang", "en")),
                lambda d: (d["envelope"]["optional"].append("extraKey"),
                           d["envelope"]["order"].insert(-1, "extraKey")),
                lambda d: d["envelope"]["order"].insert(2, d["envelope"]["order"].pop(3)),
                lambda d: d["form"].__setitem__("max_body_bytes", 1 << 20),
                lambda d: d["form"].__setitem__("max_business_bytes", 8192),
                lambda d: d["form"].__setitem__("max_envelope_value_chars", 256.0),
            ):
                widened = json.loads(json.dumps(doc))
                widen(widened)
                cpath = os.path.join(tmp, "contract.json")
                with open(cpath, "w", encoding="utf-8") as fh:
                    json.dump(widened, fh)
                with self.assertRaises(R):
                    boundary.Contract.load(contract_path=cpath)
            with self.assertRaises(R):
                boundary.Contract.load(contract_path=os.path.join(tmp, "missing.json"))
        finally:
            shutil.rmtree(tmp)
        with self.assertRaises(R):
            boundary.Contract({}, "x")
        # A valid document constructed directly, bypassing source verification,
        # is not a usable Contract.
        with open(CONTRACT_PATH, encoding="utf-8") as fh:
            valid = json.load(fh)
        good = boundary.Contract.load()
        for fingerprint in ("x", good.source_digest):
            with self.assertRaises(R):
                boundary.Contract(valid, fingerprint)

    def test_route_only_from_select_route(self):
        boundary, contract = load()
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.Route("1", "AZ", "https://a1-us.iotbing.com/api.json")
        for bad in ("", "+1", "01", "1 ", " 1", "86", "CN", "AY", "us", "44\n", "\u0664\u0664", 1, None, "EU"):
            with self.assertRaises(boundary.BoundaryRejected, msg=repr(bad)):
                boundary.select_route(bad, contract)


class RoundtripTests(GuardedTestCase):
    def _check(self, operation, business, action, version, country="1", sid=None, extra=None):
        boundary, contract = load()
        route = boundary.select_route(country, contract)
        env = synth_envelope(sid=sid, extra=extra)
        profile = SyntheticProfile()
        req = boundary.build_request(getattr(boundary.Operation, operation), business,
                                     route=route, envelope=env, contract=contract, profile=profile)
        plaintext, params, body = independent_body(action, version, business, env)
        # Seam: the validated business JSON reaches the encryptor exactly.
        self.assertEqual(profile.sealed, [(env["requestId"], plaintext)])
        self.assertEqual(json.loads(plaintext), business)
        self.assertEqual(profile.canonicals, [independent_canonical({k: v for k, v in params.items() if k != "sign"})])
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.url, route.url)
        self.assertEqual(req.headers, (("Content-Type", "application/x-www-form-urlencoded"),))
        self.assertEqual(req.body, body)
        # Lowest real serializer seam: aiohttp FormData from the same dict.
        from aiohttp import FormData
        payload = FormData(params)()
        self.assertEqual(bytes(payload._value), req.body)
        self.assertEqual(payload.content_type, "application/x-www-form-urlencoded")
        parsed = form_pairs(req.body)
        self.assertEqual([k for k, _ in parsed], [k for k in SOURCE_ORDER if k in params])
        fields = dict(parsed)
        self.assertEqual((fields["a"], fields["v"]), (action, version))
        # postData is ciphertext, not JSON; only the synthetic key opens it.
        with self.assertRaises(ValueError):
            json.loads(fields["postData"])
        self.assertEqual(json.loads(synth_open(fields["postData"])), business)
        rec = Recorder()
        out = boundary.send(req, operation=getattr(boundary.Operation, operation),
                            expected_business=business, route=route, contract=contract, exchange=rec)
        self.assertEqual(out, b"synthetic-response")
        self.assertEqual(rec.calls, [req])
        return req

    def test_real_form_roundtrip(self):
        self._check("TOKEN", TOKEN_BIZ, "thing.m.user.username.token.get", "2.0")
        self._check("PASSWORD_LOGIN", LOGIN_BIZ, "thing.m.user.email.password.login", "3.0")
        self._check("HOMES", HOMES_BIZ, "m.life.home.space.list", "1.0", sid="synthetic-sid")
        self._check("DEVICES", DEVICES_BIZ, "m.life.my.group.device.list", "2.2", sid="synthetic-sid")
        self._check("DEVICES", {"gid": "home-ABC_1"}, "m.life.my.group.device.list", "2.2",
                    sid="synthetic-sid", extra={"appRnVersion": "5.0", "bizDM": "synthetic"})
        self._check("TOKEN", dict(TOKEN_BIZ, countryCode="44"), "thing.m.user.username.token.get",
                    "2.0", country="44")

    def test_safe_repr_and_fixed_rejection(self):
        boundary, contract = load()
        route = boundary.select_route("1", contract)
        env = synth_envelope(extra={"deviceId": SENTINEL})
        req = boundary.build_request(boundary.Operation.TOKEN, dict(TOKEN_BIZ, username=SENTINEL + "@x.invalid"),
                                     route=route, envelope=env, contract=contract, profile=SyntheticProfile())
        for text in (repr(req), str(req)):
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn("postData", text)
            self.assertNotIn(route.url, text)

        def leaky(*_a):
            raise RuntimeError(SENTINEL)

        for profile in (SyntheticProfile(seal=leaky), SyntheticProfile(sign=leaky)):
            with self.assertRaises(boundary.BoundaryRejected) as caught:
                boundary.build_request(boundary.Operation.TOKEN, TOKEN_BIZ, route=route,
                                       envelope=synth_envelope(), contract=contract, profile=profile)
            err = caught.exception
            self.assertEqual(err.args, ())
            self.assertEqual(str(err), "boundary rejected")
            self.assertIsNone(err.__context__)
            self.assertIsNone(err.__cause__)
            self.assertNotIn(SENTINEL, repr(err))


def _prepared(operation="TOKEN", business=None, country="1", sid=None, request_id="req-0001"):
    boundary, contract = load()
    route = boundary.select_route(country, contract)
    if business is None:
        business = {"TOKEN": TOKEN_BIZ, "PASSWORD_LOGIN": LOGIN_BIZ,
                    "HOMES": HOMES_BIZ, "DEVICES": DEVICES_BIZ}[operation]
    if sid is None and operation in ("HOMES", "DEVICES"):
        sid = "synthetic-sid"
    op = getattr(boundary.Operation, operation)
    req = boundary.build_request(op, business, route=route, envelope=synth_envelope(request_id, sid=sid),
                                 contract=contract, profile=SyntheticProfile())
    return boundary, contract, route, op, business, req


class RejectBeforeWireTests(GuardedTestCase):
    def assertBuildRejected(self, operation, business, *, country="1", envelope=None, profile=None, route=None):
        boundary, contract = load()
        route = route or boundary.select_route(country, contract)
        prof = profile or SyntheticProfile()
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.build_request(operation, business, route=route,
                                   envelope=envelope if envelope is not None else synth_envelope(),
                                   contract=contract, profile=prof)
        return prof

    def test_reject_before_wire_build(self):
        boundary, contract = load()
        Op = boundary.Operation
        sid_env = synth_envelope(sid="synthetic-sid")
        cases = {
            "raw publish action": ("thing.m.device.dp.publish", {}, None),
            "raw read action string": ("m.life.home.space.list", {}, None),
            "status action": ("s.m.dev.dp.get", {"devId": "x"}, None),
            "extra field": (Op.TOKEN, dict(TOKEN_BIZ, extra="x"), None),
            "missing field": (Op.TOKEN, {"countryCode": "1", "username": EMAIL}, None),
            "nested dict": (Op.TOKEN, dict(TOKEN_BIZ, username={"x": EMAIL}), None),
            "nested list": (Op.DEVICES, {"gid": [1]}, sid_env),
            "string false": (Op.TOKEN, dict(TOKEN_BIZ, isUid="false"), None),
            "isUid int 0": (Op.TOKEN, dict(TOKEN_BIZ, isUid=0), None),
            "ifencrypt bool": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, ifencrypt=True), None),
            "ifencrypt str": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, ifencrypt="1"), None),
            "options changed": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, options='{"group":1}'), None),
            "passwd uppercase": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, passwd="AB" * 128), None),
            "passwd plaintext": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, passwd="hunter2"), None),
            "passwd odd hex": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, passwd="a" * 255), None),
            "email without at": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, email="synthetic-user"), None),
            "username without at": (Op.TOKEN, dict(TOKEN_BIZ, username="5550100"), None),
            "username whitespace": (Op.TOKEN, dict(TOKEN_BIZ, username=" " + EMAIL), None),
            "business action conflict": (Op.HOMES, {"a": "thing.m.device.dp.publish"}, sid_env),
            "business version conflict": (Op.HOMES, {"v": "9.9"}, sid_env),
            "token business on login": (Op.PASSWORD_LOGIN, TOKEN_BIZ, None),
            "login business on token": (Op.TOKEN, LOGIN_BIZ, None),
            "country route mismatch": (Op.TOKEN, dict(TOKEN_BIZ, countryCode="44"), None),
            "unsupported country": (Op.TOKEN, dict(TOKEN_BIZ, countryCode="86"), None),
            "country int": (Op.TOKEN, dict(TOKEN_BIZ, countryCode=1), None),
            "gid bool": (Op.DEVICES, {"gid": True}, sid_env),
            "gid zero": (Op.DEVICES, {"gid": 0}, sid_env),
            "gid empty": (Op.DEVICES, {"gid": ""}, sid_env),
            "gid float": (Op.DEVICES, {"gid": 1.5}, sid_env),
            "gid slash": (Op.DEVICES, {"gid": "a/b"}, sid_env),
            "oversized username": (Op.TOKEN, dict(TOKEN_BIZ, username="a" * 9000 + "@x.invalid"), None),
            "oversized token": (Op.PASSWORD_LOGIN, dict(LOGIN_BIZ, token="t" * 9000), None),
            "business not mapping": (Op.TOKEN, [("countryCode", "1")], None),
            "envelope overwrites a": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"a": "thing.m.device.dp.publish"})),
            "envelope overwrites v": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"v": "1.0"})),
            "envelope overwrites postData": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"postData": "x"})),
            "envelope overwrites sign": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"sign": "0" * 64})),
            "envelope unknown key": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"h5": "1"})),
            "envelope literal os": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"os": "iOS"})),
            "envelope literal cp": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"cp": "none"})),
            "envelope non-str": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"time": 1700000000})),
            "envelope control char": (Op.TOKEN, TOKEN_BIZ, synth_envelope(extra={"lang": "en\r\nX: y"})),
            "envelope missing key": (Op.TOKEN, TOKEN_BIZ, {k: v for k, v in synth_envelope().items() if k != "chKey"}),
            "envelope empty requestId": (Op.TOKEN, TOKEN_BIZ, synth_envelope(request_id="")),
            "sid before login": (Op.TOKEN, TOKEN_BIZ, synth_envelope(sid="synthetic-sid")),
            "sid on password login": (Op.PASSWORD_LOGIN, LOGIN_BIZ, synth_envelope(sid="synthetic-sid")),
            "homes without sid": (Op.HOMES, {}, synth_envelope()),
            "devices without sid": (Op.DEVICES, DEVICES_BIZ, synth_envelope()),
            "empty sid": (Op.HOMES, {}, synth_envelope(sid="")),
        }
        for name, (operation, business, env) in cases.items():
            with self.subTest(name):
                prof = self.assertBuildRejected(operation, business, envelope=env)
                if name.startswith(("envelope", "sid", "homes", "devices", "empty sid")) or "country" in name:
                    self.assertEqual(prof.sealed, [], name)

    def test_reject_bad_provider_output(self):
        boundary, contract = load()
        Op = boundary.Operation
        outputs = {
            "seal not base64": dict(seal=lambda rid, pt: "!!not-base64!!"),
            "seal wrong length": dict(seal=lambda rid, pt: base64.b64encode(b"\0" * 40).decode()),
            "seal bytes": dict(seal=lambda rid, pt: synth_seal(rid, pt).encode()),
            "seal unrelated business": dict(seal=lambda rid, pt: synth_seal(rid, pt + b" ")),
            "seal urlsafe alphabet": dict(seal=lambda rid, pt: synth_seal(rid, pt).replace("+", "-").replace("/", "_") + "-"),
            "sign uppercase": dict(sign=lambda c: synth_sign(c).upper()),
            "sign short": dict(sign=lambda c: "abc"),
            "sign none": dict(sign=lambda c: None),
        }
        for name, kwargs in outputs.items():
            with self.subTest(name):
                self.assertBuildRejected(Op.TOKEN, TOKEN_BIZ, profile=SyntheticProfile(**kwargs))

    def test_reject_before_wire(self):
        """Prepared requests mutated after build never reach the exchange."""
        def body_mut(fn):
            def apply(req):
                return mutate(req, body=fn(req.body))
            return apply

        a_publish = body_mut(lambda b: replace_field(b, "a", "thing.m.device.dp.publish"))
        cases = {
            "replace drops binding": lambda req: dataclasses.replace(req, body=req.body),
            "action to publish": a_publish,
            "action to other read": body_mut(lambda b: replace_field(b, "a", "m.life.home.space.list")),
            "version mismatch": body_mut(lambda b: replace_field(b, "v", "1.0")),
            "duplicate action": body_mut(lambda b: b + b"&a=thing.m.user.username.token.get"),
            "duplicate postData": body_mut(lambda b: b + b"&postData=AAAA"),
            "duplicate sign equal": body_mut(lambda b: b + b"&sign=" + dict(form_pairs(b))["sign"].encode()),
            "reordered": body_mut(lambda b: reencode(list(reversed(form_pairs(b))))),
            "extra field": body_mut(lambda b: b + b"&h5=1"),
            "sid injected": body_mut(lambda b: b + b"&sid=x"),
            "sign changed": body_mut(lambda b: replace_field(b, "sign", "0" * 64)),
            "envelope changed": body_mut(lambda b: replace_field(b, "deviceId", "other")),
            "empty pair": body_mut(lambda b: b + b"&"),
            "no equals": body_mut(lambda b: b + b"&flag"),
            "double equals": body_mut(lambda b: b.replace(b"os=Android", b"os=Andr=oid")),
            "bad percent": body_mut(lambda b: b.replace(b"lang=en", b"lang=%zz")),
            "truncated percent": body_mut(lambda b: b.replace(b"lang=en", b"lang=e%4")),
            "noncanonical percent": body_mut(lambda b: b.replace(b"lang=en", b"lang=%65n")),
            "lowercase percent hex": body_mut(lambda b: b.replace(b"%2B", b"%2b", 1) if b"%2B" in b else b.replace(b"%40", b"%40%2b")),
            "invalid utf8": body_mut(lambda b: b.replace(b"lang=en", b"lang=%FF")),
            "non-ascii byte": body_mut(lambda b: b.replace(b"lang=en", "lang=\u00e9".encode())),
            "space raw": body_mut(lambda b: b.replace(b"lang=en", b"lang=e n")),
            "oversized": body_mut(lambda b: b + b"&pad=" + b"x" * 20000),
            "caller header": lambda req: mutate(req, headers=req.headers + (("X-Extra", "1"),)),
            "header case": lambda req: mutate(req, headers=(("content-type", "application/x-www-form-urlencoded"),)),
            "content type charset": lambda req: mutate(req, headers=(("Content-Type", "application/x-www-form-urlencoded; charset=utf-8"),)),
            "no headers": lambda req: mutate(req, headers=()),
            "method GET": lambda req: mutate(req, method="GET"),
            "body str": lambda req: mutate(req, body=req.body.decode()),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                boundary, contract, route, op, business, req = _prepared()
                bad = fn(req)
                rec = Recorder()
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.send(bad, operation=op, expected_business=business, route=route,
                                  contract=contract, exchange=rec)
                self.assertEqual(rec.calls, [])
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.validate_request(bad, operation=op, expected_business=business,
                                              route=route, contract=contract)

    def test_structural_form_parser_alone(self):
        """The final-byte parser rejects malformed forms even without the binding."""
        boundary, contract = load()
        _, _, _, _, _, req = _prepared()
        self.assertEqual([k for k, _ in boundary._parse_form(req.body, contract)][:2], ["a", "v"])
        for raw in (b"a=x&a=y", b"a=%zz", b"a=%4", b"a=%65", b"a=%2b", b"a=%FF", b"a=e n",
                    b"a=x&", b"flag", b"a=x=y", b"v=1&a=x", b"pad=1", "a=\u00e9".encode(), b"",
                    b"a=" + b"x" * 9000, "a=x"):
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary._parse_form(raw, contract)

    def test_reject_binding_mismatch(self):
        boundary, contract, route, op, business, req = _prepared("HOMES")
        Op = boundary.Operation
        other_route = boundary.select_route("44", contract)
        cases = {
            "wrong operation": dict(operation=Op.DEVICES, expected_business={}),
            "wrong operation business": dict(operation=Op.DEVICES, expected_business=DEVICES_BIZ),
            "wrong expected business": dict(expected_business={"x": 1}),
            "other route": dict(route=other_route),
            "operation string": dict(operation="m.life.home.space.list"),
        }
        for name, override in cases.items():
            with self.subTest(name):
                kwargs = dict(operation=op, expected_business=business, route=route, contract=contract)
                kwargs.update(override)
                rec = Recorder()
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.send(req, exchange=rec, **kwargs)
                self.assertEqual(rec.calls, [])
        rec = Recorder()
        for obj in (None, b"body", {"body": req.body}):
            with self.assertRaises(boundary.BoundaryRejected):
                boundary.send(obj, operation=op, expected_business=business, route=route,
                              contract=contract, exchange=rec)
        self.assertEqual(rec.calls, [])
        # The untouched request still goes out exactly once.
        boundary.send(req, operation=op, expected_business=business, route=route, contract=contract, exchange=rec)
        self.assertEqual(rec.calls, [req])

    def test_ciphertext_substitution_rejected(self):
        boundary, contract, route, op, biz_a, req_a = _prepared(
            "TOKEN", dict(TOKEN_BIZ, username="alpha@example.invalid"), request_id="req-a")
        _, _, _, _, biz_b, req_b = _prepared(
            "TOKEN", dict(TOKEN_BIZ, username="bravo@example.invalid"), request_id="req-b")
        post_b = dict(form_pairs(req_b.body))["postData"]
        self.assertNotEqual(post_b, dict(form_pairs(req_a.body))["postData"])
        # Same requestId so only ciphertext differs: still rejected.
        _, _, _, _, _, req_c = _prepared(
            "TOKEN", dict(TOKEN_BIZ, username="charlie@example.invalid"), request_id="req-a")
        post_c = dict(form_pairs(req_c.body))["postData"]
        for name, body in {
            "postData from other business": replace_field(req_a.body, "postData", post_b),
            "postData same requestId": replace_field(req_a.body, "postData", post_c),
            "whole other body": req_b.body,
        }.items():
            with self.subTest(name):
                _, _, _, _, _, victim = _prepared(
                    "TOKEN", dict(TOKEN_BIZ, username="alpha@example.invalid"), request_id="req-a")
                mutate(victim, body=body)
                rec = Recorder()
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.send(victim, operation=op, expected_business=biz_a, route=route,
                                  contract=contract, exchange=rec)
                self.assertEqual(rec.calls, [])
        # And a request cannot be revalidated against the other business.
        rec = Recorder()
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.send(req_b, operation=op, expected_business=biz_a, route=route,
                          contract=contract, exchange=rec)
        self.assertEqual(rec.calls, [])


class PasswordOneShotTests(GuardedTestCase):
    """A prepared PASSWORD_LOGIN request is one-shot at the final boundary."""

    def _assert_fixed(self, boundary, caught):
        err = caught.exception
        self.assertIsInstance(err, boundary.BoundaryRejected)
        self.assertEqual(err.args, ())
        self.assertIsNone(err.__context__)
        self.assertIsNone(err.__cause__)

    def test_password_request_sent_at_most_once(self):
        boundary, contract, route, op, business, req = _prepared("PASSWORD_LOGIN")
        kwargs = dict(operation=op, expected_business=business, route=route, contract=contract)
        rec = Recorder()
        self.assertEqual(boundary.send(req, exchange=rec, **kwargs), b"synthetic-response")
        for _ in range(3):
            with self.assertRaises(boundary.BoundaryRejected) as caught:
                boundary.send(req, exchange=rec, **kwargs)
            self._assert_fixed(boundary, caught)
        with self.assertRaises(boundary.BoundaryRejected) as caught:
            boundary.validate_request(req, **kwargs)
        self._assert_fixed(boundary, caught)
        self.assertEqual(rec.calls, [req])

    def test_failed_password_exchange_still_consumes_the_binding(self):
        boundary, contract, route, op, business, req = _prepared("PASSWORD_LOGIN")
        kwargs = dict(operation=op, expected_business=business, route=route, contract=contract)
        calls = []

        def failing(request):
            calls.append(request)
            raise TimeoutError("ambiguous")
        with self.assertRaises(TimeoutError):
            boundary.send(req, exchange=failing, **kwargs)
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.send(req, exchange=failing, **kwargs)
        self.assertEqual(calls, [req])

    def test_concurrent_password_resend_admits_one(self):
        import threading
        boundary, contract, route, op, business, req = _prepared("PASSWORD_LOGIN")
        kwargs = dict(operation=op, expected_business=business, route=route, contract=contract)
        rec = Recorder()
        outcomes = []

        def attempt():
            try:
                boundary.send(req, exchange=rec, **kwargs)
                outcomes.append("sent")
            except boundary.BoundaryRejected:
                outcomes.append("rejected")
        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["rejected"] * 15 + ["sent"])
        self.assertEqual(rec.calls, [req])


class DestinationTests(GuardedTestCase):
    HOSTILE = [
        "HTTPS://a1-us.iotbing.com/api.json",
        "https://A1-US.iotbing.com/api.json",
        "https://a1-us.iotbing.com./api.json",
        "https://a1-us.iotbing.com.evil.example/api.json",
        "https://evil-a1-us.iotbing.com/api.json",
        "https://xa1-us.iotbing.com/api.json",
        "https://user@a1-us.iotbing.com/api.json",
        "https://user:pw@a1-us.iotbing.com/api.json",
        "https://a1-us.iotbing.com@evil.example/api.json",
        "https://a1-us.iotbing.com\\@evil.example/api.json",
        "http://a1-us.iotbing.com/api.json",
        "https://a1-us.iotbing.com:8443/api.json",
        "https://a1-us.iotbing.com:0443/api.json",
        "https://a1-us.iotbing.com:/api.json",
        "https://a1-us.iotbing.com/api.json?x=1",
        "https://a1-us.iotbing.com/api.json?",
        "https://a1-us.iotbing.com/api.json#frag",
        "https://a1-us.iotbing.com/API.json",
        "https://a1-us.iotbing.com/api.json/",
        "https://a1-us.iotbing.com//api.json",
        "https://a1-us.iotbing.com/v1/api.json",
        "https://a1-us.iotbing.com/%61pi.json",
        "https://a1-us.iotbing.com/./api.json",
        "https://a1-us.iotbing.com;x/api.json",
        "https://a1%2Dus.iotbing.com/api.json",
        "https://a1-us.iotbing.com/api.json;p",
        " https://a1-us.iotbing.com/api.json",
        "https://a1-us.iotbing.com/api.json\n",
        "https://\u04301-us.iotbing.com/api.json",
        "https://xn--1-us-3ve.iotbing.com/api.json",
        "https://127.0.0.1/api.json",
        "https://10.0.0.1/api.json",
        "https://192.168.210.1/api.json",
        "https://[::1]/api.json",
        "https://[::ffff:127.0.0.1]/api.json",
        "https://0x7f000001/api.json",
        "https://2130706433/api.json",
        "https://localhost/api.json",
        "https://h2-eu.iot-dns.com/api.json",
        "https://a1.tuyaeu.com/api.json",
        "https://a1.tuyaus.com/api.json",
        "https://a1.tuyacn.com/api.json",
        "//a1-us.iotbing.com/api.json",
        "a1-us.iotbing.com/api.json",
        "",
    ]

    def test_destination_matrix(self):
        for url in self.HOSTILE:
            with self.subTest(url=url):
                boundary, contract, route, op, business, req = _prepared()
                self.assertEqual(route.url, "https://a1-us.iotbing.com/api.json")
                mutate(req, url=url)
                rec = Recorder()
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.send(req, operation=op, expected_business=business, route=route,
                                  contract=contract, exchange=rec)
                self.assertEqual(rec.calls, [])

    def test_explicit_default_port_accepted_structurally(self):
        boundary, contract, route, op, business, req = _prepared()
        mutate(req, url="https://a1-us.iotbing.com:443/api.json")
        rec = Recorder()
        boundary.send(req, operation=op, expected_business=business, route=route, contract=contract, exchange=rec)
        self.assertEqual(len(rec.calls), 1)

    def test_country_route_mismatch_and_forged_route(self):
        boundary, contract = load()
        az, eu = boundary.select_route("1", contract), boundary.select_route("44", contract)
        # Business country 44 cannot be sent via the AZ route, and vice versa.
        for route, country in ((az, "44"), (eu, "1"), (eu, "91")):
            with self.subTest(region=route.region, country=country):
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.build_request(boundary.Operation.TOKEN, dict(TOKEN_BIZ, countryCode=country),
                                           route=route, envelope=synth_envelope(), contract=contract,
                                           profile=SyntheticProfile())
        # A route forged past the constructor gate is rechecked against the contract.
        forged = object.__new__(boundary.Route)
        for name, value in (("country_code", "1"), ("region", "AZ"),
                            ("url", "https://a1.tuyaeu.com/api.json")):
            object.__setattr__(forged, name, value)
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.build_request(boundary.Operation.TOKEN, TOKEN_BIZ, route=forged,
                                   envelope=synth_envelope(), contract=contract, profile=SyntheticProfile())
        # Post-login operations still require the contract route.
        boundary2, contract2, route2, op2, biz2, req2 = _prepared("HOMES")
        rec = Recorder()
        with self.assertRaises(boundary.BoundaryRejected):
            boundary2.send(req2, operation=op2, expected_business=biz2, route=forged,
                           contract=contract2, exchange=rec)
        self.assertEqual(rec.calls, [])


class DirectedRouteTests(GuardedTestCase):
    """Server-directed API host: strict allowlist, fixed family label."""

    ACCEPTED = [
        ("https://a1.tuyaeu.com", "https://a1.tuyaeu.com/api.json", "TUYAEU"),
        ("https://a1.tuyaus.com/", "https://a1.tuyaus.com/api.json", "TUYAUS"),
        ("https://a1.tuyacn.com/api.json", "https://a1.tuyacn.com/api.json", "TUYACN"),
        ("https://a1.tuyain.com", "https://a1.tuyain.com/api.json", "TUYAIN"),
        ("https://a1-sg.iotbing.com", "https://a1-sg.iotbing.com/api.json", "IOTBING"),
        ("https://a1-in.iotbing.com:443/api.json", "https://a1-in.iotbing.com/api.json", "IOTBING"),
        ("https://a1.iotbing.com", "https://a1.iotbing.com/api.json", "IOTBING"),
        ("https://a1-eu.tuyaeu.com", "https://a1-eu.tuyaeu.com/api.json", "TUYAEU"),
        ("https://a1-ueaz.tuyaus.com/api.json", "https://a1-ueaz.tuyaus.com/api.json", "TUYAUS"),
        ("https://a1x9.tuyacn.com", "https://a1x9.tuyacn.com/api.json", "TUYACN"),
        ("https://a1-in.tuyain.com", "https://a1-in.tuyain.com/api.json", "TUYAIN"),
        ("https://a1.iot334.com", "https://a1.iot334.com/api.json", "IOT334"),
        ("https://a1-ru.iot334.com/", "https://a1-ru.iot334.com/api.json", "IOT334"),
    ]
    REJECTED = [
        "https://a1.tuyaeu.com/other.json",
        "https://a1.tuyaeu.com/api.json/",
        "https://a1.tuyaeu.com/v1/api.json",
        "https://a1.tuyaeu.com//",
        "https://a1.tuyaeu.com/API.json",
        "http://a1.tuyaeu.com",
        "http://a1.tuyaeu.com/api.json",
        "HTTPS://a1.tuyaeu.com",
        "https://a1.tuyaeu.com:8443",
        "https://a1.tuyaeu.com:8443/api.json",
        "https://a1.tuyaeu.com:",
        "https://a1.tuyaeu.com:0443/api.json",
        "https://127.0.0.1",
        "https://192.168.210.1/api.json",
        "https://[::1]/api.json",
        "https://2130706433",
        "https://localhost",
        "https://a1.localhost/api.json",
        "https://user@a1.tuyaeu.com",
        "https://user:pw@a1.tuyaeu.com/api.json",
        "https://a1.tuyaeu.com?x=1",
        "https://a1.tuyaeu.com/api.json?x=1",
        "https://a1.tuyaeu.com/api.json?",
        "https://a1.tuyaeu.com#frag",
        "https://a1.tuyaeu.com/api.json#frag",
        "https://a1.tuyaeu.com/api.json;p",
        "https://a1.tuyaeu.com;p",
        "https://A1.tuyaeu.com",
        "https://a1.TUYAEU.com/api.json",
        "https://a1.tuyaeu.com.",
        "https://a1.tuyaeu.com./api.json",
        "https://xn--a1-tuya.tuyaeu.com",
        "https://a1.xn--tuyaeu-9ta.com",
        "https://\u0430\u0031.tuyaeu.com",
        "https://evil-a1.iotbing.com",
        "https://xa1.iotbing.com",
        "https://a1.iotbing.com.attacker.net",
        "https://a1.iotbing.com.attacker.net/api.json",
        "https://a1.tuya.com",
        "https://a1.tuya.com/api.json",
        "https://a2.tuyaeu.com",
        "https://b.a1.tuyaeu.com",
        "https://a1.sub.tuyaeu.com",
        "https://a1-.iotbing.com",
        "https://a1_x.iotbing.com",
        "https://a1..iotbing.com",
        "https://h2-eu.iot-dns.com",
        "https://a1" + "x" * 62 + ".iotbing.com",
        "https://a1" + "x" * 300 + ".iotbing.com",
        "https://a1.tuyaeu.com" + "/" * 300,
        " https://a1.tuyaeu.com",
        "https://a1.tuyaeu.com ",
        "https://a1.tuyaeu.com\n",
        "https://a1.tuyaeu.com\\api.json",
        "https://a1%2Etuyaeu.com",
        "https://a1[.tuyaeu.com",
        "https://[xyz]",
        "https://[a1.tuyaeu.com]",
        "https://a1.tuyaeu.com]",
        "//a1.tuyaeu.com",
        "a1.tuyaeu.com",
        "https://",
        "",
    ]
    NON_STRINGS = [None, 5, b"https://a1.tuyaeu.com", ["https://a1.tuyaeu.com"],
                   {"mobileApiUrl": "https://a1.tuyaeu.com"}, True]

    def test_allowlisted_forms_accepted_with_family_label(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        for value, url, label in self.ACCEPTED:
            with self.subTest(value=value):
                route = boundary.directed_route(value, base, contract)
                self.assertIsInstance(route, boundary.Route)
                self.assertIsNot(route, base)
                self.assertEqual(route.url, url)
                self.assertEqual(route.country_code, "1")
                self.assertEqual(route.region, base.region)
                self.assertIs(route.api_domain, boundary.ApiDomain[label])
                self.assertEqual(route.api_domain.value, label)
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    route.url = "https://evil.example/api.json"

    def test_directed_country_code_kept_for_every_country(self):
        boundary, contract = load()
        for country in ("1", "44", "91", "7"):
            base = boundary.select_route(country, contract)
            route = boundary.directed_route("https://a1-sg.iotbing.com", base, contract)
            self.assertEqual((route.country_code, route.region), (base.country_code, base.region))

    def test_rejected_forms(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        for value in self.REJECTED + self.NON_STRINGS:
            with self.subTest(value=repr(value)[:60]):
                with self.assertRaises(boundary.BoundaryRejected) as caught:
                    boundary.directed_route(value, base, contract)
                self.assertIs(type(caught.exception), boundary.BoundaryRejected)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(str(caught.exception), "boundary rejected")

    def test_same_route_returns_label_same(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        self.assertIs(base.api_domain, boundary.ApiDomain.SAME)
        for value in ("https://a1-us.iotbing.com", "https://a1-us.iotbing.com/",
                      "https://a1-us.iotbing.com/api.json",
                      "https://a1-us.iotbing.com:443/api.json"):
            with self.subTest(value=value):
                self.assertIs(boundary.directed_route(value, base, contract), base)

    def test_label_set_is_closed_and_carries_no_host(self):
        boundary, _contract = load()
        self.assertEqual([member.name for member in boundary.ApiDomain],
                         ["SAME", "IOTBING", "TUYAEU", "TUYAUS", "TUYACN", "TUYAIN", "IOT334"])
        for member in boundary.ApiDomain:
            self.assertEqual(member.name, member.value)
            self.assertNotIn(".", member.value)
            self.assertRegex(member.value, r"\A[A-Z0-9]+\Z")
        hosts = boundary._DIRECTED_API_HOSTS
        self.assertIs(type(hosts), tuple)
        self.assertEqual(hosts, (
            ("exact", "a1.tuyaeu.com", boundary.ApiDomain.TUYAEU),
            ("exact", "a1.tuyaus.com", boundary.ApiDomain.TUYAUS),
            ("exact", "a1.tuyacn.com", boundary.ApiDomain.TUYACN),
            ("exact", "a1.tuyain.com", boundary.ApiDomain.TUYAIN),
            ("a1-label", "iotbing.com", boundary.ApiDomain.IOTBING),
            ("a1-label", "tuyaeu.com", boundary.ApiDomain.TUYAEU),
            ("a1-label", "tuyaus.com", boundary.ApiDomain.TUYAUS),
            ("a1-label", "tuyacn.com", boundary.ApiDomain.TUYACN),
            ("a1-label", "tuyain.com", boundary.ApiDomain.TUYAIN),
            ("a1-label", "iot334.com", boundary.ApiDomain.IOT334),
        ))
        # A sentinel subdomain never reaches the label or the route repr.
        base = boundary.select_route("1", load()[1])
        route = boundary.directed_route("https://a1-zqsentinelhost.iotbing.com", base, load()[1])
        self.assertIs(route.api_domain, boundary.ApiDomain.IOTBING)
        for text in (route.api_domain.value, route.api_domain.name, repr(route.api_domain),
                     str(route.api_domain), repr(route)):
            self.assertNotIn("zqsentinelhost", text)
            self.assertNotIn("iotbing.com", text)

    def test_bad_inputs_rejected(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        for args in (("https://a1.tuyaeu.com", None, contract),
                     ("https://a1.tuyaeu.com", base, None),
                     ("https://a1.tuyaeu.com", "https://a1-us.iotbing.com/api.json", contract)):
            with self.subTest(args=repr(args[1])[:20] + repr(args[2])[:20]):
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.directed_route(*args)

    def test_at_most_one_redirect(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        moved = boundary.directed_route("https://a1.tuyaeu.com", base, contract)
        # Restating the accepted destination returns the same route.
        for value in ("https://a1.tuyaeu.com", "https://a1.tuyaeu.com/api.json"):
            self.assertIs(boundary.directed_route(value, moved, contract), moved)
        # Any further change, even to an allowlisted host or back, is rejected.
        for value in ("https://a1.tuyaus.com", "https://a1-us.iotbing.com",
                      "https://a1-sg.iotbing.com/api.json"):
            with self.subTest(value=value):
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.directed_route(value, moved, contract)

    def test_directed_route_builds_and_sends_to_new_url_only(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        moved = boundary.directed_route("https://a1-sg.iotbing.com", base, contract)
        op = boundary.Operation.HOMES
        req = boundary.build_request(op, HOMES_BIZ, route=moved,
                                     envelope=synth_envelope(sid="synthetic-sid"),
                                     contract=contract, profile=SyntheticProfile())
        self.assertEqual(req.url, "https://a1-sg.iotbing.com/api.json")
        self.assertTrue(boundary.is_directed_request(req))
        # The request is bound to the directed route, not the original one.
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.validate_request(req, operation=op, expected_business=HOMES_BIZ,
                                      route=base, contract=contract)
        rec = Recorder()
        boundary.send(req, operation=op, expected_business=HOMES_BIZ, route=moved,
                      contract=contract, exchange=rec)
        self.assertEqual([r.url for r in rec.calls], ["https://a1-sg.iotbing.com/api.json"])
        # Business country still has to equal the (original) route country.
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.build_request(boundary.Operation.TOKEN, dict(TOKEN_BIZ, countryCode="44"),
                                   route=moved, envelope=synth_envelope(), contract=contract,
                                   profile=SyntheticProfile())
        # Mutating the bound URL to another allowlisted host is rejected.
        mutate(req, url="https://a1-xx.iotbing.com/api.json")
        self.assertFalse(boundary.is_directed_request(req))
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.send(req, operation=op, expected_business=HOMES_BIZ, route=moved,
                          contract=contract, exchange=rec)
        self.assertEqual(len(rec.calls), 1)

    def test_is_directed_request_only_for_bound_directed_requests(self):
        boundary, contract, route, op, business, req = _prepared("HOMES")
        self.assertFalse(boundary.is_directed_request(req))
        hand = boundary.WireRequest("POST", "https://a1-sg.iotbing.com/api.json",
                                    (("Content-Type", boundary.CONTENT_TYPE),), req.body)
        self.assertFalse(boundary.is_directed_request(hand))
        for other in (None, "https://a1-sg.iotbing.com/api.json", object()):
            self.assertFalse(boundary.is_directed_request(other))

    def test_directed_route_only_for_session_list_operations(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        moved = boundary.directed_route("https://a1-sg.iotbing.com", base, contract)
        for op, business, sid in ((boundary.Operation.TOKEN, TOKEN_BIZ, None),
                                  (boundary.Operation.PASSWORD_LOGIN, LOGIN_BIZ, None)):
            with self.subTest(op=op.name):
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.build_request(op, business, route=moved,
                                           envelope=synth_envelope(sid=sid),
                                           contract=contract, profile=SyntheticProfile())
        req = boundary.build_request(boundary.Operation.DEVICES, DEVICES_BIZ, route=moved,
                                     envelope=synth_envelope(sid="synthetic-sid"),
                                     contract=contract, profile=SyntheticProfile())
        self.assertTrue(boundary.is_directed_request(req))

    def test_issued_directed_route_mutated_in_place_rejected(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        moved = boundary.directed_route("https://a1-sg.iotbing.com", base, contract)
        object.__setattr__(moved, "url", "https://a1-other.iotbing.com/api.json")
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.build_request(boundary.Operation.HOMES, HOMES_BIZ, route=moved,
                                   envelope=synth_envelope(sid="synthetic-sid"),
                                   contract=contract, profile=SyntheticProfile())

    def test_subclassed_request_not_directed(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        moved = boundary.directed_route("https://a1-sg.iotbing.com", base, contract)
        req = boundary.build_request(boundary.Operation.HOMES, HOMES_BIZ, route=moved,
                                     envelope=synth_envelope(sid="synthetic-sid"),
                                     contract=contract, profile=SyntheticProfile())

        class Alias(boundary.WireRequest):
            def __hash__(self):
                return hash(req)

            def __eq__(self, other):
                return other is req or other is self
        alias = Alias(req.method, req.url, req.headers, req.body)
        self.assertFalse(boundary.is_directed_request(alias))

    def test_forged_directed_route_rejected(self):
        boundary, contract = load()
        base = boundary.select_route("1", contract)
        moved = boundary.directed_route("https://a1-sg.iotbing.com", base, contract)
        forgeries = []
        for url, label in (("https://a1-sg.iotbing.com/api.json", boundary.ApiDomain.IOTBING),
                           ("https://evil.example/api.json", boundary.ApiDomain.IOTBING),
                           ("https://a1-sg.iotbing.com/api.json", boundary.ApiDomain.TUYAEU),
                           ("https://a1-sg.iotbing.com/api.json", "IOTBING")):
            forged = object.__new__(boundary.Route)
            for name, value in (("country_code", "1"), ("region", "AZ"), ("url", url),
                                ("api_domain", label)):
                object.__setattr__(forged, name, value)
            forgeries.append(forged)
        # A copy made past the constructor gate is not the issued route.
        import copy
        forgeries.append(copy.copy(moved))
        # The issued route with a changed country is not accepted either.
        changed = copy.copy(moved)
        object.__setattr__(changed, "country_code", "44")
        forgeries.append(changed)
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.Route("1", "AZ", "https://a1-sg.iotbing.com/api.json",
                           boundary.ApiDomain.IOTBING)
        for forged in forgeries:
            with self.subTest(forged=forged.url):
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.build_request(boundary.Operation.TOKEN, TOKEN_BIZ, route=forged,
                                           envelope=synth_envelope(), contract=contract,
                                           profile=SyntheticProfile())
                with self.assertRaises(boundary.BoundaryRejected):
                    boundary.directed_route("https://a1.tuyaus.com", forged, contract)


if __name__ == "__main__":
    unittest.main()
