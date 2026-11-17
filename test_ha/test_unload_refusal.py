"""Failed HA unload must restore observation without reusing retired controls."""
import asyncio
from unittest.mock import patch
from uuid import uuid4

from test_write_lifecycle import pinned, _current, _ids, _provision, _setup, _load, until


async def test_platform_refusal_restarts_with_new_coordinator_and_retained_intent(pinned):
    ctx = pinned
    operation_id = uuid4().hex
    store_id = await _provision(ctx, retained=operation_id)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await _current(hub)
    old_adapter = hub.adapter
    with patch.object(ctx.hass.config_entries, "async_unload_platforms", return_value=False):
        assert not await ctx.hass.config_entries.async_unload(entry.entry_id)
    await until(lambda: hub.adapter is not None and hub.adapter is not old_adapter)
    assert hub.bridge is not None
    assert (await _load(ctx, store_id))["operation_id"] == operation_id
    assert not ctx.writes
    # HA marks FAILED_UNLOAD non-recoverable, so explicitly clean up the fixture.
    assert await hub.async_stop()


async def test_drain_refusal_does_not_overlap_and_retries(pinned, monkeypatch):
    ctx = pinned
    store_id = await _provision(ctx)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await _current(hub)
    old_adapter = hub.adapter
    real_stop = old_adapter.async_stop
    allow = asyncio.Event()

    async def refuse_until_released(timeout):
        if not allow.is_set():
            return False
        return await real_stop(timeout)

    monkeypatch.setattr(old_adapter, "async_stop", refuse_until_released)
    assert not await ctx.hass.config_entries.async_unload(entry.entry_id)
    await asyncio.sleep(0.1)
    assert hub.adapter is None and hub._parked
    assert not ctx.writes
    allow.set()
    await until(lambda: hub.adapter is not None and hub.adapter is not old_adapter)
    assert not hub._parked and not ctx.writes
    assert await hub.async_stop()


async def test_detach_refusal_keeps_parked_source_and_retries(pinned, monkeypatch):
    ctx = pinned
    store_id = await _provision(ctx)
    entry = await _setup(ctx, _ids(store_id, ctx.leaf))
    hub = entry.runtime_data
    await _current(hub)
    old_adapter, old_bridge = hub.adapter, hub.bridge
    real_detach = old_bridge.async_detach
    allow = asyncio.Event()
    attempts = []

    async def refuse_until_released():
        attempts.append(True)
        if not allow.is_set():
            return False
        return await real_detach()

    monkeypatch.setattr(old_bridge, "async_detach", refuse_until_released)
    assert not await ctx.hass.config_entries.async_unload(entry.entry_id)
    await asyncio.sleep(0.1)
    assert attempts and hub._parked and hub._parked[0][0] is old_bridge
    assert not ctx.writes
    allow.set()
    await until(lambda: hub.adapter is not None and hub.adapter is not old_adapter)
    assert not hub._parked and not ctx.writes
    assert await hub.async_stop()
