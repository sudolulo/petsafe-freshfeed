"""Entity behavior against the real Home Assistant integration."""

from datetime import datetime, timezone

from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache
from homeassistant.core import State

from conftest import set_raw


async def setup(hass, source_entry):
    entry = MockConfigEntry(domain="petsafe_freshfeed", title="PetSafe FreshFeed", data={"source_entry_id": source_entry.entry_id}, unique_id=source_entry.entry_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def state(hass, domain, key):
    slug = {"meals_left": "estimated_meals_remaining", "food_level": "estimated_food_level", "out_of_food": "estimated_empty_tray", "firmware_a": "firmware_version", "firmware_b": "hardware_version"}.get(key, key)
    return hass.states.get(f"{domain}.petsafe_freshfeed_{slug}")


async def test_live_values(hass, source_entry, raw_entities):
    entry = await setup(hass, source_entry)
    expected = {"status": "no_food", "meals_left": "0", "compartment": "6", "food_level": "empty", "defrost_remaining": "0", "brightness": "10", "firmware_a": "2.5.18", "firmware_b": "7.1.0.0", "last_fed": "unknown", "last_feed_result": "unknown", "next_meal": "unknown", "meals_today": "0"}
    expected_binary = {"connected": "on", "fault": "off", "out_of_food": "on", "defrosting": "off"}
    for domain, values in (("sensor", expected), ("binary_sensor", expected_binary)):
        for key, value in values.items():
            assert state(hass, domain, key).state == value
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entries) == 23
    source_devices = dr.async_entries_for_config_entry(dr.async_get(hass), source_entry.entry_id)
    assert len(source_devices) == 1
    for entity in entries:
        assert entity.device_id == source_devices[0].id
        if entity.unique_id.endswith("_schedule"):
            assert hass.states.get(entity.entity_id).attributes["source_state"] == "unknown"
        elif not entity.unique_id.endswith(('_quick_feed', '_write_status')):
            assert hass.states.get(entity.entity_id).attributes["basis"] in {"observed", "inferred", "ha_observed", "schedule"}


async def test_entities_follow_changes(hass, source_entry, raw_entities):
    await setup(hass, source_entry)
    set_raw(hass, raw_entities[145], "1")
    await hass.async_block_till_done()
    assert state(hass, "sensor", "meals_left").state == "5"
    assert state(hass, "sensor", "food_level").state == "high"
    assert state(hass, "sensor", "status").state == "standby"
    assert state(hass, "binary_sensor", "out_of_food").state == "off"
    assert state(hass, "event", "feeding").attributes["event_type"] == "refilled"
    set_raw(hass, raw_entities[145], "2")
    await hass.async_block_till_done()
    assert state(hass, "sensor", "last_fed").state not in {"unknown", "unavailable"}
    assert state(hass, "sensor", "last_feed_result").state == "dispensed"
    assert state(hass, "sensor", "meals_today").state == "1"
    assert state(hass, "event", "feeding").attributes["event_type"] == "dispensed"
    assert state(hass, "event", "feeding").attributes["compartment"] == 2
    set_raw(hass, raw_entities[150], "1")
    set_raw(hass, raw_entities[142], "10")
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", "fault").state == "on"
    assert state(hass, "binary_sensor", "defrosting").state == "on"
    assert state(hass, "sensor", "defrost_remaining").state == "10"


async def test_unavailable_raw_makes_entities_unavailable(hass, source_entry, raw_entities):
    await setup(hass, source_entry)
    set_raw(hass, raw_entities[4], "unavailable")
    await hass.async_block_till_done()
    for domain, key in (("sensor", "status"), ("sensor", "meals_left"), ("sensor", "food_level"), ("binary_sensor", "fault")):
        assert state(hass, domain, key).state == "unavailable"
    assert state(hass, "binary_sensor", "connected").state == "off"
    assert state(hass, "sensor", "last_fed").state != "unavailable"


async def test_last_fed_restored(hass, source_entry, raw_entities):
    stamp = datetime(2026, 11, 1, 12, tzinfo=timezone.utc).isoformat()
    mock_restore_cache(hass, [State("sensor.petsafe_freshfeed_last_fed", stamp)])
    await setup(hass, source_entry)
    assert datetime.fromisoformat(state(hass, "sensor", "last_fed").state) == datetime.fromisoformat(stamp)


async def test_statistics_store_is_authoritative_over_restore_cache(hass, source_entry, raw_entities):
    mock_restore_cache(hass, [State('sensor.petsafe_freshfeed_observed_meals_dispensed_total', '999')])
    entry = await setup(hass, source_entry)
    total = state(hass, 'sensor', 'observed_meals_dispensed_total')
    assert total.state == '0'
    assert total.attributes['basis'] == 'ha_observed'
    assert total.attributes['statistics_reason'] is None
    assert total.attributes['coverage'] == 'Best-effort local observations; excludes offline gaps and unsaved increments.'
    assert total.attributes['counting_since'] == entry.runtime_data.counting_since.isoformat()
    registry = er.async_get(hass).async_get(total.entity_id)
    assert registry.unique_id == f'{entry.entry_id}_observed_meals_dispensed_total'
    assert registry.translation_key == 'observed_meals_dispensed_total'
    assert total.name == 'PetSafe FreshFeed Observed meals dispensed total'
    await hass.config_entries.async_unload(entry.entry_id)


async def test_no_writable_platforms(hass, source_entry, raw_entities):
    entry = await setup(hass, source_entry)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert not {e.domain for e in entities} & {"button", "number", "switch", "select", "text", "time"}
    assert set(hass.services.async_services()['petsafe_freshfeed']) == {'save_schedule', 'command', 'recover'}
