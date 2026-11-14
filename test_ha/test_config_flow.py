"""Config flow tests for petsafe_freshfeed."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from conftest import SOURCE_ENTRY_ID, make_source_entry

DOMAIN = "petsafe_freshfeed"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
async def mock_setup_entry(hass):
    """Flow tests do not set the integration up (platforms are covered elsewhere).

    Created entries are unloaded while the patch is still active, so hass teardown never
    runs the real unload against an entry the real setup never touched.
    """
    with patch(
        "custom_components.petsafe_freshfeed.async_setup_entry", AsyncMock(return_value=True)
    ) as mock, patch(
        "custom_components.petsafe_freshfeed.async_unload_entry", AsyncMock(return_value=True)
    ):
        yield mock
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(entry.entry_id)


async def _init(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_no_source_aborts(hass):
    result = await _init(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_source"


async def test_wrong_profile_ignored(hass):
    make_source_entry(hass, data_type="other_profile")
    result = await _init(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_source"


async def test_single_source_creates_entry(hass, source_entry):
    result = await _init(hass)
    assert result["type"] is FlowResultType.FORM
    assert "SECRET" not in repr(result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "PetSafe FreshFeed"
    assert result["data"] == {"source_entry_id": SOURCE_ENTRY_ID}
    assert "SECRET" not in repr(result)
    entry = result["result"]
    assert entry.unique_id == SOURCE_ENTRY_ID
    assert "SECRET" not in repr(dict(entry.data)) + repr(dict(entry.options))


async def test_options_type_wins(hass):
    make_source_entry(hass, data_type="other", options={"type": "petsafe_freshfeed_raw"})
    result = await _init(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": SOURCE_ENTRY_ID}


async def test_options_type_wins_over_matching_data(hass):
    make_source_entry(hass, options={"type": "other"})
    result = await _init(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_source"


async def test_multiple_sources_select(hass, source_entry):
    second = make_source_entry(hass, entry_id="01M5NATKW5YS05D36KGFB38D8Q", title="FreshFeed Two")
    result = await _init(hass)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    assert [str(key) for key in schema] == ["source_entry_id", "enable_controls"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source_entry_id": second.entry_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "FreshFeed Two"
    assert result["data"] == {"source_entry_id": second.entry_id}
    assert result["result"].unique_id == second.entry_id


async def test_duplicate_aborts(hass, source_entry):
    result = await _init(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    result = await _init(hass)
    if result["type"] is FlowResultType.FORM:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def test_package_import_has_no_ha_at_module_level():
    proc = subprocess.run(
        ["/usr/bin/python3", "-B", "-c",
         "import sys, custom_components.petsafe_freshfeed.sqlite_witness;"
         "assert 'homeassistant' not in sys.modules"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr


# ----- explicit durable-store provisioning (Task 7) -----------------------------
#
# Every store lives under an owned synthetic config directory (tmp_path), never a
# real HA .storage. No network, no credentials.

import asyncio  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402

import attrs  # noqa: E402
from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: E402

from custom_components.petsafe_freshfeed import store_bootstrap  # noqa: E402

IDS = {"source_entry_id", "intent_store_id", "intent_binding_id"}


@pytest.fixture
def config_root(hass, tmp_path, monkeypatch):
    config = tmp_path / "config"
    (config / ".storage").mkdir(parents=True)
    monkeypatch.setattr(hass.config, "config_dir", str(config))
    return config / ".storage"


@pytest.fixture
def provisions(monkeypatch):
    """Count calls to the one store creator; still runs the real provisioning."""
    calls = []
    real = store_bootstrap.async_provision_store

    async def counted(config_root, *, binding_id, store_id):
        calls.append((config_root, binding_id, store_id))
        return await real(config_root, binding_id=binding_id, store_id=store_id)
    monkeypatch.setattr(store_bootstrap, "async_provision_store", counted)
    return calls


def _leaf(hass, raw_entities):
    return er.async_get(hass).async_get(raw_entities[145]).device_id


def _database(config_root, binding_id):
    return config_root / "petsafe_freshfeed" / binding_id / "intent.sqlite"


async def test_user_flow_enable_controls_provisions_once_then_persists_ids(
        hass, source_entry, raw_entities, config_root, provisions):
    result = await _init(hass)
    assert [str(key) for key in result["data_schema"].schema] == ["enable_controls"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = dict(result["data"])
    assert set(data) == IDS
    binding_id = _leaf(hass, raw_entities)
    assert data["intent_binding_id"] == binding_id
    assert len(data["intent_store_id"]) == 32
    int(data["intent_store_id"], 16)
    assert provisions == [(str(config_root), binding_id, data["intent_store_id"])]
    database = _database(config_root, binding_id)
    assert database.is_file() and database.stat().st_size > 0
    assert "local_key" not in repr(data)


async def test_user_flow_without_controls_is_read_only_and_creates_nothing(
        hass, source_entry, raw_entities, config_root, provisions):
    result = await _init(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_controls": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"source_entry_id": SOURCE_ENTRY_ID}
    assert provisions == [] and os.listdir(config_root) == []


async def test_provision_failure_persists_nothing(
        hass, source_entry, raw_entities, config_root, monkeypatch):
    async def fail(*args, **kwargs):
        raise store_bootstrap.StoreBootstrapError()
    monkeypatch.setattr(store_bootstrap, "async_provision_store", fail)
    result = await _init(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "provision_failed"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_partial_binding_directory_is_never_adopted_or_retried(
        hass, source_entry, raw_entities, config_root, provisions):
    binding_id = _leaf(hass, raw_entities)
    partial = config_root / "petsafe_freshfeed" / binding_id
    partial.mkdir(parents=True, mode=0o700)
    os.chmod(partial.parent, 0o700)
    result = await _init(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "provision_failed"
    assert len(provisions) == 1  # exactly one attempt, no retry
    assert os.listdir(partial) == []  # left untouched, never adopted
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_missing_registry_leaf_aborts_without_provisioning(
        hass, source_entry, config_root, provisions):
    result = await _init(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "binding_unavailable"
    assert provisions == [] and os.listdir(config_root) == []


@pytest.mark.parametrize("device_changes", [
    {"config_subentry_id": "subentry"},
    {"composite_subentries": {"source": {None}}},
], ids=["subentry", "composite"])
@pytest.mark.parametrize("flow_kind", ["user", "options"])
async def test_ineligible_registry_device_never_provisions(
        hass, source_entry, raw_entities, config_root, provisions, device_changes, flow_kind):
    entry = await _read_only_entry(hass) if flow_kind == "options" else None
    registry = dr.async_get(hass)
    leaf_id = _leaf(hass, raw_entities)
    real_get = registry.async_get
    device = attrs.evolve(real_get(leaf_id), **device_changes)
    with patch.object(registry, "async_get", side_effect=lambda device_id: (
            device if device_id == leaf_id else real_get(device_id))):
        if entry is None:
            result = await _init(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"enable_controls": True})
        else:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "binding_unavailable"
    assert provisions == [] and os.listdir(config_root) == []
    if entry is None:
        assert hass.config_entries.async_entries(DOMAIN) == []
    else:
        assert dict(entry.data) == {"source_entry_id": SOURCE_ENTRY_ID}


@pytest.mark.parametrize("case", ["missing_id", "empty_id", "nul_id", "child", "mismatched_identifier", "options_override"])
@pytest.mark.parametrize("flow_kind", ["user", "options"])
async def test_invalid_source_identity_never_provisions(
        hass, source_entry, raw_entities, config_root, provisions, case, flow_kind):
    entry = await _read_only_entry(hass) if flow_kind == "options" else None
    data, options = dict(source_entry.data), dict(source_entry.options)
    if case == "missing_id":
        data.pop("device_id")
    elif case == "empty_id":
        data["device_id"] = ""
    elif case == "nul_id":
        data["device_id"] = "bad\x00id"
    elif case == "child":
        options["device_cid"] = "child"
    elif case == "options_override":
        options["device_id"] = "another-device"
    hass.config_entries.async_update_entry(source_entry, data=data, options=options)
    registry = dr.async_get(hass)
    leaf_id = _leaf(hass, raw_entities)
    real_get = registry.async_get
    device = real_get(leaf_id)
    if case == "mismatched_identifier":
        device = attrs.evolve(device, identifiers={("tuya_local", "another-device")})
    with patch.object(registry, "async_get", side_effect=lambda device_id: (
            device if device_id == leaf_id else real_get(device_id))):
        if entry is None:
            result = await _init(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"enable_controls": True})
        else:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "binding_unavailable"
    assert provisions == [] and os.listdir(config_root) == []
    if entry is None:
        assert hass.config_entries.async_entries(DOMAIN) == []
    else:
        assert dict(entry.data) == {"source_entry_id": SOURCE_ENTRY_ID}


async def _read_only_entry(hass):
    result = await _init(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {"source_entry_id": SOURCE_ENTRY_ID}
    return result["result"]


async def test_existing_entry_stays_read_only_until_options_enable_controls(
        hass, source_entry, raw_entities, config_root, provisions):
    entry = await _read_only_entry(hass)
    assert provisions == []
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert [str(key) for key in result["data_schema"].schema] == ["enable_controls"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.data) == {"source_entry_id": SOURCE_ENTRY_ID} and provisions == []
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(entry.data) == IDS and dict(entry.options) == {}
    binding_id = _leaf(hass, raw_entities)
    assert entry.data["intent_binding_id"] == binding_id
    assert len(provisions) == 1 and _database(config_root, binding_id).is_file()
    # Already enabled: no second provisioning and no way to clear the IDs.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "controls_already_enabled"
    assert len(provisions) == 1


async def test_options_provision_failure_keeps_entry_read_only(
        hass, source_entry, raw_entities, config_root, monkeypatch):
    entry = await _read_only_entry(hass)

    async def fail(*args, **kwargs):
        raise store_bootstrap.StoreBootstrapError()
    monkeypatch.setattr(store_bootstrap, "async_provision_store", fail)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "provision_failed"
    assert dict(entry.data) == {"source_entry_id": SOURCE_ENTRY_ID}


async def test_cancelled_options_provisioning_persists_nothing_and_never_retries(
        hass, source_entry, raw_entities, config_root, provisions, monkeypatch):
    entry = await _read_only_entry(hass)
    entered, release = threading.Event(), threading.Event()
    real_mkdir = store_bootstrap._mkdir

    def gated_mkdir(name, fd):
        real_mkdir(name, fd)
        if name != "petsafe_freshfeed":
            entered.set()
            release.wait(5)
    monkeypatch.setattr(store_bootstrap, "_mkdir", gated_mkdir)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    task = asyncio.ensure_future(hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True}))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # the actual filesystem work drains first
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dict(entry.data) == {"source_entry_id": SOURCE_ENTRY_ID}
    binding_id = _leaf(hass, raw_entities)
    assert os.listdir(config_root / "petsafe_freshfeed" / binding_id) == []
    assert not [thread for thread in threading.enumerate()
                if thread.name.startswith("petsafe-bootstrap")]
    # A later explicit attempt refuses the partial reservation; nothing adopted.
    for flow in hass.config_entries.options.async_progress():
        hass.config_entries.options.async_abort(flow["flow_id"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "provision_failed"
    assert dict(entry.data) == {"source_entry_id": SOURCE_ENTRY_ID}
    assert len(provisions) == 2
