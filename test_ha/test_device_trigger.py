"""Device triggers listen only to new observed feeding events."""
import pytest
import probatio
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er, device_registry as dr
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petsafe_freshfeed.const import EVENT_KINDS


@pytest.fixture
def feeding(hass):
    entry = MockConfigEntry(domain="petsafe_freshfeed")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("petsafe_freshfeed", "synthetic")})
    entity = registry.async_get_or_create("event", "petsafe_freshfeed", f"{entry.entry_id}_feeding", config_entry=entry, device_id=device.id)
    yield entry, entity
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def config(entity, kind="dispensed"):
    return {"platform": "device", "domain": "petsafe_freshfeed", "device_id": entity.device_id, "entity_id": entity.id, "type": kind}


async def test_lists_exact_seven_translated_triggers(hass, feeding):
    from custom_components.petsafe_freshfeed import device_trigger as trigger
    _, entity = feeding
    triggers = await trigger.async_get_triggers(hass, entity.device_id)
    assert {item["type"] for item in triggers} == set(EVENT_KINDS)
    assert all(item == config(entity, item["type"]) for item in triggers)


async def test_each_kind_and_context(hass, feeding):
    from custom_components.petsafe_freshfeed import device_trigger as trigger
    _, entity = feeding
    calls = []
    info = {"trigger_data": {}, "variables": {}, "name": "test"}
    unsubs = []
    for kind in EVENT_KINDS:
        async def action(variables, context, selected=kind):
            calls.append((selected, variables, context))
        validated = await trigger.async_validate_trigger_config(hass, config(entity, kind))
        unsubs.append(await trigger.async_attach_trigger(hass, validated, action, info))
    hass.states.async_set(entity.entity_id, "2026-01-01T00:00:00+00:00", {"event_type": "dispensed"})
    await hass.async_block_till_done()
    context = Context()
    for index, kind in enumerate(EVENT_KINDS, 1):
        hass.states.async_set(entity.entity_id, f"2026-01-01T00:00:{index:02}+00:00", {"event_type": kind}, context=context)
        await hass.async_block_till_done()
    assert len(calls) == len(EVENT_KINDS)
    assert [selected for selected, _, _ in calls] == list(EVENT_KINDS)
    assert all(v["trigger"]["platform"] == "device" and c == context for _, v, c in calls)
    for unsub in unsubs:
        unsub()
    hass.states.async_set(entity.entity_id, "2026-01-01T00:01:00+00:00", {"event_type": "dispensed"})
    await hass.async_block_till_done()
    assert len(calls) == len(EVENT_KINDS)


async def test_restore_unavailable_and_attribute_changes_do_not_fire(hass, feeding):
    from custom_components.petsafe_freshfeed import device_trigger as trigger
    _, entity = feeding
    calls = []
    async def action(variables, context): calls.append(variables)
    unsub = await trigger.async_attach_trigger(hass, config(entity), action, {"trigger_data": {}, "variables": {}, "name": "test"})
    for value, attrs in [("unknown", {}), ("2026-01-01T00:00:00+00:00", {"event_type": "dispensed"}), ("2026-01-01T00:00:00+00:00", {"event_type": "dispensed", "extra": 1}), ("unavailable", {}), ("2026-01-01T00:00:01+00:00", {"event_type": "dispensed", "restored": True}), ("2026-01-01T00:00:02+00:00", {"event_type": "dispensed"})]:
        hass.states.async_set(entity.entity_id, value, attrs)
        await hass.async_block_till_done()
    assert calls == []
    for second in (3, 4):
        hass.states.async_set(entity.entity_id, f"2026-01-01T00:00:0{second}+00:00", {"event_type": "dispensed"})
        await hass.async_block_till_done()
    assert len(calls) == 2
    unsub()


async def test_mismatched_device_foreign_disabled_missing_entity_rejected(hass, feeding):
    from custom_components.petsafe_freshfeed import device_trigger as trigger
    _, entity = feeding
    for patch in ({"device_id": "other"}, {"entity_id": "event.missing"}, {"type": "bad"}, {"domain": "other"}):
        with pytest.raises((probatio.Invalid, ValueError)):
            await trigger.async_validate_trigger_config(hass, {**config(entity), **patch})
    registry = er.async_get(hass)
    entry, _ = feeding
    foreign = registry.async_get_or_create("event", "other", "foreign", config_entry=entry, device_id=entity.device_id)
    wrong = registry.async_get_or_create("event", "petsafe_freshfeed", "wrong", config_entry=entry, device_id=entity.device_id)
    for candidate in (foreign, wrong):
        with pytest.raises((probatio.Invalid, ValueError)):
            await trigger.async_validate_trigger_config(hass, config(candidate))
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    assert await trigger.async_get_triggers(hass, entity.device_id) == []
    with pytest.raises((probatio.Invalid, ValueError)):
        await trigger.async_validate_trigger_config(hass, config(entity))
    entry.mock_state(hass, ConfigEntryState.LOADED)
    registry.async_update_entity(entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    with pytest.raises((probatio.Invalid, ValueError)):
        await trigger.async_validate_trigger_config(hass, config(entity))


async def test_renamed_feeding_entity_resolves_registry_id(hass, feeding):
    from custom_components.petsafe_freshfeed import device_trigger as trigger
    _, entity = feeding
    assert (await trigger.async_validate_trigger_config(hass, {**config(entity), "entity_id": entity.entity_id}))["entity_id"] == entity.id
    registry = er.async_get(hass)
    calls = []
    async def action(variables, context): calls.append(variables)
    info = {"trigger_data": {}, "variables": {}, "name": "test"}
    unsub = await trigger.async_attach_trigger(hass, config(entity), action, info)
    hass.states.async_set(entity.entity_id, "2026-01-01T00:00:00+00:00", {"event_type": "dispensed"})
    await hass.async_block_till_done()
    unsub()
    renamed = registry.async_update_entity(entity.entity_id, new_entity_id="event.renamed_feeding")
    assert (await trigger.async_validate_trigger_config(hass, config(entity)))["entity_id"] == renamed.id
    unsub = await trigger.async_attach_trigger(hass, config(entity), action, info)
    hass.states.async_set(renamed.entity_id, "2026-01-01T00:00:01+00:00", {"event_type": "dispensed"})
    await hass.async_block_till_done()
    assert not calls
    hass.states.async_set(entity.entity_id, "2026-01-01T00:00:01+00:00", {"event_type": "dispensed"})
    await hass.async_block_till_done()
    assert not calls
    hass.states.async_set(renamed.entity_id, "2026-01-01T00:00:02+00:00", {"event_type": "dispensed"})
    await hass.async_block_till_done()
    assert len(calls) == 1
    unsub()
