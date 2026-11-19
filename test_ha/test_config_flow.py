"""Standalone provisioning regressions, ported from borrowed-source flows."""
import asyncio
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

import attrs
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.petsafe_freshfeed import store_bootstrap

DOMAIN = "petsafe_freshfeed"
REPO = Path(__file__).resolve().parent.parent


async def test_missing_profile_never_falls_back(hass, source_entry):
    from custom_components.petsafe_freshfeed import account
    with patch("custom_components.petsafe_freshfeed.config_flow.account.load_app_profile",
               side_effect=account.ProfileUnavailable()):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_unavailable"


def test_package_import_has_no_ha_at_module_level():
    proc = subprocess.run(["/usr/bin/python3", "-B", "-c",
        "import sys, custom_components.petsafe_freshfeed.sqlite_witness;assert 'homeassistant' not in sys.modules"],
        cwd=REPO, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
def provision_context(hass, tmp_path, monkeypatch):
    root = tmp_path / "config"
    (root / ".storage").mkdir(parents=True)
    monkeypatch.setattr(hass.config, "config_dir", str(root))
    entry = MockConfigEntry(domain=DOMAIN, data={"connection_backend": "owned", "device_id": "synthetic-device", "inert_history": {"preserve": True}})
    entry.add_to_hass(hass)
    leaf = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("tuya_local", "synthetic-device")})
    calls = []
    real = store_bootstrap.async_provision_store
    async def counted(config_root, *, binding_id, store_id):
        calls.append((config_root, binding_id, store_id))
        return await real(config_root, binding_id=binding_id, store_id=store_id)
    monkeypatch.setattr(store_bootstrap, "async_provision_store", counted)
    return entry, leaf, root / ".storage", calls


async def enable(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(result["flow_id"], {"enable_controls": True})


async def test_options_provisions_once_preserving_data(hass, provision_context):
    entry, leaf, root, calls = provision_context
    before = dict(entry.data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.data) == before and calls == [] and os.listdir(root) == []
    result = await enable(hass, entry)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.data) == {**before, "intent_binding_id": leaf.id, "intent_store_id": entry.data["intent_store_id"]}
    assert len(entry.data["intent_store_id"]) == 32
    int(entry.data["intent_store_id"], 16)
    assert calls == [(str(root), leaf.id, entry.data["intent_store_id"])]
    database = root / "petsafe_freshfeed" / leaf.id / "intent.sqlite"
    assert database.is_file() and database.stat().st_size > 0
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"enable_controls": True})
    assert result["reason"] == "controls_already_enabled" and len(calls) == 1


async def test_provision_failure_persists_nothing(hass, provision_context, monkeypatch):
    entry, leaf, root, calls = provision_context
    before = dict(entry.data)
    async def fail(*args, **kwargs):
        raise store_bootstrap.StoreBootstrapError()
    monkeypatch.setattr(store_bootstrap, "async_provision_store", fail)
    result = await enable(hass, entry)
    assert result["reason"] == "provision_failed"
    assert dict(entry.data) == before and os.listdir(root) == []


@pytest.mark.parametrize("changes", [{"config_subentry_id": "subentry"}, {"composite_subentries": {"source": {None}}}, {"identifiers": {("tuya_local", "foreign")}}])
async def test_ineligible_owned_leaf_never_provisions(hass, provision_context, changes):
    entry, leaf, root, calls = provision_context
    before = dict(entry.data)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "registry_device_id": leaf.id})
    before = dict(entry.data)
    registry = dr.async_get(hass)
    original = registry.async_get
    invalid = attrs.evolve(leaf, **changes)
    with patch.object(registry, "async_get", side_effect=lambda ident: invalid if ident == leaf.id else original(ident)):
        result = await enable(hass, entry)
    assert result["reason"] == "binding_unavailable"
    assert dict(entry.data) == before and calls == [] and os.listdir(root) == []


async def test_partial_directory_never_adopted(hass, provision_context):
    entry, leaf, root, calls = provision_context
    before = dict(entry.data)
    partial = root / "petsafe_freshfeed" / leaf.id
    partial.mkdir(parents=True, mode=0o700)
    os.chmod(partial.parent, 0o700)
    result = await enable(hass, entry)
    assert result["reason"] == "provision_failed"
    assert dict(entry.data) == before and len(calls) == 1 and os.listdir(partial) == []


async def test_cancelled_options_provisioning_persists_nothing_and_never_retries(hass, provision_context, monkeypatch):
    entry, leaf, root, calls = provision_context
    before = dict(entry.data)
    entered, release = threading.Event(), threading.Event()
    real_mkdir = store_bootstrap._mkdir
    def gated_mkdir(name, fd):
        real_mkdir(name, fd)
        if name != "petsafe_freshfeed":
            entered.set()
            release.wait(5)
    monkeypatch.setattr(store_bootstrap, "_mkdir", gated_mkdir)
    task = asyncio.ensure_future(enable(hass, entry))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dict(entry.data) == before
    assert os.listdir(root / "petsafe_freshfeed" / leaf.id) == []
    assert not [t for t in threading.enumerate() if t.name.startswith("petsafe-bootstrap")]
    for flow in hass.config_entries.options.async_progress():
        hass.config_entries.options.async_abort(flow["flow_id"])
    result = await enable(hass, entry)
    assert result["reason"] == "provision_failed"
    assert dict(entry.data) == before and len(calls) == 2
