"""Recovery of bridge attachment without another source state event."""
import asyncio
from unittest.mock import patch
from uuid import uuid4

from test_write_lifecycle import pinned, _current, _ids, _provision, _setup, until


async def test_transient_startup_attach_failure_recovers_on_periodic_check(pinned, monkeypatch):
    from custom_components.petsafe_freshfeed import hub as hub_module

    real_attach = hub_module.async_attach
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("transient attach failure")
        return await real_attach(*args, **kwargs)

    monkeypatch.setattr(hub_module, "async_attach", fail_once)
    operation_id = uuid4().hex
    store_id = await _provision(pinned, retained=operation_id)
    entry = await _setup(pinned, _ids(store_id, pinned.leaf))
    hub = entry.runtime_data
    assert hub.bridge is None and attempts == 1
    await _current(hub)
    assert hub.adapter.view["state"] == "uncertain"
    assert hub.adapter.view["operation_id"] == operation_id
    assert not pinned.writes


async def test_pending_stale_attach_after_refused_unload_eventually_recovers(pinned, monkeypatch):
    from custom_components.petsafe_freshfeed import hub as hub_module

    operation_id = uuid4().hex
    store_id = await _provision(pinned, retained=operation_id)
    entry = await _setup(pinned, _ids(store_id, pinned.leaf))
    hub = entry.runtime_data
    await _current(hub)
    old_adapter = hub.adapter
    assert old_adapter.view["state"] == "uncertain"
    assert old_adapter.view["operation_id"] == operation_id
    assert await hub._source_unloaded()
    real_attach = hub_module.async_attach
    entered, release = asyncio.Event(), asyncio.Event()
    stale = []

    async def gated(*args, **kwargs):
        entered.set()
        await release.wait()
        bridge = await real_attach(*args, **kwargs)
        stale.append(bridge)
        return bridge

    monkeypatch.setattr(hub_module, "async_attach", gated)
    hub._attach_task = pinned.hass.async_create_task(hub._async_attach_bridge(notify=True))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        with patch.object(pinned.hass.config_entries, "async_unload_platforms", return_value=False):
            assert not await pinned.hass.config_entries.async_unload(entry.entry_id)
        await until(lambda: hub._retry_task is not None and hub._retry_task.done())
        assert hub.bridge is None and not hub._attach_task.done()
        monkeypatch.setattr(hub_module, "async_attach", real_attach)
        release.set()
        await until(lambda: bool(stale) and not stale[0]._active)
        await _current(hub)
        assert hub.bridge is not stale[0] and hub.bridge.is_current()
        assert old_adapter._stopped and hub.adapter is not old_adapter
        assert hub.adapter.view["state"] == "uncertain"
        assert hub.adapter.view["operation_id"] == operation_id
        assert not pinned.writes
    finally:
        release.set()
        assert await hub.async_stop()
