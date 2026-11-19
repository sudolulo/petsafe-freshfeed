"""PetSafe-owned setting entities (DP110/111/125) and the raw quick-feed state (DP140).

Created only for the owned backend. Writes go only through the guarded write
adapter (single owned client, durable witness, one CONTROL, no retry) and are
acknowledged only by a later eligible device report of the same DP with the
requested value within a bounded window; otherwise the outcome is uncertain.
The device is the injected protocol 3.5 fake (no network, no physical feeder).
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petsafe_freshfeed import write_adapter
from custom_components.petsafe_freshfeed.const import (
    CONF_INTENT_BINDING_ID, CONF_INTENT_STORE_ID, CONF_SOURCE_ENTRY_ID, DOMAIN,
    OWNED_SETTING_ENTITIES,
)
from custom_components.petsafe_freshfeed.local_source import ACK_DPS, DeviceReport, DispatchMark
from custom_components.petsafe_freshfeed.services import acknowledgement, recovery_allowed, friendly_reason
from custom_components.petsafe_freshfeed.write_adapter import IntentContext, RecoveryEvidence
from test_owned_hub import (  # noqa: F401 - pytest fixtures by name
    leaf_for, make_entry, owned, owned_data, provision, setup, until,
)
from test_owned_source import HEADER_35, no_tuya_local, pinned  # noqa: F401

ROLES = {"stainless_inserts": ("switch", "110"), "meal_counter": ("switch", "125"),
         "brightness_setting": ("number", "111"), "quick_feed_state": ("sensor", "140")}


def entity_id(hass, entry, role):
    domain = ROLES[role][0]
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{role}")


async def controlled(ctx):
    """An owned entry with provisioned controls and a constructed adapter."""
    entry = make_entry(ctx, owned_data())
    leaf = leaf_for(ctx, entry)
    store_id = await provision(ctx, leaf.id)
    ctx.hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: leaf.id})
    assert await ctx.hass.config_entries.async_setup(entry.entry_id)
    await ctx.hass.async_block_till_done()
    hub = entry.runtime_data
    await until(lambda: hub.adapter is not None and hub.adapter.view["state"] == "idle")
    safe = {"4": "standby", "145": 6, "140": "EAAA", "142": 0}
    ctx.device.push_status(safe)
    await until(lambda: all(hub.adapter._live_dps.get(k) == v for k, v in safe.items()))
    return entry, hub


def control_dps(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return data.get("dps")


def echo_later(ctx, dps, delay=0.05):
    """Device answers the CONTROL, then reports the DP in a later status push."""
    device = ctx.device

    def respond(sock, payload):
        sock.push(device.frame(13, b"", sock.session_key))

        def push():
            device.dps.update(dps)
            sock.push(device.frame(8, HEADER_35 + json.dumps({"dps": dps}).encode(),
                                   sock.session_key))
        threading.Timer(delay, push).start()
    device.on_control = respond


def silent(ctx):
    device = ctx.device
    device.on_control = lambda sock, payload: sock.push(device.frame(13, b"", sock.session_key))


async def call(hass, domain, service, target, **data):
    await hass.services.async_call(domain, service, {"entity_id": target, **data}, blocking=True)


# --------------------------------------------------------------------------
# Entities and state


def test_setting_roles_are_fixed():
    assert {role: (spec[0], spec[1]) for role, spec in OWNED_SETTING_ENTITIES.items()} == {
        role: (domain, dp) for role, (domain, dp) in ROLES.items()}
    assert {"110", "111", "125"} <= ACK_DPS


async def test_owned_creates_setting_entities_with_observed_state(owned):
    ctx = owned
    entry = await setup(ctx, owned_data())
    hass = ctx.hass
    ids = {role: entity_id(hass, entry, role) for role in ROLES}
    assert all(ids.values()), ids
    registry = er.async_get(hass)
    assert {registry.async_get(e).device_id for e in ids.values()} == {entry.runtime_data.device_id}
    assert hass.states.get(ids["stainless_inserts"]).state == "on"
    assert hass.states.get(ids["meal_counter"]).state == "on"
    number = hass.states.get(ids["brightness_setting"])
    assert float(number.state) == 10
    assert (number.attributes["min"], number.attributes["max"], number.attributes["step"]) == (0, 10, 1)
    quick = hass.states.get(ids["quick_feed_state"])
    assert quick.state == "EAAA"
    assert registry.async_get(ids["quick_feed_state"]).entity_category == "diagnostic"
    # Actual later reports drive state; nothing is fabricated.
    ctx.device.push_status({"110": False, "111": 3, "125": False, "140": "EAEA"})
    await until(lambda: hass.states.get(ids["brightness_setting"]).state == "3")
    assert hass.states.get(ids["stainless_inserts"]).state == "off"
    assert hass.states.get(ids["meal_counter"]).state == "off"
    assert hass.states.get(ids["quick_feed_state"]).state == "EAEA"
    assert ctx.device.control_frames() == []


async def test_owned_settings_are_translated_config_entities(owned):
    entry = await setup(owned, owned_data())
    entities = [e for platform in owned.hass.data["entity_components"].values()
                for e in platform.entities if getattr(e, "hub", None) is entry.runtime_data]
    for key, icon in {"stainless_inserts": "mdi:silverware", "meal_counter": "mdi:counter",
                      "brightness_setting": "mdi:brightness-6"}.items():
        entity = next(e for e in entities if e.key == key)
        assert entity.entity_category == "config"
        assert entity.icon == icon
        assert "_attr_name" not in entity.__dict__
        assert entity.translation_key == key
    for key, name in {"stainless_inserts": "Stainless inserts",
                      "meal_counter": "Meal counter"}.items():
        entity = next(e for e in entities if e.key == key)
        assert entity.name == name
    quick = next(e for e in entities if e.key == "quick_feed_state")
    assert quick.name == "Quick feed state"
    assert quick.options is None
    assert quick.native_value == "EAAA"
    assert owned.device.control_frames() == []


async def test_existing_brightness_registry_identity_survives_translation(owned):
    entry = make_entry(owned, owned_data())
    registry = er.async_get(owned.hass)
    old = registry.async_get_or_create(
        'number', DOMAIN, f'{entry.entry_id}_brightness_setting',
        suggested_object_id='petsafe_freshfeed_brightness',
        config_entry=entry, original_name='Brightness setting')
    registry.async_update_entity(old.entity_id, name='My display')
    assert old.entity_id == 'number.petsafe_freshfeed_brightness'
    assert await owned.hass.config_entries.async_setup(entry.entry_id)
    await owned.hass.async_block_till_done()
    assert await owned.hass.config_entries.async_reload(entry.entry_id)
    await owned.hass.async_block_till_done()
    current = registry.async_get(old.entity_id)
    assert current.id == old.id
    assert current.unique_id == old.unique_id
    assert current.name == 'My display'
    assert current.original_name == 'Display brightness'
    assert entity_id(owned.hass, entry, 'brightness_setting') == old.entity_id
    assert owned.device.control_frames() == []


async def test_setting_unknown_reason_is_not_exposed(owned):
    from unittest.mock import AsyncMock
    from custom_components.petsafe_freshfeed.owned_setting import OwnedSetting
    entry = await setup(owned, owned_data())
    hub = entry.runtime_data
    hub.adapter = __import__('types').SimpleNamespace(async_submit=AsyncMock(
        return_value={"admitted": False, "reason": "SENTINEL-private-value"}))
    try:
        with pytest.raises(HomeAssistantError) as caught:
            await OwnedSetting(hub, entry.entry_id, "stainless_inserts").async_set(False)
        assert caught.value.translation_placeholders == {"reason": friendly_reason("unknown")}
        assert "SENTINEL" not in str(caught.value)
        assert owned.device.control_frames() == []
    finally:
        hub.adapter = None


async def test_legacy_backend_has_no_setting_entities(hass, source_entry, raw_entities):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE_ENTRY_ID: source_entry.entry_id})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all(entity_id(hass, entry, role) is None for role in ROLES)
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if e.domain in ("switch", "number")]


# --------------------------------------------------------------------------
# Writes


async def test_controls_disabled_refuses_without_sending(owned):
    ctx = owned
    entry = await setup(ctx, owned_data())
    hass = ctx.hass
    for domain, service, role, data in (
            ("switch", "turn_off", "stainless_inserts", {}),
            ("switch", "turn_off", "meal_counter", {}),
            ("number", "set_value", "brightness_setting", {"value": 4})):
        with pytest.raises(HomeAssistantError) as caught:
            await call(hass, domain, service, entity_id(hass, entry, role), **data)
        assert caught.value.translation_key == "controls_disabled"
        assert caught.value.translation_domain == DOMAIN
    assert hass.states.get(entity_id(hass, entry, "stainless_inserts")).state == "on"
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


@pytest.mark.parametrize("role,service,data,dp,value", [
    ("stainless_inserts", "turn_off", {}, "110", False),
    ("meal_counter", "turn_off", {}, "125", False),
    ("brightness_setting", "set_value", {"value": 4}, "111", 4),
])
async def test_setting_write_acknowledged_by_later_report(owned, role, service, data, dp, value):
    ctx = owned
    entry, hub = await controlled(ctx)
    hass = ctx.hass
    echo_later(ctx, {dp: value})
    target = entity_id(hass, entry, role)
    await call(hass, ROLES[role][0], service, target, **data)
    assert [control_dps(p) for p in ctx.device.controls] == [{dp: value}]
    assert len(ctx.device.control_frames()) == 1
    view = hub.adapter.view
    assert view["state"] == "verified" and view["operation"] == "settings"
    assert not hub.adapter.transport.needs_reconciliation
    expected = {"110": "off", "125": "off", "111": "4"}[dp]
    await until(lambda: hass.states.get(target).state == expected)


async def test_unacknowledged_setting_is_uncertain_without_retry(owned, monkeypatch):
    ctx = owned
    monkeypatch.setattr(write_adapter, "SETTINGS_ACK_TIMEOUT", 0.3)
    entry, hub = await controlled(ctx)
    hass = ctx.hass
    silent(ctx)
    target = entity_id(hass, entry, "stainless_inserts")
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, "switch", "turn_off", target)
    assert caught.value.translation_key == "setting_uncertain"
    # No fabricated state, exactly one CONTROL, no retry.
    assert hass.states.get(target).state == "on"
    assert len(ctx.device.control_frames()) == 1
    assert hub.adapter.view["state"] == "uncertain"
    # A matching report after the bounded window does not acknowledge.
    ctx.device.push_status({"110": False})
    await until(lambda: hass.states.get(target).state == "off")
    await asyncio.sleep(0.1)
    assert hub.adapter.view["state"] == "uncertain"
    # Later writes are refused while the intent is uncertain: nothing sent.
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, "switch", "turn_on", target)
    assert caught.value.translation_key == "setting_refused"
    assert caught.value.translation_placeholders == {"reason": friendly_reason("intent_uncertain")}
    assert len(ctx.device.control_frames()) == 1


async def test_number_rejects_non_integer_without_sending(owned):
    ctx = owned
    entry, hub = await controlled(ctx)
    hass = ctx.hass
    target = entity_id(hass, entry, "brightness_setting")
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, "number", "set_value", target, value=4.5)
    assert caught.value.translation_key == "invalid_setting"
    with pytest.raises(Exception):  # HA's own range validation
        await call(hass, "number", "set_value", target, value=11)
    assert ctx.device.control_frames() == [] and ctx.device.controls == []
    assert hub.adapter.view["state"] == "idle"


@pytest.mark.parametrize("condition,reason", [
    ("disabled", "controls_disabled"), ("busy", "busy"),
    ("uncertain", "intent_uncertain"), ("unsafe", "feeding_active"),
])
async def test_unchanged_setting_obeys_admission(owned, condition, reason, monkeypatch):
    from custom_components.petsafe_freshfeed.owned_setting import OwnedSetting

    ctx = owned
    entry, hub = await controlled(ctx)
    entity = OwnedSetting(hub, entry.entry_id, "stainless_inserts")
    if condition == "disabled":
        monkeypatch.setattr(hub, "adapter", None)
    elif condition == "busy":
        monkeypatch.setattr(hub.adapter.transport, "_busy", True)
    elif condition == "uncertain":
        monkeypatch.setattr(hub.adapter.transport, "_blocked", True)
    else:
        monkeypatch.setitem(hub.adapter._live_dps, "4", "feeding")
    try:
        with pytest.raises(HomeAssistantError) as caught:
            await entity.async_set(True)
    finally:
        monkeypatch.undo()
    if condition == "disabled":
        assert caught.value.translation_key == reason
    else:
        assert caught.value.translation_key == "setting_refused"
        assert caught.value.translation_placeholders == {"reason": friendly_reason(reason)}
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), 4.5, -1, 11])
async def test_number_entity_rejects_invalid_original_values(value):
    from types import SimpleNamespace
    from custom_components.petsafe_freshfeed.number import OwnedBrightness

    hub = SimpleNamespace(_observed={111: "10"}, adapter=None)
    entity = OwnedBrightness.__new__(OwnedBrightness)
    entity.hub = hub
    entity.dp = "111"
    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_set_native_value(value)
    assert caught.value.translation_key == "invalid_setting"


async def test_unchanged_setting_sends_nothing(owned):
    ctx = owned
    entry, hub = await controlled(ctx)
    await call(ctx.hass, "switch", "turn_on", entity_id(ctx.hass, entry, "stainless_inserts"))
    await call(ctx.hass, "number", "set_value", entity_id(ctx.hass, entry, "brightness_setting"),
               value=10)
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


@pytest.mark.parametrize("dps,reason", [
    ({"140": "EAEA"}, "quick_feed_active"),
    ({"4": "feeding"}, "feeding_active"),
    ({"142": 5}, "defrost_active"),
])
async def test_setting_refused_during_feed_activity(owned, dps, reason):
    ctx = owned
    entry, hub = await controlled(ctx)
    ctx.device.push_status(dps)
    key, value = next(iter(dps.items()))
    await until(lambda: hub.adapter._live_dps.get(key) == value)
    with pytest.raises(HomeAssistantError) as caught:
        await call(ctx.hass, "switch", "turn_off", entity_id(ctx.hass, entry, "meal_counter"))
    assert caught.value.translation_key == "setting_refused"
    assert caught.value.translation_placeholders == {"reason": friendly_reason(reason)}
    assert ctx.device.control_frames() == []


@pytest.mark.parametrize("missing", [None, "4", "145", "140", "142"])
async def test_setting_refused_without_complete_status(owned, missing):
    ctx = owned
    entry, hub = await controlled(ctx)
    if missing is None:
        hub.adapter._live_dps.clear()
    else:
        hub.adapter._live_dps.pop(missing, None)
    result = await hub.adapter.async_submit("settings", {"110": False})
    assert result["admitted"] is False and result["reason"] == "status_unavailable"
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


async def test_adapter_validates_setting_requests_strictly(owned):
    ctx = owned
    entry, hub = await controlled(ctx)
    adapter = hub.adapter
    for dps in ({"110": 1}, {"125": 0}, {"111": True}, {"111": 11}, {"111": -1},
                {"111": 4.0}, {"110": False, "125": False}, {"140": "EA=="}, {"3": 1}, {}):
        result = await adapter.async_submit("settings", dps)
        assert result["admitted"] is False and result["reason"] == "invalid_request", dps
    result = await adapter.async_submit("settings", {"110": False}, base_revision="x")
    assert result["reason"] == "invalid_request"
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


async def test_setting_refused_while_another_write_in_flight(owned):
    ctx = owned
    entry, hub = await controlled(ctx)
    adapter = hub.adapter
    gate = threading.Event()
    device = ctx.device

    def slow(sock, payload):
        gate.wait(2)
        sock.push(device.frame(13, b"", sock.session_key))
    device.on_control = slow
    first = asyncio.ensure_future(adapter.async_submit("settings", {"110": False}))
    await until(lambda: adapter.view["state"] == "pending")
    second = await adapter.async_submit("settings", {"125": False})
    assert second["admitted"] is False and second["reason"] == "busy"
    feed = await adapter.async_submit("feed", {"140": "EA=="}, confirmed=True)
    assert feed["admitted"] is False and feed["reason"] == "busy"
    gate.set()
    await first
    assert len(ctx.device.control_frames()) == 1


# --------------------------------------------------------------------------
# Pure predicates


def _report(dps, sequence=5, eligible=True):
    return DeviceReport(1, sequence, datetime.now(timezone.utc), dps, eligible)


def test_settings_acknowledgement_requires_same_dp_and_value():
    context = IntentContext("settings", "a" * 32, "s", 1, {"110": False})
    mark = DispatchMark("a" * 32, 1, 3)
    assert acknowledgement(context, mark, _report({"110": False})) == "observed"
    assert acknowledgement(context, mark, _report({"110": True})) == "ignore"
    assert acknowledgement(context, mark, _report({"110": 0})) == "ignore"
    assert acknowledgement(context, mark, _report({"125": False})) == "ignore"
    assert acknowledgement(context, mark, _report({"110": False}, sequence=3)) == "ignore"
    number = IntentContext("settings", "a" * 32, "s", 1, {"111": 4})
    assert acknowledgement(number, mark, _report({"111": 4})) == "observed"
    assert acknowledgement(number, mark, _report({"111": True})) == "ignore"
    assert acknowledgement(IntentContext("settings", "a" * 32, "s", 1, {}), mark,
                           _report({"110": False})) == "ignore"


@pytest.mark.parametrize("change", [
    {"140": None}, {"142": None}, {"140": "invalid"}, {"140": "EAEA"},
    {"142": 1}, {"142": False}, {"4": "feeding"}, {"145": 0},
])
@pytest.mark.parametrize("retained", [False, True])
def test_settings_recovery_rejects_incomplete_or_unsafe_evidence(change, retained):
    safe = {"4": "standby", "145": 2, "140": "EAAA", "142": 0, "110": True}
    for key, value in change.items():
        if value is None:
            safe.pop(key)
        else:
            safe[key] = value
    context = IntentContext("settings", "a" * 32, "s", 1, {} if retained else {"110": False})
    assert recovery_allowed(context, RecoveryEvidence(context, 3, 5, (_report(safe),))) is False


def test_settings_recovery_needs_safe_post_request_status():
    safe = {"4": "standby", "145": 2, "140": "EAAA", "142": 0}
    context = IntentContext("settings", "a" * 32, "s", 1, {"110": False})
    ok = RecoveryEvidence(context, 3, 5, (_report({**safe, "110": True}),))
    assert recovery_allowed(context, ok) is True
    missing = RecoveryEvidence(context, 3, 5, (_report(safe),))
    assert recovery_allowed(context, missing) is False
    retained = IntentContext("settings", "a" * 32, "s", 1, {})
    assert recovery_allowed(retained, RecoveryEvidence(retained, 3, 5, (_report(safe),))) is True
    busy = RecoveryEvidence(context, 3, 5, (_report({**safe, "4": "feeding", "110": True}),))
    assert recovery_allowed(context, busy) is False
