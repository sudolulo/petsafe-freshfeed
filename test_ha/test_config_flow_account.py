"""Config flow tests for the "Sign in with PetSafe" path.

Synthetic values only: no network, no Tuya Local, no real device. The account
client, the LAN locator and the transient local admission probe are patched at
the names ``config_flow`` uses. Owned-entry creation, setup, controls and host
reconfigure against the pinned client are in ``test_config_flow_owned.py``.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
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
# Account material: never persisted anywhere, unlike the owned local connection.
ACCOUNT_SENTINELS = (FAKE_EMAIL, FAKE_PASSWORD)
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
    return Located(host=FAKE_HOST, version="3.5")


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
    state = Env(hass)
    state.probe = AsyncMock(return_value=True)
    with patch(f"{CF}.account.load_app_profile", side_effect=state.load_app_profile), \
            patch(f"{CF}.account.discover", side_effect=state.discover) as discover, \
            patch(f"{CF}.lan_locate.locate", side_effect=state.locate), \
            patch(f"{CF}._async_probe", state.probe), \
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
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "account"
    return result


def _flow(hass, result):
    return hass.config_entries.flow._progress[result["flow_id"]]


def _no_sentinels(text, sentinels=SENTINELS):
    for sentinel in sentinels:
        assert sentinel not in text, sentinel


def _visible(result):
    """A flow result without the created entry's own data (the owned connection)."""
    return repr({key: value for key, value in result.items() if key not in ("data", "result")})


def _check_our_entries(hass):
    for entry in hass.config_entries.async_entries(DOMAIN):
        _no_sentinels(repr(dict(entry.data)) + repr(dict(entry.options)) + entry.title
                      + repr(entry.unique_id), ACCOUNT_SENTINELS)


# --- tests ----------------------------------------------------------------------


async def test_profile_lookup_cancellation_removes_submitted_password(hass, env):
    flow = _flow(hass, await _to_account(hass))
    submitted = _signin()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_profile(_hass):
        entered.set()
        await release.wait()

    with patch(f"{CF}._async_app_profile", side_effect=wait_for_profile):
        task = asyncio.create_task(flow.async_step_account(submitted))
        await entered.wait()
        try:
            assert "password" not in submitted
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert "password" not in submitted


async def test_profile_lookup_exception_removes_submitted_password(hass, env):
    flow = _flow(hass, await _to_account(hass))
    submitted = _signin()
    with patch(f"{CF}._async_app_profile", side_effect=RuntimeError("lookup failed")):
        with pytest.raises(RuntimeError, match="lookup failed"):
            await flow.async_step_account(submitted)
    assert "password" not in submitted


async def test_new_setup_persists_redacted_cloud_handoff(hass, env):
    account = _account()
    from custom_components.petsafe_freshfeed.account import boundary
    route = boundary.select_route("1", boundary.Contract.load())
    handoff = account.restore_session({"sid": "synthetic-sid", "ecode": "synthetic-ecode"}, {
        "country_code": "1", "region": route.region, "url": route.url,
        "api_domain": route.api_domain.value})
    assert handoff is not None
    env.discovery = account.Discovery(account.Outcome.OK, (_feeder(),), session=handoff)
    result = await _configure(hass, await _to_account(hass), _signin())
    result = await _configure(hass, result, {})
    assert result["data"].get("cloud_session") == handoff.storage_data()
    assert result["data"]["cloud_route"] == handoff.storage_route()
    assert result["result"].options["cloud_read_enabled"] is True


async def test_reauth_verifies_existing_device_preserves_lan(hass, env):
    account = _account()
    from custom_components.petsafe_freshfeed.account import boundary
    route = boundary.select_route("1", boundary.Contract.load())
    handoff = account.restore_session({"sid": "synthetic-sid", "ecode": "synthetic-ecode"}, {
        "country_code": "1", "region": route.region, "url": route.url,
        "api_domain": route.api_domain.value})
    env.discovery = account.Discovery(account.Outcome.OK, (_feeder(),), session=handoff)
    original = {"connection_backend": "owned", "device_id": FAKE_DEVICE_ID,
                "local_key": FAKE_KEY, "host": FAKE_HOST, "inert": 9}
    entry = MockConfigEntry(domain=DOMAIN, data=original, options={"unknown": 8})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id}, data=dict(entry.data))
    assert result["type"] is FlowResultType.FORM
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert dict(entry.data) == {**original, "cloud_session": handoff.storage_data(),
                               "cloud_route": handoff.storage_route()}
    assert entry.options == {"unknown": 8, "cloud_read_enabled": True}
    env.probe.assert_not_awaited()
    assert env.locate_calls == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("controls", [False, True])
async def test_cloud_mode_change_with_existing_controls(hass, env, enabled, controls):
    original = {"connection_backend": "owned", "intent_store_id": "store",
                "intent_binding_id": "binding", "local_key": FAKE_KEY}
    if not enabled:
        original.update(cloud_session={"sid": "synthetic", "ecode": "synthetic"},
                        cloud_route={})
    entry = MockConfigEntry(domain=DOMAIN, data=original,
                            options={"cloud_read_enabled": not enabled})
    entry.add_to_hass(hass)
    with patch.object(type(entry), "async_start_reauth") as reauth, patch(
        f"{CF}._async_provision", new=AsyncMock()) as provision:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"],
            {"cloud_read_enabled": enabled, "enable_controls": controls})
    if enabled:
        assert result.get("reason") == "cloud_sign_in"
        reauth.assert_called_once()
        assert dict(entry.data) == original
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        reauth.assert_not_called()
        assert entry.options["cloud_read_enabled"] is False
        assert dict(entry.data) == {k: v for k, v in original.items()
                                   if k not in {"cloud_session", "cloud_route"}}
    provision.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_unchanged_cloud_mode_does_not_require_controls_provisioning(hass, env, enabled):
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "intent_store_id": "store", "intent_binding_id": "binding"},
        options={"cloud_read_enabled": enabled})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
        {"cloud_read_enabled": enabled, "enable_controls": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cloud_read_enabled"] is enabled


async def test_enabling_cloud_and_controls_preserves_both_requests(hass, env):
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned"},
                            options={"cloud_read_enabled": False})
    entry.add_to_hass(hass)
    with patch(f"{CF}.owned_binding_id", return_value="leaf"), patch(
        f"{CF}._async_provision", new=AsyncMock(return_value={"intent_store_id": "store", "intent_binding_id": "leaf"})
    ) as provision, patch.object(type(entry), "async_start_reauth") as reauth:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"cloud_read_enabled": True, "enable_controls": True})
    provision.assert_awaited_once()
    assert entry.data["intent_store_id"] == "store"
    reauth.assert_called_once()
    assert result["reason"] == "cloud_sign_in"


async def test_fully_local_fences_pending_reauth(hass, env):
    account = _account()
    from custom_components.petsafe_freshfeed.account import boundary
    route = boundary.select_route("1", boundary.Contract.load())
    handoff = account.restore_session({"sid": "synthetic-sid", "ecode": "synthetic-ecode"}, {
        "country_code": "1", "region": route.region, "url": route.url,
        "api_domain": route.api_domain.value})
    env.discovery = account.Discovery(account.Outcome.OK, (_feeder(),), session=handoff)
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "device_id": FAKE_DEVICE_ID, "cloud_session": handoff.storage_data(),
        "cloud_route": handoff.storage_route()}, options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    reauth = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id}, data=dict(entry.data))
    options = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(options["flow_id"], {"cloud_read_enabled": False})
    # Fully local aborts the pending reauth flow: no stale flow can send the
    # password or block a later fresh sign-in.
    assert not [flow for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
                if flow["flow_id"] == reauth["flow_id"]]
    env.discover_mock.assert_not_called()
    assert entry.options["cloud_read_enabled"] is False
    assert "cloud_session" not in entry.data and "cloud_route" not in entry.data
    options = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        options["flow_id"], {"cloud_read_enabled": True})
    assert result["type"] is FlowResultType.ABORT
    fresh = [flow for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
             if flow["context"].get("source") == config_entries.SOURCE_REAUTH]
    assert len(fresh) == 1 and fresh[0]["flow_id"] != reauth["flow_id"]


async def test_fully_local_during_reauth_profile_load_never_discovers(hass, env):
    import asyncio
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "device_id": FAKE_DEVICE_ID}, options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    reauth = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id}, data=dict(entry.data))
    # Model a profile lookup yielding while the submitted request is in flight.
    started, release = asyncio.Event(), asyncio.Event()

    async def load_profile(_hass):
        started.set()
        await release.wait()
        return PROFILE

    with patch(f"{CF}._async_app_profile", side_effect=load_profile):
        task = asyncio.create_task(_configure(hass, reauth, _signin()))
        try:
            async with asyncio.timeout(5):
                await started.wait()
            options = await hass.config_entries.options.async_init(entry.entry_id)
            await hass.config_entries.options.async_configure(
                options["flow_id"], {"cloud_read_enabled": False})
        finally:
            release.set()
        async with asyncio.timeout(5):
            await task
    assert entry.options["cloud_read_enabled"] is False
    env.discover_mock.assert_not_called()


@pytest.mark.parametrize("reason", ["aborted", "account_unavailable"])
async def test_early_account_abort_removes_submitted_password(hass, env, reason):
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "device_id": FAKE_DEVICE_ID}, options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id}, data=dict(entry.data))
    flow = _flow(hass, result)
    submitted = _signin()

    async def profile_after_disable(_hass):
        if reason == "aborted":
            hass.data[DOMAIN][_cf()._CLOUD_DISABLE_GENERATIONS][entry.entry_id] = 1
            return PROFILE
        return None

    with patch(f"{CF}._async_app_profile", side_effect=profile_after_disable):
        result = await flow.async_step_account(submitted)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert "password" not in submitted
    env.discover_mock.assert_not_called()


async def test_fully_local_and_controls_are_both_applied(hass, env):
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "cloud_session": {"sid": "synthetic", "ecode": "synthetic"}, "cloud_route": {}},
        options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    with patch(f"{CF}.owned_binding_id", return_value="leaf"), patch(
        f"{CF}._async_provision", AsyncMock(return_value={"intent_store_id": "store",
                                                          "intent_binding_id": "leaf"})) as provision:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"],
            {"cloud_read_enabled": False, "enable_controls": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cloud_read_enabled"] is False
    assert "cloud_session" not in entry.data and "cloud_route" not in entry.data
    provision.assert_awaited_once()
    assert entry.data["intent_store_id"] == "store"


async def test_fully_local_persists_before_cancelled_drain(hass, env):
    import asyncio
    from types import SimpleNamespace

    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "cloud_session": {"sid": "sentinel", "ecode": "sentinel"}, "cloud_route": {}},
        options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()

    entry.runtime_data = SimpleNamespace(cloud_read=SimpleNamespace(close=close))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    task = asyncio.create_task(hass.config_entries.options.async_configure(
        result["flow_id"], {"cloud_read_enabled": False}))
    await entered.wait()
    try:
        assert "cloud_session" not in entry.data and "cloud_route" not in entry.data
        assert entry.options["cloud_read_enabled"] is False
    finally:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_fully_local_fences_read_completing_before_close(hass, env):
    import asyncio
    from types import SimpleNamespace
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead

    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "cloud_session": {"sid": "sentinel", "ecode": "sentinel"}, "cloud_route": {}},
        options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch():
        started.set()
        await release.wait()
        return SimpleNamespace(outcome="ok", dp1=None, dp140="pending", dp140_present=True)

    helper = CloudRead(fetch)
    published = []
    helper.subscribe(published.append)
    entry.runtime_data = SimpleNamespace(cloud_read=helper)
    demand = asyncio.create_task(helper.demand(epoch=1, sequences={}))
    await started.wait()
    update = hass.config_entries.async_update_entry

    def update_then_complete(entry, **kwargs):
        # The read completes while the options flow persists Fully local.
        release.set()
        return update(entry, **kwargs)

    with patch.object(hass.config_entries, "async_update_entry", side_effect=update_then_complete):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"cloud_read_enabled": False})
    await demand
    assert published == []
    assert helper.evidence("140") is None


@pytest.mark.parametrize('recovering', [False, True])
async def test_fully_local_clears_adapter_cloud_veto_without_reload(hass, env, recovering):
    from time import monotonic
    from types import SimpleNamespace
    from custom_components.petsafe_freshfeed.account import DPRead
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    from custom_components.petsafe_freshfeed.write_adapter import WriteAdapter, _Collector

    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned"},
                            options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    async def fetch():
        return DPRead('ok', None, 'EAEA', True)
    cloud = CloudRead(fetch)
    runtime = SimpleNamespace(cloud_read=cloud)
    entry.runtime_data = runtime
    adapter = WriteAdapter(hass, entry, SimpleNamespace(epoch=1), None, None, None)
    runtime.adapter = adapter
    adapter._live_dps = {'4': 'standby', '142': 0, '145': 2}
    adapter._live_times = dict.fromkeys(adapter._live_dps, monotonic())
    if recovering:
        adapter._collector = _Collector(None, 0, lambda *_: True)
    await cloud.demand(epoch=1, sequences={})
    assert adapter._decision('feed')[0] == 'quick_feed_active'
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result['flow_id'], {'cloud_read_enabled': False})
    assert adapter._decision('feed')[0] is None
    if recovering:
        assert adapter._collector.reason == 'recovery_rejected'
    adapter._live_dps['140'] = 'EAEA'
    adapter._live_sequences['140'] = 1
    assert adapter._decision('feed')[0] == 'quick_feed_active'
    adapter._live_dps['140'] = 'EAAA'
    assert adapter._decision('feed')[0] is None


async def test_options_default_preserves_enabled_cloud_on_controls_submission(hass, env):
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned",
        "cloud_session": {"sid": "sentinel", "ecode": "sentinel"}, "cloud_route": {}},
        options={"cloud_read_enabled": True})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    values = result["data_schema"]({"enable_controls": False})
    assert values["cloud_read_enabled"] is True
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["cloud_read_enabled"] is True
    assert "cloud_session" in entry.data


async def test_fully_local_deletes_cloud_secrets_only(hass, env):
    from types import SimpleNamespace
    original = {"connection_backend": "owned", "device_id": FAKE_DEVICE_ID,
                "local_key": FAKE_KEY, "host": FAKE_HOST, "inert": 9,
                "cloud_session": {"sid": "sentinel", "ecode": "sentinel"}, "cloud_route": {}}
    entry = MockConfigEntry(domain=DOMAIN, data=original,
                            options={"unknown": 8, "cloud_read_enabled": True})
    entry.add_to_hass(hass)
    close = AsyncMock()
    entry.runtime_data = SimpleNamespace(cloud_read=SimpleNamespace(close=close))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                              {"cloud_read_enabled": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "cloud_session" not in entry.data and "cloud_route" not in entry.data
    assert entry.data["local_key"] == FAKE_KEY and entry.data["inert"] == 9
    assert entry.options == {"unknown": 8, "cloud_read_enabled": False}
    close.assert_awaited_once()


@pytest.mark.parametrize('session', [None, {}, {'ecode': 'synthetic'}])
async def test_sidless_upgrade_is_fully_local(hass, session):
    from custom_components.petsafe_freshfeed import async_migrate_entry
    entry = MockConfigEntry(domain=DOMAIN, data={'connection_backend': 'owned',
                            'cloud_session': session}, version=1, minor_version=1)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.options['cloud_read_enabled'] is False


async def test_owned_minor_upgrade_preserves_unknown_fields(hass):
    from custom_components.petsafe_freshfeed import async_migrate_entry
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned", "inert": 7},
                            options={"unknown": 8}, version=1, minor_version=1)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.data == {"connection_backend": "owned", "inert": 7}
    assert entry.options == {"unknown": 8, "cloud_read_enabled": False}
    assert entry.minor_version == 2
    assert await async_migrate_entry(hass, entry)
    assert entry.options == {"unknown": 8, "cloud_read_enabled": False}



async def test_account_path_creates_owned_entry(hass, env):
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"name": FAKE_NAME}
    # Controls are never offered here: they are a separate, explicit options step.
    assert [str(key) for key in result["data_schema"].schema] == []
    env.probe.assert_not_awaited()
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"connection_backend": "owned", "device_id": FAKE_DEVICE_ID,
                              "local_key": FAKE_KEY, "host": FAKE_HOST, "protocol_version": "3.5"}
    assert result["title"] == FAKE_NAME
    assert result["result"].unique_id == FAKE_DEVICE_ID
    assert result["description"] == "owned_read_only"
    assert hass.config_entries.async_entries(TL_DOMAIN) == []
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    env.probe.assert_awaited_once()
    connection = env.probe.await_args.args[1]
    assert (connection.device_id, connection.host, connection.version) == (
        FAKE_DEVICE_ID, FAKE_HOST, "3.5")
    assert FAKE_KEY not in repr(connection)
    assert env.discover_mock.call_count == 1
    assert env.locate_calls == [FAKE_DEVICE_ID]


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
    env.probe.assert_not_awaited()
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


async def test_admission_failure_retries_host_without_new_signin(hass, env):
    env.probe.return_value = False
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "host"
    assert result["errors"] == {"base": "local_admission_failed"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    env.probe.return_value = True
    result = await _configure(hass, result, {"host": "10.0.0.5"})
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "10.0.0.5"
    assert env.discover_mock.call_count == 1 and env.probe.await_count == 2


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
    for result in results[:-1]:
        _no_sentinels(_visible(result))
    # The created entry's context names its unique ID, the device ID, and nothing else.
    _no_sentinels(_visible(results[-1]), set(SENTINELS) - {FAKE_DEVICE_ID})
    # The context names the unique ID (the device ID) only; never key, host or account.
    _no_sentinels("".join(contexts) + repr(flow.context),
                  (FAKE_EMAIL, FAKE_PASSWORD, FAKE_KEY, FAKE_DEVICE_ID_2, FAKE_HOST))
    _check_our_entries(hass)
    _no_sentinels(caplog.text)
    assert flow._feeders == () and flow._feeder is None and flow._host is None


async def test_existing_sources_do_not_offer_legacy_path(hass, env, source_entry):
    make_source_entry(hass, entry_id="01M5NATKW5YS05D36KGFB38D8Q", title="FreshFeed Two")
    result = await _init(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account"
    assert "menu_options" not in result
    assert env.discover_mock.call_count == 0
    env.probe.assert_not_awaited()


async def test_no_menu_without_app_profile(hass, env, source_entry):
    env.profile_error = _account().ProfileUnavailable()
    first = await _init(hass)
    assert first["type"] is FlowResultType.ABORT and first["reason"] == "account_unavailable"
    # The profile check is cached per hass: a second flow reads nothing again.
    result = await _init(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "account_unavailable"
    assert env.load_calls == 1


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


async def test_account_path_needs_no_tuya_local(hass, env):
    # No Tuya Local entry or integration anywhere: sign-in still reaches the feeder.
    assert hass.config_entries.async_entries(TL_DOMAIN) == []
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    assert TL_DOMAIN not in hass.data


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
    env.probe.assert_not_awaited()


@pytest.mark.parametrize("host", ["10.0.0.5", "172.16.4.4", "172.31.255.254", FAKE_HOST])
async def test_host_step_accepts_rfc1918(hass, env, host):
    env.located = None
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    result = await _configure(hass, result, {"host": host})
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert env.probe.await_args.args[1].host == host
    assert result["data"]["host"] == host


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
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "account"


async def test_account_shown_when_profile_loads(hass, source_entry):
    with patch(f"{CF}.account.load_app_profile", return_value=PROFILE):
        result = await _init(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "account"
    assert "menu_options" not in result


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
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def gated_probe(hass_, connection):
        entered.set()
        await gate.wait()
        return True
    env.probe.side_effect = gated_probe
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    first = asyncio.create_task(_configure(hass, result, {}))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(_configure(hass, result, {}))
    try:
        await asyncio.sleep(0.05)
        probes_while_running = env.probe.await_count
    finally:
        gate.set()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 5)
    assert probes_while_running == 1
    assert second_result["type"] is FlowResultType.FORM
    assert second_result["step_id"] == "confirm"
    assert first_result["type"] is FlowResultType.CREATE_ENTRY
    assert env.probe.await_count == 1
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


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


async def test_unexpected_confirm_exception_aborts_without_text(hass, env):
    env.probe.side_effect = OSError(OS_SENTINEL + FAKE_KEY)
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    flow = _flow(hass, result)
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "aborted"
    assert OS_SENTINEL not in repr(result)
    _no_sentinels(repr(result))
    assert flow._feeder is None and flow._feeders == ()
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_ignored_own_entry_does_not_block(hass, env):
    MockConfigEntry(domain=DOMAIN, unique_id=FAKE_DEVICE_ID, source=config_entries.SOURCE_IGNORE,
                    data={}).add_to_hass(hass)
    result = await _to_account(hass)
    result = await _configure(hass, result, _signin())
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == FAKE_DEVICE_ID


def test_every_flow_reason_has_a_string():
    config_flow = _cf()
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())
    assert strings == english
    abort = strings["config"]["abort"]
    error = strings["config"]["error"]
    abort_needed = {
        "account_unavailable", "aborted", "already_configured",
        "tuya_local_conflict", "unsupported_local_configuration",
        "invalid_local_configuration", "reconfigure_successful", "legacy_backend_removed",
        "reconfigure_in_progress", "unload_failed", "already_in_progress"}
    assert abort_needed - set(abort) == set()
    error_needed = set(config_flow._OUTCOME_ERRORS.values()) | {
        "invalid_auth", "cannot_connect", "no_feeders", "rate_limited", "invalid_host",
        config_flow.ADMISSION_FAILED}
    assert error_needed - set(error) == set()
    assert config_flow.CREATED_READ_ONLY in strings["config"]["create_entry"]
    assert {"reconfigure", "confirm", "host"} <= set(strings["config"]["step"])
    options_abort = strings["options"]["abort"]
    assert {"controls_already_enabled", "binding_unavailable", "provision_failed",
            "legacy_backend_removed"} <= set(options_abort)
    # The Tuya Local setup assist is no longer part of onboarding.
    for gone in ("tuya_local_missing", "tuya_local_unsupported", "profile_conflict"):
        assert gone not in abort
    assert not any(key.startswith("tl_entry_kept") for key in abort)


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


def test_completion_text_explains_controls_step():
    strings = json.loads((INTEGRATION / "strings.json").read_text())
    confirm = strings["config"]["step"]["confirm"]["description"]
    assert "read-only" in confirm and "Tuya Local" not in confirm
    created = strings["config"]["create_entry"]["owned_read_only"]
    assert "{name}" in created and "Enable controls" in created
    assert "retained-intent store" in created
