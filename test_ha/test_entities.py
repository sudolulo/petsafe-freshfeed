"""Entity behavior against the real owned Home Assistant integration."""
from datetime import datetime, timezone

from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache
from homeassistant.core import State

from test_owned_hub import owned_data, setup as owned_setup
from owned_test_support import until


async def setup(owned):
    owned.device.dps.pop('1', None)
    owned.device.dps.pop('140', None)
    owned.device.dps['145'] = 6
    return await owned_setup(owned, owned_data(), title='PetSafe FreshFeed')


def state(hass, domain, key):
    slug = {'meals_left': 'estimated_meals_remaining', 'food_level': 'estimated_food_level', 'out_of_food': 'estimated_empty_tray', 'firmware_a': 'firmware_version', 'firmware_b': 'hardware_version'}.get(key, key)
    return hass.states.get(f'{domain}.petsafe_freshfeed_{slug}')


async def push(owned, dps):
    hub = owned.entries[-1].runtime_data
    before = hub.last_report
    owned.device.push_status(dps)
    await until(lambda: hub.last_report != before)
    await owned.hass.async_block_till_done()


async def test_live_values(hass, owned):
    entry = await setup(owned)
    expected = {'status': 'no_food', 'meals_left': '0', 'compartment': '6', 'food_level': 'empty', 'defrost_remaining': '0', 'brightness': '10', 'firmware_a': '2.5.18', 'firmware_b': '7.1.0.0', 'last_fed': 'unknown', 'last_feed_result': 'unknown', 'next_meal': 'unknown', 'meals_today': '0'}
    expected_binary = {'connected': 'on', 'fault': 'off', 'out_of_food': 'on', 'defrosting': 'off'}
    for domain, values in (('sensor', expected), ('binary_sensor', expected_binary)):
        for key, value in values.items():
            assert state(hass, domain, key).state == value
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entries) == 28  # includes disabled raw quick-feed diagnostic and the refill button
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    for entity in entries:
        assert entity.device_id == devices[0].id
        if entity.unique_id.endswith('_schedule'):
            assert hass.states.get(entity.entity_id).attributes['source_state'] == 'unknown'
        elif entity.domain in {'sensor', 'binary_sensor', 'event'} and not entity.unique_id.endswith(('_quick_feed', '_quick_feed_state', '_write_status')):
            assert hass.states.get(entity.entity_id).attributes['basis'] in {'observed', 'inferred', 'ha_observed', 'schedule'}


async def test_entities_follow_changes(hass, owned):
    await setup(owned)
    await push(owned, {'145': 1})
    assert state(hass, 'sensor', 'meals_left').state == '5'
    assert state(hass, 'sensor', 'food_level').state == 'high'
    assert state(hass, 'sensor', 'status').state == 'standby'
    assert state(hass, 'binary_sensor', 'out_of_food').state == 'off'
    assert state(hass, 'event', 'feeding').attributes['event_type'] == 'refilled'
    await push(owned, {'145': 2})
    assert state(hass, 'sensor', 'last_fed').state not in {'unknown', 'unavailable'}
    assert state(hass, 'sensor', 'last_feed_result').state == 'dispensed'
    assert state(hass, 'sensor', 'meals_today').state == '1'
    assert state(hass, 'event', 'feeding').attributes['event_type'] == 'dispensed'
    assert state(hass, 'event', 'feeding').attributes['compartment'] == 2
    await push(owned, {'150': 1, '142': 10})
    assert state(hass, 'binary_sensor', 'fault').state == 'on'
    assert state(hass, 'binary_sensor', 'defrosting').state == 'on'
    assert state(hass, 'sensor', 'defrost_remaining').state == '10'


async def test_malformed_observed_values_make_entities_unknown(hass, owned):
    await setup(owned)
    await push(owned, {'4': 'standby', '145': 2, '142': 10, '150': 1})
    await push(owned, {'4': ['standby'], '145': {}, '142': None, '150': []})
    for domain, key in (('sensor', 'status'), ('sensor', 'meals_left'),
                        ('sensor', 'food_level'), ('sensor', 'defrost_remaining'),
                        ('binary_sensor', 'fault'), ('binary_sensor', 'defrosting')):
        assert state(hass, domain, key).state in {'unknown', 'unavailable'}


async def test_disconnection_makes_entities_unavailable(hass, owned):
    await setup(owned)
    owned.device.refuse = True
    owned.device.sockets[-1].drop()
    await until(lambda: state(hass, 'binary_sensor', 'connected').state == 'off')
    for domain, key in (('sensor', 'status'), ('sensor', 'meals_left'), ('sensor', 'food_level'), ('binary_sensor', 'fault')):
        assert state(hass, domain, key).state == 'unavailable'
    assert state(hass, 'sensor', 'last_fed').state != 'unavailable'


async def test_last_fed_restored(hass, owned):
    stamp = datetime(2026, 11, 1, 12, tzinfo=timezone.utc).isoformat()
    mock_restore_cache(hass, [State('sensor.petsafe_freshfeed_last_fed', stamp)])
    await setup(owned)
    assert datetime.fromisoformat(state(hass, 'sensor', 'last_fed').state) == datetime.fromisoformat(stamp)


async def test_statistics_store_is_authoritative_over_restore_cache(hass, owned):
    mock_restore_cache(hass, [State('sensor.petsafe_freshfeed_observed_meals_dispensed_total', '999')])
    entry = await setup(owned)
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


async def test_owned_settings_default_disabled(hass, owned):
    entry = await setup(owned)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    settings = [e for e in entities if e.domain in {'number', 'switch'}]
    assert len(settings) == 3
    assert entry.runtime_data.write_view['reason'] == 'controls_unavailable'
    assert {e.unique_id.removeprefix(entry.entry_id + '_') for e in settings} == {
        'stainless_inserts', 'meal_counter', 'brightness_setting'}
    assert not {e.domain for e in entities} & {'select', 'text', 'time'}
    assert [e.unique_id for e in entities if e.domain == 'button'] == [f'{entry.entry_id}_refill_reset']
    assert set(hass.services.async_services()['petsafe_freshfeed']) == {'save_schedule', 'command', 'recover'}
