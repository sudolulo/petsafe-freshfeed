"""Recovery of bridge attachment without another source state event."""
import asyncio
from unittest.mock import patch
from uuid import uuid4

from test_write_lifecycle import lifecycle, _current, _ids, _provision, _setup, until


async def test_transient_startup_attach_failure_recovers_on_periodic_check(lifecycle, monkeypatch):
    from custom_components.petsafe_freshfeed import hub as hub_module

    real_attach = hub_module.OwnedSource.async_start
    monkeypatch.setattr(hub_module.FreshFeedHub, 'OWNED_RETRY', 0.05)
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("transient attach failure")
        return await real_attach(*args, **kwargs)

    monkeypatch.setattr(hub_module.OwnedSource, "async_start", fail_once)
    operation_id = uuid4().hex
    store_id = await _provision(lifecycle, retained=operation_id)
    entry = await _setup(lifecycle, _ids(store_id, lifecycle.leaf))
    hub = entry.runtime_data
    assert hub.bridge is None and attempts == 1
    await _current(hub)
    assert hub.adapter.view["state"] == "uncertain"
    assert hub.adapter.view["operation_id"] == operation_id
    assert not lifecycle.writes


async def test_pending_stale_attach_after_refused_unload_eventually_recovers(lifecycle, monkeypatch):
    from custom_components.petsafe_freshfeed import hub as hub_module

    operation_id = uuid4().hex
    store_id = await _provision(lifecycle, retained=operation_id)
    entry = await _setup(lifecycle, _ids(store_id, lifecycle.leaf))
    hub = entry.runtime_data
    await _current(hub)
    old_adapter = hub.adapter
    assert old_adapter.view["state"] == "uncertain"
    assert old_adapter.view["operation_id"] == operation_id
    assert await hub._source_unloaded()
    real_attach = hub_module.OwnedSource.async_start
    entered, release = asyncio.Event(), asyncio.Event()
    stale = []

    async def gated(bridge):
        await real_attach(bridge)
        stale.append(bridge)
        entered.set()
        await release.wait()

    monkeypatch.setattr(hub_module.OwnedSource, "async_start", gated)
    hub._attach_task = lifecycle.hass.async_create_task(hub._async_attach_owned(notify=True))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        with patch.object(lifecycle.hass.config_entries, "async_unload_platforms", return_value=False):
            assert not await lifecycle.hass.config_entries.async_unload(entry.entry_id)
        await until(lambda: hub._retry_task is not None and hub._retry_task.done())
        assert hub.bridge is None and not hub._attach_task.done()
        monkeypatch.setattr(hub_module.OwnedSource, "async_start", real_attach)
        release.set()
        await until(lambda: bool(stale) and stale[0]._state == 'stopped')
        await _current(hub)
        assert hub.bridge is not stale[0] and hub.bridge.is_current()
        assert old_adapter._stopped and hub.adapter is not old_adapter
        assert hub.adapter.view["state"] == "uncertain"
        assert hub.adapter.view["operation_id"] == operation_id
        assert not lifecycle.writes
    finally:
        release.set()
        assert await hub.async_stop()
