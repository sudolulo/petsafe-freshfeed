"""Lifecycle tests for FreshFeed's owned client and durable adapter."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petsafe_freshfeed.const import DOMAIN
from test_owned_source import pinned, no_tuya_local  # noqa: F401
from test_owned_hub import owned, owned_data, make_entry, leaf_for  # noqa: F401


async def until(predicate, *, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _ids(store_id, binding_id):
    from custom_components.petsafe_freshfeed.const import CONF_INTENT_BINDING_ID, CONF_INTENT_STORE_ID
    return {CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: binding_id}


async def _provision(ctx, *, retained=None, operation='feed'):
    from custom_components.petsafe_freshfeed.store_bootstrap import async_provision_store, open_store
    store_id = uuid4().hex
    await async_provision_store(ctx.root, binding_id=ctx.leaf, store_id=store_id)
    if retained is not None:
        await open_store(ctx.root, binding_id=ctx.leaf, store_id=store_id).async_prepare_intent(
            {"operation_id": retained, "operation": operation, "state": "pending"})
    return store_id


async def _setup(ctx, extra=None, *, expect=True):
    entry = ctx.entry
    ctx.hass.config_entries.async_update_entry(entry, data={**entry.data, **(extra or {})})
    assert await ctx.hass.config_entries.async_setup(entry.entry_id) is expect
    return entry


async def _load(ctx, store_id):
    from custom_components.petsafe_freshfeed.store_bootstrap import open_store
    return await open_store(ctx.root, binding_id=ctx.leaf, store_id=store_id).async_load_last_intent()


async def _current(hub):
    try:
        await until(lambda: hub.adapter is not None and hub.bridge is not None and hub.bridge.is_current())
    except TimeoutError as exc:
        raise AssertionError(f"bridge={hub.bridge!r} adapter={hub.adapter!r} controls={hub._controls_reason!r} schedule_error={hub.schedule.error!r} attach={hub._attach_task!r}") from exc
    from test_write_adapter import _queue_reports
    _queue_reports(SimpleNamespace(bridge=hub.bridge),
                   {"4":"standby", "145":2, "140":"EAAA", "142":0})


@pytest.fixture
async def lifecycle(owned):
    """Real owned source/transport over the encrypted protocol-3.5 fake."""
    owned.entry = make_entry(owned, owned_data())
    owned.leaf = leaf_for(owned, owned.entry).id
    owned.hass.config_entries.async_update_entry(
        owned.entry, data={**owned.entry.data, "registry_device_id": owned.leaf})
    owned.writes = owned.device.controls
    return owned


async def test_coordinator_retries_transient_adapter_compatibility_failure(lifecycle, monkeypatch):
    from custom_components.petsafe_freshfeed import hub as hub_module
    ctx=lifecycle
    real_create=hub_module.async_create_adapter; calls=0
    async def fail_once(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==1: raise ValueError("temporary compatibility check failure")
        return await real_create(*args,**kwargs)
    monkeypatch.setattr(hub_module,"async_create_adapter",fail_once)
    store_id=await _provision(ctx); entry=await _setup(ctx,_ids(store_id,ctx.leaf)); hub=entry.runtime_data
    await until(lambda:hub._controls_reason=="source_incompatible")
    assert hub.adapter is None and calls==1
    # The hub's existing one-second periodic bridge check retries transient
    # adapter construction; it performs no device write.
    await until(lambda: hub.adapter is not None)
    assert calls>=2 and hub._controls_reason is None
    assert await ctx.hass.config_entries.async_unload(entry.entry_id)


async def test_read_only_startup_never_provisions(lifecycle):
    ctx = lifecycle
    entry = await _setup(ctx)
    await until(lambda: entry.runtime_data.bridge is not None)
    hub = entry.runtime_data
    assert hub.adapter is None and hub.witness is None
    assert not (Path(ctx.root) / "petsafe_freshfeed").exists()
    assert not ctx.writes


@pytest.mark.parametrize("corrupt", [False, True])
async def test_missing_or_corrupt_store_is_not_repaired(lifecycle, corrupt):
    ctx = lifecycle
    store_id = uuid4().hex
    path = Path(ctx.root) / "petsafe_freshfeed" / ctx.leaf / "intent.sqlite"
    if corrupt:
        path.parent.mkdir(parents=True, mode=0o700)
        path.parent.parent.chmod(0o700)
        path.write_bytes(b"not a sqlite database")
        path.chmod(0o600)
    before = path.read_bytes() if corrupt else None
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    await _current(entry.runtime_data)
    view = entry.runtime_data.adapter.view
    assert view["state"] == "blocked"
    assert view["storage_fault"] or view["integrity_fault"]
    assert (path.read_bytes() if path.exists() else None) == before
    assert not ctx.writes


@pytest.mark.parametrize('operation', ['feed', 'cancel'])
async def test_reloaded_coordinator_rearms_retained_intent_without_replay(lifecycle, operation):
    ctx = lifecycle
    operation_id = uuid4().hex
    store_id = await _provision(ctx, retained=operation_id, operation=operation)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    await _current(entry.runtime_data)
    old = entry.runtime_data.adapter
    assert old.view["state"] == "uncertain"
    session = old.view["session_id"]
    assert await ctx.hass.config_entries.async_reload(entry.entry_id)
    await _current(entry.runtime_data)
    new = entry.runtime_data.adapter
    assert new is not old and old._stopped
    assert new.view["state"] == "uncertain"
    assert new.view["operation_id"] == operation_id
    assert new.view["operation"] == operation
    assert new.view["session_id"] != session
    assert not await new.async_recover(operation_id, session, confirmed=True)
    if operation == 'cancel':
        from test_write_adapter import _queue_reports
        request = ctx.hass.async_create_task(
            new.async_recover(operation_id, new.view['session_id'], confirmed=True),
            eager_start=True,
        )
        assert new.view['recovery_phase'] == 'collecting'
        _queue_reports(SimpleNamespace(bridge=entry.runtime_data.bridge),
                       {'4': 'standby', '145': 2, '140': 'EAAA', '142': 0})
        assert await request
        assert new.view['recovery_phase'] == 'resolved'
    assert not ctx.writes


async def test_source_retirement_fences_old_coordinator(lifecycle):
    ctx = lifecycle
    store_id = await _provision(ctx)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await _current(hub)
    old, bridge = hub.adapter, hub.bridge
    assert await hub._source_unloaded()
    assert old._stopped and bridge._state == "stopped"
    assert hub.adapter is None and hub.bridge is None
    await hub._async_attach_owned()
    await _current(hub)
    assert hub.adapter is not old and hub.bridge is not bridge
    assert not ctx.writes


async def test_api_replacement_retires_old_coordinator(lifecycle):
    ctx = lifecycle
    store_id = await _provision(ctx)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await _current(hub)
    old, bridge = hub.adapter, hub.bridge
    # Retirement replaces the owned API, rather than borrowing a foreign one.
    original_api = bridge.api
    assert await hub._source_unloaded()
    await hub._async_attach_owned()
    await _current(hub)
    assert old._stopped and bridge._state == "stopped"
    assert hub.adapter is not old
    assert hub.bridge.api is not original_api
    assert not ctx.writes


async def test_failed_setup_fences_constructed_coordinator(lifecycle, monkeypatch):
    from custom_components.petsafe_freshfeed import async_setup_entry
    ctx = lifecycle
    store_id = await _provision(ctx)
    entry = ctx.entry
    ctx.hass.config_entries.async_update_entry(
        entry, data={**entry.data, **_ids(store_id, ctx.leaf)})
    captured = []
    async def fail_forward(entry, platforms):
        await _current(entry.runtime_data)
        captured.append((entry.runtime_data.adapter, entry.runtime_data.bridge))
        raise RuntimeError('synthetic setup failure')
    monkeypatch.setattr(ctx.hass.config_entries, 'async_forward_entry_setups', fail_forward)
    with pytest.raises(RuntimeError, match='synthetic setup failure'):
        await async_setup_entry(ctx.hass, entry)
    adapter, bridge = captured[0]
    assert adapter._stopped and bridge._state == "stopped"
    assert not entry.runtime_data._parked
    assert not ctx.writes


async def test_unload_waits_for_construction_and_retains_on_timeout(lifecycle, monkeypatch):
    from custom_components.petsafe_freshfeed import hub as hub_module
    ctx = lifecycle
    real = hub_module.async_create_adapter
    entered, release = asyncio.Event(), asyncio.Event()
    async def gated(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real(*args, **kwargs)
    monkeypatch.setattr(hub_module, "async_create_adapter", gated)
    monkeypatch.setattr(hub_module, "LIFECYCLE_TIMEOUT", 0.05)
    store_id = await _provision(ctx)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await asyncio.wait_for(entered.wait(), 5)
    bridge = hub.bridge
    try:
        assert not await hub.async_drain_controls()
        assert hub.adapter is None and hub._parked and bridge._state == "running"
        assert hub._parked[0][4] is not None
    finally:
        release.set()
    assert await hub.async_stop()
    assert not hub._parked and bridge._state == "stopped"
    assert not ctx.writes
