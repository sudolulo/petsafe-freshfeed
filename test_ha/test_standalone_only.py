"""Standalone cutoff and inert historical-data regression contracts."""
import ast
import builtins
from copy import deepcopy
import io
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petsafe_freshfeed import async_setup_entry
from custom_components.petsafe_freshfeed.const import DOMAIN
from test_owned_upgrade_051 import seed, assert_identity, assert_retained
from test_owned_hub import owned  # noqa: F401
from test_owned_source import pinned, no_tuya_local  # noqa: F401


@pytest.mark.parametrize("backend", [None, "legacy", "unknown"])
async def test_non_owned_setup_fails_translated_without_mutation(hass, monkeypatch, backend):
    from custom_components.petsafe_freshfeed import hub, repairs
    from homeassistant.helpers.storage import Store
    data = {"opaque": {"keep": True}}
    if backend is not None:
        data["connection_backend"] = backend
    entry = MockConfigEntry(domain=DOMAIN, data=data, options={"opaque": True})
    entry.add_to_hass(hass)
    before = deepcopy((dict(entry.data), dict(entry.options)))
    devices = dict(dr.async_get(hass)._devices)
    entities = dict(er.async_get(hass).entities)
    for name in ("async_clear_offline_issue", "async_reconcile_protocol_issue"):
        monkeypatch.setattr(repairs, name, Mock(side_effect=AssertionError(name)))
    monkeypatch.setattr(hub, "FreshFeedHub", Mock(side_effect=AssertionError("hub constructed")))
    monkeypatch.setattr(Store, "async_load", AsyncMock(side_effect=AssertionError("storage read")))
    with pytest.raises(ConfigEntryError) as raised:
        await async_setup_entry(hass, entry)
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "legacy_backend_removed"
    assert (dict(entry.data), dict(entry.options)) == before[:2]
    assert dict(dr.async_get(hass)._devices) == devices
    assert dict(er.async_get(hass).entities) == entities


@pytest.mark.parametrize("backend", [None, "legacy", "unknown"])
@pytest.mark.parametrize("kind", ["options", "reconfigure"])
async def test_non_owned_options_reconfigure_refuse_without_provision(hass, monkeypatch, backend, kind):
    from custom_components.petsafe_freshfeed import config_flow
    data = {"opaque": True, **({"connection_backend": backend} if backend else {})}
    entry = MockConfigEntry(domain=DOMAIN, data=data, options={"keep": True})
    entry.add_to_hass(hass)
    monkeypatch.setattr(config_flow, "_async_provision", AsyncMock(side_effect=AssertionError("provision")))
    monkeypatch.setattr(config_flow, "_async_probe", AsyncMock(side_effect=AssertionError("probe")))
    if kind == "options":
        result = await hass.config_entries.options.async_init(entry.entry_id)
    else:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": kind, "entry_id": entry.entry_id})
    assert result["reason"] == "legacy_backend_removed"
    assert dict(entry.data) == data and dict(entry.options) == {"keep": True}


def trap_journal_io(monkeypatch, journal_dir):
    """Reject direct opens and directory scans of migration leftovers only."""
    def guarded(original):
        def call(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                candidate = Path(os.fsdecode(path)).absolute()
                if candidate == journal_dir or journal_dir in candidate.parents:
                    raise AssertionError(f"migration journal accessed: {candidate}")
            return original(path, *args, **kwargs)
        return call

    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    monkeypatch.setattr(io, "open", guarded(io.open))
    monkeypatch.setattr(os, "open", guarded(os.open))
    monkeypatch.setattr(os, "scandir", guarded(os.scandir))
    monkeypatch.setattr(os, "listdir", guarded(os.listdir))


@pytest.mark.parametrize("journal", ["valid", "malformed", "unreadable"])
@pytest.mark.parametrize("source_change", ["changed", "deleted"])
async def test_owned_setup_ignores_leftover_journals(owned, monkeypatch, journal, source_change):
    from homeassistant.config_entries import ConfigEntryDisabler
    entry, record = await seed(owned, monkeypatch)
    source = MockConfigEntry(domain="tuya_local", entry_id="former-source", data={"device_id": "old-device"}, disabled_by=ConfigEntryDisabler.USER)
    source.add_to_hass(owned.hass)
    data = {**entry.data, "migrated_from": source.entry_id, "source_entry_id": source.entry_id,
            "migration_staged_connection": {"opaque": "inert"}}
    owned.hass.config_entries.async_update_entry(entry, data=data)
    path = Path(owned.hass.config.config_dir) / ".storage/petsafe_freshfeed_migrations" / f"{entry.entry_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = '{"version":1}' if journal == "valid" else '{broken'
    path.write_text(content)
    if journal == "unreadable":
        path.chmod(0)
    if source_change == "deleted":
        await owned.hass.config_entries.async_remove(source.entry_id)
    else:
        owned.hass.config_entries.async_update_entry(source, options={"host": "192.168.210.99"})
    try:
        with monkeypatch.context() as traps:
            trap_journal_io(traps, path.parent)
            assert await owned.hass.config_entries.async_setup(entry.entry_id)
            await owned.hass.async_block_till_done()
        assert_identity(owned, entry)
        await assert_retained(owned, entry, record)
        assert entry.runtime_data.snapshot.available
        assert dict(entry.data) == data
        with monkeypatch.context() as traps:
            trap_journal_io(traps, path.parent)
            assert await owned.hass.config_entries.async_reload(entry.entry_id)
            await owned.hass.async_block_till_done()
        assert_identity(owned, entry)
        await assert_retained(owned, entry, record)
        assert owned.device.control_frames() == []
    finally:
        path.chmod(0o600)
    assert path.read_text() == content


async def test_direct_owned_hub_start_ignores_journals(owned, monkeypatch):
    from custom_components.petsafe_freshfeed.hub import FreshFeedHub

    entry, record = await seed(owned, monkeypatch)
    journal_dir = Path(owned.hass.config.config_dir) / ".storage/petsafe_freshfeed_migrations"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / f"{entry.entry_id}.json").write_text("{broken")
    entry.runtime_data = FreshFeedHub(owned.hass, entry)
    with monkeypatch.context() as traps:
        trap_journal_io(traps, journal_dir)
        await entry.runtime_data.async_start()
        await owned.hass.async_block_till_done()
        await assert_retained(owned, entry, record)
    assert entry.runtime_data.snapshot.available
    assert owned.device.control_frames() == []


def test_no_migration_admission_imports_or_handlers():
    from custom_components.petsafe_freshfeed import config_flow, hub
    import inspect
    assert list(inspect.signature(hub.FreshFeedHub.async_start).parameters) == ["self"]
    for cls in (config_flow.FreshFeedConfigFlow, config_flow.FreshFeedOptionsFlow):
        for name in ("existing", "migrate", "migrate_host", "resume", "cancel", "remove_source"):
            assert not hasattr(cls, f"async_step_{name}")
    root = Path(__file__).parents[1] / "custom_components/petsafe_freshfeed"
    for name in ("__init__.py", "config_flow.py", "hub.py"):
        tree = ast.parse((root / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("migration")
                assert all(not alias.name.startswith("migration") for alias in node.names)
