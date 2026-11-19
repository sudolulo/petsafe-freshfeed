"""Receive-claim failures recover without a source lifecycle event."""
import asyncio
from uuid import uuid4

from test_write_lifecycle import lifecycle, _ids, _provision, _setup, until


async def test_claim_refusal_before_store_open_is_retried(lifecycle, monkeypatch):
    from custom_components.petsafe_freshfeed import hub as hub_module

    real_claim = hub_module.OwnedSource.async_claim_receive
    healthy = asyncio.Event()

    async def claim_when_healthy(bridge):
        if not healthy.is_set():
            raise ValueError("transient receive claim refusal")
        await real_claim(bridge)

    monkeypatch.setattr(hub_module, "COORDINATOR_READY_TIMEOUT", 0.05)
    monkeypatch.setattr(hub_module.OwnedSource, "async_claim_receive", claim_when_healthy)
    operation_id = uuid4().hex
    store_id = await _provision(lifecycle, retained=operation_id)
    entry = await _setup(lifecycle, _ids(store_id, lifecycle.leaf))
    hub = entry.runtime_data
    await until(lambda: hub._controls_reason == "source_incompatible")
    assert hub.bridge is not None and hub.bridge.is_current()
    assert hub.witness is None and hub.adapter is None
    assert not lifecycle.writes
    healthy.set()
    await until(lambda: hub.adapter is not None)
    assert hub.adapter.view["state"] == "uncertain"
    assert hub.adapter.view["operation_id"] == operation_id
    assert not lifecycle.writes
