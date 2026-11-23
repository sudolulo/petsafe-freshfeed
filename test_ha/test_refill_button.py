"""Owned "Reset after refill" button: the existing guarded `refilled` command.

The press is the confirmation; the caller must be an administrator, exactly
like the `petsafe_freshfeed.command` service. Refusals use the same public
reasons and send nothing. The device is the injected protocol 3.5 fake.
"""
from __future__ import annotations

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petsafe_freshfeed.const import CONF_SOURCE_ENTRY_ID, DOMAIN
from test_owned_hub import (  # noqa: F401 - pytest fixtures by name
    owned, owned_data, setup, until,
)
from test_owned_settings import control_dps, controlled, echo_later
from test_owned_source import no_tuya_local, pinned  # noqa: F401


def button_id(hass, entry):
    return er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_refill_reset")


async def press(hass, target, context):
    await hass.services.async_call("button", "press", {"entity_id": target},
                                   blocking=True, context=context)


async def test_owned_creates_refill_button(owned):
    entry = await setup(owned, owned_data())
    hass = owned.hass
    target = button_id(hass, entry)
    assert target is not None
    registry = er.async_get(hass)
    ours = [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "button"]
    assert [e.entity_id for e in ours] == [target]
    reg = registry.async_get(target)
    assert reg.device_id == entry.runtime_data.device_id
    assert reg.entity_category is None
    assert reg.translation_key == "refill_reset"
    entity = next(e for e in hass.data["entity_components"]["button"].entities
                  if getattr(e, "hub", None) is entry.runtime_data)
    assert entity.name == "Reset after refill"
    assert entity.icon == "mdi:tray-arrow-down"
    assert entity.entity_category is None
    assert owned.device.control_frames() == []


async def test_legacy_backend_has_no_refill_button(hass, source_entry, raw_entities):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE_ENTRY_ID: source_entry.entry_id})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert button_id(hass, entry) is None
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if e.domain == "button"]


async def test_press_sends_refilled_once(owned, hass_admin_user):
    ctx = owned
    entry, hub = await controlled(ctx)
    echo_later(ctx, {"145": 1})
    await press(ctx.hass, button_id(ctx.hass, entry), Context(user_id=hass_admin_user.id))
    assert [control_dps(p) for p in ctx.device.controls] == [{"120": True}]
    assert len(ctx.device.control_frames()) == 1
    # Critical commands are acknowledged by the later DP145 report, not the send.
    await until(lambda: hub.adapter.view["state"] == "verified")
    assert hub.adapter.view["operation"] == "refilled"
    assert len(ctx.device.control_frames()) == 1


async def test_press_already_refilled_refuses_without_sending(owned, hass_admin_user):
    ctx = owned
    entry, hub = await controlled(ctx)
    ctx.device.push_status({"145": 1})
    await until(lambda: hub.adapter._live_dps.get("145") == 1)
    with pytest.raises(ServiceValidationError) as caught:
        await press(ctx.hass, button_id(ctx.hass, entry), Context(user_id=hass_admin_user.id))
    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "already_refilled"
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


async def test_press_requires_admin(owned, hass_owner_user):
    # hass_owner_user claims ownership first; otherwise the first user created becomes owner.
    ctx = owned
    entry, hub = await controlled(ctx)
    target = button_id(ctx.hass, entry)
    # A non-admin user who may control entities, and an automation with no user.
    user = await ctx.hass.auth.async_create_user("Member", group_ids=["system-users"])
    assert not user.is_admin
    for context in (Context(user_id=user.id), Context()):
        with pytest.raises(ServiceValidationError) as caught:
            await press(ctx.hass, target, context)
        assert caught.value.translation_key == "admin_required"
    assert ctx.device.control_frames() == [] and ctx.device.controls == []
    assert hub.adapter.view["state"] == "idle"


async def test_press_without_adapter_refuses(owned, hass_admin_user):
    ctx = owned
    entry = await setup(ctx, owned_data())
    assert entry.runtime_data.adapter is None
    with pytest.raises(ServiceValidationError) as caught:
        await press(ctx.hass, button_id(ctx.hass, entry), Context(user_id=hass_admin_user.id))
    assert caught.value.translation_key == "source_unavailable"
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


async def test_press_unknown_reason_is_not_exposed(owned, hass_admin_user):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    ctx = owned
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    submit = AsyncMock(return_value={"admitted": False, "reason": "SENTINEL-private-value"})
    hub.adapter = SimpleNamespace(async_submit=submit)
    try:
        with pytest.raises(ServiceValidationError) as caught:
            await press(ctx.hass, button_id(ctx.hass, entry), Context(user_id=hass_admin_user.id))
        assert caught.value.translation_key == "request_refused"
        assert "SENTINEL" not in str(caught.value)
        submit.assert_awaited_once_with("refilled", {"120": True}, confirmed=True)
    finally:
        hub.adapter = None


async def test_admitted_unverified_press_does_not_raise(owned, hass_admin_user):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    ctx = owned
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    submit = AsyncMock(return_value={"admitted": True, "state": "uncertain"})
    hub.adapter = SimpleNamespace(async_submit=submit)
    try:
        await press(ctx.hass, button_id(ctx.hass, entry), Context(user_id=hass_admin_user.id))
        submit.assert_awaited_once_with("refilled", {"120": True}, confirmed=True)
    finally:
        hub.adapter = None


async def test_button_unavailable_when_hub_unavailable(owned):
    ctx = owned
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    target = button_id(ctx.hass, entry)
    assert ctx.hass.states.get(target).state != "unavailable"
    ctx.device.refuse = True
    ctx.device.sockets[-1].drop()
    await until(lambda: not hub.snapshot.available)
    await ctx.hass.async_block_till_done()
    assert ctx.hass.states.get(target).state == "unavailable"
    assert ctx.device.control_frames() == []
