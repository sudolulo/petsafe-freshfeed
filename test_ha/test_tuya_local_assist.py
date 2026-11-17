"""Tests for tuya_local_assist: preflight, profile install, reuse lookup and the driver.

Every test runs against a stub ``tuya_local`` flow handler registered in the test
Home Assistant instance. The stub mirrors the pinned Tuya Local flow's step IDs,
field names, ``SelectSelector`` option dicts (compound ``config_type||manufacturer||model``
values) and abort reasons; ``test_pinned_tl_flow_contract`` checks that mirror against
the pinned public source when it is present. All values are synthetic (FAKE/SYNTH);
no network, no real device, no real Tuya Local installation.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

TL_DOMAIN = "tuya_local"
PINNED_RELEASE = "2026.10.0"
# Pinned public Tuya Local source, named by the PETSAFE_PINNED_TUYA_LOCAL
# environment variable (path to its custom_components/tuya_local directory;
# never committed or shipped). The contract test skips when it is unset or missing.
_PINNED_TL_ENV = os.environ.get("PETSAFE_PINNED_TUYA_LOCAL", "")
PINNED_TL = Path(_PINNED_TL_ENV) if _PINNED_TL_ENV else None
REPO = Path(__file__).resolve().parent.parent
BUNDLED_PROFILE = REPO / "custom_components/petsafe_freshfeed/profile/petsafe_freshfeed_raw.yaml"
TL_FILES = ("config_flow.py", "discovery.py", "helpers/device_config.py", "device.py")

FAKE_KEY = "FAKESYNTHKEY0x9q"  # 16 printable ASCII, synthetic
FAKE_DEVICE_ID = "fakesynthdev0000001"
FAKE_HOST = "192.168.77.201"  # synthetic RFC 1918 address
SENTINELS = (FAKE_KEY, FAKE_DEVICE_ID, FAKE_HOST)

# --- stub mirror of the pinned Tuya Local flow ---------------------------------

STUB_SETUP_MODES = ["cloud", "manual", "cloud_fresh_login"]
STUB_PROTOCOLS = ["auto", "3.3", "3.1", "3.2", "3.4", "3.5", "3.22", "3.42", "3.52"]
STUB_LOCAL_FIELDS = ("device_id", "host", "local_key", "protocol_version", "poll_only", "device_cid")
STUB_STEP_IDS = ("user", "local", "select_type", "select_type_auto_detected", "choose_entities")
STUB_TYPE_FIELD = "type"
STUB_NAME_FIELD = "name"
STUB_ABORTS = ("not_supported", "already_configured")
_STUB_LOGGER = logging.getLogger("custom_components.tuya_local.config_flow")


class StubTuyaLocalFlow(ConfigFlow):
    """Mirror of TL ``ConfigFlowHandler`` (pinned 2026.10.0) for the manual path."""

    VERSION = 13
    MINOR_VERSION = 25
    behaviour: dict = {}
    seen: list = []

    def __init__(self) -> None:
        self.data = {}
        self._detected = None

    async def async_step_integration_discovery(self, discovery_info):
        await self.async_set_unique_id(discovery_info.get("device_id"))
        self._abort_if_unique_id_configured()
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            mode = user_input.get("setup_mode")
            if mode == "manual":
                odd = self.behaviour.get("user_next")
                if odd:
                    return self.async_show_form(step_id=odd, data_schema=vol.Schema({}))
                return await self.async_step_local()
            return self.async_abort(reason="cloud_not_stubbed")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("setup_mode"): SelectSelector(SelectSelectorConfig(
                    options=STUB_SETUP_MODES, mode=SelectSelectorMode.LIST,
                    translation_key="setup_mode")),
            }),
            errors={},
            last_step=False,
        )

    async def async_step_cloud(self, user_input=None):
        return self.async_show_form(step_id="cloud", data_schema=vol.Schema({}))

    async def async_step_local(self, user_input=None):
        errors = {}
        if user_input is not None:
            self.seen.append(("local", dict(user_input)))
            gate = self.behaviour.get("gate")
            if gate is not None:
                self.behaviour["entered"].set()
                await gate.wait()
            proto = user_input.get("protocol_version")
            if proto != "auto":
                user_input["protocol_version"] = float(proto)
            if self.behaviour.get("connect", True):
                self.data = user_input
                if proto == "auto":
                    self._detected = 3.3
                    self.data = {**self.data, "protocol_version": 3.3}
                await self.async_set_unique_id(user_input["device_id"])
                self._abort_if_unique_id_configured()
                return await self.async_step_select_type()
            errors["base"] = "connection"
        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema({
                vol.Required("device_id"): str,
                vol.Required("host", default=""): str,
                vol.Required("local_key"): str,
                vol.Required("protocol_version", default="auto"): vol.In(STUB_PROTOCOLS),
                vol.Required("poll_only", default=False): bool,
                vol.Optional("device_cid"): str,
            }),
            errors=errors,
        )

    async def async_step_select_type(self, user_input=None):
        if user_input is not None:
            self.seen.append(("type", user_input[STUB_TYPE_FIELD]))
            parts = user_input[STUB_TYPE_FIELD].split("||", 2)
            self.data["type"] = parts[0]
            if len(parts) > 1 and parts[1]:
                self.data["manufacturer"] = parts[1]
            if len(parts) > 2 and parts[2]:
                self.data["model"] = parts[2]
            return await self.async_step_choose_entities()
        types = self.behaviour.get("types", [("petsafe_freshfeed_raw", "PetSafe", "FreshFeed")])
        options = [SelectOptionDict(value=f"{t}||{m}||{n}", label=f"{m} {n} ({t})")
                   for t, m, n in types]
        _STUB_LOGGER.warning("Device matches %s with quality of %d%%. LOCAL DPS: %s",
                             types[0][0] if types else None, 100, '{"145": 6}')
        if not options:
            return self.async_abort(reason="not_supported")
        schema = vol.Schema({
            vol.Required(STUB_TYPE_FIELD, default=options[0]["value"]):
                SelectSelector(SelectSelectorConfig(options=options)),
        })
        if self._detected:
            return self.async_show_form(step_id="select_type_auto_detected", data_schema=schema)
        return self.async_show_form(step_id="select_type", data_schema=schema)

    async def async_step_select_type_auto_detected(self, user_input=None):
        return await self.async_step_select_type(user_input)

    async def async_step_choose_entities(self, user_input=None):
        if user_input is not None:
            title = user_input.pop(STUB_NAME_FIELD)
            return self.async_create_entry(title=title, data={**self.data, **user_input})
        return self.async_show_form(
            step_id="choose_entities",
            data_schema=vol.Schema({vol.Required(STUB_NAME_FIELD, default="FreshFeed"): str}),
        )


def _register_dp145(hass, entry):
    device_id = entry.data["device_id"]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(TL_DOMAIN, device_id)}, name="FreshFeed")
    er.async_get(hass).async_get_or_create(
        "sensor", TL_DOMAIN, f"{device_id}-sensor_dp_145_raw",
        config_entry=entry, device_id=device.id)


# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def assist():
    """Import lazily so a missing module fails each test, not collection."""
    return importlib.import_module("custom_components.petsafe_freshfeed.tuya_local_assist")


@pytest.fixture
def feeder():
    from custom_components.petsafe_freshfeed.account.client import Feeder
    return Feeder._make(FAKE_DEVICE_ID, FAKE_KEY, "SYNTH Feeder")


@pytest.fixture
def located():
    from custom_components.petsafe_freshfeed.lan_locate import Located
    return Located(host=FAKE_HOST, version="3.3")


def _make_tl_tree(root: Path) -> dict[str, str]:
    pins = {}
    for name in TL_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# SYNTH stand-in for tuya_local/{name}\n".encode()
        path.write_bytes(body)
        pins[f"custom_components/tuya_local/{name}"] = hashlib.sha256(body).hexdigest()
    (root / "devices").mkdir()
    return pins


@pytest.fixture
async def stub_tl(hass, tmp_path, monkeypatch, assist):
    """Register the stub tuya_local integration, flow handler and a synthetic install."""
    root = tmp_path / "custom_components" / "tuya_local"
    pins = _make_tl_tree(root)
    monkeypatch.setattr(assist, "_load_pins", lambda: (PINNED_RELEASE, dict(pins)))
    monkeypatch.setattr(assist, "ENTITY_WAIT_S", 0.5)
    behaviour = {"setup": "ok"}
    seen: list = []

    async def setup_entry(hass_, entry):
        mode = behaviour["setup"]
        if mode == "fail":
            return False
        if mode == "retry":
            raise ConfigEntryNotReady
        if mode == "ok":
            _register_dp145(hass_, entry)
        elif mode == "hang":
            await behaviour["setup_gate"].wait()
            _register_dp145(hass_, entry)
        elif mode == "late":
            async def later():
                await asyncio.sleep(0.1)
                _register_dp145(hass_, entry)
            hass_.async_create_task(later())
        return True

    integration = mock_integration(hass, MockModule(
        TL_DOMAIN,
        async_setup_entry=setup_entry,
        async_unload_entry=AsyncMock(return_value=True),
        partial_manifest={"version": behaviour.get("version", PINNED_RELEASE), "config_flow": True},
    ))
    integration.file_path = root
    mock_platform(hass, f"{TL_DOMAIN}.config_flow", None)
    StubTuyaLocalFlow.behaviour = behaviour
    StubTuyaLocalFlow.seen = seen
    with mock_config_flow(TL_DOMAIN, StubTuyaLocalFlow):
        yield SimpleNamespace(root=root, pins=pins, behaviour=behaviour, seen=seen,
                              integration=integration)
        for entry in hass.config_entries.async_entries(TL_DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)


def _install_profile_bytes(stub, data: bytes) -> Path:
    target = stub.root / "devices" / "petsafe_freshfeed_raw.yaml"
    target.write_bytes(data)
    return target


@pytest.fixture
def ready(stub_tl):
    _install_profile_bytes(stub_tl, BUNDLED_PROFILE.read_bytes())
    return stub_tl


def _tl_progress(hass):
    return hass.config_entries.flow.async_progress_by_handler(TL_DOMAIN, include_uninitialized=True)


async def _drive_error(assist, hass, feeder, located, host=None, name="SYNTH Feeder"):
    with pytest.raises(assist.AssistError) as caught:
        await assist.async_create_source_entry(hass, feeder, located, host, name=name)
    return caught.value


def _local_data_for(stub):
    return [data for kind, data in stub.seen if kind == "local"]


# --- driver --------------------------------------------------------------------


async def test_drives_pinned_flow_to_entry(hass, assist, ready, feeder, located):
    entry_id = await assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder")
    entry = hass.config_entries.async_get_entry(entry_id)
    assert entry.domain == TL_DOMAIN and entry.state is ConfigEntryState.LOADED
    assert entry.title == "SYNTH Feeder"
    assert entry.unique_id == FAKE_DEVICE_ID
    assert entry.data["type"] == "petsafe_freshfeed_raw"
    assert _local_data_for(ready) == [{
        "device_id": FAKE_DEVICE_ID, "host": FAKE_HOST, "local_key": FAKE_KEY,
        "protocol_version": "3.3", "poll_only": False}]
    assert _tl_progress(hass) == []


async def test_picks_compound_type_option_by_prefix(hass, assist, ready, feeder, located):
    ready.behaviour["types"] = [
        ("other_feeder", "PetSafe", "FreshFeed"),
        ("petsafe_freshfeed_raw_v2", "PetSafe", "FreshFeed"),
        ("petsafe_freshfeed_raw", "PetSafe", "FreshFeed"),
        ("xpetsafe_freshfeed_raw", "", ""),
    ]
    entry_id = await assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder")
    assert [v for kind, v in ready.seen if kind == "type"] == ["petsafe_freshfeed_raw||PetSafe||FreshFeed"]
    assert hass.config_entries.async_get_entry(entry_id).data["type"] == "petsafe_freshfeed_raw"


@pytest.mark.parametrize("types", [
    [("other_feeder", "PetSafe", "FreshFeed")],
    [("petsafe_freshfeed_raw", "A", "B"), ("petsafe_freshfeed_raw", "C", "D")],
    [],
])
async def test_no_single_profile_option_is_not_supported(hass, assist, ready, feeder, located, types):
    ready.behaviour["types"] = types
    error = await _drive_error(assist, hass, feeder, located)
    assert error.reason == "not_supported"
    assert _tl_progress(hass) == []
    assert hass.config_entries.async_entries(TL_DOMAIN) == []


async def test_auto_detected_step_accepted(hass, assist, ready, feeder):
    entry_id = await assist.async_create_source_entry(hass, feeder, None, FAKE_HOST, name="SYNTH Feeder")
    assert _local_data_for(ready)[0]["protocol_version"] == "auto"
    assert _local_data_for(ready)[0]["host"] == FAKE_HOST
    assert hass.config_entries.async_get_entry(entry_id).data["protocol_version"] == 3.3


async def test_requires_located_or_host(hass, assist, ready, feeder):
    with pytest.raises(ValueError):
        await assist.async_create_source_entry(hass, feeder, None, None, name="SYNTH Feeder")
    assert ready.seen == [] and _tl_progress(hass) == []


async def test_unexpected_step_aborts_flow(hass, assist, ready, feeder, located):
    ready.behaviour["user_next"] = "cloud"
    error = await _drive_error(assist, hass, feeder, located)
    assert error.reason == "unexpected_step"
    assert _tl_progress(hass) == []
    assert hass.config_entries.async_entries(TL_DOMAIN) == []


async def test_error_form_aborts_flow_and_drops_results(hass, assist, ready, feeder, located):
    ready.behaviour["connect"] = False
    error = await _drive_error(assist, hass, feeder, located)
    assert error.reason == "connection_failed"
    assert error.args == ("connection_failed",)
    assert set(vars(error)) == {"reason"}
    assert error.__cause__ is None and error.__context__ is None
    text = str(error) + repr(error)
    assert not any(s in text for s in SENTINELS)
    assert _tl_progress(hass) == []
    assert hass.config_entries.async_entries(TL_DOMAIN) == []


async def test_connection_error_maps_to_connection_failed(hass, assist, ready, feeder):
    ready.behaviour["connect"] = False
    error = await _drive_error(assist, hass, feeder, None, host=FAKE_HOST)
    assert error.reason == "connection_failed"
    assert len(_local_data_for(ready)) == 1  # exactly one submit, never retried
    assert _tl_progress(hass) == []


async def test_cancellation_aborts_flow(hass, assist, ready, feeder, located):
    ready.behaviour["gate"] = asyncio.Event()
    ready.behaviour["entered"] = asyncio.Event()
    task = hass.async_create_task(
        assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder"))
    await asyncio.wait_for(ready.behaviour["entered"].wait(), 5)
    assert len(_tl_progress(hass)) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _tl_progress(hass) == []
    assert hass.config_entries.async_entries(TL_DOMAIN) == []


async def _drive_with_midflight(assist, hass, stub, feeder, located, action):
    """Run the drive, pausing in TL's local step to run ``action`` (after the guards)."""
    stub.behaviour["gate"] = asyncio.Event()
    stub.behaviour["entered"] = asyncio.Event()
    task = hass.async_create_task(
        assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder"))
    await asyncio.wait_for(stub.behaviour["entered"].wait(), 5)
    result = await action()
    stub.behaviour["gate"].set()
    with pytest.raises(assist.AssistError) as caught:
        await task
    return caught.value, result


async def test_already_configured_abort_maps_to_other(hass, assist, ready, feeder, located):
    # The entry appears after the create-time guard ran: TL's own unique-id abort maps.
    async def add_entry():
        MockConfigEntry(domain=TL_DOMAIN, unique_id=FAKE_DEVICE_ID,
                        data={"type": "some_other_profile"}).add_to_hass(hass)
    error, _ = await _drive_with_midflight(assist, hass, ready, feeder, located, add_entry)
    assert error.reason == "already_configured_other"
    assert len(_local_data_for(ready)) == 1  # reached TL's own abort, not the guard
    assert _tl_progress(hass) == []


async def test_already_in_progress_abort_maps(hass, assist, ready, feeder, located):
    # A TL discovery flow for the same device starts after our pre-abort ran and
    # claims the unique id first: TL's async_set_unique_id aborts ours.
    async def discover():
        return await hass.config_entries.flow.async_init(
            TL_DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
            data={"device_id": FAKE_DEVICE_ID, "host": FAKE_HOST, "version": "3.3"})
    error, other = await _drive_with_midflight(assist, hass, ready, feeder, located, discover)
    assert other["type"] == "form"
    assert error.reason == "already_in_progress"
    assert [f["flow_id"] for f in _tl_progress(hass)] == [other["flow_id"]]
    hass.config_entries.flow.async_abort(other["flow_id"])


async def test_other_abort_maps_to_aborted(hass, assist, ready, feeder, located, monkeypatch):
    async def abort_local(self, user_input=None):
        return self.async_abort(reason="something_new")
    monkeypatch.setattr(StubTuyaLocalFlow, "async_step_local", abort_local)
    error = await _drive_error(assist, hass, feeder, located)
    assert error.reason == "aborted"
    assert _tl_progress(hass) == []


async def test_schema_drift_is_unexpected_step(hass, assist, ready, feeder, located, monkeypatch):
    async def drifted_local(self, user_input=None):
        return self.async_show_form(step_id="local", data_schema=vol.Schema({
            vol.Required("device_id"): str, vol.Required("host"): str,
            vol.Required("local_key"): str, vol.Required("new_field"): str}))
    monkeypatch.setattr(StubTuyaLocalFlow, "async_step_local", drifted_local)
    error = await _drive_error(assist, hass, feeder, located)
    assert error.reason == "unexpected_step"
    assert ready.seen == [] and _tl_progress(hass) == []


async def test_discovery_flow_for_device_is_aborted_first(hass, assist, ready, feeder, located):
    mine = await hass.config_entries.flow.async_init(
        TL_DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"device_id": FAKE_DEVICE_ID, "host": FAKE_HOST, "version": "3.3"})
    other = await hass.config_entries.flow.async_init(
        TL_DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"device_id": "fakesynthdevOTHER", "host": "192.168.77.202", "version": "3.3"})
    assert mine["type"] == "form" and other["type"] == "form"
    entry_id = await assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder")
    assert hass.config_entries.async_get_entry(entry_id).unique_id == FAKE_DEVICE_ID
    assert [f["flow_id"] for f in _tl_progress(hass)] == [other["flow_id"]]
    hass.config_entries.flow.async_abort(other["flow_id"])


async def test_entry_not_loaded_or_missing_dp145_entity(hass, assist, ready, feeder, located):
    ready.behaviour["setup"] = "fail"
    error = await _drive_error(assist, hass, feeder, located)
    assert error.reason == "entry_not_loaded"
    kept = hass.config_entries.async_entries(TL_DOMAIN)
    assert len(kept) == 1 and kept[0].state is not ConfigEntryState.LOADED  # kept, reported
    await hass.config_entries.async_remove(kept[0].entry_id)

    ready.behaviour["setup"] = "no_entity"
    error = await _drive_error(assist, hass, feeder, located)
    assert error.reason == "entry_not_loaded"
    kept = hass.config_entries.async_entries(TL_DOMAIN)
    assert len(kept) == 1 and kept[0].state is ConfigEntryState.LOADED
    await hass.config_entries.async_remove(kept[0].entry_id)

    ready.behaviour["setup"] = "late"  # entity registered after setup returned
    entry_id = await assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder")
    assert hass.config_entries.async_get_entry(entry_id).state is ConfigEntryState.LOADED


async def test_local_key_never_logged(hass, assist, ready, feeder, located, caplog, request):
    caplog.set_level(logging.DEBUG)
    root = logging.getLogger()
    saved_level = root.level
    request.addfinalizer(lambda: root.setLevel(saved_level))  # setLevel also clears caches
    root.setLevel(logging.DEBUG)
    # success
    entry_id = await assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder")
    await hass.config_entries.async_remove(entry_id)
    # connection failure
    ready.behaviour["connect"] = False
    await _drive_error(assist, hass, feeder, None, host=FAKE_HOST)
    ready.behaviour["connect"] = True
    # unexpected step
    ready.behaviour["user_next"] = "cloud"
    await _drive_error(assist, hass, feeder, located)
    ready.behaviour.pop("user_next")
    # cancellation
    ready.behaviour["gate"] = asyncio.Event()
    ready.behaviour["entered"] = asyncio.Event()
    task = hass.async_create_task(
        assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder"))
    await asyncio.wait_for(ready.behaviour["entered"].wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await hass.async_block_till_done()
    assert caplog.records, "expected log records (the stub logs at WARNING)"
    assert any(r.name == _STUB_LOGGER.name for r in caplog.records)
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.args) + (record.exc_text or "")
        if record.exc_info:
            rendered += logging.Formatter().formatException(record.exc_info)
        assert FAKE_KEY not in rendered, record.name
        if record.name.startswith("custom_components.petsafe_freshfeed"):
            assert not any(s in rendered for s in SENTINELS), record.name


def _assert_clean_error(error, reason):
    assert error.reason == reason
    assert error.__cause__ is None and error.__context__ is None
    assert error.args == (reason,)
    for text in (repr(error), str(error), repr(error.args)):
        assert not any(s in text for s in SENTINELS)


async def _raising_local(self, user_input=None):
    raise Exception(FAKE_KEY)  # noqa: TRY002 - stands in for an arbitrary TL internal error


async def _unknown_flow_local(self, user_input=None):
    raise UnknownFlow(FAKE_KEY)


async def _invalid_data_local(self, user_input=None):
    # Same field names, but device_id typed int: HA's schema check raises InvalidData.
    return self.async_show_form(step_id="local", data_schema=vol.Schema({
        vol.Required("device_id"): int, vol.Required("host"): str,
        vol.Required("local_key"): str, vol.Required("protocol_version"): str,
        vol.Required("poll_only"): bool, vol.Optional("device_cid"): str}))


@pytest.mark.parametrize(("step", "reason"), [
    (_raising_local, "aborted"),
    (_unknown_flow_local, "aborted"),
    (_invalid_data_local, "unexpected_step"),
])
async def test_mapped_errors_carry_no_exception_context(
        hass, assist, ready, feeder, located, monkeypatch, step, reason):
    monkeypatch.setattr(StubTuyaLocalFlow, "async_step_local", step)
    error = await _drive_error(assist, hass, feeder, located)
    _assert_clean_error(error, reason)
    assert _tl_progress(hass) == []
    assert hass.config_entries.async_entries(TL_DOMAIN) == []


async def test_preflight_and_install_errors_carry_no_context(hass, assist, stub_tl, monkeypatch):
    def broken_install(devices, data):
        raise OSError(FAKE_KEY)
    monkeypatch.setattr(assist, "_install", broken_install)
    with pytest.raises(assist.AssistError) as caught:
        await assist.async_install_profile(hass)
    _assert_clean_error(caught.value, "profile_install_failed")

    monkeypatch.setattr(assist, "PROFILE_SOURCE", stub_tl.root / "absent-SYNTH.yaml")
    with pytest.raises(assist.AssistError) as caught:
        await assist.async_preflight(hass)
    _assert_clean_error(caught.value, "profile_install_failed")


async def test_first_step_failure_leaves_no_uninitialized_flow(
        hass, assist, ready, feeder, located, monkeypatch):
    async def raising_user(self, user_input=None):
        raise Exception(FAKE_KEY)  # noqa: TRY002
    monkeypatch.setattr(StubTuyaLocalFlow, "async_step_user", raising_user)
    error = await _drive_error(assist, hass, feeder, located)
    _assert_clean_error(error, "aborted")
    assert _tl_progress(hass) == []


async def test_drive_timeout_maps_to_connection_failed(hass, assist, ready, feeder, located, monkeypatch):
    monkeypatch.setattr(assist, "DRIVE_TIMEOUT_S", 0.2)
    ready.behaviour["gate"] = asyncio.Event()  # never set: TL's connection test hangs
    ready.behaviour["entered"] = asyncio.Event()
    error = await asyncio.wait_for(_drive_error(assist, hass, feeder, located), 5)
    _assert_clean_error(error, "connection_failed")
    assert _tl_progress(hass) == []
    assert hass.config_entries.async_entries(TL_DOMAIN) == []


async def test_drive_timeout_constant(assist):
    assert assist.DRIVE_TIMEOUT_S == 60.0


async def test_create_entry_without_usable_entry_reports_kept(
        hass, assist, ready, feeder, located, monkeypatch):
    # Tuya Local created the entry, but the create_entry result carries no usable
    # entry: the drive must not report "nothing was created".
    manager = hass.config_entries.flow
    real_configure = manager.async_configure

    async def configure(flow_id, user_input=None):
        result = await real_configure(flow_id, user_input)
        if result.get("type") is FlowResultType.CREATE_ENTRY:
            result = {**result, "result": None}
        return result
    monkeypatch.setattr(manager, "async_configure", configure)
    error = await _drive_error(assist, hass, feeder, located)
    _assert_clean_error(error, "entry_not_loaded")
    kept = hass.config_entries.async_entries(TL_DOMAIN)
    assert len(kept) == 1 and kept[0].unique_id == FAKE_DEVICE_ID
    assert _tl_progress(hass) == []
    await hass.config_entries.async_remove(kept[0].entry_id)


async def test_drive_timeout_after_entry_created_reports_kept(
        hass, assist, ready, feeder, located, monkeypatch):
    # DRIVE_TIMEOUT_S fires while Tuya Local sets up the entry it already added.
    monkeypatch.setattr(assist, "DRIVE_TIMEOUT_S", 0.2)
    release = asyncio.Event()
    ready.behaviour["setup"] = "hang"
    ready.behaviour["setup_gate"] = release
    try:
        error = await asyncio.wait_for(_drive_error(assist, hass, feeder, located), 5)
    finally:
        release.set()
    _assert_clean_error(error, "entry_not_loaded")
    kept = hass.config_entries.async_entries(TL_DOMAIN)
    assert len(kept) == 1 and kept[0].unique_id == FAKE_DEVICE_ID
    assert _tl_progress(hass) == []
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(kept[0].entry_id)


async def test_entry_from_elsewhere_before_final_submit_is_not_kept(
        hass, assist, ready, feeder, located, monkeypatch):
    # A matching entry that appears while TL's connection test runs (and before
    # the final submit) was not created by this drive: the timeout stays
    # connection_failed and the entry is not claimed as kept.
    monkeypatch.setattr(assist, "DRIVE_TIMEOUT_S", 0.2)
    ready.behaviour["gate"] = asyncio.Event()  # never set
    ready.behaviour["entered"] = asyncio.Event()
    task = hass.async_create_task(_drive_error(assist, hass, feeder, located))
    await asyncio.wait_for(ready.behaviour["entered"].wait(), 5)
    other = MockConfigEntry(domain=TL_DOMAIN, unique_id=FAKE_DEVICE_ID,
                            data={"type": "some_other_profile"})
    other.add_to_hass(hass)
    error = await asyncio.wait_for(task, 5)
    _assert_clean_error(error, "connection_failed")
    assert _tl_progress(hass) == []


async def test_setup_retry_ends_load_wait_early(hass, assist, ready, feeder, located, monkeypatch):
    monkeypatch.setattr(assist, "ENTITY_WAIT_S", 5.0)
    ready.behaviour["setup"] = "retry"
    loop = asyncio.get_running_loop()
    start = loop.time()
    error = await _drive_error(assist, hass, feeder, located)
    elapsed = loop.time() - start
    assert error.reason == "entry_not_loaded"
    assert elapsed < 2.0, elapsed
    kept = hass.config_entries.async_entries(TL_DOMAIN)
    assert len(kept) == 1 and kept[0].state is ConfigEntryState.SETUP_RETRY  # kept
    await hass.config_entries.async_remove(kept[0].entry_id)


async def test_picks_config_type_option_with_empty_manufacturer_and_model(
        hass, assist, ready, feeder, located):
    # The real profile has no manufacturer/model products: TL offers "type||||".
    ready.behaviour["types"] = [
        ("petsafe_freshfeed_raw_v2", "", ""),
        ("petsafe_freshfeed_raw", "", ""),
        ("other_feeder", "", ""),
    ]
    entry_id = await assist.async_create_source_entry(hass, feeder, located, None, name="SYNTH Feeder")
    assert [v for kind, v in ready.seen if kind == "type"] == ["petsafe_freshfeed_raw||||"]
    assert hass.config_entries.async_get_entry(entry_id).data["type"] == "petsafe_freshfeed_raw"


@pytest.mark.parametrize("kwargs", [
    dict(data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID}),  # loaded, reusable
    dict(data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID},
         disabled_by=config_entries.ConfigEntryDisabler.USER, state=ConfigEntryState.NOT_LOADED),
    dict(source=config_entries.SOURCE_IGNORE, unique_id=FAKE_DEVICE_ID, data={},
         state=ConfigEntryState.NOT_LOADED),
    dict(data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID},
         state=ConfigEntryState.NOT_LOADED),
    dict(unique_id=FAKE_DEVICE_ID, data={"type": "other_profile", "device_id": "x"}),
    dict(data={"type": "petsafe_freshfeed_raw", "device_id": "x"},
         options={"device_id": FAKE_DEVICE_ID}),
], ids=["loaded", "disabled", "ignored", "not_loaded", "unique_id", "options"])
async def test_create_refuses_existing_entry_before_touching_flows(
        hass, assist, ready, feeder, located, kwargs):
    # A TL discovery flow for this device is in progress (started before the entry
    # existed); the guard must refuse before the pre-abort and before any flow starts.
    discovery = await hass.config_entries.flow.async_init(
        TL_DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"device_id": FAKE_DEVICE_ID, "host": FAKE_HOST, "version": "3.3"})
    assert discovery["type"] == "form"
    entry = _tl_entry(hass, **kwargs)
    error = await _drive_error(assist, hass, feeder, located)
    _assert_clean_error(error, "already_configured_other")
    assert ready.seen == []
    assert [f["flow_id"] for f in _tl_progress(hass)] == [discovery["flow_id"]]
    hass.config_entries.flow.async_abort(discovery["flow_id"])
    await hass.config_entries.async_remove(entry.entry_id)


# --- existing entries -----------------------------------------------------------


def _tl_entry(hass, *, state=ConfigEntryState.LOADED, **kwargs):
    entry = MockConfigEntry(domain=TL_DOMAIN, **kwargs)
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    return entry


async def test_existing_tuya_local_entry_reused_or_refused(hass, assist, stub_tl):
    assert await assist.async_existing_source(hass, FAKE_DEVICE_ID) is None
    unrelated = _tl_entry(hass, data={"type": "petsafe_freshfeed_raw", "device_id": "fakesynthdevOTHER"})
    assert await assist.async_existing_source(hass, FAKE_DEVICE_ID) is None

    reuse = _tl_entry(hass, data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID})
    assert await assist.async_existing_source(hass, FAKE_DEVICE_ID) == reuse.entry_id
    await hass.config_entries.async_remove(reuse.entry_id)

    by_options = _tl_entry(hass, data={"type": "petsafe_freshfeed_raw", "device_id": "x"},
                           options={"device_id": FAKE_DEVICE_ID})
    assert await assist.async_existing_source(hass, FAKE_DEVICE_ID) == by_options.entry_id
    await hass.config_entries.async_remove(by_options.entry_id)

    refused = [
        dict(source=config_entries.SOURCE_IGNORE, unique_id=FAKE_DEVICE_ID, data={},
             state=ConfigEntryState.NOT_LOADED),
        dict(data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID},
             disabled_by=config_entries.ConfigEntryDisabler.USER, state=ConfigEntryState.NOT_LOADED),
        dict(data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID},
             state=ConfigEntryState.NOT_LOADED),
        dict(data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID},
             state=ConfigEntryState.SETUP_ERROR),
        dict(data={"type": "other_profile", "device_id": FAKE_DEVICE_ID}),
        dict(data={"type": "petsafe_freshfeed_raw", "device_id": "x"},
             options={"type": "other_profile", "device_id": FAKE_DEVICE_ID}),
        dict(unique_id=FAKE_DEVICE_ID, data={"type": "other_profile", "device_id": "x"}),
    ]
    for kwargs in refused:
        entry = _tl_entry(hass, **kwargs)
        with pytest.raises(assist.AssistError) as caught:
            await assist.async_existing_source(hass, FAKE_DEVICE_ID)
        assert caught.value.reason == "already_configured_other", kwargs
        assert FAKE_DEVICE_ID not in str(caught.value) + repr(caught.value)
        await hass.config_entries.async_remove(entry.entry_id)

    # Two loaded matches are ambiguous: refused.
    _tl_entry(hass, data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID})
    _tl_entry(hass, data={"type": "petsafe_freshfeed_raw", "device_id": FAKE_DEVICE_ID})
    with pytest.raises(assist.AssistError) as caught:
        await assist.async_existing_source(hass, FAKE_DEVICE_ID)
    assert caught.value.reason == "already_configured_other"
    # The unrelated entry was never matched for this device and stays reusable for its own.
    assert await assist.async_existing_source(hass, "fakesynthdevOTHER") == unrelated.entry_id


# --- preflight and profile install ----------------------------------------------


async def test_preflight_refusals(hass, assist, stub_tl):
    S = assist.AssistStatus
    target = stub_tl.root / "devices" / "petsafe_freshfeed_raw.yaml"
    assert await assist.async_preflight(hass) is S.PROFILE_NEEDED
    target.write_bytes(BUNDLED_PROFILE.read_bytes() + b"# SYNTH local edit\n")
    assert await assist.async_preflight(hass) is S.PROFILE_CONFLICT
    with pytest.raises(assist.AssistError) as caught:
        await assist.async_install_profile(hass)
    assert caught.value.reason == "profile_install_failed"
    assert target.read_bytes().endswith(b"# SYNTH local edit\n")  # never overwritten
    target.unlink()
    target.symlink_to(BUNDLED_PROFILE)
    assert await assist.async_preflight(hass) is S.PROFILE_CONFLICT  # not a regular file
    target.unlink()
    target.write_bytes(BUNDLED_PROFILE.read_bytes())
    assert await assist.async_preflight(hass) is S.READY

    # Any pinned TL file changed or missing: unsupported.
    for name in TL_FILES:
        path = stub_tl.root / name
        original = path.read_bytes()
        path.write_bytes(original + b"# SYNTH drift\n")
        assert await assist.async_preflight(hass) is S.TUYA_LOCAL_UNSUPPORTED, name
        path.unlink()
        assert await assist.async_preflight(hass) is S.TUYA_LOCAL_UNSUPPORTED, name
        path.write_bytes(original)
    assert await assist.async_preflight(hass) is S.READY


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo unavailable")
async def test_preflight_fifo_profile_is_conflict(hass, assist, stub_tl):
    target = stub_tl.root / "devices" / "petsafe_freshfeed_raw.yaml"
    os.mkfifo(target)
    try:
        status = await asyncio.wait_for(assist.async_preflight(hass), 2)
    except TimeoutError:
        # Release the executor thread blocked in open() before failing.
        os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("preflight blocked opening a FIFO")
    assert status is assist.AssistStatus.PROFILE_CONFLICT


async def test_preflight_missing_tuya_local(hass, assist):
    from homeassistant import loader

    async def not_found(hass_, domain):
        raise loader.IntegrationNotFound(domain)
    import unittest.mock
    with unittest.mock.patch.object(assist, "async_get_integration", not_found):
        assert await assist.async_preflight(hass) is assist.AssistStatus.TUYA_LOCAL_MISSING


async def test_preflight_refuses_loaded_version_mismatch(hass, assist, ready):
    assert await assist.async_preflight(hass) is assist.AssistStatus.READY
    ready.integration.manifest["version"] = "2026.11.0"
    assert await assist.async_preflight(hass) is assist.AssistStatus.TUYA_LOCAL_UNSUPPORTED
    del ready.integration.manifest["version"]
    assert await assist.async_preflight(hass) is assist.AssistStatus.TUYA_LOCAL_UNSUPPORTED


async def test_install_profile_from_needed(hass, assist, stub_tl):
    devices = stub_tl.root / "devices"
    assert await assist.async_preflight(hass) is assist.AssistStatus.PROFILE_NEEDED
    await assist.async_install_profile(hass)
    target = devices / "petsafe_freshfeed_raw.yaml"
    assert target.read_bytes() == BUNDLED_PROFILE.read_bytes()
    assert sorted(p.name for p in devices.iterdir()) == ["petsafe_freshfeed_raw.yaml"]
    assert await assist.async_preflight(hass) is assist.AssistStatus.READY
    with pytest.raises(assist.AssistError):  # only from PROFILE_NEEDED
        await assist.async_install_profile(hass)


async def test_install_profile_never_replaces_racing_file(hass, assist, stub_tl, monkeypatch):
    devices = stub_tl.root / "devices"
    target = devices / "petsafe_freshfeed_raw.yaml"
    real_link = os.link

    def racing_link(src, dst, *args, **kwargs):
        Path(dst).write_bytes(b"# SYNTH raced\n")
        return real_link(src, dst, *args, **kwargs)
    monkeypatch.setattr(assist.os, "link", racing_link)
    with pytest.raises(assist.AssistError) as caught:
        await assist.async_install_profile(hass)
    assert caught.value.reason == "profile_install_failed"
    assert target.read_bytes() == b"# SYNTH raced\n"
    assert sorted(p.name for p in devices.iterdir()) == ["petsafe_freshfeed_raw.yaml"]


async def test_install_profile_os_error(hass, assist, stub_tl):
    devices = stub_tl.root / "devices"
    devices.chmod(0o500)
    try:
        if os.access(devices, os.W_OK):
            pytest.skip("running with privileges that ignore directory permissions")
        with pytest.raises(assist.AssistError) as caught:
            await assist.async_install_profile(hass)
        assert caught.value.reason == "profile_install_failed"
    finally:
        devices.chmod(0o700)


def test_assist_error_closed_reasons(assist):
    for reason in ("unexpected_step", "connection_failed", "not_supported",
                   "already_configured_other", "already_in_progress", "entry_not_loaded",
                   "aborted", "profile_install_failed"):
        error = assist.AssistError(reason)
        assert error.reason == reason and str(error) == reason and error.args == (reason,)
    with pytest.raises(ValueError):
        assist.AssistError(FAKE_KEY)
    assert {s.name for s in assist.AssistStatus} == {
        "READY", "PROFILE_NEEDED", "TUYA_LOCAL_MISSING", "TUYA_LOCAL_UNSUPPORTED", "PROFILE_CONFLICT"}


# --- the real pins and the pinned Tuya Local contract ---------------------------


def test_real_pins_and_profile_copy(assist):
    release, pins = assist._load_pins()
    assert release == PINNED_RELEASE
    assert set(pins) == {f"custom_components/tuya_local/{name}" for name in TL_FILES}
    assert all(len(v) == 64 for v in pins.values())
    assert (BUNDLED_PROFILE.read_bytes()
            == (REPO / "config/tuya_local/petsafe_freshfeed_raw.yaml").read_bytes())


def _ast_class(tree, name):
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def _method(cls, name):
    return next(n for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _kw_strings(node, func, kw):
    found = []
    for call in ast.walk(node):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == func):
            for keyword in call.keywords:
                if keyword.arg == kw and isinstance(keyword.value, ast.Constant):
                    found.append(keyword.value.value)
    return found


def test_pinned_tl_flow_contract(assist):
    if PINNED_TL is None or not PINNED_TL.is_dir():
        pytest.skip("PETSAFE_PINNED_TUYA_LOCAL is unset or not a directory: "
                    "pinned public Tuya Local source check skipped")
    release, pins = assist._load_pins()
    for name in TL_FILES:
        digest = hashlib.sha256((PINNED_TL / name).read_bytes()).hexdigest()
        assert digest == pins[f"custom_components/tuya_local/{name}"], name
    manifest = json.loads((PINNED_TL / "manifest.json").read_text())
    assert manifest["version"] == release == PINNED_RELEASE

    const = {}
    for node in ast.parse((PINNED_TL / "const.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            const[node.targets[0].id] = node.value.value
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            const[node.targets[0].id] = [e.value for e in node.value.elts]
    source = (PINNED_TL / "config_flow.py").read_text()
    tree = ast.parse(source)
    flow = _ast_class(tree, "ConfigFlowHandler")
    methods = {n.name for n in flow.body if isinstance(n, ast.AsyncFunctionDef)}
    for step in STUB_STEP_IDS + ("integration_discovery",):
        assert f"async_step_{step}" in methods, step
    assert all(
        any(isinstance(n, ast.Assign) and n.targets[0].id == attr
            and n.value.value == getattr(StubTuyaLocalFlow, attr) for n in flow.body)
        for attr in ("VERSION", "MINOR_VERSION"))

    # user: setup_mode with the three options.
    user = _method(flow, "async_step_user")
    assert _kw_strings(user, "async_show_form", "step_id") == ["user"]
    lists = [[e.value for e in kw.value.elts] for call in ast.walk(user)
             if isinstance(call, ast.Call) for kw in call.keywords
             if kw.arg == "options" and isinstance(kw.value, ast.List)]
    assert lists == [STUB_SETUP_MODES] and assist.SETUP_MODE_MANUAL in STUB_SETUP_MODES
    assert "fields[vol.Required('setup_mode')] = SelectSelector(" in ast.unparse(user)
    assert "mode == 'manual'" in ast.unparse(user)

    # local: field names in order, protocol choices, unique id set after the test.
    local = _method(flow, "async_step_local")
    assert _kw_strings(local, "async_show_form", "step_id") == ["local"]
    fields = []
    for call in ast.walk(local):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in ("Required", "Optional") and call.args
                and isinstance(call.args[0], ast.Name)):
            fields.append(const.get(call.args[0].id, {"CONF_HOST": "host"}.get(call.args[0].id)))
    assert tuple(fields) == STUB_LOCAL_FIELDS == assist.LOCAL_FIELDS
    assert ["auto"] + [str(v) for v in const["API_PROTOCOL_VERSIONS"]] == STUB_PROTOCOLS
    text = ast.unparse(local)
    assert text.index("async_test_connection") < text.index("async_set_unique_id") \
        < text.index("_abort_if_unique_id_configured") < text.index("async_step_select_type")
    assert "errors['base'] = 'connection'" in text

    # select_type: compound value, SelectSelector, both step ids, not_supported abort.
    select = _method(flow, "async_step_select_type")
    text = ast.unparse(select)
    assert "f\"{dev_type.config_type}||{manufacturer or ''}||{model or ''}\"" in text
    assert "SelectOptionDict(value=key, label=label)" in text
    assert "SelectSelector(SelectSelectorConfig(options=type_options))" in text
    assert const["CONF_TYPE"] == STUB_TYPE_FIELD == assist.TYPE_FIELD
    assert sorted(_kw_strings(select, "async_show_form", "step_id")) == [
        "select_type", "select_type_auto_detected"]
    assert _kw_strings(select, "async_abort", "reason") == ["not_supported"]
    assert assist.TYPE_PREFIX == "petsafe_freshfeed_raw||"

    # choose_entities: the name field, then create_entry.
    choose = _method(flow, "async_step_choose_entities")
    assert _kw_strings(choose, "async_show_form", "step_id") == ["choose_entities"]
    assert "vol.Required(CONF_NAME, default=default_name): str" in ast.unparse(choose)
    assert assist.NAME_FIELD == STUB_NAME_FIELD == "name"

    # Only these abort reasons exist in the config flow (plus HA's already_configured).
    reasons = set(_kw_strings(flow, "async_abort", "reason"))
    assert reasons <= set(STUB_ABORTS) | {"no_devices"}
    assert set(assist.STEP_IDS) == set(STUB_STEP_IDS)

    # Discovery flows carry the device ID as unique id (what the pre-abort matches).
    discovery = ast.unparse(ast.parse((PINNED_TL / "discovery.py").read_text()))
    assert "context={'source': SOURCE_INTEGRATION_DISCOVERY}" in discovery
    discover = _method(flow, "async_step_integration_discovery")
    assert "await self.async_set_unique_id(device_id)" in ast.unparse(discover)

    # Ignored entries carry only unique_id; get_config rescans devices/ on each call.
    init = (PINNED_TL / "__init__.py").read_text()
    assert "entry.unique_id\n        or entry.data.get(CONF_DEVICE_CID)" in init
    device_config = (PINNED_TL / "helpers/device_config.py").read_text()
    assert "with scandir(_CONFIG_DIR) as entries:" in device_config
    assert (PINNED_TL / "devices").is_dir()
