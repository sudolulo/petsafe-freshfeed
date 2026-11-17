"""Offline tests for the minimal PetSafe account client (Task 2).

Only the Exchange and the opaque profile are fakes (synthetic_fixtures.py);
the client, the Task 1 boundary/serializer and every validation are real.
Synthetic values only; setUpModule installs fail-closed socket/DNS/urllib/
http.client/subprocess guards for this process (not universal isolation).

Synthetic-provider success proves plumbing only. No independent PetSafe
signer/envelope vector exists; see test_no_independent_signer_vector.
"""
import contextlib
import hashlib
import io
import json
import pickle
import socket
import threading
import time
import unittest
import urllib.parse

from cryptography.hazmat.primitives.asymmetric import padding

from custom_components.petsafe_freshfeed.account import client as ac
from custom_components.petsafe_freshfeed.account import boundary
import account_fixtures as sf
from account_fixtures import Envelope, FakeExchange, FakeServer, Raw, SyntheticProfile

O = ac.Outcome
AZ_URL = "https://a1-us.iotbing.com/api.json"
# A changed route the directed-host allowlist does not admit.
NOT_ALLOWLISTED = "https://a1.tuya.com"
ROWS = [
    ("TOKEN", "thing.m.user.username.token.get", "2.0"),
    ("PASSWORD_LOGIN", "thing.m.user.email.password.login", "3.0"),
    ("HOMES", "m.life.home.space.list", "1.0"),
    ("DEVICES", "m.life.my.group.device.list", "2.2"),
]


def setUpModule():
    sf.install_path_guards()


def tearDownModule():
    sf.remove_path_guards()


CONTRACT = None


def contract():
    global CONTRACT
    if CONTRACT is None:
        CONTRACT = boundary.Contract.load()
    return CONTRACT


def granted_permit():
    """The per-call permit ``account.discover`` issues (the supervisor's
    per-run nonce in the harness)."""
    return ac.PasswordPermit._issue()


def run(server=None, *, profile=None, exchange=None, permit=None, creds=None,
        authorized=True, timeout=30.0):
    profile = profile or SyntheticProfile()
    exchange = exchange or FakeExchange(server or FakeServer())
    permit = granted_permit() if permit is None else permit
    client = ac.AccountClient(profile, exchange, contract(), permit=permit)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = client.run(creds or sf.credentials(), password_authorized=authorized,
                            deadline=time.monotonic() + timeout)
    assert out.getvalue() == "" and err.getvalue() == "", "client wrote to stdout/stderr"
    return result, exchange, profile, client


def ok(homes, devices, supported, keys=None, names=None):
    """An OK result. ``keys``/``names`` default to ``supported``:
    the default fixtures (``dev`` and ``sf.default_devices``) give every
    device a valid synthetic ``localKey`` and ``name``."""
    keys = supported if keys is None else keys
    names = supported if names is None else names
    return ac.SafeResult(O.OK, homes, devices, supported,
                         key_count=keys, name_count=names)


def fail(outcome):
    return ac.SafeResult(outcome, 0, 0, 0)


def homes_of(*gids):
    return lambda: {"result": [{"gid": g, "name": sf.SENTINEL} for g in gids]}


def dev(dev_id, product=sf.OBSERVED_PRODUCT, **extra):
    record = {"devId": dev_id, "localKey": sf.SENTINEL + "-lk", "name": sf.SENTINEL}
    if product is not None:
        record["productId"] = product
    record.update(extra)
    return record


def assert_no_secrets(test, *values):
    for value in values:
        text = repr(value) + str(value)
        for secret in sf.SECRET_STRINGS:
            test.assertNotIn(secret, text)


class GuardedTestCase(unittest.TestCase):
    """Every test must end with zero offline-guard hits: a denied network or
    subprocess call can never be silently absorbed into an ordinary outcome.
    Tests that trigger guards on purpose consume their hits explicitly."""

    def setUp(self):
        sf.take_guard_hits()

    def tearDown(self):
        self.assertEqual(sf.take_guard_hits(), 0, "an offline guard fired")


# --------------------------------------------------------------------------


class OfflineGuardTests(GuardedTestCase):
    def test_offline_guard_active(self):
        with self.assertRaises(sf.NetworkAttempt):
            socket.create_connection(("a1-us.iotbing.com", 443))
        with self.assertRaises(sf.NetworkAttempt):
            socket.getaddrinfo("a1-us.iotbing.com", 443)
        import subprocess
        import urllib.request
        with self.assertRaises(sf.NetworkAttempt):
            urllib.request.urlopen(AZ_URL)
        with self.assertRaises(sf.NetworkAttempt):
            subprocess.run(["true"])
        self.assertEqual(sf.take_guard_hits(), 4)

    def test_module_teardowns_restore_the_original_functions(self):
        """Each account test module's tearDownModule undoes the path and
        offline guards: the genuine os.open/os.stat/builtins.open/io.open
        (and every other wrapped function) are back afterwards. Guards are
        reinstalled for the rest of this module."""
        import builtins
        import importlib
        import os
        import sys
        modules = [sys.modules[__name__]] + [importlib.import_module(name) for name in (
            "test_account_profile", "test_account_exchange")]
        try:
            for module in modules:
                with self.subTest(module=module.__name__):
                    sf.install_path_guards()
                    self.assertTrue(sf.path_guards_installed())
                    self.assertIsNot(os.open, sf.PRISTINE["os.open"])
                    module.tearDownModule()
                    self.assertFalse(sf.path_guards_installed())
                    self.assertIs(os.open, sf.PRISTINE["os.open"])
                    self.assertIs(os.stat, sf.PRISTINE["os.stat"])
                    self.assertIs(builtins.open, sf.PRISTINE["builtins.open"])
                    self.assertIs(io.open, sf.PRISTINE["io.open"])
                    for key, original in sf.PRISTINE.items():
                        owner, name = key.split(".")
                        self.assertIs(getattr({"os": os, "builtins": builtins, "io": io}[owner],
                                              name), original, key)
                    self.assertIsNot(socket.getaddrinfo, sf._deny)
                    self.assertIsNot(socket.socket.connect, sf._deny)
        finally:
            sf.install_path_guards()
        self.assertTrue(sf.path_guards_installed())
        self.assertIs(socket.getaddrinfo, sf._deny)

    def test_refused_dirs_come_from_the_environment(self):
        """PETSAFE_REFUSED_DIRS is os.pathsep-separated, default empty, and
        the guard refuses every listed directory. The refused directory used
        here is a synthetic, never-created path; the guard raises before any
        real open."""
        import os
        import tempfile
        import uuid
        self.assertEqual(sf.refused_roots_from_env({}), ())
        self.assertEqual(sf.refused_roots_from_env({sf.REFUSED_DIRS_ENV: ""}), ())
        base = tempfile.gettempdir()
        first = os.path.join(base, "petsafe-FAKE-refused-" + uuid.uuid4().hex)
        second = os.path.join(base, "petsafe-FAKE-refused-" + uuid.uuid4().hex)
        self.assertEqual(sf.refused_roots_from_env(
            {sf.REFUSED_DIRS_ENV: first + os.pathsep + second + os.sep}), (first, second))
        saved = sf._STATE["roots"]
        sf._STATE["roots"] = (first, second)
        try:
            for root in (first, second):
                target = os.path.join(root, "FAKE.json")
                for attempt in (lambda: open(target, "rb"), lambda: io.open(target, "rb"),
                                lambda: os.open(target, os.O_RDONLY), lambda: os.stat(target),
                                lambda: os.listdir(root)):
                    with self.assertRaises(sf.RealPathAccess):
                        attempt()
            self.assertEqual(sf.take_guard_hits(), 10)
        finally:
            sf._STATE["roots"] = saved
        self.assertFalse(os.path.lexists(first) or os.path.lexists(second))

    def test_network_attempt_is_not_an_ordinary_exception(self):
        self.assertTrue(issubclass(sf.NetworkAttempt, BaseException))
        self.assertFalse(issubclass(sf.NetworkAttempt, Exception))

    def test_guarded_call_inside_exchange_is_detected_not_an_outcome(self):
        """A guarded call inside the exchange must escape the client's
        ordinary failure handling instead of becoming PROTOCOL_ERROR."""
        def dial(request, deadline):
            socket.getaddrinfo("a1-us.iotbing.com", 443)
        for stage in ("exchange", "profile"):
            with self.subTest(stage=stage):
                if stage == "exchange":
                    exchange, profile = FakeExchange(before=dial), SyntheticProfile()
                else:
                    class Dialing(SyntheticProfile):
                        def sign(self, canonical):
                            socket.create_connection(("a1-us.iotbing.com", 443))
                    exchange, profile = FakeExchange(), Dialing()
                client = ac.AccountClient(profile, exchange, contract(), permit=granted_permit())
                with self.assertRaises(sf.NetworkAttempt):
                    client.run(sf.credentials(), password_authorized=True,
                               deadline=time.monotonic() + 30)
                self.assertEqual(sf.take_guard_hits(), 1)

    def test_extended_guards(self):
        import os
        for name in ("posix_spawn", "posix_spawnp", "spawnv", "spawnl", "spawnvp", "execv",
                     "execve", "execvp", "execl", "system"):
            if hasattr(os, name):
                with self.subTest(name=name):
                    function = getattr(os, name)
                    # Never call an unguarded exec/spawn: check first.
                    self.assertIs(function, sf._deny)
                    with self.assertRaises(sf.NetworkAttempt):
                        function("/bin/true", ["/bin/true"])
        self.assertIs(socket.socket.sendmsg, sf._deny)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            with self.assertRaises(sf.NetworkAttempt):
                probe.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))
        self.assertGreaterEqual(sf.take_guard_hits(), 9)


class SuccessTests(GuardedTestCase):
    def test_complete_success_exact_wire_sequence(self):
        server = FakeServer()
        result, exchange, profile, _ = run(server)
        self.assertEqual(result, ok(1, 1, 1))
        self.assertEqual(exchange.operations(), ROWS)
        self.assertEqual(len(exchange.password_requests()), 1)
        for request in exchange.requests:
            self.assertEqual(request.url, AZ_URL)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers, (("Content-Type", "application/x-www-form-urlencoded"),))
        # sid only on session operations; never on token/login.
        fields = [dict(urllib.parse.parse_qsl(r.body.decode("ascii"))) for r in exchange.requests]
        self.assertEqual(["sid" in f for f in fields], [False, False, True, True])
        self.assertEqual(fields[2]["sid"], sf.SID)
        # Exact business payloads seen by the fake server.
        business = dict(server.business)
        self.assertEqual(server.business[0], ("TOKEN", {"countryCode": "1", "username": sf.EMAIL, "isUid": False}))
        login = business["PASSWORD_LOGIN"]
        self.assertEqual(list(login), ["countryCode", "email", "passwd", "options", "token", "ifencrypt"])
        self.assertEqual(login["ifencrypt"], 1)
        self.assertEqual(login["options"], '{"group":1,"mfaCode":""}')
        self.assertEqual(login["token"], sf.LOGIN_TOKEN)
        self.assertEqual(business["HOMES"], {})
        self.assertEqual(business["DEVICES"], {"gid": 100001})
        # No write/status/credential action was ever emitted.
        self.assertEqual({a for _, a, _ in exchange.operations()}, {a for _, a, _ in ROWS})

    def test_password_branch_md5_hex_rsa_pkcs1_hex(self):
        """Deterministic seam: the fixture holds the synthetic private key, so
        the ciphertext is checked by independent decryption, not equality.
        Proves the encoding branch only, not server acceptance."""
        server = FakeServer()
        run(server)
        passwd = dict(server.business)["PASSWORD_LOGIN"]["passwd"]
        self.assertRegex(passwd, r"\A[0-9a-f]{256}\Z")
        plain = sf.synthetic_rsa_key().decrypt(bytes.fromhex(passwd), padding.PKCS1v15())
        expected = hashlib.md5(sf.PASSWORD.encode("utf-8"), usedforsecurity=False).hexdigest()
        self.assertEqual(plain, expected.encode("ascii"))
        self.assertEqual(plain, plain.lower())
        for _, payload in server.business:
            self.assertNotIn(sf.PASSWORD, json.dumps(payload))
            self.assertNotIn(expected, json.dumps(payload))

    def test_product_counted_by_id_not_display_name(self):
        devices = lambda gid: {"result": [
            dev("d1", product=sf.OBSERVED_PRODUCT, name="Litter box"),
            dev("d2", product=sf.OTHER_PRODUCT, name=sf.OBSERVED_PRODUCT),
            dev("d3", product=sf.OTHER_PRODUCT, name="PetSafe ScoopFree " + sf.OBSERVED_PRODUCT),
        ]}
        result, *_ = run(FakeServer(devices=devices))
        self.assertEqual(result, ok(1, 3, 1))

    def test_product_id_alias(self):
        devices = lambda gid: {"result": [{"devId": "d1", "product_id": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1, keys=0, names=0))

    def test_no_devices(self):
        result, exchange, *_ = run(FakeServer(devices=lambda gid: {"result": []}))
        self.assertEqual(result, ok(1, 0, 0))
        self.assertEqual(len(exchange.requests), 4)

    def test_no_homes(self):
        result, exchange, *_ = run(FakeServer(homes=lambda: {"result": []}))
        self.assertEqual(result, ok(0, 0, 0))
        self.assertEqual([op for op, *_ in exchange.operations()], ["TOKEN", "PASSWORD_LOGIN", "HOMES"])

    def test_multiple_homes_duplicates_counted_once(self):
        per_home = {
            11: [dev("a"), dev("b", product=sf.OTHER_PRODUCT)],
            "home-22": [dev("a"), dev("c")],
            33: [dev("b", product=sf.OTHER_PRODUCT)],
        }
        server = FakeServer(homes=lambda: {"result": [{"gid": 11}, {"groupId": "home-22"}, {"homeId": 33}, {"gid": 11}]},
                            devices=lambda gid: {"result": per_home[gid]})
        result, exchange, *_ = run(server)
        self.assertEqual(result, ok(3, 3, 2))
        gids = [b["gid"] for op, b in server.business if op == "DEVICES"]
        self.assertEqual(gids, [11, "home-22", 33])  # one list request per unique home

    def test_conflicting_duplicate_physical_product_is_protocol_error(self):
        server = FakeServer(homes=homes_of(1, 2), devices=lambda gid: {"result": [
            dev("same", product=sf.OBSERVED_PRODUCT if gid == 1 else sf.OTHER_PRODUCT)]})
        self.assertEqual(run(server)[0], fail(O.PROTOCOL_ERROR))

    def test_limits_at_ceiling_pass(self):
        gids = list(range(1, 17))
        devices = lambda gid: {"result": [dev(f"d{gid}-{i}") for i in range(8)]}
        result, exchange, *_ = run(FakeServer(homes=homes_of(*gids), devices=devices))
        self.assertEqual(result, ok(16, 128, 128))
        self.assertEqual(len(exchange.requests), 3 + 16)

    def test_sticky_after_success(self):
        exchange = FakeExchange()
        permit = granted_permit()
        client = ac.AccountClient(SyntheticProfile(), exchange, contract(), permit=permit)
        first = client.run(sf.credentials(), password_authorized=True, deadline=time.monotonic() + 30)
        second = client.run(sf.credentials(), password_authorized=True, deadline=time.monotonic() + 30)
        self.assertEqual(first, second)
        self.assertEqual(len(exchange.requests), 4)


class LimitTests(GuardedTestCase):
    def test_too_many_homes(self):
        result, exchange, *_ = run(FakeServer(homes=homes_of(*range(1, 18))))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
        self.assertNotIn("DEVICES", [op for op, *_ in exchange.operations()])

    def test_too_many_unique_devices(self):
        devices = lambda gid: {"result": [dev(f"d{gid}-{i}") for i in range(65)]}
        result, exchange, *_ = run(FakeServer(homes=homes_of(1, 2), devices=devices))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))

    def test_single_list_over_ceiling(self):
        devices = lambda gid: {"result": [dev(f"d{i}") for i in range(129)]}
        self.assertEqual(run(FakeServer(devices=devices))[0], fail(O.LIMIT_EXCEEDED))

    def test_request_ceiling(self):
        ac._check_request_size(b"x" * ac.MAX_REQUEST_BYTES)
        with self.assertRaises(ac._Stop) as caught:
            ac._check_request_size(b"x" * (ac.MAX_REQUEST_BYTES + 1))
        self.assertIs(caught.exception.outcome, O.LIMIT_EXCEEDED)
        self.assertEqual((ac.MAX_REQUEST_BYTES, ac.MAX_RESPONSE_BYTES, ac.MAX_HOMES, ac.MAX_DEVICES),
                         (64 * 1024, 1024 * 1024, 16, 128))

    def test_oversized_outer_response_rejected_before_parsing(self):
        big = Raw(b'{"result":"' + b"A" * (ac.MAX_RESPONSE_BYTES) + b'"}')
        result, exchange, profile, _ = run(FakeServer(homes=big))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
        self.assertEqual(profile.decoded, 2)  # token+login only; homes never decoded

    def test_oversized_decoded_response(self):
        class Huge(SyntheticProfile):
            def decode_response(self, prepared, response):
                decoded = super().decode_response(prepared, response)
                if prepared.operation is boundary.Operation.HOMES:
                    return {"result": [{"gid": 1, "name": "x" * ac.MAX_RESPONSE_BYTES}]}
                return decoded
        self.assertEqual(run(profile=Huge())[0], fail(O.LIMIT_EXCEEDED))

    def test_decoded_depth_and_node_bounds(self):
        deep = {"result": [{"gid": 1}]}
        node = deep["result"][0]
        for _ in range(ac.MAX_DEPTH + 2):
            node["x"] = {}
            node = node["x"]
        wide = {"result": [{"gid": i + 1} for i in range(ac.MAX_NODES)]}
        for decoded in (deep, wide):
            with self.subTest(len(json.dumps(decoded))):
                class Fixed(SyntheticProfile):
                    def decode_response(self, prepared, response, _d=decoded):
                        out = super().decode_response(prepared, response)
                        return _d if prepared.operation is boundary.Operation.HOMES else out
                self.assertEqual(run(profile=Fixed())[0], fail(O.LIMIT_EXCEEDED))

    def test_outer_deep_nesting(self):
        raw = Raw(b'{"result":' + b"[" * 50 + b"]" * 50 + b"}")
        self.assertEqual(run(FakeServer(homes=raw))[0], fail(O.LIMIT_EXCEEDED))
        raw = Raw(b'{"result":' + b"[" * 200000 + b"]" * 200000 + b"}")
        self.assertIn(run(FakeServer(homes=raw))[0], (fail(O.LIMIT_EXCEEDED), fail(O.PROTOCOL_ERROR)))


class MalformedTests(GuardedTestCase):
    CASES = {
        "non_json": dict(homes=Raw(b"<html>not json</html>")),
        "non_utf8": dict(homes=Raw(b"\xff\xfe")),
        "empty": dict(homes=Raw(b"")),
        "outer_list": dict(homes=Raw(b"[1,2]")),
        "outer_duplicate_keys": dict(homes=Raw(b'{"result":"x","result":"y"}')),
        "outer_nan": dict(homes=Raw(b'{"result":"x","t":NaN}')),
        "outer_infinity": dict(homes=Raw(b'{"result":"x","t":-Infinity}')),
        "outer_unknown_key_bad_result": dict(homes=Envelope(result="x", redirect=sf.SENTINEL)),
        "outer_result_not_str": dict(homes=Envelope(result={"gid": 1})),
        "outer_no_result": dict(homes=Envelope(success=True)),
        "undecryptable_result": dict(homes=Envelope(result="QUFBQQ==")),
        "homes_unknown_top_key_not_list": dict(homes=lambda: {"result": "x", "hasMore": True}),
        "gid_bool": dict(homes=lambda: {"result": [{"gid": True}]}),
        "gid_zero": dict(homes=lambda: {"result": [{"gid": 0}]}),
        "gid_float": dict(homes=lambda: {"result": [{"gid": 1.5}]}),
        "gid_bad_str": dict(homes=lambda: {"result": [{"gid": "a/b"}]}),
        "gid_conflict": dict(homes=lambda: {"result": [{"gid": 1, "homeId": 2}]}),
        "devices_list_level_total": dict(devices=lambda gid: {"result": {"list": [], "totalCount": 500}}),
        # An int total on a wrapper above the list counts the whole subtree.
        "devices_wrapper_total": dict(devices=lambda gid: {"result": {"list": []},
                                                           "totalCount": 500}),
        "device_product_conflict": dict(devices=lambda gid: {"result": [{"devId": "a", "productId": "p1",
                                                                          "product_id": "p2"}]}),
        "product_not_str": dict(devices=lambda gid: {"result": [{"devId": "a", "productId": 7}]}),
        "product_bad_chars": dict(devices=lambda gid: {"result": [{"devId": "a", "productId": "x y"}]}),
        "product_conflict": dict(devices=lambda gid: {"result": [{"devId": "a", "productId": "p1", "product_id": "p2"}]}),
        "token_missing_key": dict(token=lambda: {"exponent": "65537", "token": sf.LOGIN_TOKEN}),
        "token_unknown_key_missing_exponent": dict(token=lambda: {"publicKey": sf.token_fixture()["publicKey"],
                                                                   "token": sf.LOGIN_TOKEN, "pbKey": "x"}),
        "token_bad_token": dict(token=lambda: dict(sf.token_fixture(), token="")),
        "login_missing_sid": dict(login=lambda: {"ecode": sf.ECODE, "uid": sf.UID}),
        "login_unknown_key_missing_uid": dict(login=lambda: {"sid": sf.SID, "ecode": sf.ECODE, "x": 1}),
        "login_url_not_str": dict(login=lambda: {"sid": sf.SID, "ecode": sf.ECODE, "uid": sf.UID, "mobileApiUrl": 5}),
        "error_unknown_code": dict(homes=Envelope(errorCode=sf.SENTINEL, errorMsg=sf.SENTINEL + " " + sf.EMAIL)),
        "decoded_nested_error": dict(homes=lambda: {"result": {"errorCode": sf.SENTINEL,
                                                                "errorMsg": sf.SENTINEL}}),
    }

    def test_malformed_responses_protocol_error(self):
        for name, slots in self.CASES.items():
            with self.subTest(name):
                result, exchange, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertLessEqual(len(exchange.password_requests()), 1)
                assert_no_secrets(self, result)
                if "token" in slots:
                    self.assertEqual(exchange.password_requests(), [])

    # Former CASES entries that the bounded walk (vendor client.py:197-215)
    # now reads as valid shapes: a single home object, objects without an id
    # key (not records) and scalar list items are not errors.
    WALK_ACCEPTED = {
        "homes_not_list": (dict(homes=lambda: {"result": {"gid": 1}}), (1, 1, 1)),
        "homes_missing_gid": (dict(homes=lambda: {"result": [{"name": "x"}]}), (0, 0, 0)),
        "homes_record_not_dict": (dict(homes=lambda: {"result": [1]}), (0, 0, 0)),
        # No list and no record at DEVICES is an empty home, not NO_LIST.
        "devices_not_list": (dict(devices=lambda gid: {"result": None}), (1, 0, 0)),
        # A product alias with a non-string id is not a device (a group).
        "device_id_int": (dict(devices=lambda gid: {"result": [{"devId": 5, "productId": "p1"}]}),
                          (1, 0, 0)),
        "device_missing_id": (dict(devices=lambda gid: {"result": [{"productId": sf.OBSERVED_PRODUCT}]}),
                              (1, 0, 0)),
    }

    def test_former_malformed_shapes_read_by_walk(self):
        for name, (slots, counts) in self.WALK_ACCEPTED.items():
            with self.subTest(name):
                self.assertEqual(run(FakeServer(**slots))[0], ok(*counts))

    def test_non_bytes_from_exchange(self):
        class StrExchange(FakeExchange):
            def exchange(self, request, *, deadline):
                super().exchange(request, deadline=deadline)
                return "{}"
        self.assertEqual(run(exchange=StrExchange())[0], fail(O.PROTOCOL_ERROR))

    def test_decoded_non_mapping_and_non_finite(self):
        for bad in ([1], "x", {"result": [{"gid": float("nan")}]}, {1: "x"}):
            with self.subTest(repr(bad)[:20]):
                class Bad(SyntheticProfile):
                    def decode_response(self, prepared, response, _b=bad):
                        out = super().decode_response(prepared, response)
                        return _b if prepared.operation is boundary.Operation.HOMES else out
                self.assertEqual(run(profile=Bad())[0], fail(O.PROTOCOL_ERROR))


class TerminalTests(GuardedTestCase):
    def _login_error(self, code, message=""):
        return dict(login=Envelope(success=False, errorCode=code, errorMsg=message))

    def test_no_downgrade_or_second_submission(self):
        cases = [
            ("encryption_failure", dict(), dict(fail={"encrypt"}), O.UNSUPPORTED_PROFILE, 0),
            ("missing_token_key", dict(token=lambda: {"exponent": "3", "token": sf.LOGIN_TOKEN}), {}, O.PROTOCOL_ERROR, 0),
            ("invalid_public_key", dict(token=lambda: dict(sf.token_fixture(), publicKey="0")), {}, O.UNSUPPORTED_PROFILE, 0),
            ("short_ciphertext", dict(), dict(password_transform=lambda p, k: "ab"), O.UNSUPPORTED_PROFILE, 0),
            ("uppercase_ciphertext", dict(), dict(password_transform=lambda p, k: sf.md5_hex_rsa_hex(p, k).upper()), O.UNSUPPORTED_PROFILE, 0),
            ("plaintext_returned", dict(), dict(password_transform=lambda p, k: p), O.UNSUPPORTED_PROFILE, 0),
            ("token_encrypt_fallback", dict(token=Envelope(errorCode="ENCRYPT_NOT_SUPPORTED", errorMsg="use ifencrypt=0")), {}, O.UNSUPPORTED_PROFILE, 0),
            ("captcha", self._login_error("USER_CAPTCHA_REQUIRED", sf.SENTINEL), {}, O.CHALLENGE, 1),
            ("mfa", self._login_error("NEED_VERIFYCODE"), {}, O.CHALLENGE, 1),
            ("lock", self._login_error("ACCOUNT_LOCKED", "too many attempts " + sf.EMAIL), {}, O.REJECTED, 1),
            ("wrong_password", self._login_error("USER_PASSWD_WRONG"), {}, O.REJECTED, 1),
            ("unknown_profile", self._login_error("ILLEGAL_CLIENT_ID"), {}, O.UNSUPPORTED_PROFILE, 1),
            ("sign_invalid", self._login_error("SIGN_INVALID"), {}, O.UNSUPPORTED_PROFILE, 1),
            ("login_encrypt_fallback", self._login_error("PASSWORD_DECRYPT_FAILED_IFENCRYPT"), {}, O.UNSUPPORTED_PROFILE, 1),
            ("api_not_exist", self._login_error("API_NOT_EXIST"), {}, O.PROTOCOL_ERROR, 1),
            ("ambiguous_exchange", dict(login=TimeoutError(sf.SENTINEL)), {}, O.TIMEOUT, 1),
            ("exchange_os_error", dict(login=OSError(sf.SENTINEL)), {}, O.PROTOCOL_ERROR, 1),
            ("cancelled", dict(login=ac.Cancelled()), {}, O.CANCELLED, 1),
            ("changed_route", dict(login=lambda: {"sid": sf.SID, "ecode": sf.ECODE, "uid": sf.UID,
                                                 "domain": {"mobileApiUrl": NOT_ALLOWLISTED}}), {}, O.UNSUPPORTED_ROUTE, 1),
            ("changed_route_flat", dict(login=lambda: {"sid": sf.SID, "ecode": sf.ECODE, "uid": sf.UID,
                                                      "mobileApiUrl": "http://a1-us.iotbing.com"}), {}, O.UNSUPPORTED_ROUTE, 1),
            ("prepare_failure", dict(), dict(fail={"prepare"}), O.UNSUPPORTED_PROFILE, 0),
            ("seal_failure", dict(), dict(fail={"seal"}), O.UNSUPPORTED_PROFILE, 0),
            ("sign_failure", dict(), dict(fail={"sign"}), O.UNSUPPORTED_PROFILE, 0),
            ("decode_failure", dict(), dict(fail={"decode"}), O.UNSUPPORTED_PROFILE, 0),
        ]
        for name, slots, profile_kwargs, outcome, passwords in cases:
            with self.subTest(name):
                server = FakeServer(**slots)
                exchange = FakeExchange(server)
                permit = granted_permit()
                profile = SyntheticProfile(**profile_kwargs)
                client = ac.AccountClient(profile, exchange, contract(), permit=permit)
                deadline = time.monotonic() + 30
                result = client.run(sf.credentials(), password_authorized=True, deadline=deadline)
                self.assertEqual(result, fail(outcome))
                self.assertEqual(len(exchange.password_requests()), passwords)
                # Later runs on the same client and on a new client sharing the
                # worker's permit emit zero further password requests.
                again = client.run(sf.credentials(), password_authorized=True, deadline=deadline)
                self.assertEqual(again, result)
                other = ac.AccountClient(SyntheticProfile(), exchange, contract(), permit=permit)
                self.assertEqual(other.run(sf.credentials(), password_authorized=True, deadline=deadline),
                                 fail(O.BUDGET_EXHAUSTED))
                self.assertEqual(len(exchange.password_requests()), passwords)
                # Never another region, never plaintext.
                for request in exchange.requests:
                    self.assertEqual(request.url, AZ_URL)
                for _, payload in server.business:
                    self.assertNotIn(sf.PASSWORD, json.dumps(payload))
                    if "passwd" in payload:
                        self.assertRegex(payload["passwd"], r"\A[0-9a-f]{256}\Z")
                        self.assertEqual(payload["ifencrypt"], 1)
                self.assertNotIn("HOMES", [op for op, *_ in exchange.operations()])
                assert_no_secrets(self, result, client, profile)

    def test_same_route_url_accepted(self):
        for url in ("https://a1-us.iotbing.com", "https://a1-us.iotbing.com/", AZ_URL):
            with self.subTest(url):
                login = lambda u=url: {"sid": sf.SID, "ecode": sf.ECODE, "uid": sf.UID, "domain": {"mobileApiUrl": u}}
                result, exchange, *_ = run(FakeServer(login=login))
                self.assertEqual(result, ok(1, 1, 1))
                self.assertIs(result.api_domain, ac.ApiDomain.SAME)
                self.assertEqual([r.url for r in exchange.requests], [AZ_URL] * 4)

    def test_unsupported_country_routes(self):
        for country in ("86", "", "+1", "AY", "01"):
            with self.subTest(country):
                result, exchange, *_ = run(creds=sf.credentials(country=country))
                self.assertEqual(result, fail(O.UNSUPPORTED_ROUTE))
                self.assertEqual(exchange.requests, [])

    def test_invalid_username_no_requests(self):
        for username in ("not-an-email", "a@b", "x" * 300 + "@example.invalid"):
            with self.subTest(username[:12]):
                result, exchange, *_ = run(creds=sf.credentials(username=username))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(exchange.requests, [])

    def test_expired_deadline_sends_nothing(self):
        result, exchange, *_ = run(timeout=-1)
        self.assertEqual(result, fail(O.TIMEOUT))
        self.assertEqual(exchange.requests, [])

    def test_late_response_after_deadline_is_terminal(self):
        def slow(request, deadline):
            if "email.password.login" in request.body.decode("ascii"):
                while time.monotonic() <= deadline:
                    time.sleep(0.01)
        exchange = FakeExchange(before=slow)
        result, exchange, *_ = run(exchange=exchange, timeout=0.5)
        self.assertEqual(result, fail(O.TIMEOUT))
        self.assertEqual([op for op, *_ in exchange.operations()], ["TOKEN", "PASSWORD_LOGIN"])

    def test_exchange_receives_absolute_deadline(self):
        seen = []
        exchange = FakeExchange(before=lambda r, d: seen.append(d))
        start = time.monotonic()
        run(exchange=exchange, timeout=30)
        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(start + 29 < seen[0] <= time.monotonic() + 30)


def nest(payload, levels, **siblings):
    """Wrap ``payload`` in ``levels`` nested ``{"result": ...}`` dicts; the
    outermost wrapper also carries ``siblings``."""
    for _ in range(levels):
        payload = {"result": payload}
    payload.update(siblings)
    return payload


def login_reply(**extra):
    reply = {"sid": sf.SID, "ecode": sf.ECODE, "uid": sf.UID}
    reply.update(extra)
    return reply


class ExtraOuterExchange(FakeExchange):
    """Adds unknown keys to every outer envelope the fake server returns."""

    def exchange(self, request, *, deadline):
        body = super().exchange(request, deadline=deadline)
        outer = json.loads(body)
        outer.update(status="ok", tid="synthetic-tid-0001", extraFlag=True, nested={"a": [1]})
        return json.dumps(outer).encode()


class PatchOuterExchange(FakeExchange):
    """Updates the genuinely sealed outer envelope of one operation with the
    given keys; every other response is untouched."""

    def __init__(self, op, **keys):
        super().__init__()
        self.op = op
        self.keys = keys

    def exchange(self, request, *, deadline):
        body = super().exchange(request, deadline=deadline)
        if self.operations()[-1][0] != self.op:
            return body
        outer = json.loads(body)
        outer.update(self.keys)
        return json.dumps(outer).encode()


class TolerantShapeTests(GuardedTestCase):
    """Real responses may carry more fields than the synthetic minimum and may
    wrap the payload in nested ``result`` dicts (vendor client.py:177-178)."""

    def _terminal(self, slots, outcome, passwords):
        result, exchange, *_ = run(FakeServer(**slots))
        self.assertEqual(result, fail(outcome))
        self.assertEqual(len(exchange.password_requests()), passwords)
        self.assertNotIn("HOMES", [op for op, *_ in exchange.operations()])
        assert_no_secrets(self, result)

    def test_outer_envelope_extra_keys_ignored(self):
        result, exchange, *_ = run(exchange=ExtraOuterExchange())
        self.assertEqual(result, ok(1, 1, 1))
        self.assertEqual(exchange.operations(), ROWS)

    def test_outer_envelope_known_key_types_still_checked(self):
        # Control: the genuinely sealed HOMES envelope with only an unknown
        # key added is accepted, so each case below fails only on its key.
        self.assertEqual(run(exchange=PatchOuterExchange("HOMES", status="ok"))[0], ok(1, 1, 1))
        for name, extra in (("t_str", dict(t="1", status="ok")),
                            ("success_str", dict(success="true")),
                            ("success_false", dict(success=False, status="ok")),
                            ("error_msg", dict(errorMsg="x", status="ok"))):
            with self.subTest(name):
                result, exchange, *_ = run(exchange=PatchOuterExchange("HOMES", **extra))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertNotIn("DEVICES", [op for op, *_ in exchange.operations()])
        with self.subTest("no_result"):
            self.assertEqual(run(FakeServer(homes=Envelope(status="ok", success=True)))[0],
                             fail(O.PROTOCOL_ERROR))
        result, exchange, *_ = run(FakeServer(login=Envelope(errorCode="USER_PASSWD_WRONG", status="x")))
        self.assertEqual(result, fail(O.REJECTED))

    def test_token_extra_keys_accepted(self):
        token = lambda: dict(sf.token_fixture(), pbKey="synthetic-pbkey", extra={"k": 1})
        result, exchange, *_ = run(FakeServer(token=token))
        self.assertEqual(result, ok(1, 1, 1))
        self.assertEqual(len(exchange.password_requests()), 1)

    def test_token_wrapped_accepted(self):
        token = lambda: nest(dict(sf.token_fixture(), pbKey="synthetic-pbkey"), 2,
                             success=True, status="ok", t=1700000000)
        self.assertEqual(run(FakeServer(token=token))[0], ok(1, 1, 1))

    def test_token_missing_required_key_still_rejected(self):
        for missing in ("exponent", "publicKey", "token"):
            with self.subTest(missing):
                token = sf.token_fixture()
                del token[missing]
                token["pbKey"] = "synthetic-pbkey"
                self._terminal(dict(token=lambda t=token: nest(dict(t), 1, success=True)),
                               O.PROTOCOL_ERROR, 0)

    def test_login_extra_keys_and_domain_extras_accepted(self):
        login = lambda: login_reply(
            nickname=sf.SENTINEL, email=sf.EMAIL, phoneCode="1", timezoneId="UTC",
            headPic="", partnerIdentity="p", extras={"a": 1}, userType=1,
            domain={"mobileApiUrl": "https://a1-us.iotbing.com", "regionCode": "AZ",
                    "mobileMqttUrl": "m1-us.iotbing.com", "aispeechHttpsUrl": "https://x.invalid",
                    "deviceHttpsUrl": "https://y.invalid", "mobileQuicUrl": "q.invalid"})
        result, exchange, *_ = run(FakeServer(login=login))
        self.assertEqual(result, ok(1, 1, 1))
        self.assertEqual(exchange.operations(), ROWS)

    def test_login_wrapped_accepted_and_session_used(self):
        login = lambda: nest(login_reply(extraField="x"), 1, success=True, status="ok")
        result, exchange, *_ = run(FakeServer(login=login))
        self.assertEqual(result, ok(1, 1, 1))
        fields = [dict(urllib.parse.parse_qsl(r.body.decode("ascii"))) for r in exchange.requests]
        self.assertEqual([f.get("sid") for f in fields], [None, None, sf.SID, sf.SID])

    # The limit is MAX_UNWRAP=4 descents into dict ``result`` values. For a
    # list payload the innermost ``result`` is the list itself, so
    # nest(list, 5) is 4 descents.
    def test_unwrap_at_four_levels_accepted(self):
        login = lambda: nest(login_reply(), 4, success=True)
        homes = lambda: nest([{"gid": 100001}], 5)
        self.assertEqual(run(FakeServer(login=login, homes=homes))[0], ok(1, 1, 1))

    def test_unwrap_deeper_than_four_rejected(self):
        self._terminal(dict(token=lambda: nest(sf.token_fixture(), 5)), O.PROTOCOL_ERROR, 0)
        self._terminal(dict(login=lambda: nest(login_reply(), 5)), O.PROTOCOL_ERROR, 1)
        result, exchange, *_ = run(FakeServer(homes=lambda: nest([{"gid": 1}], 6)))
        self.assertEqual(result, fail(O.PROTOCOL_ERROR))
        self.assertNotIn("DEVICES", [op for op, *_ in exchange.operations()])

    def test_nested_error_levels_map_like_outer(self):
        cases = [
            ("token_nested_code", dict(token=lambda: nest({"errorCode": "ENCRYPT_NOT_SUPPORTED"}, 1)),
             O.UNSUPPORTED_PROFILE, 0),
            ("token_success_false", dict(token=lambda: nest(sf.token_fixture(), 1, success=False)),
             O.PROTOCOL_ERROR, 0),
            ("token_inner_success_false", dict(token=lambda: nest(dict(sf.token_fixture(), success=False), 2)),
             O.PROTOCOL_ERROR, 0),
            ("token_unknown_code", dict(token=lambda: dict(sf.token_fixture(), errorCode="SOME_NEW_CODE")),
             O.PROTOCOL_ERROR, 0),
            ("login_top_code", dict(login=lambda: login_reply(errorCode="USER_PASSWD_WRONG")),
             O.REJECTED, 1),
            ("login_nested_code", dict(login=lambda: nest({"errorCode": "USER_CAPTCHA_REQUIRED",
                                                            "errorMsg": sf.SENTINEL}, 2, success=True)),
             O.CHALLENGE, 1),
            ("login_nested_success_false", dict(login=lambda: nest(dict(login_reply(), success=False), 1)),
             O.PROTOCOL_ERROR, 1),
            ("login_code_not_str", dict(login=lambda: nest(dict(login_reply(), errorCode=7), 1)),
             O.PROTOCOL_ERROR, 1),
        ]
        for name, slots, outcome, passwords in cases:
            with self.subTest(name):
                self._terminal(slots, outcome, passwords)

    def test_nonempty_error_msg_at_decoded_level_is_protocol_error(self):
        for name, login in (("login_top", lambda: login_reply(errorMsg="x")),
                            ("login_nested", lambda: nest(login_reply(errorMsg="x"), 1, success=True)),
                            ("login_wrapper", lambda: nest(login_reply(), 1, errorMsg="x"))):
            with self.subTest(name):
                result, exchange, profile, _ = run(FakeServer(login=login))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(len(exchange.password_requests()), 1)
                self.assertNotIn("HOMES", [op for op, *_ in exchange.operations()])
                self.assertIsNone(profile._sid)
                self.assertIsNone(profile._ecode)
                assert_no_secrets(self, result)
        for name, slots in (("homes", dict(homes=lambda: {"result": [], "errorMsg": "x"})),
                            ("homes_nested", dict(homes=lambda: nest({"result": [{"gid": 1}],
                                                                       "errorMsg": "x"}, 1))),
                            ("devices", dict(devices=lambda gid: {"result": [dev("d1")], "errorMsg": "x"}))):
            with self.subTest(name):
                self.assertEqual(run(FakeServer(**slots))[0], fail(O.PROTOCOL_ERROR))

    def test_empty_or_null_error_msg_ignored(self):
        for value in ("", None):
            with self.subTest(repr(value)):
                login = lambda: nest(login_reply(errorMsg=value), 1, errorMsg=value)
                homes = lambda: {"result": [{"gid": 1}], "errorMsg": value}
                devices = lambda gid: {"result": [dev("d1")], "errorMsg": value}
                result, exchange, *_ = run(FakeServer(login=login, homes=homes, devices=devices))
                self.assertEqual(result, ok(1, 1, 1))
                self.assertEqual(exchange.operations(), ROWS)

    def test_list_level_more_pages_signal_rejected(self):
        cases = (
            ("homes_has_more", dict(homes=lambda: {"result": [{"gid": 1}], "hasMore": True})),
            ("homes_nested_has_more", dict(homes=lambda: nest({"result": [{"gid": 1}], "hasMore": True}, 1))),
            ("homes_total_count", dict(homes=lambda: {"result": [{"gid": 1}], "totalCount": 2})),
            ("homes_total", dict(homes=lambda: {"result": [], "total": 1})),
            ("devices_has_more", dict(devices=lambda gid: {"result": [dev("d1")], "hasMore": True})),
            ("devices_total_count", dict(devices=lambda gid: nest({"result": [dev("d1")],
                                                                    "totalCount": 3}, 1))),
            # Signals on a wrapper above the list level count too.
            ("count_outside_list_level", dict(homes=lambda: nest({"result": [{"gid": 1}]}, 1,
                                                                 totalCount=9, hasMore=True))),
        )
        for name, slots in cases:
            with self.subTest(name):
                result, exchange, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                if "homes" in slots:
                    self.assertNotIn("DEVICES", [op for op, *_ in exchange.operations()])

    def test_list_level_consistent_or_non_int_counts_accepted(self):
        cases = (
            ("equal_and_false", lambda: {"result": [{"gid": 1}], "totalCount": 1, "total": 1,
                                         "hasMore": False}),
            ("less_than_len", lambda: {"result": [{"gid": 1}], "totalCount": 0}),
            ("bool_total_count", lambda: {"result": [{"gid": 1}], "totalCount": True, "total": True}),
            ("str_and_none", lambda: {"result": [{"gid": 1}], "totalCount": "9", "total": None,
                                      "hasMore": "true"}),
            ("consistent_outside_list_level", lambda: nest({"result": [{"gid": 1}]}, 1,
                                                           totalCount=1, hasMore=False)),
        )
        for name, homes in cases:
            with self.subTest(name):
                self.assertEqual(run(FakeServer(homes=homes))[0], ok(1, 1, 1))

    def test_wrapped_lists_with_extra_siblings(self):
        homes = lambda: nest({"result": [{"gid": 7}, {"gid": 8}], "hasMore": False}, 1,
                             success=True, status="ok")
        devices = lambda gid: {"result": [dev("d%d" % gid), dev("x%d" % gid, product="other")],
                               "totalCount": 2, "success": True}
        self.assertEqual(run(FakeServer(homes=homes, devices=devices))[0], ok(2, 4, 2))

    def test_list_not_a_list_after_unwrap_rejected(self):
        for name, slots in (("homes_str", dict(homes=lambda: nest("x", 2))),
                            ("devices_nested_error", dict(devices=lambda gid: nest({"errorCode": "X"}, 1)))):
            with self.subTest(name):
                self.assertEqual(run(FakeServer(**slots))[0], fail(O.PROTOCOL_ERROR))
        # No list at DEVICES is an empty home (NO_LIST is HOMES only).
        self.assertEqual(run(FakeServer(devices=lambda gid: nest(None, 2, status="ok")))[0],
                         ok(1, 0, 0))
        # An empty list under another key is an empty result (bounded walk).
        self.assertEqual(run(FakeServer(homes=lambda: nest({"list": []}, 1)))[0], ok(0, 0, 0))

    def test_route_mismatch_still_unsupported_with_extras(self):
        for login in (
            lambda: login_reply(nickname="n", domain={"mobileApiUrl": NOT_ALLOWLISTED,
                                                      "regionCode": "EU"}),
            lambda: nest(login_reply(domain={"mobileApiUrl": NOT_ALLOWLISTED, "x": 1}), 1,
                         status="ok"),
            lambda: login_reply(extra=1, mobileApiUrl="http://a1-us.iotbing.com"),
        ):
            with self.subTest():
                self._terminal(dict(login=login), O.UNSUPPORTED_ROUTE, 1)

    def test_login_domain_and_url_types_still_checked(self):
        for login in (lambda: login_reply(domain="https://a1-us.iotbing.com"),
                      lambda: login_reply(domain={"mobileApiUrl": 5, "regionCode": "AZ"}),
                      lambda: login_reply(sid=""),
                      lambda: nest({"sid": sf.SID, "uid": sf.UID, "session": "s", "eCode": "e"}, 1)):
            with self.subTest():
                self._terminal(dict(login=login), O.PROTOCOL_ERROR, 1)

    def test_unwrap_result_helper(self):
        inner = {"sid": "s"}
        self.assertIs(ac.unwrap_result(inner), inner)
        self.assertIs(ac.unwrap_result({"result": {"result": inner}, "x": 1}), inner)
        listed = {"result": [1], "x": 1}
        self.assertIs(ac.unwrap_result({"result": listed}), listed)
        for bad, outcome in (([], O.PROTOCOL_ERROR), (nest({}, 5), O.PROTOCOL_ERROR),
                             ({"errorCode": "SIGN_INVALID"}, O.UNSUPPORTED_PROFILE),
                             ({"result": {"sid": "s", "errorMsg": "x"}}, O.PROTOCOL_ERROR),
                             ({"result": {"success": False}}, O.PROTOCOL_ERROR)):
            with self.subTest(repr(bad)[:30]):
                with self.assertRaises(ac._Stop) as caught:
                    ac.unwrap_result(bad)
                self.assertIs(caught.exception.outcome, outcome)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)


class PermitTests(GuardedTestCase):
    def test_permit_cannot_be_minted(self):
        self.assertFalse(ac.PasswordPermit().claim())
        permit = granted_permit()
        results = []
        threads = [threading.Thread(target=lambda: results.append(permit.claim())) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [False] * 31 + [True])

    def test_no_marker_or_process_budget_api(self):
        """The harness supervisor nonce and process budget are gone: no public
        marker, no registration seam, no attempt budget; a permit is granted
        only by the private per-call ``_issue`` and only once."""
        for name in ("PASSWORD_AUTHORIZATION_MARKER", "_expect_authorization",
                     "AttemptBudget"):
            self.assertFalse(hasattr(ac, name), name)
        self.assertFalse(hasattr(ac.PasswordPermit, "from_marker"))
        permit = granted_permit()
        self.assertTrue(permit.claim())
        self.assertFalse(permit.claim())
        self.assertFalse(granted_permit() is permit)
        with self.assertRaises(TypeError):
            pickle.dumps(permit)

    def test_send_latch_requires_claim_and_admits_once(self):
        permit = granted_permit()
        token = object()
        self.assertFalse(permit.admit_send(token))
        self.assertTrue(permit.claim())
        self.assertFalse(permit.admit_send(None))
        self.assertTrue(permit.admit_send(token))
        self.assertFalse(permit.admit_send(token))
        self.assertFalse(permit.admit_send(object()))

    def test_unauthorized_or_missing_permit_sends_nothing(self):
        for kwargs in (dict(authorized=False), dict(permit=ac.PasswordPermit())):
            with self.subTest(sorted(kwargs)):
                result, exchange, profile, _ = run(**kwargs)
                self.assertEqual(result, fail(O.BUDGET_EXHAUSTED))
                self.assertEqual(exchange.requests, [])
                self.assertEqual(profile.encrypt_calls, 0)
        client = ac.AccountClient(SyntheticProfile(), FakeExchange(), contract())
        self.assertEqual(client.run(sf.credentials(), password_authorized=True,
                                    deadline=time.monotonic() + 5), fail(O.BUDGET_EXHAUSTED))

    def test_unauthorized_run_does_not_consume_permit(self):
        permit = granted_permit()
        run(permit=permit, authorized=False)
        self.assertEqual(run(permit=permit)[0], ok(1, 1, 1))

    def test_recorded_password_request_cannot_be_resent(self):
        """Record the prepared password WireRequest, then try to re-send it
        through boundary.send and through a second client's own send path:
        the fake exchange receives exactly one password-bearing request."""
        fixed_passwd = "ab" * 128
        server = FakeServer()
        exchange = FakeExchange(server)
        profile = SyntheticProfile(password_transform=lambda p, k: fixed_passwd)
        result = run(exchange=exchange, profile=profile)[0]
        self.assertEqual(result, ok(1, 1, 1))
        self.assertEqual(len(exchange.password_requests()), 1)
        index = [op for op, *_ in exchange.operations()].index("PASSWORD_LOGIN")
        recorded = exchange.requests[index]
        business = server.business[index][1]
        route = boundary.select_route(sf.COUNTRY, contract())
        # Path 1: the public boundary.send with the exact matching business.
        resend = []
        with self.assertRaises(boundary.BoundaryRejected) as caught:
            boundary.send(recorded, operation=boundary.Operation.PASSWORD_LOGIN,
                          expected_business=business, route=route, contract=contract(),
                          exchange=lambda request: resend.append(request) or b"")
        self.assertEqual(caught.exception.args, ())
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(resend, [])
        # Path 2: a second client (its own fresh permit) whose prepared password
        # call is replaced by the recorded request with an identical business.
        original_build = boundary.build_request
        sends = []
        original_send = boundary.send

        def replay_build(operation, business, **kwargs):
            if operation is boundary.Operation.PASSWORD_LOGIN:
                return recorded
            return original_build(operation, business, **kwargs)

        def counting_send(request, **kwargs):
            sends.append(kwargs["operation"])
            return original_send(request, **kwargs)
        boundary.build_request = replay_build
        boundary.send = counting_send
        try:
            second = run(exchange=exchange,
                         profile=SyntheticProfile(password_transform=lambda p, k: fixed_passwd))[0]
        finally:
            boundary.build_request = original_build
            boundary.send = original_send
        self.assertNotEqual(second.outcome, O.OK)
        self.assertEqual(len(exchange.password_requests()), 1)
        self.assertEqual(sum(1 for request in exchange.requests if request is recorded), 1)
        # The client's token request went out through boundary.send.
        self.assertEqual(sends, [boundary.Operation.TOKEN])

    def test_shared_permit_across_clients(self):
        permit = granted_permit()
        exchange = FakeExchange()
        first = run(exchange=exchange, permit=permit)[0]
        second = run(exchange=exchange, permit=permit)[0]
        self.assertEqual((first, second), (ok(1, 1, 1), fail(O.BUDGET_EXHAUSTED)))
        self.assertEqual(len(exchange.password_requests()), 1)
        self.assertEqual(len(exchange.requests), 4)


class PrivacyTests(GuardedTestCase):
    def test_result_and_objects_carry_no_secrets(self):
        sf.RecordingDict.lookups = []
        try:
            result, exchange, profile, client = run(profile=SyntheticProfile(record_access=True))
            lookup_list = list(sf.RecordingDict.lookups)
            lookups = set(lookup_list)
        finally:
            sf.RecordingDict.lookups = None
        self.assertEqual(result, ok(1, 1, 1))
        self.assertEqual((result.key_count, result.name_count), (1, 1))
        self.assertTrue(lookups)
        # localKey and name: read at most once per supported record (one
        # here); every other device field stays unread.
        self.assertEqual(lookup_list.count("localKey"), 1)
        self.assertEqual(lookup_list.count("name"), 1)
        self.assertFalse(lookups & {"local_key", "ip", "lanIp", "uuid", "secKey"})
        creds = sf.credentials()
        blob = pickle.dumps(result)
        for secret in sf.SECRET_STRINGS + (sf.SENTINEL + "-lk", "synthdev0001", sf.OBSERVED_PRODUCT):
            self.assertNotIn(secret.encode(), blob)
        assert_no_secrets(self, result, client, profile, creds, f"{creds}", f"{creds!r}", exchange.requests[0])
        self.assertEqual(pickle.loads(blob), result)
        self.assertEqual(set(vars(result)), {"outcome", "home_count", "device_count", "supported_count",
                                             "api_domain", "stage", "reason", "key_count",
                                             "name_count"})
        for text in (repr(result), str(result)):
            for secret in (sf.SENTINEL + "-lk", sf.SENTINEL + "-display-name"):
                self.assertNotIn(secret, text)
        for secret in (sf.SENTINEL + "-lk", sf.SENTINEL + "-display-name"):
            self.assertNotIn(secret.encode(), blob)
        self.assertIs(result.api_domain, ac.ApiDomain.SAME)

    def test_client_retains_no_credential_or_session(self):
        result, exchange, profile, client = run()
        state = repr(vars(client)) + repr({k: getattr(client, k) for k in getattr(client, "__slots__", ())})
        for secret in sf.SECRET_STRINGS:
            self.assertNotIn(secret, state)

    def test_hostile_error_text_not_emitted(self):
        server = FakeServer(login=Envelope(errorCode="USER_PASSWD_WRONG",
                                           errorMsg=sf.SENTINEL + sf.EMAIL + sf.PASSWORD))
        result, *_ = run(server)
        self.assertEqual(result, fail(O.REJECTED))
        assert_no_secrets(self, result, pickle.dumps(result))

    def test_exchange_exception_context_not_retained(self):
        server = FakeServer(homes=RuntimeError(sf.SENTINEL))
        result, *_ = run(server)
        self.assertEqual(result, fail(O.PROTOCOL_ERROR))

    def test_safe_result_validation(self):
        ac.SafeResult(O.OK, 16, 128, 128)
        for args in ((O.REJECTED, 1, 0, 0), (O.OK, -1, 0, 0), (O.OK, True, 0, 0),
                     (O.OK, 17, 0, 0), (O.OK, 1, 129, 0), (O.OK, 1, 1, 2),
                     ("OK", 0, 0, 0), (O.OK, 1.0, 0, 0),
                     (O.OK, 0, 0, 0, "SAME"), (O.OK, 0, 0, 0, None),
                     (O.OK, 0, 0, 0, "a1.tuyaeu.com"), (O.REJECTED, 0, 0, 0, "TUYAEU")):
            with self.subTest(args):
                with self.assertRaises(ValueError):
                    ac.SafeResult(*args)
        self.assertIs(ac.SafeResult(O.OK, 0, 0, 0).api_domain, ac.ApiDomain.SAME)
        self.assertIs(ac.ApiDomain, boundary.ApiDomain)
        # A failure may carry the accepted directed label.
        ac.SafeResult(O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.TUYAEU)
        self.assertEqual(ac.SafeResult(O.OK, 1, 2, 2).key_count, 0)
        self.assertEqual(ac.SafeResult(O.OK, 1, 2, 2).name_count, 0)
        ac.SafeResult(O.OK, 1, 2, 2, key_count=2, name_count=2)
        self.assertNotEqual(ac.SafeResult(O.OK, 1, 2, 2, key_count=1),
                            ac.SafeResult(O.OK, 1, 2, 2, key_count=2))
        for kwargs in ({"key_count": 3}, {"name_count": 3}, {"key_count": -1},
                       {"name_count": -1}, {"key_count": True}, {"name_count": True},
                       {"key_count": 1.0}, {"name_count": "1"}, {"key_count": None}):
            with self.subTest(kwargs):
                with self.assertRaises(ValueError):
                    ac.SafeResult(O.OK, 1, 2, 2, **kwargs)
        for kwargs in ({"key_count": 1}, {"name_count": 1}):
            with self.subTest(failure=kwargs):
                with self.assertRaises(ValueError):
                    ac.SafeResult(O.REJECTED, 0, 0, 0, **kwargs)
        self.assertEqual(ac._failure(O.REJECTED).key_count, 0)
        self.assertEqual(ac._failure(O.REJECTED).name_count, 0)
        self.assertEqual({o.name for o in O}, {
            "OK", "REJECTED", "CHALLENGE", "UNSUPPORTED_PROFILE", "UNSUPPORTED_ROUTE",
            "PROTOCOL_ERROR", "LIMIT_EXCEEDED", "TIMEOUT", "CANCELLED", "BUDGET_EXHAUSTED",
            "STARTUP_FAILED", "CLEANUP_FAILED"})

    def test_credentials_redacted(self):
        creds = sf.credentials()
        for text in (repr(creds), str(creds), f"{creds}", "%s" % (creds,)):
            for secret in (sf.EMAIL, sf.PASSWORD):
                self.assertNotIn(secret, text)

    def test_prepared_call_not_constructible_and_redacted(self):
        with self.assertRaises(Exception):
            ac.PreparedCall(None, None, None, None, None, None)
        seen = []

        class Peek(SyntheticProfile):
            def decode_response(self, prepared, response):
                seen.append(repr(prepared) + str(prepared))
                return super().decode_response(prepared, response)
        run(profile=Peek())
        self.assertEqual(len(seen), 4)
        for text in seen:
            for secret in sf.SECRET_STRINGS:
                self.assertNotIn(secret, text)

    def test_decode_bound_to_matching_call(self):
        """A provider handed a different call's context cannot decode."""
        stale = []

        class Swap(SyntheticProfile):
            def decode_response(self, prepared, response):
                if prepared.operation is boundary.Operation.TOKEN:
                    stale.append(prepared)
                if prepared.operation is boundary.Operation.HOMES:
                    return super().decode_response(stale[0], response)
                return super().decode_response(prepared, response)
        self.assertEqual(run(profile=Swap())[0], fail(O.UNSUPPORTED_PROFILE))

    def test_no_generic_action_or_url_api(self):
        public = {n for n in dir(ac.AccountClient) if not n.startswith("_")}
        self.assertEqual(public, {"run"})
        for name in ("publish", "call", "request", "action", "url", "get_local_keys"):
            self.assertFalse(any(name in n.lower() for n in dir(ac) if not n.startswith("_")
                                 and n not in ("PreparedCall", "MAX_REQUEST_BYTES")), name)


EU_URL = "https://a1.tuyaeu.com/api.json"
SG_URL = "https://a1-sg.iotbing.com/api.json"
SENTINEL_HOST = "a1-zqsentinelhost.iotbing.com"


class DirectedRouteTests(GuardedTestCase):
    """A server-directed API host is followed once, only through the
    boundary allowlist, for HOMES and DEVICES only."""

    def assert_directed(self, result, exchange, server, url, label, counts=(1, 1, 1)):
        self.assertEqual(result, ac.SafeResult(O.OK, *counts, label,
                                               key_count=counts[2],
                                               name_count=counts[2]))
        self.assertIs(result.api_domain, label)
        self.assertEqual(exchange.operations()[:2], ROWS[:2])
        self.assertEqual([op for op, *_ in exchange.operations()].count("TOKEN"), 1)
        self.assertEqual(len(exchange.password_requests()), 1)
        urls = [r.url for r in exchange.requests]
        self.assertEqual(urls[:2], [AZ_URL, AZ_URL])
        self.assertEqual(urls[2:], [url] * (len(urls) - 2))
        self.assertEqual([op for op, _ in server.business].count("PASSWORD_LOGIN"), 1)

    def test_bare_base_redirect_followed_for_homes_and_devices(self):
        for field in ("top", "domain"):
            with self.subTest(field=field):
                if field == "top":
                    login = lambda: login_reply(mobileApiUrl="https://a1.tuyaeu.com")
                else:
                    login = lambda: login_reply(domain={"mobileApiUrl": "https://a1.tuyaeu.com"})
                server = FakeServer(login=login, homes=homes_of(1, 2),
                                    devices=lambda gid: {"result": [dev("d%d" % gid)]})
                result, exchange, profile, client = run(server)
                self.assert_directed(result, exchange, server, EU_URL, ac.ApiDomain.TUYAEU,
                                     (2, 2, 2))
                self.assertEqual([op for op, *_ in exchange.operations()],
                                 ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES", "DEVICES"])
                fields = [dict(urllib.parse.parse_qsl(r.body.decode("ascii")))
                          for r in exchange.requests]
                self.assertEqual([f.get("sid") for f in fields],
                                 [None, None, sf.SID, sf.SID, sf.SID])
                # Business country code is unchanged on the directed route.
                self.assertEqual(server.business[0][1]["countryCode"], "1")
                assert_no_secrets(self, result, client)

    def test_full_url_and_agreeing_spellings_accepted(self):
        for top, inner in (((SG_URL, "https://a1-sg.iotbing.com")),
                           ("https://a1-sg.iotbing.com/", SG_URL),
                           (SG_URL, SG_URL)):
            with self.subTest(top=top, inner=inner):
                login = lambda t=top, i=inner: login_reply(mobileApiUrl=t,
                                                           domain={"mobileApiUrl": i})
                server = FakeServer(login=login)
                result, exchange, *_ = run(server)
                self.assert_directed(result, exchange, server, SG_URL, ac.ApiDomain.IOTBING)

    def test_directed_equal_to_route_is_same(self):
        login = lambda: login_reply(mobileApiUrl="https://a1-us.iotbing.com",
                                    domain={"mobileApiUrl": AZ_URL})
        result, exchange, *_ = run(FakeServer(login=login))
        self.assertEqual(result, ok(1, 1, 1))
        self.assertIs(result.api_domain, ac.ApiDomain.SAME)
        self.assertEqual(exchange.operations(), ROWS)
        self.assertEqual([r.url for r in exchange.requests], [AZ_URL] * 4)

    def test_conflicting_top_level_and_domain_unsupported(self):
        for top, inner in (("https://a1.tuyaeu.com", "https://a1.tuyaus.com"),
                           ("https://a1-us.iotbing.com", "https://a1.tuyaeu.com"),
                           ("https://a1.tuyaeu.com", "https://a1-us.iotbing.com"),
                           ("https://a1.tuyaeu.com", NOT_ALLOWLISTED)):
            with self.subTest(top=top, inner=inner):
                login = lambda t=top, i=inner: login_reply(mobileApiUrl=t,
                                                           domain={"mobileApiUrl": i})
                result, exchange, *_ = run(FakeServer(login=login))
                self.assertEqual(result, fail(O.UNSUPPORTED_ROUTE))
                self.assertIs(result.api_domain, ac.ApiDomain.SAME)
                self.assertEqual(exchange.operations(), ROWS[:2])
                self.assertEqual(len(exchange.password_requests()), 1)

    def test_non_allowlisted_redirect_unsupported(self):
        for url in (NOT_ALLOWLISTED, "https://evil-a1.iotbing.com",
                    "https://a1.iotbing.com.attacker.net", "https://a1.tuyaeu.com:8443",
                    "https://a1.tuyaeu.com/other.json", "https://127.0.0.1"):
            with self.subTest(url=url):
                result, exchange, *_ = run(FakeServer(login=lambda u=url: login_reply(mobileApiUrl=u)))
                self.assertEqual(result, fail(O.UNSUPPORTED_ROUTE))
                self.assertIs(result.api_domain, ac.ApiDomain.SAME)
                self.assertEqual(exchange.operations(), ROWS[:2])

    def test_empty_or_null_directed_values_are_absent(self):
        # Vendor password_client.py:388-395 skips falsy values; so do we.
        cases = (
            ("top_empty", dict(mobileApiUrl="")),
            ("top_null", dict(mobileApiUrl=None)),
            ("domain_null", dict(domain=None)),
            ("domain_empty", dict(domain={"mobileApiUrl": ""})),
            ("domain_value_null", dict(domain={"mobileApiUrl": None})),
            ("both_empty", dict(mobileApiUrl="", domain={"mobileApiUrl": None})),
        )
        for name, fields in cases:
            with self.subTest(name):
                result, exchange, *_ = run(FakeServer(login=lambda f=fields: login_reply(**f)))
                self.assertEqual(result, ok(1, 1, 1))
                self.assertIs(result.api_domain, ac.ApiDomain.SAME)
                self.assertEqual([r.url for r in exchange.requests], [AZ_URL] * 4)
        # An empty value beside a real one: the real one is followed.
        for fields in (dict(mobileApiUrl="", domain={"mobileApiUrl": "https://a1.tuyaeu.com"}),
                       dict(mobileApiUrl="https://a1.tuyaeu.com", domain={"mobileApiUrl": None}),
                       dict(mobileApiUrl="https://a1.tuyaeu.com", domain=None)):
            with self.subTest(fields=sorted(fields)):
                server = FakeServer(login=lambda f=fields: login_reply(**f))
                result, exchange, *_ = run(server)
                self.assert_directed(result, exchange, server, EU_URL, ac.ApiDomain.TUYAEU)
        # Non-string, non-null values stay protocol errors.
        for fields in (dict(mobileApiUrl=0), dict(mobileApiUrl=False), dict(domain="x"),
                       dict(domain={"mobileApiUrl": []})):
            with self.subTest(bad=sorted(fields)):
                result, exchange, *_ = run(FakeServer(login=lambda f=fields: login_reply(**f)))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(exchange.operations(), ROWS[:2])

    def test_empty_or_null_directed_values_in_lists_are_absent(self):
        login = lambda: login_reply(mobileApiUrl="https://a1.tuyaeu.com")
        homes = lambda: {"result": [{"gid": 1}], "mobileApiUrl": "", "domain": None}
        devices = lambda gid: {"result": [dev("d1")], "mobileApiUrl": None,
                               "domain": {"mobileApiUrl": ""}}
        server = FakeServer(login=login, homes=homes, devices=devices)
        result, exchange, *_ = run(server)
        self.assert_directed(result, exchange, server, EU_URL, ac.ApiDomain.TUYAEU)

    def test_second_directed_change_is_unsupported(self):
        login = lambda: login_reply(mobileApiUrl="https://a1.tuyaeu.com")
        cases = (
            ("homes_top", dict(homes=lambda: {"result": [{"gid": 1}],
                                              "mobileApiUrl": "https://a1.tuyaus.com"}),
             ["TOKEN", "PASSWORD_LOGIN", "HOMES"]),
            ("homes_domain", dict(homes=lambda: nest({"result": [{"gid": 1}]}, 1,
                                                     domain={"mobileApiUrl": "https://a1-us.iotbing.com"})),
             ["TOKEN", "PASSWORD_LOGIN", "HOMES"]),
            ("devices_top", dict(devices=lambda gid: {"result": [dev("d1")],
                                                      "mobileApiUrl": "https://a1-sg.iotbing.com"}),
             ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"]),
            ("devices_not_allowlisted", dict(devices=lambda gid: {"result": [dev("d1")],
                                                                  "mobileApiUrl": NOT_ALLOWLISTED}),
             ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"]),
            ("homes_not_str", dict(homes=lambda: {"result": [{"gid": 1}], "mobileApiUrl": 5}),
             ["TOKEN", "PASSWORD_LOGIN", "HOMES"]),
        )
        for name, slots, ops in cases:
            with self.subTest(name):
                result, exchange, *_ = run(FakeServer(login=login, **slots))
                self.assertEqual(result, ac.SafeResult(O.UNSUPPORTED_ROUTE, 0, 0, 0,
                                                       ac.ApiDomain.TUYAEU))
                self.assertEqual([op for op, *_ in exchange.operations()], ops)
                self.assertEqual(len(exchange.password_requests()), 1)

    def test_directed_change_without_login_redirect_is_unsupported(self):
        homes = lambda: {"result": [{"gid": 1}], "mobileApiUrl": "https://a1.tuyaeu.com"}
        result, exchange, *_ = run(FakeServer(homes=homes))
        self.assertEqual(result, fail(O.UNSUPPORTED_ROUTE))
        self.assertEqual([op for op, *_ in exchange.operations()], ["TOKEN", "PASSWORD_LOGIN", "HOMES"])

    def test_restated_current_destination_in_lists_accepted(self):
        login = lambda: login_reply(mobileApiUrl="https://a1.tuyaeu.com")
        homes = lambda: {"result": [{"gid": 1}], "mobileApiUrl": EU_URL}
        devices = lambda gid: {"result": [dev("d1")], "domain": {"mobileApiUrl": "https://a1.tuyaeu.com/"}}
        server = FakeServer(login=login, homes=homes, devices=devices)
        result, exchange, *_ = run(server)
        self.assert_directed(result, exchange, server, EU_URL, ac.ApiDomain.TUYAEU)
        # Without a login redirect, lists restating the contract route are fine.
        homes = lambda: {"result": [{"gid": 1}], "mobileApiUrl": "https://a1-us.iotbing.com"}
        self.assertEqual(run(FakeServer(homes=homes))[0], ok(1, 1, 1))

    def test_failure_after_redirect_carries_label(self):
        login = lambda: login_reply(mobileApiUrl="https://a1.tuyaus.com")
        result, exchange, *_ = run(FakeServer(login=login, homes=lambda: {"result": "x"}))
        self.assertEqual(result, ac.SafeResult(O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.TUYAUS))
        self.assertEqual([r.url for r in exchange.requests][2:], ["https://a1.tuyaus.com/api.json"])

    def test_label_never_carries_the_directed_host(self):
        login = lambda: login_reply(domain={"mobileApiUrl": "https://" + SENTINEL_HOST})
        server = FakeServer(login=login)
        result, exchange, profile, client = run(server)
        self.assert_directed(result, exchange, server, "https://%s/api.json" % SENTINEL_HOST,
                             ac.ApiDomain.IOTBING)
        blob = pickle.dumps(result)
        for text in (repr(result), str(result), result.api_domain.value, repr(client),
                     repr(vars(client))):
            self.assertNotIn("zqsentinelhost", text)
            self.assertNotIn("iotbing.com", text)
        self.assertNotIn(b"zqsentinelhost", blob)
        self.assertNotIn(b"iotbing.com", blob)

    def test_redirect_requires_valid_session_first(self):
        login = lambda: {"sid": sf.SID, "ecode": sf.ECODE, "mobileApiUrl": "https://a1.tuyaeu.com"}
        result, exchange, *_ = run(FakeServer(login=login))
        self.assertEqual(result, fail(O.PROTOCOL_ERROR))
        self.assertEqual(exchange.operations(), ROWS[:2])


def labels(result):
    """The closed diagnostic labels of a result, by member name."""
    return (result.stage.name, result.reason.name)


def fixed_homes(decoded):
    """A profile whose HOMES decode returns ``decoded`` verbatim (bypasses the
    fake server's JSON round trip so walk-bound shapes stay exact)."""
    class Fixed(SyntheticProfile):
        def decode_response(self, prepared, response, _d=decoded):
            out = super().decode_response(prepared, response)
            return _d if prepared.operation is boundary.Operation.HOMES else out
    return Fixed()


def chain(leaf, levels, key="x"):
    """``levels`` nested ``{key: ...}`` dicts around ``leaf``."""
    for _ in range(levels):
        leaf = {key: leaf}
    return leaf


class NestedListTests(GuardedTestCase):
    """Homes and devices are found by a bounded walk of every nested object
    (vendor client.py:197-215 ``_walk``), not only at one fixed level."""

    HOME = {"gid": 100001, "name": sf.SENTINEL}

    def home_shapes(self):
        h = self.HOME
        return (
            ("obj_1", {"result": {"homes": [h]}}),
            ("obj_2", {"result": {"data": {"list": [h]}}}),
            ("obj_3", {"result": {"a": {"b": {"spaces": [h]}}}}),
            ("list_1", {"result": [[h]]}),
            ("list_2", {"result": [{"group": [h]}]}),
            ("list_3", {"result": [{"g": [{"h": [h]}]}]}),
            ("direct_object", {"result": {"home": dict(h)}}),
            ("result_object", {"result": dict(h)}),
            ("renamed_key", {"result": {"spaceList": [h], "hasMore": False}}),
        )

    def test_homes_nested_at_depth_found(self):
        for name, body in self.home_shapes():
            with self.subTest(name):
                result, exchange, *_ = run(FakeServer(homes=lambda b=body: b))
                self.assertEqual(result, ok(1, 1, 1))
                self.assertEqual(labels(result), ("NONE", "NONE"))
                self.assertEqual(exchange.operations(), ROWS)

    def test_devices_nested_at_depth_found(self):
        d = dev("d1")
        shapes = (
            ("obj_1", {"result": {"devices": [d]}}),
            ("obj_2", {"result": {"data": {"deviceList": [d]}}}),
            ("obj_3", {"result": {"a": {"b": {"c": [d]}}}}),
            ("list_1", {"result": [[d]]}),
            ("list_2", {"result": [{"devs": [d]}]}),
            ("list_3", {"result": [{"g": [{"h": [d]}]}]}),
            ("renamed_key", {"result": {"deviceRespList": [d, dev("d2", product=sf.OTHER_PRODUCT)],
                                         "totalCount": 2}}),
        )
        for name, body in shapes:
            with self.subTest(name):
                result, *_ = run(FakeServer(devices=lambda gid, b=body: b))
                expected = ok(1, 2, 1) if name == "renamed_key" else ok(1, 1, 1)
                self.assertEqual(result, expected)
                self.assertEqual(labels(result), ("NONE", "NONE"))

    def test_records_are_leaves_and_duplicates_kept(self):
        # A collected record is not descended into: a nested object with an
        # id-like key inside a device record is not a second device.
        d = dev("d1", moduleMap={"wifi": {"id": 7}}, sub=[{"devId": "zz"}])
        result, *_ = run(FakeServer(devices=lambda gid: {"result": [d]}))
        self.assertEqual(result, ok(1, 1, 1))
        # Duplicate home ids are deduplicated (one DEVICES request each);
        # duplicate device ids across nested lists count once.
        homes = lambda: {"result": {"a": [{"gid": 5}], "b": [{"gid": 5}, {"homeId": 6}]}}
        devices = lambda gid: {"result": {"x": [dev("d1")], "y": [dev("d1")]}}
        server = FakeServer(homes=homes, devices=devices)
        result, *_ = run(server)
        self.assertEqual(result, ok(2, 1, 1))
        self.assertEqual([b["gid"] for op, b in server.business if op == "DEVICES"], [5, 6])

    def test_empty_results_count_zero(self):
        for name, slots, expected in (
            ("homes_empty_list", dict(homes=lambda: {"result": []}), ok(0, 0, 0)),
            ("homes_nested_empty", dict(homes=lambda: {"result": {"list": []}}), ok(0, 0, 0)),
            ("homes_no_gid_records", dict(homes=lambda: {"result": [{"name": "x"}]}), ok(0, 0, 0)),
            ("homes_scalar_items", dict(homes=lambda: {"result": [1, "x", None]}), ok(0, 0, 0)),
            ("devices_empty", dict(devices=lambda gid: {"result": []}), ok(1, 0, 0)),
            ("devices_nested_empty", dict(devices=lambda gid: {"result": {"a": {"list": []}}}),
             ok(1, 0, 0)),
            ("devices_no_id_records", dict(devices=lambda gid: {"result": [{"productId": "p1"}]}),
             ok(1, 0, 0)),
        ):
            with self.subTest(name):
                result, *_ = run(FakeServer(**slots))
                self.assertEqual(result, expected)
                self.assertIs(result.outcome, O.OK)
                self.assertEqual(labels(result), ("NONE", "NONE"))

    def test_no_list_anywhere_is_protocol_error(self):
        for name, slots, stage in (
            ("homes_str", dict(homes=lambda: {"result": "x"}), "HOMES"),
            # (hasMore true here would now be PAGINATION: checked on every dict.)
            ("homes_none", dict(homes=lambda: {"result": None, "hasMore": False}), "HOMES"),
            ("homes_object_no_list", dict(homes=lambda: {"result": {"name": "x"}}), "HOMES"),
            ("homes_empty_object", dict(homes=lambda: {}), "HOMES"),
            ("homes_result_empty_object", dict(homes=lambda: {"result": {}}), "HOMES"),
        ):
            with self.subTest(name):
                result, exchange, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), (stage, "NO_LIST"))
                self.assertNotIn("DEVICES", [op for op, *_ in exchange.operations()])
        # NO_LIST is a HOMES-only rule: at DEVICES a body with no list and no
        # record anywhere is an empty home (0 devices).
        for name, body in (("devices_empty_object", {}), ("devices_none", {"result": None}),
                           ("devices_result_empty_object", {"result": {}}),
                           ("devices_str", {"result": "x"})):
            with self.subTest(name):
                result, *_ = run(FakeServer(devices=lambda gid, b=body: b))
                self.assertEqual(result, ok(1, 0, 0))
                self.assertEqual(labels(result), ("NONE", "NONE"))

    def test_empty_home_plus_real_home_counts(self):
        for empty in ({}, {"result": None}, {"result": {}}):
            with self.subTest(repr(empty)):
                devices = lambda gid, e=empty: (e if gid == 1 else
                                                {"result": [dev("d2"),
                                                            dev("d3", product=sf.OTHER_PRODUCT)]})
                server = FakeServer(homes=homes_of(1, 2), devices=devices)
                result, *_ = run(server)
                self.assertEqual(result, ok(2, 2, 1))
                self.assertEqual(labels(result), ("NONE", "NONE"))
                self.assertEqual([b["gid"] for op, b in server.business if op == "DEVICES"],
                                 [1, 2])

    def test_devices_server_flags_still_checked_without_list(self):
        for name, body, outcome, reason in (
            ("success_false", {"success": False}, O.PROTOCOL_ERROR, "SERVER_ERROR_FLAG"),
            ("error_msg", {"result": None, "errorMsg": "x"}, O.PROTOCOL_ERROR,
             "SERVER_ERROR_FLAG"),
            ("nested_code", {"result": {"errorCode": "SIGN_INVALID"}}, O.UNSUPPORTED_PROFILE,
             "SERVER_ERROR_CODE"),
        ):
            with self.subTest(name):
                result, *_ = run(FakeServer(devices=lambda gid, b=body: b))
                self.assertEqual(result, fail(outcome))
                self.assertEqual(labels(result), ("DEVICES", reason))

    def test_walk_depth_cap(self):
        # Root is level 1; the record dict at level 10 is accepted, at 11 not.
        at_ten = {"result": chain({"gid": 100001}, 8)}
        at_eleven = {"result": chain({"gid": 100001}, 9)}
        self.assertEqual(ac._home_ids(at_ten), [100001])
        result, *_ = run(profile=fixed_homes(at_ten))
        self.assertEqual(result, ok(1, 1, 1))
        for decoded in (at_eleven, {"result": chain([], 9)}):
            with self.subTest(len(json.dumps(decoded))):
                result, exchange, *_ = run(profile=fixed_homes(decoded))
                self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
                self.assertEqual(labels(result), ("HOMES", "WALK_BOUNDS"))
                self.assertNotIn("DEVICES", [op for op, *_ in exchange.operations()])

    def test_walk_node_cap(self):
        # root + list + N dicts: 2048 visited containers pass, 2049 do not.
        under = {"result": [{} for _ in range(2046)]}
        over = {"result": [{} for _ in range(2047)]}
        self.assertEqual(ac._home_ids(under), [])
        with self.assertRaises(ac._Stop) as caught:
            ac._home_ids(over)
        self.assertIs(caught.exception.outcome, O.LIMIT_EXCEEDED)
        self.assertIs(caught.exception.reason, ac.Reason.WALK_BOUNDS)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        result, *_ = run(profile=fixed_homes(over))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
        self.assertEqual(labels(result), ("HOMES", "WALK_BOUNDS"))
        devices = {"result": [[] for _ in range(2047)]}
        with self.assertRaises(ac._Stop) as caught:
            ac._device_products(devices)
        self.assertIs(caught.exception.reason, ac.Reason.WALK_BOUNDS)

    def test_limits_still_apply_to_walked_records(self):
        homes = lambda: {"result": {"a": [{"gid": i} for i in range(1, 10)],
                                    "b": [{"gid": i} for i in range(10, 18)]}}
        result, *_ = run(FakeServer(homes=homes))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
        self.assertEqual(labels(result), ("HOMES", "LIMIT"))
        devices = lambda gid: {"result": {"a": [dev("a%d" % i) for i in range(65)],
                                          "b": [dev("b%d" % i) for i in range(64)]}}
        result, *_ = run(FakeServer(devices=devices))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
        self.assertEqual(labels(result), ("DEVICES", "LIMIT"))

    def test_error_levels_checked_on_every_walked_dict(self):
        for name, slots, outcome, reason in (
            ("homes_inner_code", dict(homes=lambda: {"result": [{"errorCode": "SOME_NEW_CODE",
                                                                "list": []}]}),
             O.PROTOCOL_ERROR, "SERVER_ERROR_CODE"),
            ("homes_inner_flag", dict(homes=lambda: {"result": {"a": [{"success": False}]}}),
             O.PROTOCOL_ERROR, "SERVER_ERROR_FLAG"),
            ("devices_inner_msg", dict(devices=lambda gid: {"result": {"a": {"errorMsg": "x",
                                                                              "l": [dev("d1")]}}}),
             O.PROTOCOL_ERROR, "SERVER_ERROR_FLAG"),
            ("devices_inner_mapped", dict(devices=lambda gid: {"result": [{"errorCode": "SIGN_INVALID"}]}),
             O.UNSUPPORTED_PROFILE, "SERVER_ERROR_CODE"),
            ("homes_inner_code_type", dict(homes=lambda: {"result": [{"errorCode": 5, "gid": 1}]}),
             O.PROTOCOL_ERROR, "FIELD_TYPE"),
        ):
            with self.subTest(name):
                result, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(outcome))
                stage = "HOMES" if "homes" in slots else "DEVICES"
                self.assertEqual(labels(result), (stage, reason))

    def test_falsy_error_code_ignored_on_nested_dict(self):
        # Vendor client.py:182 tests truthiness: every falsy errorCode is absent.
        for value in (None, "", 0, False, [], {}, 0.0):
            with self.subTest(repr(value)):
                ac._check_level_error({"errorCode": value})
                homes = lambda v=value: {"result": {"meta": {"errorCode": v},
                                                     "list": [{"gid": 1, "errorCode": v}]}}
                devices = lambda gid, v=value: {"result": [{"info": {"errorCode": v}},
                                                            dev("d1")]}
                result, *_ = run(FakeServer(homes=homes, devices=devices))
                self.assertEqual(result, ok(1, 1, 1))
        # A non-empty string keeps its mapping; a truthy non-string is FIELD_TYPE.
        for value, outcome, reason in (("SIGN_INVALID", O.UNSUPPORTED_PROFILE, "SERVER_ERROR_CODE"),
                                       ("SOME_NEW_CODE", O.PROTOCOL_ERROR, "SERVER_ERROR_CODE"),
                                       (5, O.PROTOCOL_ERROR, "FIELD_TYPE"),
                                       (True, O.PROTOCOL_ERROR, "FIELD_TYPE"),
                                       ([1], O.PROTOCOL_ERROR, "FIELD_TYPE"),
                                       ({"a": 1}, O.PROTOCOL_ERROR, "FIELD_TYPE")):
            with self.subTest(repr(value)):
                homes = lambda v=value: {"result": {"meta": {"errorCode": v},
                                                     "list": [{"gid": 1}]}}
                result, *_ = run(FakeServer(homes=homes))
                self.assertEqual(result, fail(outcome))
                self.assertEqual(labels(result), ("HOMES", reason))

    def test_pagination_at_the_containing_level(self):
        for name, slots, stage in (
            ("homes_nested_has_more", dict(homes=lambda: {"result": {"d": {"list": [{"gid": 1}],
                                                                          "hasMore": True}}}),
             "HOMES"),
            ("homes_nested_total", dict(homes=lambda: {"result": {"list": [{"gid": 1}],
                                                                   "totalCount": 2}}), "HOMES"),
            ("homes_empty_total", dict(homes=lambda: {"result": {"list": [], "total": 1}}), "HOMES"),
            ("devices_in_list_total", dict(devices=lambda gid: {"result": [{"devs": [dev("d1")],
                                                                             "total": 5}]}),
             "DEVICES"),
        ):
            with self.subTest(name):
                result, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), (stage, "PAGINATION"))
        # Consistent, bool or non-int counts are not more-pages signals; a
        # consistent count on a wrapper counts its whole subtree.
        for homes in (lambda: {"result": {"list": [{"gid": 1}], "total": 1, "hasMore": False}},
                      lambda: {"result": {"list": [{"gid": 1}], "total": True, "totalCount": "9"}},
                      lambda: {"result": {"inner": {"list": [{"gid": 1}]}, "totalCount": 1,
                                          "hasMore": False}}):
            with self.subTest("accepted"):
                self.assertEqual(run(FakeServer(homes=homes))[0], ok(1, 1, 1))

    def test_has_more_on_any_non_record_dict(self):
        for name, slots, stage in (
            ("wrapper", dict(homes=lambda: {"result": {"inner": {"list": [{"gid": 1}]},
                                                       "hasMore": True}}), "HOMES"),
            ("root_no_list", dict(homes=lambda: {"result": {"home": {"gid": 1}}, "hasMore": True}),
             "HOMES"),
            ("sibling_object", dict(homes=lambda: {"result": {"list": [{"gid": 1}],
                                                              "page": {"hasMore": True}}}),
             "HOMES"),
            ("devices_wrapper", dict(devices=lambda gid: {"result": {"d": {"l": [dev("d1")]},
                                                                     "hasMore": True}}),
             "DEVICES"),
            ("devices_no_list", dict(devices=lambda gid: {"result": None, "hasMore": True}),
             "DEVICES"),
        ):
            with self.subTest(name):
                result, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), (stage, "PAGINATION"))
        # A record's own hasMore is never read (records are leaves).
        devices = lambda gid: {"result": [dev("d1", hasMore=True)]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1))

    def test_total_counts_whole_subtree(self):
        d1, d2 = dev("d1"), dev("d2", product=sf.OTHER_PRODUCT)
        # Rooms: the total on the wrapper counts devices nested in every room.
        rooms = lambda gid: {"result": {"total": 2, "rooms": [{"devices": [d1]},
                                                              {"devices": [d2]}]}}
        result, *_ = run(FakeServer(devices=rooms))
        self.assertEqual(result, ok(1, 2, 1))
        self.assertEqual(labels(result), ("NONE", "NONE"))
        for name, slots, stage in (
            ("rooms_short", dict(devices=lambda gid: {"result": {"total": 3, "rooms": [
                {"devices": [d1]}, {"devices": [d2]}]}}), "DEVICES"),
            ("wrapper_total_count", dict(homes=lambda: {"result": {"inner": {"list": [{"gid": 1}]},
                                                                   "totalCount": 9}}), "HOMES"),
            ("wrapper_empty_list", dict(devices=lambda gid: {"result": {"list": []},
                                                             "totalCount": 500}), "DEVICES"),
            ("root_total_no_list", dict(devices=lambda gid: {"result": None, "total": 1}),
             "DEVICES"),
        ):
            with self.subTest(name):
                result, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), (stage, "PAGINATION"))
        # Every ancestor counts the record once: nested totals all consistent.
        homes = lambda: {"total": 2, "result": {"total": 2, "a": {"total": 1, "l": [{"gid": 1}]},
                                                "b": [{"total": 1, "l": [{"gid": 2}]}]}}
        # (The default DEVICES reply is one device id, the same for each home.)
        self.assertEqual(run(FakeServer(homes=homes))[0], ok(2, 1, 1))

    def test_walk_does_not_read_other_device_fields(self):
        sf.RecordingDict.lookups = []
        try:
            devices = lambda gid: {"result": {"x": [dev("d1", ip="10.0.0.1", uuid="u",
                                                         lanIp="x", devKey="other")]}}
            result, *_ = run(FakeServer(devices=devices), profile=SyntheticProfile(record_access=True))
            lookup_list = list(sf.RecordingDict.lookups)
            lookups = set(lookup_list)
        finally:
            sf.RecordingDict.lookups = None
        self.assertEqual(result, ok(1, 1, 1))
        # localKey and name: at most once for the one supported record;
        # every other device field stays unread.
        self.assertEqual(lookup_list.count("localKey"), 1)
        self.assertEqual(lookup_list.count("name"), 1)
        self.assertFalse(lookups & {"local_key", "ip", "lanIp", "uuid", "secKey", "devKey"})


class DiagnosticLabelTests(GuardedTestCase):
    """Each failure names a closed stage and reason; never server text."""

    def test_enum_members_exact(self):
        self.assertEqual([m.name for m in ac.Stage],
                         ["NONE", "SETUP", "TOKEN", "LOGIN", "HOMES", "DEVICES", "RESULT"])
        self.assertEqual([m.name for m in ac.Reason], [
            "NONE", "SERVER_ERROR_CODE", "SERVER_ERROR_FLAG", "OUTER_SHAPE", "SEAL", "DECODE",
            "UNWRAP_DEPTH", "MISSING_FIELD", "FIELD_TYPE", "NO_LIST", "RECORD_SHAPE",
            "RECORD_ID", "RECORD_PRODUCT", "PAGINATION", "DIRECTED_URL", "DIRECTED_CONFLICT",
            "LIMIT", "BUDGET", "TIMEOUT", "CANCELLED", "PROFILE", "WALK_BOUNDS",
            "TRANSPORT", "TRANSPORT_BIND", "TRANSPORT_CONNECT", "TRANSPORT_STATUS",
            "TRANSPORT_ENCODING", "TRANSPORT_LENGTH", "TRANSPORT_IO"])
        for enum in (ac.Stage, ac.Reason):
            for member in enum:
                self.assertEqual(member.value, member.name)

    def test_success_labels_none(self):
        result, *_ = run()
        self.assertEqual(result, ok(1, 1, 1))
        self.assertIs(result.stage, ac.Stage.NONE)
        self.assertIs(result.reason, ac.Reason.NONE)

    def test_stage_and_reason_per_failure(self):
        cases = [
            ("homes_unmapped_code", dict(homes=lambda: {"result": [], "errorCode": "SOME_NEW_CODE"}),
             O.PROTOCOL_ERROR, "HOMES", "SERVER_ERROR_CODE"),
            ("homes_outer_unmapped_code", dict(homes=Envelope(errorCode="SOME_NEW_CODE")),
             O.PROTOCOL_ERROR, "HOMES", "SERVER_ERROR_CODE"),
            ("homes_mapped_code", dict(homes=lambda: {"errorCode": "USER_PASSWD_WRONG"}),
             O.REJECTED, "HOMES", "SERVER_ERROR_CODE"),
            ("devices_mapped_code", dict(devices=lambda gid: Envelope(errorCode="SIGN_INVALID")),
             O.UNSUPPORTED_PROFILE, "DEVICES", "SERVER_ERROR_CODE"),
            ("login_mapped_code", dict(login=Envelope(success=False, errorCode="USER_PASSWD_WRONG")),
             O.REJECTED, "LOGIN", "SERVER_ERROR_CODE"),
            ("token_mapped_code", dict(token=lambda: {"errorCode": "ENCRYPT_NOT_SUPPORTED"}),
             O.UNSUPPORTED_PROFILE, "TOKEN", "SERVER_ERROR_CODE"),
            ("homes_success_false", dict(homes=lambda: {"result": [], "success": False}),
             O.PROTOCOL_ERROR, "HOMES", "SERVER_ERROR_FLAG"),
            ("homes_outer_success_false", dict(homes=Envelope(success=False)),
             O.PROTOCOL_ERROR, "HOMES", "SERVER_ERROR_FLAG"),
            ("login_error_msg", dict(login=lambda: login_reply(errorMsg="x")),
             O.PROTOCOL_ERROR, "LOGIN", "SERVER_ERROR_FLAG"),
            ("device_bad_id", dict(devices=lambda gid: {"result": [{"devId": "a/b", "productId": "p1"}]}),
             O.PROTOCOL_ERROR, "DEVICES", "RECORD_ID"),
            ("device_bad_device_id", dict(devices=lambda gid: {"result": [{"deviceId": "a/b",
                                                                              "productId": "p1"}]}),
             O.PROTOCOL_ERROR, "DEVICES", "RECORD_ID"),
            ("device_id_without_product", dict(devices=lambda gid: {"result": [{"devId": "d1"}]}),
             O.PROTOCOL_ERROR, "DEVICES", "RECORD_PRODUCT"),
            ("device_bad_product", dict(devices=lambda gid: {"result": [{"devId": "a", "productId": "x y"}]}),
             O.PROTOCOL_ERROR, "DEVICES", "RECORD_PRODUCT"),
            ("device_product_not_str", dict(devices=lambda gid: {"result": [{"devId": "a", "productId": 7}]}),
             O.PROTOCOL_ERROR, "DEVICES", "RECORD_PRODUCT"),
            ("device_product_conflict", dict(devices=lambda gid: {"result": [{"devId": "a", "productId": "p1",
                                                                              "product_id": "p2"}]}),
             O.PROTOCOL_ERROR, "DEVICES", "RECORD_SHAPE"),
            ("home_bad_gid", dict(homes=lambda: {"result": [{"gid": 0}]}),
             O.PROTOCOL_ERROR, "HOMES", "RECORD_ID"),
            ("home_gid_conflict", dict(homes=lambda: {"result": [{"gid": 1, "homeId": 2}]}),
             O.PROTOCOL_ERROR, "HOMES", "RECORD_SHAPE"),
            ("homes_has_more", dict(homes=lambda: {"result": [{"gid": 1}], "hasMore": True}),
             O.PROTOCOL_ERROR, "HOMES", "PAGINATION"),
            ("devices_total", dict(devices=lambda gid: {"result": [dev("d1")], "totalCount": 3}),
             O.PROTOCOL_ERROR, "DEVICES", "PAGINATION"),
            ("login_not_allowlisted", dict(login=lambda: login_reply(mobileApiUrl=NOT_ALLOWLISTED)),
             O.UNSUPPORTED_ROUTE, "LOGIN", "DIRECTED_URL"),
            ("login_conflict", dict(login=lambda: login_reply(mobileApiUrl="https://a1.tuyaeu.com",
                                                              domain={"mobileApiUrl": "https://a1.tuyaus.com"})),
             O.UNSUPPORTED_ROUTE, "LOGIN", "DIRECTED_CONFLICT"),
            ("homes_new_direction", dict(homes=lambda: {"result": [{"gid": 1}],
                                                        "mobileApiUrl": "https://a1.tuyaeu.com"}),
             O.UNSUPPORTED_ROUTE, "HOMES", "DIRECTED_CONFLICT"),
            ("homes_not_allowlisted", dict(homes=lambda: {"result": [{"gid": 1}],
                                                          "mobileApiUrl": NOT_ALLOWLISTED}),
             O.UNSUPPORTED_ROUTE, "HOMES", "DIRECTED_URL"),
            ("login_missing_sid", dict(login=lambda: {"ecode": sf.ECODE, "uid": sf.UID}),
             O.PROTOCOL_ERROR, "LOGIN", "MISSING_FIELD"),
            ("login_empty_sid", dict(login=lambda: login_reply(sid="")),
             O.PROTOCOL_ERROR, "LOGIN", "FIELD_TYPE"),
            ("login_url_type", dict(login=lambda: login_reply(mobileApiUrl=5)),
             O.PROTOCOL_ERROR, "LOGIN", "FIELD_TYPE"),
            ("token_missing", dict(token=lambda: {"exponent": "3", "token": sf.LOGIN_TOKEN}),
             O.PROTOCOL_ERROR, "TOKEN", "MISSING_FIELD"),
            ("token_bad", dict(token=lambda: dict(sf.token_fixture(), token="")),
             O.PROTOCOL_ERROR, "TOKEN", "FIELD_TYPE"),
            ("token_unwrap_depth", dict(token=lambda: nest(sf.token_fixture(), 5)),
             O.PROTOCOL_ERROR, "TOKEN", "UNWRAP_DEPTH"),
            ("homes_unwrap_depth", dict(homes=lambda: nest([{"gid": 1}], 6)),
             O.PROTOCOL_ERROR, "HOMES", "UNWRAP_DEPTH"),
            ("homes_non_json", dict(homes=Raw(b"<html>")), O.PROTOCOL_ERROR, "HOMES", "OUTER_SHAPE"),
            ("homes_outer_no_result", dict(homes=Envelope(success=True)),
             O.PROTOCOL_ERROR, "HOMES", "MISSING_FIELD"),
            ("homes_outer_bad_type", dict(homes=Envelope(result={"gid": 1})),
             O.PROTOCOL_ERROR, "HOMES", "OUTER_SHAPE"),
            ("homes_seal", dict(homes=Envelope(result="QUFBQQ==")), O.PROTOCOL_ERROR, "HOMES", "SEAL"),
            ("homes_oversized", dict(homes=Raw(b'{"result":"' + b"A" * ac.MAX_RESPONSE_BYTES + b'"}')),
             O.LIMIT_EXCEEDED, "HOMES", "LIMIT"),
            ("login_timeout", dict(login=TimeoutError(sf.SENTINEL)), O.TIMEOUT, "LOGIN", "TIMEOUT"),
            ("login_cancelled", dict(login=ac.Cancelled()), O.CANCELLED, "LOGIN", "CANCELLED"),
            ("homes_exchange_error", dict(homes=OSError(sf.SENTINEL)), O.PROTOCOL_ERROR, "HOMES", "NONE"),
            ("conflicting_duplicate", dict(homes=homes_of(1, 2), devices=lambda gid: {"result": [
                dev("same", product=sf.OBSERVED_PRODUCT if gid == 1 else sf.OTHER_PRODUCT)]}),
             O.PROTOCOL_ERROR, "DEVICES", "RECORD_PRODUCT"),
        ]
        for name, slots, outcome, stage, reason in cases:
            with self.subTest(name):
                result, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(outcome))
                self.assertEqual(labels(result), (stage, reason))

    def test_profile_and_setup_labels(self):
        for name, kwargs, outcome, stage, reason in (
            ("encrypt", dict(profile=SyntheticProfile(fail={"encrypt"})), O.UNSUPPORTED_PROFILE,
             "LOGIN", "PROFILE"),
            ("prepare", dict(profile=SyntheticProfile(fail={"prepare"})), O.UNSUPPORTED_PROFILE,
             "TOKEN", "PROFILE"),
            ("decode", dict(profile=SyntheticProfile(fail={"decode"})), O.UNSUPPORTED_PROFILE,
             "TOKEN", "PROFILE"),
            ("short_cipher", dict(profile=SyntheticProfile(password_transform=lambda p, k: "ab")),
             O.UNSUPPORTED_PROFILE, "LOGIN", "PROFILE"),
            ("bad_email", dict(creds=sf.credentials(username="not-an-email")), O.PROTOCOL_ERROR,
             "SETUP", "FIELD_TYPE"),
            ("bad_country", dict(creds=sf.credentials(country="86")), O.UNSUPPORTED_ROUTE,
             "SETUP", "FIELD_TYPE"),
            ("expired", dict(timeout=-1), O.TIMEOUT, "SETUP", "TIMEOUT"),
            ("unauthorized", dict(authorized=False), O.BUDGET_EXHAUSTED, "SETUP", "BUDGET"),
            ("no_permit", dict(permit=ac.PasswordPermit()), O.BUDGET_EXHAUSTED, "SETUP", "BUDGET"),
        ):
            with self.subTest(name):
                result, *_ = run(**kwargs)
                self.assertEqual(result, fail(outcome))
                self.assertEqual(labels(result), (stage, reason))

    def test_failure_labels_survive_directed_label(self):
        login = lambda: login_reply(mobileApiUrl="https://a1.tuyaus.com")
        result, *_ = run(FakeServer(login=login, homes=lambda: {"result": [{"gid": "a/b"}]}))
        self.assertEqual(result, ac.SafeResult(O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.TUYAUS))
        self.assertEqual(labels(result), ("HOMES", "RECORD_ID"))
        # Any change away from an accepted directed route is rejected by the
        # boundary allowlist itself (only a SAME route can be redirected).
        homes = lambda: {"result": [{"gid": 1}], "mobileApiUrl": "https://a1.tuyaeu.com"}
        result, *_ = run(FakeServer(login=login, homes=homes))
        self.assertEqual(result, ac.SafeResult(O.UNSUPPORTED_ROUTE, 0, 0, 0, ac.ApiDomain.TUYAUS))
        self.assertEqual(labels(result), ("HOMES", "DIRECTED_URL"))

    def test_safe_result_label_validation(self):
        S, R = ac.Stage, ac.Reason
        ac.SafeResult(O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.SAME, S.HOMES, R.NO_LIST)
        for args in ((O.OK, 0, 0, 0, ac.ApiDomain.SAME, S.HOMES, R.NONE),
                     (O.OK, 0, 0, 0, ac.ApiDomain.SAME, S.NONE, R.LIMIT),
                     (O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.SAME, S.NONE, R.LIMIT),
                     (O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.SAME, "HOMES", R.NONE),
                     (O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.SAME, S.HOMES, "NO_LIST"),
                     (O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.SAME, S.HOMES, None),
                     (O.PROTOCOL_ERROR, 0, 0, 0, ac.ApiDomain.SAME, ac.ApiDomain.SAME, R.NONE)):
            with self.subTest(args[-2:]):
                with self.assertRaises(ValueError):
                    ac.SafeResult(*args)

    def test_every_stop_site_names_a_reason(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(ac))
        sites = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_Stop":
                sites += 1
                self.assertGreaterEqual(len(node.args), 2, "line %d" % node.lineno)
                reason = node.args[1]
                self.assertIsInstance(reason, ast.Attribute, "line %d" % node.lineno)
                self.assertEqual(getattr(reason.value, "id", None), "Reason", "line %d" % node.lineno)
                self.assertIn(reason.attr, ac.Reason.__members__, "line %d" % node.lineno)
                if len(node.args) > 2:
                    stage = node.args[2]
                    self.assertEqual(getattr(stage.value, "id", None), "Stage")
                    self.assertIn(stage.attr, ac.Stage.__members__)
        self.assertGreater(sites, 50)

    def test_sentinel_in_every_server_field_never_emitted(self):
        mark = "ZQLEAKMARK9"
        rec = {"name": mark, "errorMsg": "", "localKey": mark, "ip": mark, mark: mark}
        slots_list = [
            dict(homes=Envelope(errorCode=mark, errorMsg=mark, result=mark)),
            dict(homes=Envelope(errorCode=mark, errorMsg=mark)),
            dict(homes=lambda: {"errorCode": mark, "errorMsg": mark, mark: mark}),
            dict(homes=lambda: {"result": [dict(rec, gid=mark + "/")], mark: [mark]}),
            dict(homes=lambda: {"result": {mark: [dict(rec, gid=1, errorCode=mark)]}}),
            dict(homes=lambda: {"result": [dict(rec, gid=1)], "mobileApiUrl": "https://" + mark}),
            dict(devices=lambda gid: {"result": [dict(rec, devId=mark + " x", productId="p1")]}),
            dict(devices=lambda gid: {"result": [dict(rec, devId="d", productId=mark + " x")]}),
            dict(devices=lambda gid: {"result": [dev("d")], "errorMsg": mark}),
            dict(login=lambda: login_reply(mobileApiUrl="https://" + mark + ".example",
                                           errorCode="", nickname=mark)),
            dict(login=Envelope(errorCode=mark, errorMsg=mark, success=False)),
            dict(login=lambda: {"sid": mark, "ecode": mark, "uid": mark,
                                "domain": {"mobileApiUrl": mark}}),
            dict(token=lambda: {"errorCode": mark, "errorMsg": mark}),
            dict(token=lambda: dict(sf.token_fixture(), token=mark + " ")),
            dict(homes=OSError(mark), login=lambda: login_reply()),
            dict(homes=Raw(mark.encode())),
        ]
        # Every transport reason: an ExchangeFailed subclass whose message
        # carries the mark, raised at each stage; only the closed label leaves.
        transport_reasons = []
        for cls, _reason in transport_classes():
            marked = type("Marked", (cls,), {"__init__": lambda self: Exception.__init__(self, mark)})
            for slot in ("token", "homes"):
                slots_list.append({slot: marked()})
                transport_reasons.append(len(slots_list) - 1)
        for index, slots in enumerate(slots_list):
            with self.subTest(index):
                result, *_ = run(FakeServer(**slots))
                self.assertNotEqual(result.outcome, O.OK)
                self.assertIn(result.stage, list(ac.Stage))
                self.assertIn(result.reason, list(ac.Reason))
                line = json.dumps(result_line(result), sort_keys=True)
                for text in (repr(result), str(result), line, result.stage.value,
                             result.reason.value):
                    self.assertNotIn(mark, text)
                    self.assertNotIn(mark.lower(), text.lower())
                self.assertNotIn(mark.encode(), pickle.dumps(result))
                assert_no_secrets(self, result, line)
                if index in transport_reasons:
                    self.assertTrue(result.reason.name.startswith("TRANSPORT_"))
        self.assertEqual(len(transport_reasons), 2 * len(transport_classes()))


def result_line(result):
    """The fields a debug log line may carry (harness offline_gate.result_dict)."""
    return {"outcome": result.outcome.value, "home_count": result.home_count,
            "device_count": result.device_count, "supported_count": result.supported_count,
            "key_count": result.key_count, "name_count": result.name_count,
            "api_domain": result.api_domain.value, "stage": result.stage.value,
            "reason": result.reason.value}


def transport_classes():
    """Each live_exchange failure subclass with its expected closed reason."""
    from custom_components.petsafe_freshfeed.account import exchange as lx
    return ((lx.ExchangeBindFailed, "TRANSPORT_BIND"),
            (lx.ExchangeConnectFailed, "TRANSPORT_CONNECT"),
            (lx.ExchangeStatusFailed, "TRANSPORT_STATUS"),
            (lx.ExchangeEncodingFailed, "TRANSPORT_ENCODING"),
            (lx.ExchangeLengthFailed, "TRANSPORT_LENGTH"),
            (lx.ExchangeIOFailed, "TRANSPORT_IO"))


class TransportReasonTests(GuardedTestCase):
    """An exchange failure (live_exchange.ExchangeFailed) is reason TRANSPORT;
    any other unexpected exception stays reason NONE; timeouts are TIMEOUT."""

    def test_exchange_failed_is_transport_at_each_stage(self):
        from custom_components.petsafe_freshfeed.account import exchange as live_exchange
        for slot, stage in (("token", "TOKEN"), ("login", "LOGIN"), ("homes", "HOMES"),
                            ("devices", "DEVICES")):
            with self.subTest(stage):
                result, *_ = run(FakeServer(**{slot: live_exchange.ExchangeFailed()}))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), (stage, "TRANSPORT"))

    def test_exchange_failed_kind_maps_to_closed_reason(self):
        from custom_components.petsafe_freshfeed.account import exchange as live_exchange
        for cls, reason in transport_classes():
            for slot, stage in (("token", "TOKEN"), ("login", "LOGIN"), ("homes", "HOMES"),
                                ("devices", "DEVICES")):
                with self.subTest(cls=cls.__name__, stage=stage):
                    self.assertTrue(issubclass(cls, live_exchange.ExchangeFailed))
                    result, *_ = run(FakeServer(**{slot: cls()}))
                    self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                    self.assertEqual(labels(result), (stage, reason))

    def test_unrecognised_kind_is_plain_transport(self):
        from custom_components.petsafe_freshfeed.account import exchange as live_exchange
        base = live_exchange.ExchangeFailed
        instance_kind = base()
        instance_kind.kind = "BIND"  # an instance attribute is never read
        cases = [instance_kind]
        for kind in (None, "BOGUS", "bind", 5, b"BIND", ("BIND",), type("S", (str,), {})("BIND")):
            cases.append(type("Odd", (base,), {"kind": kind})())
        for exc in cases:
            with self.subTest(kind=repr(type(exc).__dict__.get("kind"))):
                result, *_ = run(FakeServer(homes=exc))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), ("HOMES", "TRANSPORT"))
        # A non-ExchangeFailed exception carrying a kind stays reason NONE.
        odd = type("NotTransport", (RuntimeError,), {"kind": "CONNECT"})(sf.SENTINEL)
        result, *_ = run(FakeServer(homes=odd))
        self.assertEqual(labels(result), ("HOMES", "NONE"))

    def test_plain_exception_is_none(self):
        for exc in (RuntimeError(sf.SENTINEL), OSError(sf.SENTINEL)):
            with self.subTest(type(exc).__name__):
                result, *_ = run(FakeServer(homes=exc))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), ("HOMES", "NONE"))

    def test_timeouts_unchanged(self):
        from custom_components.petsafe_freshfeed.account import exchange as live_exchange
        for exc in (TimeoutError(sf.SENTINEL), live_exchange.ExchangeTimeout()):
            with self.subTest(type(exc).__name__):
                result, *_ = run(FakeServer(homes=exc))
                self.assertEqual(result, fail(O.TIMEOUT))
                self.assertEqual(labels(result), ("HOMES", "TIMEOUT"))

    def test_transport_stop_suppresses_cause_and_text(self):
        from custom_components.petsafe_freshfeed.account import exchange as live_exchange
        server = FakeServer(homes=live_exchange.ExchangeFailed())
        client = ac.AccountClient(SyntheticProfile(), FakeExchange(server), contract(),
                                  permit=granted_permit())
        stops = []
        original = client._flow

        def flow(*args):
            try:
                return original(*args)
            except ac._Stop as stop:
                stops.append(stop)
                raise
        client._flow = flow
        result = client.run(sf.credentials(), password_authorized=True,
                            deadline=time.monotonic() + 30)
        self.assertEqual(labels(result), ("HOMES", "TRANSPORT"))
        self.assertEqual(len(stops), 1)
        self.assertIsNone(stops[0].__cause__)
        self.assertTrue(stops[0].__suppress_context__)
        for text in (repr(result), str(result)):
            self.assertNotIn("failed", text.lower())
            self.assertNotIn("ExchangeFailed", text)


class DeviceRuleTests(GuardedTestCase):
    """A device record needs an id alias AND a product alias; an object with
    only an id (group, mesh, room) is skipped, not an error."""

    def test_group_with_int_id_is_not_a_device(self):
        devices = lambda gid: {"result": {
            "groups": [{"id": 4242, "name": sf.SENTINEL}],
            "meshes": [{"id": "mesh-1", "devIds": ["x"]}],
            "devices": [dev("d1"), dev("d2", product=sf.OTHER_PRODUCT)],
            "room": {"id": 7, "inner": [dev("d3")]}}}
        result, *_ = run(FakeServer(devices=devices))
        self.assertEqual(result, ok(1, 3, 2))
        self.assertEqual(labels(result), ("NONE", "NONE"))

    def test_device_with_product_and_bad_string_id_still_record_id(self):
        for key in ("devId", "id", "deviceId"):
            for bad in ("a/b", "", "x" * 65):
                with self.subTest(key=key, bad=bad[:5]):
                    devices = lambda gid, k=key, b=bad: {"result": [{
                        k: b, "productId": sf.OBSERVED_PRODUCT}]}
                    result, *_ = run(FakeServer(devices=devices))
                    self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                    self.assertEqual(labels(result), ("DEVICES", "RECORD_ID"))

    def test_product_with_non_string_id_is_not_a_device(self):
        # A group carrying a product alias and an int id is walked into,
        # never RECORD_ID; a null id is no alias at all.
        for bad in (5, 4242, True, 1.5, None):
            with self.subTest(repr(bad)):
                devices = lambda gid, b=bad: {"result": [{
                    "id": b, "productId": sf.OBSERVED_PRODUCT,
                    "list": [dev("d1"), dev("d2", product=sf.OTHER_PRODUCT)]}]}
                result, *_ = run(FakeServer(devices=devices))
                self.assertEqual(result, ok(1, 2, 1))
                self.assertEqual(labels(result), ("NONE", "NONE"))
                devices = lambda gid, b=bad: {"result": [{"devId": b,
                                                           "productId": sf.OBSERVED_PRODUCT}]}
                self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 0, 0))

    def test_device_id_alias(self):
        # vendor client.py:212 reads deviceId as a device key.
        self.assertIn("deviceId", ac._DEVICE_ID_KEYS)
        self.assertEqual(ac._DEVICE_ID_KEYS, ("devId", "id", "deviceId"))
        self.assertEqual(ac._HOME_ID_KEYS, ("gid", "groupId", "homeId"))
        self.assertEqual(ac._PRODUCT_KEYS, ("productId", "product_id"))
        devices = lambda gid: {"result": [{"deviceId": "d1", "productId": sf.OBSERVED_PRODUCT},
                                          {"deviceId": "d2", "devId": "d2",
                                           "productId": sf.OTHER_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 2, 1, keys=0, names=0))
        # Disagreeing string aliases: the first in key order (devId) is the id.
        devices = lambda gid: {"result": [{"deviceId": "d1", "devId": "d9",
                                           "productId": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1, keys=0, names=0))

    def test_device_with_bad_product_still_record_product(self):
        for bad in ("x y", 7, None, ""):
            with self.subTest(repr(bad)):
                devices = lambda gid, b=bad: {"result": [{"devId": "d1", "productId": b}]}
                result, *_ = run(FakeServer(devices=devices))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), ("DEVICES", "RECORD_PRODUCT"))

    def test_devices_without_any_product_key_are_record_product(self):
        # Id-bearing objects skipped for lacking a product alias, with no
        # device record in the response: not a silent 0, RECORD_PRODUCT.
        for body in ({"result": [{"devId": "d1", "name": sf.SENTINEL}, {"id": "d2"}]},
                     {"result": {"list": [{"deviceId": "d1"}]}},
                     {"result": [{"devId": "d1", "id": "d9"}]}):
            with self.subTest(repr(body)[:40]):
                result, *_ = run(FakeServer(devices=lambda gid, b=body: b))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), ("DEVICES", "RECORD_PRODUCT"))
        # One home with real devices and one with only product-less ids still
        # stops (the per-home check), rather than undercounting.
        devices = lambda gid: ({"result": [dev("d1")]} if gid == 1 else
                               {"result": [{"devId": "d2"}]})
        result, *_ = run(FakeServer(homes=homes_of(1, 2), devices=devices))
        self.assertEqual(labels(result), ("DEVICES", "RECORD_PRODUCT"))
        # Objects whose only ids are null or non-string are not id-bearing.
        devices = lambda gid: {"result": [{"id": 7, "name": "room"}, {"devId": None},
                                          {"id": True}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 0, 0))
        # With at least one device record, skipped id-only objects (groups,
        # meshes) are still not counted and not an error.
        devices = lambda gid: {"result": [dev("d1"), {"id": "mesh-1"}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1))

    def test_null_product_is_record_product(self):
        for product in ({"productId": None}, {"productId": None, "product_id": None}):
            with self.subTest(repr(product)):
                devices = lambda gid, p=product: {"result": [dict(dev("d0"), devId="d0"),
                                                              dict({"devId": "d1"}, **p)]}
                result, *_ = run(FakeServer(devices=devices))
                self.assertEqual(labels(result), ("DEVICES", "RECORD_PRODUCT"))
        devices = lambda gid: {"result": [{"devId": "d1", "productId": None,
                                           "product_id": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1, keys=0, names=0))

    def test_skipped_object_is_walked_into(self):
        # Not a record, so it is descended into like any other object. It is
        # an id-bearing entity, so its own total is not a page size
        # (EntityTotalTests); its hasMore still stops the run.
        devices = lambda gid: {"result": [{"id": 1, "list": [dev("d1")], "total": 1}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1))
        devices = lambda gid: {"result": [{"id": 1, "list": [dev("d1")], "total": 2}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1))
        devices = lambda gid: {"result": [{"id": 1, "list": [dev("d1")], "hasMore": True}]}
        result, *_ = run(FakeServer(devices=devices))
        self.assertEqual(labels(result), ("DEVICES", "PAGINATION"))

    def test_home_rule_unchanged(self):
        # Homes still need only an id alias.
        self.assertEqual(ac._home_ids({"result": [{"gid": 3}, {"groupId": "h-4"}]}), [3, "h-4"])


class AgreeingAliasTests(GuardedTestCase):
    """Several alias keys with equal values (same type) are accepted."""

    def test_home_gid_and_group_id_equal(self):
        homes = lambda: {"result": [{"gid": 100001, "groupId": 100001}]}
        server = FakeServer(homes=homes)
        result, *_ = run(server)
        self.assertEqual(result, ok(1, 1, 1))
        self.assertEqual([b["gid"] for op, b in server.business if op == "DEVICES"], [100001])
        self.assertEqual(ac._home_ids({"result": [{"gid": 9, "groupId": 9, "homeId": 9}]}), [9])

    def test_home_alias_disagree_is_record_shape(self):
        for record in ({"gid": 1, "groupId": 2}, {"gid": "1", "groupId": 1},
                       {"gid": 1, "groupId": 1, "homeId": 2}, {"gid": 1, "homeId": True}):
            with self.subTest(repr(record)):
                result, *_ = run(FakeServer(homes=lambda r=record: {"result": [r]}))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), ("HOMES", "RECORD_SHAPE"))

    def test_device_dev_id_and_id_equal(self):
        devices = lambda gid: {"result": [dev("d1", id="d1")]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1))
        devices = lambda gid: {"result": [{"devId": "d1", "productId": sf.OBSERVED_PRODUCT,
                                           "product_id": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1, keys=0, names=0))

    def test_device_id_takes_first_string_alias(self):
        # Vendor client.py:212 takes ``devId or deviceId or id``: a device
        # whose other id alias is a row number or a different string is
        # still one device, identified by the first string alias in
        # ``_DEVICE_PICK_ORDER``.
        for name, record in (
            ("int_row_id", {"devId": "d1", "id": 1, "productId": sf.OBSERVED_PRODUCT}),
            ("other_string_id", {"devId": "d1", "id": "row-7",
                                 "productId": sf.OBSERVED_PRODUCT}),
            ("int_id_then_device_id", {"id": 7, "deviceId": "d1",
                                       "productId": sf.OBSERVED_PRODUCT}),
            ("bool_dev_id_then_id", {"devId": True, "id": "d1",
                                     "productId": sf.OBSERVED_PRODUCT}),
        ):
            with self.subTest(name):
                server = FakeServer(devices=lambda gid, r=record: {"result": [r]})
                result, *_ = run(server)
                self.assertEqual(result, ok(1, 1, 1, keys=0, names=0))
                self.assertEqual(labels(result), ("NONE", "NONE"))
        # Pick order is the vendor's ``devId or deviceId or id``: with no
        # devId, deviceId wins over id, so these two records are one device.
        pick = lambda gid: {"result": [{"id": "row-7", "deviceId": "d1",
                                        "productId": sf.OBSERVED_PRODUCT},
                                       {"devId": "d1", "productId": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=pick))[0], ok(1, 1, 1, keys=0, names=0))
        # Two records whose first string alias matches are one device.
        twice = lambda gid: {"result": [{"devId": "d1", "id": 1, "productId": sf.OBSERVED_PRODUCT},
                                        {"devId": "d1", "id": 2, "productId": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=twice))[0], ok(1, 1, 1, keys=0, names=0))

    def test_device_alias_disagree_is_record_shape(self):
        for record in ({"devId": "a", "productId": "p1", "product_id": "p2"},):
            with self.subTest(repr(record)):
                result, *_ = run(FakeServer(devices=lambda gid, r=record: {"result": [r]}))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), ("DEVICES", "RECORD_SHAPE"))

    def test_alias_unit(self):
        self.assertEqual(ac._alias({"a": 1}, ("a", "b")), (1, True))
        self.assertEqual(ac._alias({"c": 1}, ("a", "b")), (None, False))
        self.assertEqual(ac._alias({"a": 1, "b": 1}, ("a", "b")), (1, True))
        for record in ({"a": 1, "b": 2}, {"a": "1", "b": 1}, {"a": 1, "b": 1.0},
                       {"a": 1, "b": True}):
            with self.subTest(repr(record)):
                with self.assertRaises(ac._Stop) as caught:
                    ac._alias(record, ("a", "b"))
                self.assertIs(caught.exception.reason, ac.Reason.RECORD_SHAPE)


class EntityTotalTests(GuardedTestCase):
    """``total``/``totalCount`` on an id-bearing entity (a room, group or
    mesh object) is that entity's own count, not a page size: it is not
    compared against the walked records. ``hasMore`` true still stops."""

    def test_entity_total_not_compared(self):
        d1 = dev("d1")
        for name, body in (
            ("group_int_id", {"result": [{"id": 4242, "total": 5, "devices": [d1]}]}),
            ("group_string_id_no_product", {"result": [{"id": "g1", "totalCount": 9},
                                                       d1]}),
            ("group_id_key", {"result": [{"groupId": 7, "total": 3, "list": [d1]}]}),
            ("mesh_beside_devices", {"result": {"meshes": [{"id": 1, "total": 4}],
                                                "list": [d1]}}),
        ):
            with self.subTest(name):
                result, *_ = run(FakeServer(devices=lambda gid, b=body: b))
                self.assertEqual(result, ok(1, 1, 1))
                self.assertEqual(labels(result), ("NONE", "NONE"))

    def test_entity_has_more_still_pagination(self):
        body = {"result": [{"id": 4242, "hasMore": True, "devices": [dev("d1")]}]}
        result, *_ = run(FakeServer(devices=lambda gid: body))
        self.assertEqual(result, fail(O.PROTOCOL_ERROR))
        self.assertEqual(labels(result), ("DEVICES", "PAGINATION"))

    def test_page_total_still_compared(self):
        body = {"result": {"total": 5, "list": [dev("d1")]}}
        result, *_ = run(FakeServer(devices=lambda gid: body))
        self.assertEqual(result, fail(O.PROTOCOL_ERROR))
        self.assertEqual(labels(result), ("DEVICES", "PAGINATION"))


class NullAliasTests(GuardedTestCase):
    """A null alias value is no alias: ignored when collecting values, and an
    object whose every id alias is null is not a record."""

    def test_alias_ignores_null_values(self):
        H = ac._HOME_ID_KEYS
        self.assertEqual(ac._alias({"gid": 1, "groupId": None}, H), (1, True))
        self.assertEqual(ac._alias({"gid": None, "homeId": "h-1"}, H), ("h-1", True))
        self.assertEqual(ac._alias({"gid": None}, H), (None, False))
        self.assertEqual(ac._alias({"gid": None, "groupId": None}, H), (None, False))
        # Falsy but non-null values are still values.
        for value in (0, "", False):
            with self.subTest(repr(value)):
                self.assertEqual(ac._alias({"gid": value}, H), (value, True))
        with self.assertRaises(ac._Stop) as caught:
            ac._alias({"gid": 1, "groupId": None, "homeId": 2}, H)
        self.assertIs(caught.exception.reason, ac.Reason.RECORD_SHAPE)

    def test_null_id_home_is_walked_into(self):
        server = FakeServer(homes=lambda: {"result": [{"gid": 1, "groupId": None}]})
        self.assertEqual(run(server)[0], ok(1, 1, 1))
        self.assertEqual([b["gid"] for op, b in server.business if op == "DEVICES"], [1])
        server = FakeServer(homes=lambda: {"result": [{"gid": None, "homes": [{"gid": 2}]}]})
        self.assertEqual(run(server)[0], ok(1, 1, 1))
        self.assertEqual([b["gid"] for op, b in server.business if op == "DEVICES"], [2])
        self.assertEqual(run(FakeServer(homes=lambda: {"result": [{"gid": None}]}))[0],
                         ok(0, 0, 0))
        self.assertEqual(ac._home_ids({"result": {"gid": None, "inner": {"homeId": 3}}}), [3])

    def test_falsy_non_null_home_id_still_record_id(self):
        for value in (0, "", False):
            with self.subTest(repr(value)):
                result, *_ = run(FakeServer(homes=lambda v=value: {"result": [{"gid": v}]}))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), ("HOMES", "RECORD_ID"))

    def test_null_id_device_alias_ignored(self):
        devices = lambda gid: {"result": [{"devId": None, "id": "d1",
                                           "productId": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(run(FakeServer(devices=devices))[0], ok(1, 1, 1, keys=0, names=0))
        devices = lambda gid: {"result": [{"devId": "", "productId": sf.OBSERVED_PRODUCT}]}
        self.assertEqual(labels(run(FakeServer(devices=devices))[0]), ("DEVICES", "RECORD_ID"))


class BareArrayTests(GuardedTestCase):
    """A HOMES/DEVICES plaintext that is a JSON array is read as if wrapped
    in ``{"result": ...}``; TOKEN and PASSWORD_LOGIN still reject it."""

    def test_bare_array_homes_and_devices(self):
        server = FakeServer(homes=lambda: [{"gid": 1}, {"gid": 2}],
                            devices=lambda gid: [dev("d%d" % gid),
                                                 dev("x%d" % gid, product=sf.OTHER_PRODUCT)])
        result, exchange, *_ = run(server)
        self.assertEqual(result, ok(2, 4, 2))
        self.assertEqual(labels(result), ("NONE", "NONE"))
        self.assertEqual(run(FakeServer(homes=lambda: []))[0], ok(0, 0, 0))
        self.assertEqual(run(FakeServer(devices=lambda gid: []))[0], ok(1, 0, 0))

    def test_bare_array_rejected_for_token_and_login(self):
        for name, slots, stage in (("token", dict(token=lambda: [sf.token_fixture()]), "TOKEN"),
                                   ("login", dict(login=lambda: [login_reply()]), "LOGIN")):
            with self.subTest(name):
                result, exchange, *_ = run(FakeServer(**slots))
                self.assertEqual(result, fail(O.PROTOCOL_ERROR))
                self.assertEqual(labels(result), (stage, "DECODE"))

    def test_bare_array_bounds_still_apply(self):
        # Depth: the implicit wrapper adds one level, so a nested array that
        # fits only as a wrapped body is still bounded.
        deep = chain([], ac.MAX_DEPTH, key="x")
        result, *_ = run(FakeServer(homes=lambda: [deep]))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
        # Nodes.
        result, *_ = run(FakeServer(homes=lambda: [0] * (ac.MAX_NODES + 1)))
        self.assertEqual(result, fail(O.LIMIT_EXCEEDED))
        self.assertEqual(labels(result), ("HOMES", "LIMIT"))
        # Records and pagination rules apply as for a wrapped body.
        result, *_ = run(FakeServer(homes=lambda: [{"gid": 0}]))
        self.assertEqual(labels(result), ("HOMES", "RECORD_ID"))
        result, *_ = run(FakeServer(homes=lambda: [{"gid": i} for i in range(1, 18)]))
        self.assertEqual(labels(result), ("HOMES", "LIMIT"))


class DependencyRecordTests(GuardedTestCase):
    def test_no_independent_signer_vector(self):
        """No independent PetSafe signer/envelope vector or runtime charset
        evidence exists (SOURCE_REVIEW.md qualification 1). The client records
        the dependency, and a profile that cannot provide the capability
        terminates with UNSUPPORTED_PROFILE before any password request."""
        self.assertIn("signer", " ".join(ac.UNRESOLVED_DEPENDENCIES).lower())
        self.assertIn("product", " ".join(ac.UNRESOLVED_DEPENDENCIES).lower())
        result, exchange, *_ = run(profile=SyntheticProfile(fail={"encrypt"}))
        self.assertEqual(result, fail(O.UNSUPPORTED_PROFILE))
        self.assertEqual(exchange.password_requests(), [])


# Synthetic, obviously fake local keys (never real material).
SYNTH_KEY = "synthfakekey0016"
SYNTH_KEY_SENTINEL = sf.SENTINEL + "-sk"


def bare(dev_id, product=sf.OBSERVED_PRODUCT, **fields):
    """A device record with only an id, a product and the given fields."""
    record = {"devId": dev_id, "productId": product}
    record.update(fields)
    return record


def devices_of(*records):
    return lambda gid: {"result": list(records)}


class KeyAndNameCountTests(GuardedTestCase):
    """key_count / name_count: how many counted supported devices carry a
    plausible ``localKey`` / a usable ``name``. Values never leave the walk."""

    def test_key_count_counts_supported_devices_with_local_key(self):
        result, *_ = run(FakeServer(devices=devices_of(bare("d1", localKey=SYNTH_KEY),
                                                       bare("d2"))))
        self.assertEqual(result, ok(1, 2, 2, keys=1, names=0))
        self.assertEqual(result.key_count, 1)

    def test_key_count_ignores_non_string_and_out_of_range(self):
        for value in (12345678, [SYNTH_KEY], {"k": SYNTH_KEY}, None, True, 12.5,
                      "a" * 7, "a" * 8, "a" * 15, "a" * 17, "a" * 64, "a" * 65, "",
                      "synth key1aaaaaa", "synthkey\t1aaaaaa", "synthkey\x7f1aaaaaa"[:16],
                      "synthkey\u00e91aaaaa", "synthkey1aaaaaa\n"):
            with self.subTest(value=type(value).__name__ + str(len(value) if hasattr(value, "__len__") else "")):
                result, *_ = run(FakeServer(devices=devices_of(bare("d1", localKey=value))))
                self.assertEqual(result, ok(1, 1, 1, keys=0, names=0))
        for value in ("a" * 16, "!" + "~" * 15, SYNTH_KEY):
            with self.subTest(valid=len(value)):
                result, *_ = run(FakeServer(devices=devices_of(bare("d1", localKey=value))))
                self.assertEqual(result, ok(1, 1, 1, keys=1, names=0))
        # Only the exact ``localKey`` alias counts.
        for alias in ("local_key", "localkey", "LocalKey", "secKey"):
            with self.subTest(alias=alias):
                result, *_ = run(FakeServer(devices=devices_of(bare("d1", **{alias: SYNTH_KEY}))))
                self.assertEqual(result, ok(1, 1, 1, keys=0, names=0))

    def test_key_count_ignores_unsupported_devices(self):
        devices = devices_of(bare("d1", product=sf.OTHER_PRODUCT, localKey=SYNTH_KEY,
                                  name="Other"),
                             bare("d2", name="Feeder"))
        result, *_ = run(FakeServer(devices=devices))
        self.assertEqual(result, ok(1, 2, 1, keys=0, names=1))
        result, *_ = run(FakeServer(devices=devices_of(
            bare("d1", product=sf.OTHER_PRODUCT, localKey=SYNTH_KEY, name="Other"))))
        self.assertEqual(result, ok(1, 1, 0, keys=0, names=0))

    def test_name_count(self):
        cases = (("Feeder", 1), ("  Kitchen feeder  ", 1), ("x", 1), ("a" * 64, 1),
                 (" " + "a" * 64 + " ", 1), ("a" * 65, 0), ("", 0), ("   ", 0), ("\t\n", 0),
                 (123, 0), (None, 0), (["Feeder"], 0), (True, 0))
        for value, expected in cases:
            with self.subTest(value=repr(value)[:12]):
                result, *_ = run(FakeServer(devices=devices_of(bare("d1", name=value))))
                self.assertEqual(result, ok(1, 1, 1, keys=0, names=expected))
        result, *_ = run(FakeServer(devices=devices_of(bare("d1"))))
        self.assertEqual(result, ok(1, 1, 1, keys=0, names=0))
        # Two supported, one named, plus an unsupported named device.
        result, *_ = run(FakeServer(devices=devices_of(
            bare("d1", name="Feeder"), bare("d2"),
            bare("d3", product=sf.OTHER_PRODUCT, name="Other"))))
        self.assertEqual(result, ok(1, 3, 2, keys=0, names=1))

    def test_counts_follow_deduplicated_supported_devices(self):
        # One feeder shared by two homes counts once; a key or name seen in
        # any of its records counts it.
        per_home = {1: [bare("shared", localKey=SYNTH_KEY, name="Feeder")],
                    2: [bare("shared"), bare("own", localKey=SYNTH_KEY)]}
        result, *_ = run(FakeServer(homes=homes_of(1, 2), devices=lambda gid: {
            "result": per_home[gid]}))
        self.assertEqual(result, ok(2, 2, 2, keys=2, names=1))
        result, *_ = run(FakeServer(homes=homes_of(1, 2), devices=lambda gid: {
            "result": [bare("shared", localKey=SYNTH_KEY)]}))
        self.assertEqual(result, ok(2, 1, 1, keys=1, names=0))

    def test_failures_carry_no_key_or_name_count(self):
        # A later record failing validation discards counts already seen.
        result, *_ = run(FakeServer(devices=devices_of(
            bare("d1", localKey=SYNTH_KEY, name="Feeder"), bare("bad id", localKey=SYNTH_KEY))))
        self.assertEqual(result, fail(O.PROTOCOL_ERROR))
        self.assertEqual((result.key_count, result.name_count), (0, 0))
        result, *_ = run(FakeServer(login=Envelope(errorCode="USER_PASSWD_WRONG", errorMsg="x")))
        self.assertEqual((result.key_count, result.name_count), (0, 0))

    def test_local_key_and_name_read_only_from_supported_records(self):
        home = lambda: {"result": [{"gid": 1, "name": sf.SENTINEL, "localKey": SYNTH_KEY}]}
        devices = devices_of(bare("s1", localKey=SYNTH_KEY_SENTINEL, name=sf.SENTINEL),
                             bare("s2", localKey=SYNTH_KEY_SENTINEL, name=sf.SENTINEL),
                             bare("u1", product=sf.OTHER_PRODUCT, localKey=SYNTH_KEY_SENTINEL,
                                  name=sf.SENTINEL, ip="192.0.2.1", uuid="u"))
        sf.RecordingDict.owned = []
        try:
            result, *_ = run(FakeServer(homes=home, devices=devices),
                             profile=SyntheticProfile(record_access=True))
            owned = list(sf.RecordingDict.owned)
        finally:
            sf.RecordingDict.owned = None
        self.assertEqual(result, ok(1, 3, 2, keys=2, names=2))
        for field in ("localKey", "name"):
            readers = [owner for owner, key in owned if key == field]
            self.assertEqual(sorted(readers), ["s1", "s2"], field)
        forbidden = {"local_key", "ip", "lanIp", "uuid", "secKey", "devKey"}
        self.assertFalse({key for _owner, key in owned} & forbidden)
        self.assertFalse([key for owner, key in owned if owner in ("u1", 1)
                          and key in ("localKey", "name")])
        blob = pickle.dumps(result)
        for secret in (SYNTH_KEY_SENTINEL, sf.SENTINEL, SYNTH_KEY):
            self.assertNotIn(secret, repr(result) + str(result))
            self.assertNotIn(secret.encode(), blob)

    def test_client_never_retains_key_or_name(self):
        result, exchange, profile, client = run(FakeServer(devices=devices_of(
            bare("d1", localKey=SYNTH_KEY_SENTINEL, name=sf.SENTINEL + "-n"))))
        self.assertEqual(result, ok(1, 1, 1))
        state = repr(vars(client)) + repr({k: getattr(client, k)
                                           for k in getattr(client, "__slots__", ())})
        self.assertNotIn(SYNTH_KEY_SENTINEL, state)
        self.assertNotIn(sf.SENTINEL + "-n", state)



# --------------------------------------------------------------------------
# account.discover: the production entry point (plan 2026-11-17 Task 2)
# --------------------------------------------------------------------------

# Obviously synthetic 16-character local keys and app profile values.
FEED_KEY_A = "SYNTHKEY-aaaa-01"
FEED_KEY_B = "SYNTHKEY-bbbb-02"
FEED_KEY_OTHER = "SYNTHKEY-zzzz-99"
SYNTH_APP = {
    "app_id": "SYNTHAPPID-0001", "app_secret": "SYNTHSECRET-0002",
    "cert_sha256_hex": "0123456789abcdef" * 4, "app_key": "SYNTHAPPKEY-0003",
    "package": "invalid.synthetic.app", "app_version": "0.0.0-synthetic",
    "ttid": "synthetic-ttid", "sdk_version": "0.0.1-synthetic",
    "device_core_version": "0.0.2-synthetic",
}
DEFAULT_NAME = "PetSafe FreshFeed"


def account_pkg():
    from custom_components.petsafe_freshfeed import account
    return account


class DiscoverTests(GuardedTestCase):
    def setUp(self):
        super().setUp()
        import os
        import tempfile
        # System temp directory: the profile loader needs no same-filesystem
        # location, and nothing is left under test_backend/.
        tmp = tempfile.TemporaryDirectory(prefix="petsafe-test-discover-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "app_profile.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(SYNTH_APP, handle)
        self.app = account_pkg().load_app_profile(path=path)

    def discover(self, server=None, *, request=None, profile=None, exchange=None, **kwargs):
        account = account_pkg()
        exchange = exchange or FakeExchange(server or FakeServer())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            found = account.discover(request or sf.credentials(), profile=profile or self.app,
                                     _exchange=exchange, _session=SyntheticProfile(), **kwargs)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        return found, exchange

    def test_public_surface(self):
        account = account_pkg()
        self.assertEqual(account.SUPPORTED_COUNTRIES, (("1", "Americas"), ("44", "Europe"),
                                                       ("91", "India"), ("7", "Russia")))
        self.assertIs(account.Outcome, ac.Outcome)
        self.assertIs(account.Stage, ac.Stage)
        self.assertIs(account.Reason, ac.Reason)
        self.assertEqual(account.Outcome.OK.value, "OK")
        self.assertFalse(hasattr(account.Outcome, "OFFLINE_OK"))
        for name in ("discover", "SignIn", "Discovery", "Feeder", "load_app_profile",
                     "AppProfile", "ProfileUnavailable"):
            self.assertTrue(hasattr(account, name), name)
        # The supported countries are exactly the contract's routes.
        self.assertEqual({code for code, _label in account.SUPPORTED_COUNTRIES},
                         {"1", "44", "91", "7"})
        for code, _label in account.SUPPORTED_COUNTRIES:
            boundary.select_route(code, contract())

    def test_discovery_returns_feeder_material(self):
        devices = lambda gid: {"result": [
            {"devId": "synthdev0001", "productId": sf.OBSERVED_PRODUCT,
             "localKey": FEED_KEY_A, "name": "  Kitchen feeder  "}]}
        found, exchange = self.discover(FakeServer(devices=devices))
        self.assertIs(found.outcome, ac.Outcome.OK)
        self.assertEqual((found.stage, found.reason), (ac.Stage.NONE, ac.Reason.NONE))
        self.assertEqual(len(found.feeders), 1)
        feeder = found.feeders[0]
        self.assertIsInstance(feeder, ac.Feeder)
        self.assertEqual((feeder.device_id, feeder.local_key, feeder.name),
                         ("synthdev0001", FEED_KEY_A, "Kitchen feeder"))
        self.assertIs(type(found.feeders), tuple)
        self.assertEqual([op for op, *_ in exchange.operations()],
                         ["TOKEN", "PASSWORD_LOGIN", "HOMES", "DEVICES"])
        self.assertEqual(len(exchange.password_requests()), 1)

    def test_discovery_filters_and_dedupes(self):
        """Several homes, a feeder shared by two homes, and non-FreshFeed
        devices: only product knjcty4hcaby0qaa is offered, each device ID
        once, in first-seen order; an unsupported record's key and name are
        never read."""
        per_home = {
            11: [{"devId": "feedA", "productId": sf.OBSERVED_PRODUCT, "localKey": FEED_KEY_A,
                  "name": "Feeder A"},
                 {"devId": "other1", "productId": sf.OTHER_PRODUCT, "localKey": FEED_KEY_OTHER,
                  "name": "Litter box"}],
            22: [{"devId": "feedB", "productId": sf.OBSERVED_PRODUCT, "localKey": FEED_KEY_B},
                 {"devId": "feedA", "productId": sf.OBSERVED_PRODUCT, "localKey": FEED_KEY_A,
                  "name": "Feeder A again"},
                 {"devId": "other2", "productId": sf.OTHER_PRODUCT}],
        }
        server = FakeServer(homes=homes_of(11, 22), devices=lambda gid: {"result": per_home[gid]})

        class Recording(SyntheticProfile):
            def __init__(self):
                super().__init__(record_access=True)

        sf.RecordingDict.owned = []
        try:
            account = account_pkg()
            exchange = FakeExchange(server)
            found = account.discover(sf.credentials(), profile=self.app, _exchange=exchange,
                                     _session=Recording())
            owned = list(sf.RecordingDict.owned)
        finally:
            sf.RecordingDict.owned = None
        self.assertIs(found.outcome, ac.Outcome.OK)
        self.assertEqual([(f.device_id, f.local_key, f.name) for f in found.feeders],
                         [("feedA", FEED_KEY_A, "Feeder A"), ("feedB", FEED_KEY_B, DEFAULT_NAME)])
        for owner in ("other1", "other2"):
            self.assertNotIn((owner, "localKey"), owned)
            self.assertNotIn((owner, "name"), owned)
        # Each supported record: localKey and name looked up at most once.
        self.assertEqual(owned.count(("feedB", "localKey")), 1)
        self.assertEqual(owned.count(("feedA", "localKey")), 2)  # two records, one per home

    def test_shared_feeder_key_rules(self):
        cases = {
            # The key comes from whichever record carries a valid one.
            "first_missing": ([{"localKey": "short"}, {"localKey": FEED_KEY_A}], FEED_KEY_A),
            "second_missing": ([{"localKey": FEED_KEY_A}, {}], FEED_KEY_A),
            # Two different valid keys for one device: not understood.
            "disagree": ([{"localKey": FEED_KEY_A}, {"localKey": FEED_KEY_B}], None),
        }
        for label, (extras, expected) in cases.items():
            with self.subTest(label):
                records = {g: [dict({"devId": "feedA", "productId": sf.OBSERVED_PRODUCT}, **extra)]
                           for g, extra in zip((1, 2), extras)}
                found, _ = self.discover(FakeServer(
                    homes=homes_of(1, 2), devices=lambda gid, r=records: {"result": r[gid]}))
                if expected is None:
                    self.assertIs(found.outcome, ac.Outcome.PROTOCOL_ERROR)
                    self.assertEqual(labels(found), ("DEVICES", "RECORD_SHAPE"))
                    self.assertEqual(found.feeders, ())
                else:
                    self.assertIs(found.outcome, ac.Outcome.OK)
                    self.assertEqual([f.local_key for f in found.feeders], [expected])

    def test_feeder_without_valid_local_key_is_not_offered(self):
        for value in (None, 12345678, "", "short", "a" * 17, "SYNTHKEY aaaa-01",
                      "SYNTHKEY-aaaa-0\u00e9", "SYNTHKEY-aaaa-0\n"):
            with self.subTest(kind=type(value).__name__):
                record = {"devId": "feedA", "productId": sf.OBSERVED_PRODUCT, "name": "A"}
                if value is not None:
                    record["localKey"] = value
                found, _ = self.discover(FakeServer(devices=lambda gid, r=record: {"result": [r]}))
                self.assertIs(found.outcome, ac.Outcome.OK)
                self.assertEqual(found.feeders, ())

    def test_name_bounded_and_control_chars_rejected(self):
        cases = [
            ("  Kitchen  ", "Kitchen"),
            ("x" * 64, "x" * 64),
            ("  " + "y" * 64 + "\t", "y" * 64),
            ("Caf\u00e9 feeder", "Caf\u00e9 feeder"),
            ("x" * 65, DEFAULT_NAME),
            ("", DEFAULT_NAME),
            ("   ", DEFAULT_NAME),
            ("bad\nname", DEFAULT_NAME),
            ("tab\tinside", DEFAULT_NAME),
            ("nul\x00", DEFAULT_NAME),
            ("del\x7f", DEFAULT_NAME),
            ("c1\x85inside", DEFAULT_NAME),
            ("line\u2028sep", DEFAULT_NAME),
            ("zero\u200bwidth", DEFAULT_NAME),
            ("bidi\u202e", DEFAULT_NAME),
            (123, DEFAULT_NAME),
            (None, DEFAULT_NAME),
            (["x"], DEFAULT_NAME),
        ]
        for value, expected in cases:
            with self.subTest(value=repr(value)[:20]):
                record = {"devId": "feedA", "productId": sf.OBSERVED_PRODUCT,
                          "localKey": FEED_KEY_A, "name": value}
                found, _ = self.discover(FakeServer(devices=lambda gid, r=record: {"result": [r]}))
                self.assertIs(found.outcome, ac.Outcome.OK)
                self.assertEqual([f.name for f in found.feeders], [expected])
        record = {"devId": "feedA", "productId": sf.OBSERVED_PRODUCT, "localKey": FEED_KEY_A}
        found, _ = self.discover(FakeServer(devices=lambda gid: {"result": [record]}))
        self.assertEqual([f.name for f in found.feeders], [DEFAULT_NAME])

    def test_signin_feeder_repr_and_pickle_redacted(self):
        import copy
        import dataclasses
        request = sf.credentials()
        found, _ = self.discover()
        feeder = found.feeders[0]
        secrets = (sf.EMAIL, sf.PASSWORD, sf.SENTINEL, "synthdev0001")
        for obj in (request, feeder, found):
            for text in (repr(obj), str(obj), f"{obj}", "%s" % (obj,), format(obj, "")):
                for secret in secrets:
                    self.assertNotIn(secret, text)
            for dump in (lambda: pickle.dumps(obj), lambda: pickle.dumps(obj, protocol=0),
                         lambda: copy.copy(obj), lambda: copy.deepcopy(obj)):
                with self.assertRaises(TypeError):
                    dump()
            with self.assertRaises(TypeError):
                dataclasses.asdict(obj)
            self.assertFalse(hasattr(obj, "__dict__"))
            self.assertTrue(type(obj).__slots__)
        for obj, name in ((request, "_password"), (feeder, "local_key"), (found, "feeders")):
            with self.assertRaises(AttributeError):
                setattr(obj, name, "x")
        self.assertEqual(repr(request), "<SignIn redacted>")
        self.assertEqual(repr(feeder), "<Feeder redacted>")
        self.assertEqual(repr(found), "<Discovery OK feeders=1>")
        self.assertEqual(request.country_code, sf.COUNTRY)
        self.assertFalse(hasattr(request, "email"))
        self.assertFalse(hasattr(request, "password"))
        with self.assertRaises(TypeError):
            ac.SignIn(sf.EMAIL, sf.PASSWORD)
        with self.assertRaises(TypeError):
            ac.Feeder("synthdev0001", FEED_KEY_A, "A")

    def test_failures_carry_no_feeders(self):
        cases = [
            (dict(login=Envelope(errorCode="USER_PASSWD_WRONG")), ac.Outcome.REJECTED),
            (dict(login=Envelope(errorCode="NEED_VERIFYCODE")), ac.Outcome.CHALLENGE),
            (dict(devices=lambda gid: {"result": [], "hasMore": True}), ac.Outcome.PROTOCOL_ERROR),
            (dict(homes=TimeoutError()), ac.Outcome.TIMEOUT),
        ]
        for slots, outcome in cases:
            with self.subTest(outcome.name):
                found, exchange = self.discover(FakeServer(**slots))
                self.assertIs(found.outcome, outcome)
                self.assertEqual(found.feeders, ())
                self.assertLessEqual(len(exchange.password_requests()), 1)

    def test_invalid_inputs_send_nothing(self):
        account = account_pkg()
        for kwargs, outcome in (
                (dict(request=object()), ac.Outcome.PROTOCOL_ERROR),
                (dict(request=sf.credentials(country="86")), ac.Outcome.UNSUPPORTED_ROUTE),
                (dict(request=sf.credentials(username="not-an-email")), ac.Outcome.PROTOCOL_ERROR),
                (dict(request=sf.credentials(password="")), ac.Outcome.PROTOCOL_ERROR),
                (dict(profile=object()), ac.Outcome.UNSUPPORTED_PROFILE),
                (dict(deadline_s=0), ac.Outcome.PROTOCOL_ERROR),
                (dict(deadline_s=-1.0), ac.Outcome.PROTOCOL_ERROR),
                (dict(deadline_s=float("nan")), ac.Outcome.PROTOCOL_ERROR),
                (dict(deadline_s=float("inf")), ac.Outcome.PROTOCOL_ERROR),
                (dict(deadline_s=True), ac.Outcome.PROTOCOL_ERROR),
                (dict(deadline_s=121.0), ac.Outcome.PROTOCOL_ERROR)):
            with self.subTest(sorted(kwargs)):
                exchange = FakeExchange()
                request = kwargs.pop("request", sf.credentials())
                profile = kwargs.pop("profile", self.app)
                found = account.discover(request, profile=profile, _exchange=exchange,
                                         _session=SyntheticProfile(), **kwargs)
                self.assertIs(found.outcome, outcome)
                self.assertIs(found.stage, ac.Stage.SETUP)
                self.assertEqual(found.feeders, ())
                self.assertEqual(exchange.requests, [])

    def test_each_call_one_password_request_no_retry(self):
        """discover issues a fresh per-call permit: one password-bearing
        request per call, none retried, even after a failure."""
        exchange = FakeExchange(FakeServer(login=Envelope(errorCode="USER_PASSWD_WRONG")))
        account = account_pkg()
        for _ in range(2):
            found = account.discover(sf.credentials(), profile=self.app, _exchange=exchange,
                                     _session=SyntheticProfile())
            self.assertIs(found.outcome, ac.Outcome.REJECTED)
        self.assertEqual(len(exchange.password_requests()), 2)
        self.assertEqual(len(exchange.requests), 4)

    def test_signin_is_single_use(self):
        """A SignIn is spent by its first discover, on success, failure or an
        early SETUP return: the stored password is cleared and a second
        discover with the same SignIn sends nothing and returns the fixed
        spent-budget outcome."""
        account = account_pkg()
        first_servers = {
            "success": FakeServer(),
            "rejected": FakeServer(login=Envelope(errorCode="USER_PASSWD_WRONG")),
            "transport": FakeServer(token=TimeoutError()),
        }
        for label, server in first_servers.items():
            with self.subTest(label):
                request = sf.credentials()
                first, _ = self.discover(server, request=request)
                self.assertEqual(request._password, "")
                exchange = FakeExchange()
                second = account.discover(request, profile=self.app, _exchange=exchange,
                                          _session=SyntheticProfile())
                self.assertIs(second.outcome, ac.Outcome.BUDGET_EXHAUSTED)
                self.assertEqual(labels(second), ("SETUP", "BUDGET"))
                self.assertEqual(second.feeders, ())
                self.assertEqual(exchange.requests, [])
        # An early SETUP return (invalid deadline) also spends the SignIn.
        request = sf.credentials()
        found = account.discover(request, profile=self.app, deadline_s=0,
                                 _exchange=FakeExchange(), _session=SyntheticProfile())
        self.assertIs(found.outcome, ac.Outcome.PROTOCOL_ERROR)
        self.assertEqual(request._password, "")
        exchange = FakeExchange()
        again = account.discover(request, profile=self.app, _exchange=exchange,
                                 _session=SyntheticProfile())
        self.assertIs(again.outcome, ac.Outcome.BUDGET_EXHAUSTED)
        self.assertEqual(exchange.requests, [])

    def test_concurrent_discovers_each_send_one_password_request(self):
        """Two discover calls in flight at once (separate SignIns, separate
        fake exchanges, forced to overlap at TOKEN): each makes exactly one
        password request and both succeed."""
        account = account_pkg()
        barrier = threading.Barrier(2, timeout=10)

        def overlap(request, _deadline):
            if len(exchanges_by_thread[threading.get_ident()].requests) == 1:
                barrier.wait()

        exchanges_by_thread = {}
        results = {}
        errors = []

        def worker(index):
            try:
                exchange = FakeExchange(FakeServer(), before=overlap)
                exchanges_by_thread[threading.get_ident()] = exchange
                found = account.discover(sf.credentials(), profile=self.app, _exchange=exchange,
                                         _session=SyntheticProfile())
                results[index] = (found, exchange)
            except BaseException as error:  # surfaced below
                errors.append(type(error).__name__)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [0, 1])
        for found, exchange in results.values():
            self.assertIs(found.outcome, ac.Outcome.OK)
            self.assertEqual(len(exchange.password_requests()), 1)
        self.assertFalse(barrier.broken)

    def test_concurrent_discovers_sharing_one_signin_send_one_password_request(self):
        """The same SignIn passed to two concurrent discover calls: at most
        one password request in total; the other call is the spent-budget
        outcome."""
        account = account_pkg()
        request = sf.credentials()
        start = threading.Barrier(2, timeout=10)
        results = []
        exchanges = [FakeExchange(), FakeExchange()]

        def worker(exchange):
            start.wait()
            results.append(account.discover(request, profile=self.app, _exchange=exchange,
                                            _session=SyntheticProfile()))

        threads = [threading.Thread(target=worker, args=(e,)) for e in exchanges]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(sorted(r.outcome.name for r in results), ["BUDGET_EXHAUSTED", "OK"])
        self.assertEqual(sum(len(e.password_requests()) for e in exchanges), 1)

    def test_no_registry_residue_after_discover(self):
        """After discover returns (success and failure), with the caller's
        own references dropped and gc.collect(): every prepared WireRequest
        is gone, the boundary binding, one-shot password latch and
        directed-route registries are empty, and no PasswordPermit survives."""
        import gc
        import weakref
        account = account_pkg()

        def permits():
            return sum(1 for o in gc.get_objects() if type(o) is ac.PasswordPermit)

        # Servers are built per case: a server holding a raised exception
        # instance would keep that exception's traceback (and its frames'
        # request locals) alive, which is the fake's reference, not discover's.
        for label, make in (("success", lambda: FakeServer()),
                            ("rejected", lambda: FakeServer(
                                login=Envelope(errorCode="USER_PASSWD_WRONG"))),
                            ("homes_timeout", lambda: FakeServer(homes=lambda: TimeoutError()))):
            with self.subTest(label):
                server = make()
                gc.collect()
                before_permits = permits()
                refs = []
                exchange = FakeExchange(
                    server, before=lambda request, _d: refs.append(weakref.ref(request)))
                found = account.discover(sf.credentials(), profile=self.app,
                                         _exchange=exchange, _session=SyntheticProfile())
                self.assertEqual(len(exchange.password_requests()), 1)
                del exchange, server
                gc.collect()
                self.assertTrue(refs)
                self.assertEqual([r for r in refs if r() is not None], [])
                self.assertEqual(len(boundary._BINDINGS), 0)
                self.assertEqual(len(boundary._SENT_PASSWORD), 0)
                self.assertEqual(len(boundary._DIRECTED_ROUTES), 0)
                self.assertEqual(permits(), before_permits)
                self.assertEqual(found.outcome is ac.Outcome.OK, label == "success")

    def test_fresh_import_does_no_io(self):
        """A fresh import of the account package opens no file, creates no
        socket and stats no path (os/builtins/io/socket attributes patched
        and counted; the import machinery's own posix calls are not these
        attributes and are not counted)."""
        import builtins
        import importlib
        import os
        import sys
        prefix = "custom_components.petsafe_freshfeed.account"
        saved = {name: module for name, module in sys.modules.items()
                 if name == prefix or name.startswith(prefix + ".")}
        parent = sys.modules["custom_components.petsafe_freshfeed"]
        missing = object()
        saved_attr = parent.__dict__.get("account", missing)
        calls = []

        def counting(name, original):
            def wrapper(*args, **kwargs):
                calls.append(name)
                return original(*args, **kwargs)
            return wrapper

        patches = [(builtins, "open"), (io, "open"), (os, "open"), (os, "stat"),
                   (os, "lstat"), (os, "scandir"), (os, "listdir"), (socket, "socket"),
                   (socket, "create_connection"), (socket, "getaddrinfo")]
        originals = [(owner, name, getattr(owner, name)) for owner, name in patches]
        fresh = None
        try:
            for owner, name, original in originals:
                setattr(owner, name, counting(f"{owner.__name__}.{name}", original))
            for name in saved:
                del sys.modules[name]
            fresh = importlib.import_module(prefix)
        finally:
            for owner, name, original in originals:
                setattr(owner, name, original)
            for name in [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]:
                del sys.modules[name]
            sys.modules.update(saved)
            if saved_attr is missing:
                parent.__dict__.pop("account", None)
            else:
                parent.account = saved_attr
        self.assertIsNotNone(fresh)
        self.assertIsNot(fresh, saved[prefix])
        self.assertTrue(hasattr(fresh, "discover"))
        self.assertEqual(calls, [])
        self.assertIs(sys.modules[prefix], saved[prefix])

    def test_password_never_reachable_from_failure_or_logs(self):
        """A password sentinel never appears in a discover failure result,
        its repr/str, anything reachable from it (including exception
        __context__/__cause__/__traceback__ frame locals), the SignIn after
        the call, stdout/stderr, or log records at any level."""
        import gc
        import logging
        import types
        account = account_pkg()
        secret = "FAKE-PASSWORD-SENTINEL-c41d"

        def leaky_transform(password, _public_key):
            raise RuntimeError("FAKE leak " + password)

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        def reachable_text(root):
            seen, stack, texts = set(), [root], []
            while stack and len(seen) < 20000:
                obj = stack.pop()
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                if isinstance(obj, (str, bytes)):
                    texts.append(obj if isinstance(obj, str) else obj.decode("latin-1"))
                    continue
                if isinstance(obj, (types.ModuleType, type)):
                    continue
                if isinstance(obj, BaseException):
                    stack.extend([obj.__context__, obj.__cause__, obj.__traceback__, obj.args])
                if isinstance(obj, types.TracebackType):
                    stack.extend([obj.tb_frame, obj.tb_next])
                if isinstance(obj, types.FrameType):
                    stack.extend(dict(obj.f_locals).values())
                stack.extend(gc.get_referents(obj))
            return texts

        cases = {
            "rejected": (dict(login=Envelope(errorCode="USER_PASSWD_WRONG")), None),
            "login_raises": (dict(login=RuntimeError("FAKE " + secret)), None),
            "encrypt_raises": ({}, leaky_transform),
            "token_timeout": (dict(token=TimeoutError()), None),
        }
        root_logger = logging.getLogger()
        handler = Capture(level=1)
        saved_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(1)
        try:
            for label, (slots, transform) in cases.items():
                with self.subTest(label):
                    records.clear()
                    request = sf.credentials(password=secret)
                    session = SyntheticProfile(password_transform=transform)
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        found = account.discover(request, profile=self.app,
                                                 _exchange=FakeExchange(FakeServer(**slots)),
                                                 _session=session)
                    self.assertIsNot(found.outcome, ac.Outcome.OK)
                    texts = [repr(found), str(found), f"{found}", out.getvalue(),
                             err.getvalue(), repr(request)]
                    texts += reachable_text(found)
                    texts += reachable_text(request)
                    for record in records:
                        texts.append(record.getMessage())
                        texts.append(repr(record.args))
                        if record.exc_info:
                            texts.extend(reachable_text(record.exc_info[1]))
                    for text in texts:
                        self.assertNotIn(secret, text)
                    self.assertEqual(request._password, "")
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(saved_level)

    def test_default_wiring_reaches_only_the_contract_host(self):
        """Without seams discover builds the real session and HTTPS exchange;
        an injected connection factory sees only the contract route host on
        443 with a remaining-budget timeout, and a connect failure is a
        TOKEN-stage transport outcome with no password request."""
        account = account_pkg()
        calls = []

        def factory(host, port, *, timeout, context):
            calls.append((host, port, timeout))
            raise OSError("synthetic connect failure")

        found = account.discover(sf.credentials(), profile=self.app, deadline_s=12.5,
                                 _connection_factory=factory)
        self.assertIs(found.outcome, ac.Outcome.PROTOCOL_ERROR)
        self.assertEqual(labels(found), ("TOKEN", "TRANSPORT_CONNECT"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("a1-us.iotbing.com", 443))
        self.assertTrue(0 < calls[0][2] <= 12.5)

    def test_account_package_imports(self):
        """The account package imports only stdlib modules, ``cryptography``
        and its own siblings: no Home Assistant, aiohttp, multiprocessing,
        requests or vendor code."""
        import ast
        import os
        import sys
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "custom_components", "petsafe_freshfeed", "account")
        stdlib = set(sys.stdlib_module_names)
        seen = set()
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue  # sibling module of the package
                    roots = [(node.module or "").split(".")[0]]
                else:
                    continue
                for module in roots:
                    seen.add(module)
                    self.assertTrue(module in stdlib or module == "cryptography",
                                    f"{name}: {module}")
                    self.assertNotIn(module, {"multiprocessing", "subprocess", "pickle",
                                              "aiohttp", "homeassistant", "requests"})
        self.assertIn("cryptography", seen)


if __name__ == "__main__":
    unittest.main()
