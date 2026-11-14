"""Entity hygiene: push-only entities and non-deprecated device lookup."""

import logging

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petsafe_freshfeed.const import DOMAIN


async def _setup(hass, source_entry):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source_entry_id": source_entry.entry_id},
        unique_id=source_entry.entry_id,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_do_not_poll(hass, source_entry, raw_entities):
    entry = await _setup(hass, source_entry)
    hub = entry.runtime_data
    platform_entities = [
        e
        for platform in hass.data["entity_components"].values()
        for e in platform.entities
        if getattr(e, "hub", None) is hub
    ]
    assert len(platform_entities) == 22
    assert all(e.should_poll is False for e in platform_entities)


async def test_no_deprecated_device_lookup(hass, source_entry, raw_entities, caplog):
    caplog.set_level(logging.WARNING)
    entry = await _setup(hass, source_entry)
    assert "async_get_device" not in caplog.text
    registry = er.async_get(hass)
    raw_device = registry.async_get(raw_entities[145]).device_id
    ours = [e for e in er.async_entries_for_config_entry(registry, entry.entry_id)]
    assert len(ours) == 22
    assert {e.device_id for e in ours} == {raw_device}
    status = next(e for e in ours if e.unique_id == f'{entry.entry_id}_write_status')
    state = hass.states.get(status.entity_id)
    assert state.state == 'blocked'
    assert state.attributes['can_feed'] is False
    assert state.attributes['recovery_phase'] == 'idle'
    quick = next(e for e in ours if e.unique_id == f'{entry.entry_id}_quick_feed')
    assert hass.states.get(quick.entity_id).attributes['command'] is None
