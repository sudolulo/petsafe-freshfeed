"""Offline contract coverage for the read-only DP_GET operation."""
import json
import unittest

import account_fixtures as sf
from custom_components.petsafe_freshfeed import account
from custom_components.petsafe_freshfeed.account import boundary


def setUpModule():
    sf.install_path_guards()


def tearDownModule():
    sf.remove_path_guards()


class DPGetContractTests(unittest.TestCase):
    def tearDown(self):
        self.assertEqual(sf.take_guard_hits(), 0)

    def test_shipped_contract_has_closed_dp_get(self):
        with open(boundary.DEFAULT_CONTRACT_PATH, encoding="utf-8") as stream:
            operations = json.load(stream)["operations"]
        self.assertIn("DP_GET", operations)
        self.assertEqual(operations["DP_GET"], {
            "action": "s.m.dev.dp.get", "version": "1.0", "session": "required",
            "business": [["gwId", "device_id"], ["devId", "device_id"]],
        })

    def test_public_session_restore_rejects_missing_ecode_without_request(self):
        self.assertTrue(callable(getattr(account, "restore_session", None)))
        result = account.restore_session({"sid": sf.SID}, {"country_code": "1", "region": "AZ", "url": "https://invalid", "api_domain": "SAME"})
        self.assertIsNone(result)

    def test_restored_read_is_closed_and_redacted(self):
        self.assertTrue(callable(getattr(account, "read_dps", None)))
        route = boundary.select_route("1", boundary.Contract.load())
        stored_route = {"country_code": "1", "region": route.region,
                        "url": route.url, "api_domain": route.api_domain.value}
        session = account.restore_session({"sid": sf.SID, "ecode": sf.ECODE}, stored_route)
        self.assertIsNotNone(session)
        profile = sf.SyntheticProfile()
        exchange = sf.FakeExchange()
        exchange.server.ecode = sf.ECODE
        result = account.read_dps(session, "synthdev0001", profile=None,
                                  _session=profile, _exchange=exchange)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.dp1, "AAAA")
        self.assertEqual(result.dp140.status, "idle")
        self.assertEqual(exchange.server.business, [("DP_GET", {
            "gwId": "synthdev0001", "devId": "synthdev0001"})])
        self.assertEqual(exchange.operations(), [("DP_GET", "s.m.dev.dp.get", "1.0")])
        for value in (session, result):
            for secret in sf.SECRET_STRINGS:
                self.assertNotIn(secret, repr(value))
                self.assertNotIn(secret, str(value))
        self.assertEqual(session.storage_data(), {"sid": sf.SID, "ecode": sf.ECODE})

    def test_final_wire_mutations_and_missing_session_reject(self):
        import dataclasses
        import urllib.parse
        contract = boundary.Contract.load()
        route = boundary.select_route("1", contract)
        profile = sf.SyntheticProfile()
        business = {"gwId": "synthdev0001", "devId": "synthdev0001"}
        op = boundary.Operation.DP_GET
        with self.assertRaises(boundary.BoundaryRejected):
            boundary.build_request(op, business, route=route,
                envelope=profile.prepare(op, business), contract=contract, profile=profile)
        profile._sid, profile._ecode = sf.SID, sf.ECODE
        wire = boundary.build_request(op, business, route=route,
            envelope=profile.prepare(op, business), contract=contract, profile=profile)
        self.assertEqual(dict(wire.headers), {"Content-Type": "application/x-www-form-urlencoded"})
        pairs = urllib.parse.parse_qsl(wire.body.decode("ascii"))
        for field, value in (("a", "s.m.dev.dp.publish"), ("v", "2.0"), ("sid", "")):
            body = urllib.parse.urlencode([(k, value if k == field else v) for k, v in pairs]).encode()
            changed = dataclasses.replace(wire, body=body)
            with self.assertRaises(boundary.BoundaryRejected):
                boundary.validate_request(changed, operation=op, expected_business=business,
                                          route=route, contract=contract)

    def test_directed_route_and_host_revalidated_at_final_boundary(self):
        import dataclasses
        contract = boundary.Contract.load()
        base = boundary.select_route("1", contract)
        directed = boundary.directed_route("https://a1.tuyaeu.com", base, contract)
        profile = sf.SyntheticProfile()
        profile._sid, profile._ecode = sf.SID, sf.ECODE
        business = {"gwId": "synthdev0001", "devId": "synthdev0001"}
        op = boundary.Operation.DP_GET
        wire = boundary.build_request(op, business, route=directed,
            envelope=profile.prepare(op, business), contract=contract, profile=profile)
        boundary.validate_request(wire, operation=op, expected_business=business,
                                  route=directed, contract=contract)
        for changed, route in ((wire, base),
                (dataclasses.replace(wire, url=base.url), directed)):
            with self.subTest(route=route.url), self.assertRaises(boundary.BoundaryRejected):
                boundary.validate_request(changed, operation=op, expected_business=business,
                                          route=route, contract=contract)

    def test_secret_exception_chain_does_not_escape_public_api(self):
        import traceback
        secrets = sf.SID + sf.ECODE + sf.PASSWORD
        def fail(_request, _deadline):
            try:
                raise ValueError(secrets)
            except ValueError as cause:
                raise RuntimeError(secrets) from cause
        exchange = sf.FakeExchange(before=fail)
        result = account.read_dps(self.restored(), "synthdev0001", profile=None,
                                  _session=sf.SyntheticProfile(), _exchange=exchange)
        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual(len(exchange.requests), 1)
        try:
            raise RuntimeError("caller failure")
        except RuntimeError as exc:
            text = "".join(traceback.format_exception(exc))
        for secret in sf.SECRET_STRINGS:
            self.assertNotIn(secret, repr(result) + str(result) + text)

    def restored(self):
        route = boundary.select_route("1", boundary.Contract.load())
        return account.restore_session({"sid": sf.SID, "ecode": sf.ECODE}, {
            "country_code": "1", "region": route.region, "url": route.url,
            "api_domain": route.api_domain.value})

    def test_restore_corruption_and_routes(self):
        valid = self.restored()
        for bad in (None, "", True, 12, "x" * 1025, "bad\nvalue", {}, []):
            for key in ("sid", "ecode"):
                with self.subTest(key=key, bad=bad):
                    stored = valid.storage_data()
                    stored[key] = bad
                    self.assertIsNone(account.restore_session(stored, valid.storage_route()))
        for key, bad in (("url", "https://example.invalid/api.json"),
                         ("region", "XX"), ("api_domain", "bad"),
                         ("country_code", "999")):
            route = valid.storage_route()
            route[key] = bad
            self.assertIsNone(account.restore_session(valid.storage_data(), route))

    def test_payload_outcomes_and_no_secret_logs(self):
        import logging
        import io
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        cases = [({"errorCode": "USER_SESSION_INVALID"}, "session_rejected"),
                 ({"errorCode": "API_NOT_EXIST"}, "malformed"),
                 ({"errorCode": "SIGN_INVALID"}, "malformed"),
                 ({"errorCode": "USER_PASSWD_WRONG"}, "malformed"),
                 ({"errorCode": "UNKNOWN_ERROR"}, "malformed"),
                 ({"result": {"140": "IAAA", "1": "plan", "secret": sf.SID}}, "ok"),
                 ({"140": "garbage"}, "ok"),
                 ({"140": True}, "ok"),
                 ({"1": "x" * 4097}, "malformed"),
                 ({"1": False}, "malformed"),
                 ({"unrecognized": sf.SID}, "malformed"),
                 ({"success": False, "errorMsg": sf.SID}, "malformed"),
                 (sf.Raw(b"x" * (1024 * 1024 + 1)), "malformed"),
                 (RuntimeError(sf.SID + sf.ECODE + sf.PASSWORD), "unavailable"),
                 ({"result": {"1": "ok"}, "mobileApiUrl": "https://example.invalid"}, "malformed")]
        for payload, outcome in cases:
            with self.subTest(outcome=outcome, payload_type=type(payload)):
                exchange = sf.FakeExchange(sf.FakeServer(dp_get=payload))
                exchange.server.ecode = sf.ECODE
                result = account.read_dps(self.restored(), "synthdev0001", profile=None,
                    _session=sf.SyntheticProfile(), _exchange=exchange)
                self.assertEqual(result.outcome, outcome)
                self.assertEqual(len(exchange.requests), 1)
                for secret in sf.SECRET_STRINGS:
                    self.assertNotIn(secret, repr(result) + str(result) + stream.getvalue())

    def test_invalid_dp1_preserves_simultaneous_dp140_safety_evidence(self):
        for dp1 in (False, "x" * 4097):
            for raw140, expected_status in (("EAEC", "pending"), ("garbage", None)):
                with self.subTest(dp1_type=type(dp1), raw140=raw140):
                    exchange = sf.FakeExchange(sf.FakeServer(dp_get={"1": dp1, "140": raw140}))
                    exchange.server.ecode = sf.ECODE
                    result = account.read_dps(self.restored(), "synthdev0001", profile=None,
                        _session=sf.SyntheticProfile(), _exchange=exchange)
                    self.assertEqual(result.outcome, "malformed")
                    self.assertIsNone(result.dp1)
                    self.assertTrue(result.dp140_present)
                    self.assertIsNotNone(result.dp140)
                    self.assertEqual(result.dp140.status, expected_status)

    def test_invalid_session_or_device_sends_nothing(self):
        exchange = sf.FakeExchange()
        for session, device in ((None, "synthdev0001"), (self.restored(), "bad space")):
            result = account.read_dps(session, device, profile=None,
                                     _session=sf.SyntheticProfile(), _exchange=exchange)
            self.assertIn(result.outcome, ("session_rejected", "malformed"))
        self.assertEqual(exchange.requests, [])

    def test_device_id_validation_and_matching(self):
        contract = boundary.Contract.load()
        route = boundary.select_route("1", contract)
        for value in ("a", "a_-", "x" * 64):
            business = {"gwId": value, "devId": value}
            self.assertEqual(boundary._check_business(boundary.Operation.DP_GET, business, route, contract), business)
        for value in (True, 1, "", "x" * 65, "bad space"):
            with self.subTest(value=value), self.assertRaises(boundary.BoundaryRejected):
                boundary._check_business(boundary.Operation.DP_GET, {"gwId": value, "devId": value}, route, contract)
        for business in ({"gwId": "a", "devId": "b"}, {"gwId": "a", "devId": "a", "extra": 1}):
            with self.subTest(business=business), self.assertRaises(boundary.BoundaryRejected):
                boundary._check_business(boundary.Operation.DP_GET, business, route, contract)
