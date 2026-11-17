"""Config flow tests for the "Sign in with PetSafe" path (Task 6).

Synthetic values only: no network, no real Tuya Local, no real device. The
account client, the LAN locator and the Tuya Local assist are patched at the
names ``config_flow`` uses.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import SelectSelector, TextSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import SOURCE_ENTRY_ID, make_source_entry

DOMAIN = "petsafe_freshfeed"
TL_DOMAIN = "tuya_local"
CF = "custom_components.petsafe_freshfeed.config_flow"

FAKE_EMAIL = "synthetic-sentinel-mail@example.invalid"
FAKE_PASSWORD = "SyntheticSentinelPw-4242"
FAKE_KEY = "SynthKeySentinl1"
FAKE_DEVICE_ID = "synthdevicesentinel0001"
FAKE_DEVICE_ID_2 = "synthdevicesentinel0002"
FAKE_HOST = "192.168.77.231"
FAKE_NAME = "SYNTH Feeder"
SENTINELS = (FAKE_EMAIL, FAKE_PASSWORD, FAKE_KEY, FAKE_DEVICE_ID, FAKE_DEVICE_ID_2, FAKE_HOST)
TL_ENTRY_ID = "synthetic_new_tl_entry"
PROFILE = object()


def _account():
    from custom_components.petsafe_freshfeed import account
    return account


def _feeder(device_id=FAKE_DEVICE_ID, name=FAKE_NAME):
    from custom_components.petsafe_freshfeed.account.client import Feeder
    return Feeder._make(device_id, FAKE_KEY, name)


def _ok(*feeders):
    account = _account()
    return account.Discovery(account.Outcome.OK, feeders)


def _fail(outcome_name):
    account = _account()
    return account.Discovery(account.Outcome[outcome_name], (),
                             stage=account.Stage.LOGIN, reason=account.Reason.SERVER_ERROR_CODE)


def _located():
    from custom_components.petsafe_freshfeed.lan_locate import Located
    return Located(host=FAKE_HOST, version="3.3")


def _make_tl_entry(hass, entry_id=TL_ENTRY_ID, device_id=FAKE_DEVICE_ID, *, dp145=True):
    entry = MockConfigEntry(domain=TL_DOMAIN, title=FAKE_NAME, entry_id=entry_id,
                            data={"type": "petsafe_freshfeed_raw", "device_id": device_id})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    if dp145:
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(TL_DOMAIN, device_id)}, name=FAKE_NAME)
        er.async_get(hass).async_get_or_create(
            "sensor", TL_DOMAIN, f"{device_id}-sensor_dp_145_raw",
            config_entry=entry, device_id=device.id)
    return entry


class Env:
    """Patched collaborators plus a controllable monotonic clock."""

    def __init__(self, hass):
        self.hass = hass
        self.now = 1000.0
        self.discovery = _ok(_feeder())
        self.requests = []
        self.passwords = []
        self.located = _located()
        self.locate_calls = []
        self.load_calls = 0
        self.profile_error = None
        self.dp145 = True
        self.create_error = None
        self.create_side = None

    def load_app_profile(self, *args, **kwargs):
        self.load_calls += 1
        if self.profile_error is not None:
            raise self.profile_error
        return PROFILE

    def discover(self, request, *, profile, deadline_s=30.0):
        assert profile is PROFILE
        self.requests.append(request)
        self.passwords.append(request._password)
        return self.discovery

    def locate(self, device_id, **kwargs):
        self.locate_calls.append(device_id)
        return self.located

    async def create(self, hass, feeder, located, host, *, name):
        entry = _make_tl_entry(hass, device_id=feeder.device_id, dp145=self.dp145)
        if self.create_side is not None:
            self.create_side(hass, entry)
        if self.create_error is not None:
            from custom_components.petsafe_freshfeed.tuya_local_assist import AssistError
            raise AssistError(self.create_error)
        return entry.entry_id


@pytest.fixture(autouse=True)
async def mock_setup_entry(hass):
    with patch(
        "custom_components.petsafe_freshfeed.async_setup_entry", AsyncMock(return_value=True)
    ) as mock, patch(
        "custom_components.petsafe_freshfeed.async_unload_entry", AsyncMock(return_value=True)
    ):
        yield mock
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
def env(hass):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistStatus
    state = Env(hass)
    state.preflight = AsyncMock(return_value=AssistStatus.READY)
    state.install = AsyncMock(return_value=None)
    state.existing = AsyncMock(return_value=None)
    state.create_mock = AsyncMock(side_effect=state.create)
    from custom_components.petsafe_freshfeed import tuya_local_assist
    state.real_create = tuya_local_assist.async_create_source_entry  # before patching
    with patch(f"{CF}.account.load_app_profile", side_effect=state.load_app_profile), \
            patch(f"{CF}.account.discover", side_effect=state.discover) as discover, \
            patch(f"{CF}.lan_locate.locate", side_effect=state.locate), \
            patch(f"{CF}.tuya_local_assist.async_preflight", state.preflight), \
            patch(f"{CF}.tuya_local_assist.async_install_profile", state.install), \
            patch(f"{CF}.tuya_local_assist.async_existing_source", state.existing), \
            patch(f"{CF}.tuya_local_assist.async_create_source_entry", state.create_mock), \
            patch(f"{CF}._monotonic", side_effect=lambda: state.now):
        state.discover_mock = discover
        yield state


async def _init(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})


async def _configure(hass, result, data):
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


def _signin(country="1"):
    return {"country": country, "email": FAKE_EMAIL, "password": FAKE_PASSWORD}


async def _to_account(hass):
    result = await _init(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["account", "existing"]
    result = await _configure(hass, result, {"next_step_id": "account"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "account"
    return result


def _flow(hass, result):
    return hass.config_entries.flow._progress[result["flow_id"]]


def _no_sentinels(text):
    for sentinel in SENTINELS:
        assert sentinel not in text, sentinel


def _check_our_entries(hass):
    for entry in hass.config_entries.async_entries(DOMAIN):
        _no_sentinels(repr(dict(entry.data)) + repr(dict(entry.options)) + entry.title
                      + repr(entry.unique_id))


# --- tests ----------------------------------------------------------------------


async def test_account_path_creates_both_entries(hass, env):
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    assert result["description_placeholders"]["name"] == FAKE_NAME
    assert [str(key) for key in result["data_schema"].schema] == ["enable_controls"]
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": TL_ENTRY_ID}
    assert result["title"] == FAKE_NAME
    assert result["result"].unique_id == TL_ENTRY_ID
    assert len(hass.config_entries.async_entries(TL_DOMAIN)) == 1
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    env.create_mock.assert_awaited_once()
    args, kwargs = env.create_mock.await_args
    assert args[1].device_id == FAKE_DEVICE_ID and args[2] is env.located and args[3] is None
    assert kwargs == {"name": FAKE_NAME}
    env.install.assert_not_awaited()
    assert env.discover_mock.call_count == 1
    assert env.locate_calls == [FAKE_DEVICE_ID]


async def test_account_path_with_controls_keeps_existing_keys(hass, env, tmp_path, monkeypatch):
    from custom_components.petsafe_freshfeed import store_bootstrap
    provision = AsyncMock(return_value=None)
    monkeypatch.setattr(store_bootstrap, "async_provision_store", provision)
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {"enable_controls": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(result["data"]) == {"source_entry_id", "intent_store_id", "intent_binding_id"}
    assert result["data"]["source_entry_id"] == TL_ENTRY_ID
    provision.assert_awaited_once()
    _check_our_entries(hass)


@pytest.mark.parametrize(("outcome", "key"), [
    ("REJECTED", "invalid_auth"),
    ("CHALLENGE", "challenge_required"),
    ("UNSUPPORTED_ROUTE", "unsupported_region"),
    ("UNSUPPORTED_PROFILE", "unsupported_region"),
    ("TIMEOUT", "timeout"),
    ("PROTOCOL_ERROR", "cannot_connect"),
    ("LIMIT_EXCEEDED", "cannot_connect"),
    ("BUDGET_EXHAUSTED", "cannot_connect"),
    ("STARTUP_FAILED", "cannot_connect"),
    ("CANCELLED", "cannot_connect"),
    ("CLEANUP_FAILED", "cannot_connect"),
    ("OK", "no_feeders"),
])
async def test_account_errors_are_terminal_form_errors(hass, env, outcome, key):
    env.discovery = _ok() if outcome == "OK" else _fail(outcome)
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "account"
    assert result["errors"] == {"base": key}
    assert env.discover_mock.call_count == 1
    assert "SERVER_ERROR_CODE" not in repr(result) and "LOGIN" not in repr(result)
    env.create_mock.assert_not_awaited()
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_discover_exception_is_cannot_connect(hass, env):
    env.discover_mock.side_effect = RuntimeError("server text " + FAKE_EMAIL)
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["errors"] == {"base": "cannot_connect"}
    _no_sentinels(repr(result))


async def test_submit_cooldown(hass, env):
    env.discovery = _fail("REJECTED")
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["errors"] == {"base": "invalid_auth"}
    env.now += 59.0
    result = await _configure(hass, result, _signin())
    assert result["errors"] == {"base": "rate_limited"}
    assert env.discover_mock.call_count == 1
    env.now += 1.5
    env.discovery = _ok(_feeder())
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    assert env.discover_mock.call_count == 2


async def test_cooldown_blocks_before_discover(hass, env):
    # A successful sign-in also starts the cooldown, and it is per hass, not per flow.
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    assert env.discover_mock.call_count == 1
    env.now += 10.0
    other = await _to_account(hass)
    other = await _configure(hass, other, _signin())
    assert other["type"] is FlowResultType.FORM and other["errors"] == {"base": "rate_limited"}
    assert env.discover_mock.call_count == 1


async def test_new_signin_per_submit(hass, env):
    env.discovery = _fail("TIMEOUT")
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin("44"))
    env.now += 61.0
    result = await _configure(hass, result, _signin("44"))
    assert env.discover_mock.call_count == 2
    first, second = env.requests
    account = _account()
    assert type(first) is account.SignIn and type(second) is account.SignIn
    assert first is not second
    assert first.country_code == "44" and second.country_code == "44"
    assert env.passwords == [FAKE_PASSWORD, FAKE_PASSWORD]


async def test_password_not_in_flow_results_or_entries(hass, env):
    env.discovery = _fail("REJECTED")
    results = [await _to_account(hass)]
    results.append(await _configure(hass, results[-1], _signin()))
    flow = _flow(hass, results[-1])
    env.now += 61.0
    env.discovery = _ok(_feeder())
    results.append(await _configure(hass, results[-1], _signin()))
    flow_state = repr(vars(flow))
    assert FAKE_PASSWORD not in flow_state and FAKE_EMAIL not in flow_state
    results.append(await _configure(hass, results[-1], {}))
    assert results[-1]["type"] is FlowResultType.CREATE_ENTRY
    for result in results:
        assert FAKE_PASSWORD not in repr(result)
    assert FAKE_PASSWORD not in repr(flow.context)
    _check_our_entries(hass)


async def test_no_account_data_in_entries_or_context(hass, env, caplog):
    caplog.set_level(logging.DEBUG)
    env.located = None
    env.discovery = _ok(_feeder(), _feeder(FAKE_DEVICE_ID_2, "SYNTH Second"))
    results = [await _to_account(hass)]
    results.append(await _configure(hass, results[-1], _signin()))
    assert results[-1]["step_id"] == "feeder"
    flow = _flow(hass, results[-1])
    results.append(await _configure(hass, results[-1], {"feeder": "0"}))
    assert results[-1]["step_id"] == "host"
    results.append(await _configure(hass, results[-1], {"host": "8.8.8.8"}))
    results.append(await _configure(hass, results[-1], {"host": FAKE_HOST}))
    assert results[-1]["step_id"] == "confirm"
    contexts = [repr(flow.context)]
    results.append(await _configure(hass, results[-1], {}))
    assert results[-1]["type"] is FlowResultType.CREATE_ENTRY
    for result in results:
        text = repr({key: value for key, value in result.items() if key != "result"})
        _no_sentinels(text)
    _no_sentinels("".join(contexts) + repr(flow.context))
    _check_our_entries(hass)
    _no_sentinels(caplog.text)
    assert flow._feeders == () and flow._feeder is None and flow._host is None


async def test_existing_path_through_menu(hass, env, source_entry):
    result = await _init(hass)
    assert result["type"] is FlowResultType.MENU
    result = await _configure(hass, result, {"next_step_id": "existing"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "existing"
    assert [str(key) for key in result["data_schema"].schema] == ["enable_controls"]
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": SOURCE_ENTRY_ID}
    assert result["result"].unique_id == SOURCE_ENTRY_ID
    assert env.discover_mock.call_count == 0


async def test_existing_path_multiple_sources_through_menu(hass, env, source_entry):
    second = make_source_entry(hass, entry_id="01M5NATKW5YS05D36KGFB38D8Q", title="FreshFeed Two")
    result = await _init(hass)
    result = await _configure(hass, result, {"next_step_id": "existing"})
    assert result["step_id"] == "existing"
    assert [str(key) for key in result["data_schema"].schema] == ["source_entry_id", "enable_controls"]
    result = await _configure(hass, result, {"source_entry_id": second.entry_id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": second.entry_id}


async def test_no_menu_without_app_profile(hass, env, source_entry):
    env.profile_error = _account().ProfileUnavailable()
    first = await _init(hass)
    assert first["type"] is FlowResultType.FORM and first["step_id"] == "user"
    hass.config_entries.flow.async_abort(first["flow_id"])
    # The profile check is cached per hass: a second flow reads nothing again.
    result = await _init(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert env.load_calls == 1
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": SOURCE_ENTRY_ID}


async def test_profile_check_cached_when_present(hass, env):
    await _init(hass)
    await _init(hass)
    assert env.load_calls == 1


async def test_profile_loaded_in_executor(hass, env):
    import threading
    threads = []
    original = env.load_app_profile

    def record(*args, **kwargs):
        threads.append(threading.current_thread() is threading.main_thread())
        return original(*args, **kwargs)
    # A plain function, not a Mock: the test harness runs Mock executor jobs inline.
    with patch(f"{CF}.account.load_app_profile", new=record):
        await _init(hass)
    assert threads == [False]


async def test_assist_failure_leaves_no_petsafe_entry(hass, env):
    env.create_error = "connection_failed"
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    flow = _flow(hass, result)
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "connection_failed"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert flow._feeders == () and flow._feeder is None


async def test_entry_not_loaded_reports_kept_tl_entry(hass, env):
    env.create_error = "entry_not_loaded"
    env.now += 0
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tl_entry_kept_not_loaded"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert [entry.entry_id for entry in hass.config_entries.async_entries(TL_DOMAIN)] == [TL_ENTRY_ID]


@pytest.mark.parametrize(("status", "reason"), [
    ("TUYA_LOCAL_MISSING", "tuya_local_missing"),
    ("TUYA_LOCAL_UNSUPPORTED", "tuya_local_unsupported"),
    ("PROFILE_CONFLICT", "profile_conflict"),
])
async def test_preflight_refusals_abort_before_sign_in(hass, env, status, reason):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistStatus
    env.preflight.return_value = AssistStatus[status]
    result = await _init(hass)
    result = await _configure(hass, result, {"next_step_id": "account"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == reason
    assert env.discover_mock.call_count == 0


async def test_preflight_refusal_after_sign_in_aborts(hass, env):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistStatus
    result = await _to_account(hass)
    env.preflight.return_value = AssistStatus.PROFILE_CONFLICT
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "profile_conflict"
    env.existing.assert_not_awaited()


async def test_profile_needed_installs_at_confirm(hass, env):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistStatus
    env.preflight.return_value = AssistStatus.PROFILE_NEEDED
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["profile"] != ""
    env.install.assert_not_awaited()
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    env.install.assert_awaited_once()


async def test_profile_install_failure_aborts(hass, env):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistError, AssistStatus
    env.preflight.return_value = AssistStatus.PROFILE_NEEDED
    env.install.side_effect = AssistError("profile_install_failed")
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "profile_install_failed"
    env.create_mock.assert_not_awaited()
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_existing_source_refusal_aborts(hass, env):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistError
    env.existing.side_effect = AssistError("already_configured_other")
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured_other"
    assert env.locate_calls == []


@pytest.mark.parametrize("host", [
    "8.8.8.8", "127.0.0.1", "169.254.10.1", "100.64.0.1", "100.127.255.1", "0.0.0.0",
    "224.0.0.1", "255.255.255.255", "fe80::1", "::1", "fd00::1", "::ffff:192.168.1.5",
    "not-an-ip", "192.168.1", "192.168.1.300", "", " ",
])
async def test_host_step_rejects_public_and_non_ipv4(hass, env, host):
    env.located = None
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "host"
    result = await _configure(hass, result, {"host": host})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "host"
    assert result["errors"] == {"host": "invalid_host"}
    env.create_mock.assert_not_awaited()


@pytest.mark.parametrize("host", ["10.0.0.5", "172.16.4.4", "172.31.255.254", FAKE_HOST])
async def test_host_step_accepts_rfc1918(hass, env, host):
    env.located = None
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {"host": host})
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    args, _kwargs = env.create_mock.await_args
    assert args[2] is None and args[3] == host


async def test_feeder_options_are_indexes(hass, env):
    env.discovery = _ok(_feeder(), _feeder(FAKE_DEVICE_ID_2, "SYNTH Second"))
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "feeder"
    selector = result["data_schema"].schema["feeder"]
    assert isinstance(selector, SelectSelector)
    options = selector.config["options"]
    assert [option["value"] for option in options] == ["0", "1"]
    assert [option["label"] for option in options] == [FAKE_NAME, "SYNTH Second"]
    _no_sentinels(repr(result))
    result = await _configure(hass, result, {"feeder": "1"})
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["name"] == "SYNTH Second"
    assert env.locate_calls == [FAKE_DEVICE_ID_2]
    env.existing.assert_awaited_once()
    assert env.existing.await_args.args[1] == FAKE_DEVICE_ID_2


async def test_reused_source_already_configured_aborts_before_assist(hass, env):
    tl = _make_tl_entry(hass, entry_id="synthetic_reused_tl")
    MockConfigEntry(domain=DOMAIN, unique_id=tl.entry_id,
                    data={"source_entry_id": tl.entry_id}).add_to_hass(hass)
    env.existing.return_value = tl.entry_id
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert env.locate_calls == []
    env.create_mock.assert_not_awaited()
    env.install.assert_not_awaited()


async def test_reused_source_creates_entry_without_assist(hass, env):
    tl = _make_tl_entry(hass, entry_id="synthetic_reused_tl")
    env.existing.return_value = tl.entry_id
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": tl.entry_id}
    assert env.locate_calls == []
    env.create_mock.assert_not_awaited()


async def test_reused_source_without_dp145_aborts_binding_unavailable(hass, env):
    tl = _make_tl_entry(hass, entry_id="synthetic_reused_tl", dp145=False)
    env.existing.return_value = tl.entry_id
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "binding_unavailable"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_late_abort_reports_kept_tl_entry(hass, env, monkeypatch):
    from custom_components.petsafe_freshfeed import store_bootstrap

    async def fail(*args, **kwargs):
        raise store_bootstrap.StoreBootstrapError()
    monkeypatch.setattr(store_bootstrap, "async_provision_store", fail)
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    flow = _flow(hass, result)
    result = await _configure(hass, result, {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tl_entry_kept_provision_failed"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert [entry.entry_id for entry in hass.config_entries.async_entries(TL_DOMAIN)] == [TL_ENTRY_ID]
    assert flow._feeders == () and flow._feeder is None


async def test_late_binding_abort_reports_kept_tl_entry(hass, env):
    env.dp145 = False
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tl_entry_kept_binding_unavailable"
    assert len(hass.config_entries.async_entries(TL_DOMAIN)) == 1


async def test_late_already_configured_reports_kept_tl_entry(hass, env):
    def add_ours(hass, entry):
        MockConfigEntry(domain=DOMAIN, unique_id=entry.entry_id,
                        data={"source_entry_id": entry.entry_id}).add_to_hass(hass)
    env.create_side = add_ours
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tl_entry_kept_already_configured"
    assert len(hass.config_entries.async_entries(TL_DOMAIN)) == 1


async def test_password_selector_and_no_refill(hass, env):
    env.discovery = _fail("REJECTED")
    result = await _to_account(hass)
    schema = result["data_schema"].schema
    assert [str(key) for key in schema] == ["country", "email", "password"]
    country = schema["country"]
    assert isinstance(country, SelectSelector)
    from custom_components.petsafe_freshfeed.account import SUPPORTED_COUNTRIES
    assert [(o["value"], o["label"]) for o in country.config["options"]] == list(SUPPORTED_COUNTRIES)
    assert isinstance(schema["email"], TextSelector) and schema["email"].config["type"] == "email"
    assert isinstance(schema["password"], TextSelector)
    assert schema["password"].config["type"] == "password"
    result = await _configure(hass, result, _signin("44"))
    assert result["errors"] == {"base": "invalid_auth"}
    keys = {str(key): key for key in result["data_schema"].schema}
    password_key = keys["password"]
    assert password_key.default is vol_undefined()
    assert not (password_key.description or {}).get("suggested_value")
    assert FAKE_PASSWORD not in repr(result)


def vol_undefined():
    import voluptuous as vol
    return vol.UNDEFINED


async def test_blank_credentials_do_not_call_discover(hass, env):
    result = await _to_account(hass)
    result = await _configure(hass, result, {"country": "1", "email": " ", "password": ""})
    assert result["errors"] == {"base": "invalid_auth"}
    assert env.discover_mock.call_count == 0
    # A blank submit does not start the cooldown.
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"


async def test_flow_removal_clears_feeders(hass, env):
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    flow = _flow(hass, result)
    assert flow._feeder is not None
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert flow._feeders == () and flow._feeder is None and flow._located is None


# --- review follow-ups ------------------------------------------------------------

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INTEGRATION = REPO / "custom_components" / DOMAIN
OS_SENTINEL = "FAKE_KEY_SENTINEL"
_PLUGIN_ENV = "PETSAFE_SYNTH_PROFILE_PLUGIN"
_PLUGIN = '''
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def synth_valid_app_profile():
    # A loadable app profile everywhere, as if app_profile.json were bundled.
    with patch("custom_components.petsafe_freshfeed.account.load_app_profile",
               return_value=object()):
        yield
'''


def _cf():
    from custom_components.petsafe_freshfeed import config_flow
    return config_flow


@pytest.mark.skipif(not os.environ.get(_PLUGIN_ENV), reason="only inside the isolation proof run")
async def test_plugin_profile_reaches_flow(hass, source_entry):
    # Runs only in the subprocess below: proves the synthetic profile is effective.
    result = await _init(hass)
    assert result["type"] is FlowResultType.MENU


async def test_menu_shown_when_profile_loads(hass, source_entry):
    with patch(f"{CF}.account.load_app_profile", return_value=PROFILE):
        result = await _init(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["account", "existing"]


def test_existing_suite_isolated_from_bundled_profile(tmp_path):
    (tmp_path / "synth_profile_plugin.py").write_text(_PLUGIN)
    environ = dict(os.environ)
    environ["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(tmp_path), environ.get("PYTHONPATH")]))
    environ[_PLUGIN_ENV] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--asyncio-mode=auto", "-p", "no:cacheprovider",
         "-p", "synth_profile_plugin", "test_ha/test_config_flow.py",
         "test_ha/test_config_flow_account.py::test_plugin_profile_reaches_flow"],
        cwd=REPO, env=environ, capture_output=True, text=True, timeout=600, check=False)
    tail = proc.stdout[-3000:] + proc.stderr[-2000:]
    assert proc.returncode == 0, tail
    assert "skipped" not in proc.stdout.splitlines()[-1], tail


async def test_cooldown_stamp_precedes_discover_concurrently(hass, env):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_discover(request, *, profile, deadline_s=30.0):
        calls.append(threading.current_thread())
        entered.set()
        if not release.wait(10):
            raise RuntimeError("SYNTH gate timeout")
        return env.discovery

    async def releaser():
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        # Give the other submit time to reach (and pass or fail) the cooldown check.
        await asyncio.sleep(0.1)
        release.set()

    first = await _to_account(hass)
    second = await _to_account(hass)
    with patch(f"{CF}.account.discover", new=blocking_discover):
        one, two, _ = await asyncio.gather(
            _configure(hass, first, _signin()), _configure(hass, second, _signin()), releaser())
    assert len(calls) == 1
    assert calls[0] is not threading.current_thread()
    outcomes = sorted([one.get("step_id"), two.get("step_id")])
    assert outcomes == ["account", "confirm"]
    limited = one if one["step_id"] == "account" else two
    assert limited["errors"] == {"base": "rate_limited"}


async def test_discover_exception_still_burns_cooldown(hass, env):
    calls = []

    def failing_discover(request, *, profile, deadline_s=30.0):
        calls.append(1)
        raise OSError(OS_SENTINEL)

    result = await _to_account(hass)
    with patch(f"{CF}.account.discover", new=failing_discover):
        result = await _configure(hass, result, _signin())
        assert result["errors"] == {"base": "cannot_connect"}
        env.now += 30.0
        result = await _configure(hass, result, _signin())
    assert result["errors"] == {"base": "rate_limited"}
    assert len(calls) == 1
    assert OS_SENTINEL not in repr(result)


async def test_confirm_reentrant_submit_has_no_side_effects(hass, env):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistStatus
    env.preflight.return_value = AssistStatus.PROFILE_NEEDED
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def gated_create(hass, feeder, located, host, *, name):
        entered.set()
        await gate.wait()
        return await env.create(hass, feeder, located, host, name=name)
    env.create_mock.side_effect = gated_create
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    first = asyncio.create_task(_configure(hass, result, {}))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(_configure(hass, result, {}))
    try:
        await asyncio.sleep(0.05)
        create_while_running = env.create_mock.await_count
        install_while_running = env.install.await_count
    finally:
        gate.set()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 5)
    assert (create_while_running, install_while_running) == (1, 1)
    assert second_result["type"] is FlowResultType.FORM
    assert second_result["step_id"] == "confirm"
    assert first_result["type"] is FlowResultType.CREATE_ENTRY
    assert env.create_mock.await_count == 1
    assert env.install.await_count == 1
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert len(hass.config_entries.async_entries(TL_DOMAIN)) == 1


async def test_feeder_pick_drops_other_feeders(hass, env):
    env.discovery = _ok(_feeder(), _feeder(FAKE_DEVICE_ID_2, "SYNTH Second"))
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "feeder"
    flow = _flow(hass, result)
    result = await _configure(hass, result, {"feeder": "1"})
    assert result["step_id"] == "confirm"
    assert flow._feeders == ()
    assert flow._feeder.device_id == FAKE_DEVICE_ID_2


@pytest.mark.parametrize("stage", ["before_tl_entry", "after_tl_entry"])
async def test_unexpected_confirm_exception_aborts_without_text(hass, env, monkeypatch, stage):
    from custom_components.petsafe_freshfeed import store_bootstrap
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistStatus

    async def fail(*args, **kwargs):
        raise OSError(OS_SENTINEL)
    if stage == "before_tl_entry":
        env.preflight.return_value = AssistStatus.PROFILE_NEEDED
        env.install.side_effect = fail
        expected, tl_count = "aborted", 0
    else:
        monkeypatch.setattr(store_bootstrap, "async_provision_store", fail)
        expected, tl_count = "tl_entry_kept_aborted", 1
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    flow = _flow(hass, result)
    result = await _configure(hass, result, {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == expected
    assert OS_SENTINEL not in repr(result)
    _no_sentinels(repr(result))
    assert flow._feeder is None and flow._feeders == ()
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert len(hass.config_entries.async_entries(TL_DOMAIN)) == tl_count


async def test_preflight_checked_on_every_account_submit(hass, env):
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistStatus
    result = await _to_account(hass)
    env.preflight.return_value = AssistStatus.TUYA_LOCAL_UNSUPPORTED
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tuya_local_unsupported"
    assert env.discover_mock.call_count == 0
    assert _cf()._SUBMIT_AT_KEY not in hass.data.get(DOMAIN, {})


async def test_ignored_own_entry_does_not_block_reuse(hass, env):
    tl = _make_tl_entry(hass, entry_id="synthetic_reused_tl")
    MockConfigEntry(domain=DOMAIN, unique_id=tl.entry_id, source=config_entries.SOURCE_IGNORE,
                    data={}).add_to_hass(hass)
    env.existing.return_value = tl.entry_id
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": tl.entry_id}
    env.create_mock.assert_not_awaited()
    assert env.locate_calls == []


def test_every_flow_reason_has_a_string():
    from custom_components.petsafe_freshfeed import tuya_local_assist
    config_flow = _cf()
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())
    assert strings == english
    abort = strings["config"]["abort"]
    error = strings["config"]["error"]
    # "entry_not_loaded" is raised only by the source-entry creation, and the flow
    # always reports it in its kept form; every other assist reason is shown as is.
    assert config_flow._KEPT_REASONS["entry_not_loaded"] in abort
    shown = set(tuya_local_assist.REASONS) - {"entry_not_loaded"}
    abort_needed = (shown | (set(config_flow._KEPT_REASONS) - {"entry_not_loaded"})
                    | set(config_flow._KEPT_REASONS.values()) | {config_flow._KEPT_OTHER}
                    | set(config_flow._PREFLIGHT_ABORTS.values())
                    | {"account_unavailable", "aborted", "already_configured", "no_source"})
    assert abort_needed - set(abort) == set()
    error_needed = set(config_flow._OUTCOME_ERRORS.values()) | {
        "invalid_auth", "cannot_connect", "no_feeders", "rate_limited", "invalid_host"}
    assert error_needed - set(error) == set()


async def test_discover_and_locate_run_in_executor(hass, env):
    loop_thread = threading.current_thread()
    threads = {}

    def discover(request, *, profile, deadline_s=30.0):
        threads["discover"] = threading.current_thread()
        return env.discovery

    def locate(device_id, **kwargs):
        threads["locate"] = threading.current_thread()
        return env.located

    result = await _to_account(hass)
    with patch(f"{CF}.account.discover", new=discover), \
            patch(f"{CF}.lan_locate.locate", new=locate):
        result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    assert set(threads) == {"discover", "locate"}
    for thread in threads.values():
        assert thread is not loop_thread and thread is not threading.main_thread()


def test_confirm_description_uses_setup_placeholder():
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    description = strings["config"]["step"]["confirm"]["description"]
    assert "{setup}" in description and "{profile}" in description
    assert "will be created" not in description


async def test_confirm_text_on_create_and_reuse_paths(hass, env):
    config_flow = _cf()
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    setup = result["description_placeholders"]["setup"]
    assert setup == config_flow._SETUP_CREATE
    assert "will be created" in setup and "{name}" not in setup
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    tl = _make_tl_entry(hass, entry_id="synthetic_reused_tl", device_id=FAKE_DEVICE_ID_2)
    env.existing.return_value = tl.entry_id
    env.discovery = _ok(_feeder(FAKE_DEVICE_ID_2, "SYNTH Second"))
    env.now += 120
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    setup = result["description_placeholders"]["setup"]
    assert setup == config_flow._SETUP_REUSE
    assert "existing Tuya Local entry" in setup and "will be created" not in setup
    _no_sentinels(repr(result["description_placeholders"]))


@pytest.mark.parametrize("where", ["account_step", "confirm"])
async def test_preflight_unexpected_exception_is_unsupported_without_text(hass, env, where):
    if where == "account_step":
        env.preflight.side_effect = RuntimeError(FAKE_KEY)
        result = await _init(hass)
        result = await _configure(hass, result, {"next_step_id": "account"})
    else:
        result = await _to_account(hass)
        result = await _configure(hass, result, _signin())
        assert result["step_id"] == "confirm"
        env.preflight.side_effect = ValueError(FAKE_KEY)
        result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tuya_local_unsupported"
    _no_sentinels(repr(result))
    env.create_mock.assert_not_awaited()
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_kept_entry_after_drive_failure_end_to_end(hass, env, monkeypatch):
    # The real assist runs; only Tuya Local's flow drive is replaced. It adds the
    # Tuya Local entry after the final submit, then fails with unexpected_step.
    from custom_components.petsafe_freshfeed import tuya_local_assist
    from custom_components.petsafe_freshfeed.tuya_local_assist import AssistError

    async def drive(hass_, manager, flow_ids, local, name, final_sent):
        final_sent.append(True)
        _make_tl_entry(hass_, device_id=local["device_id"])
        raise AssistError("unexpected_step")
    monkeypatch.setattr(tuya_local_assist, "_drive", drive)
    env.create_mock.side_effect = env.real_create
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tl_entry_kept_not_loaded"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert [entry.entry_id for entry in hass.config_entries.async_entries(TL_DOMAIN)] == [TL_ENTRY_ID]
    _no_sentinels(repr(result))


def test_abort_texts_name_release_and_kept_profile():
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    fingerprints = json.loads((INTEGRATION / "tuya_fingerprints.json").read_text())
    release = fingerprints["provenance"]["tuya_local_release"]
    abort = strings["config"]["abort"]
    for key in ("tuya_local_missing", "tuya_local_unsupported"):
        assert release in abort[key], key
    assert '"Use an existing Tuya Local entry"' in abort["tuya_local_unsupported"]
    for key, text in abort.items():
        assert "Nothing was created" not in text, key
    for key in ("unexpected_step", "connection_failed", "not_supported", "aborted"):
        assert "No Tuya Local entry was created" in abort[key], key
        assert "may remain installed in Tuya Local" in abort[key], key
    assert "No Tuya Local entry was created" in abort["profile_install_failed"]
