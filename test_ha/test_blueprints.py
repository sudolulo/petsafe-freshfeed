"""Validate importable blueprints and execute their expanded HA automations."""
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.components.blueprint import models
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA, async_validate_config_item
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util, yaml
from pytest_homeassistant_custom_component.common import async_fire_time_changed

ROOT = Path(__file__).resolve().parents[1] / 'blueprints/automation/petsafe_freshfeed'
NAMES = ('low_estimated_meals', 'feeder_attention', 'refill_cleaning_reminder')
NOTIFY = [{'action': 'test.notify'}]
INPUTS = {
    NAMES[0]: dict(meals_entity='sensor.meals', notification_action=NOTIFY),
    NAMES[1]: dict(connected_entity='binary_sensor.connected', fault_entity='binary_sensor.fault', write_status_entity='sensor.write_status', notification_action=NOTIFY),
    NAMES[2]: dict(feeding_event='event.feeding', refill_helper='input_datetime.refill', cleaning_helper='input_datetime.cleaning', cleaning_button='input_button.cleaned', refill_notification_action=NOTIFY, cleaning_notification_action=NOTIFY),
}


def expand(name, overrides=None):
    doc = yaml.load_yaml(str(ROOT / f'{name}.yaml'))
    bp = models.Blueprint(doc, expected_domain='automation', schema=AUTOMATION_BLUEPRINT_SCHEMA)
    assert bp.metadata['source_url'] == f'https://github.com/sudolulo/petsafe-freshfeed/blob/trunk/blueprints/automation/petsafe_freshfeed/{name}.yaml'
    assert bp.metadata['homeassistant']['min_version'] == '2026.10.0'
    inputs = models.BlueprintInputs(bp, {'alias': name, 'use_blueprint': {'path': f'{name}.yaml', 'input': INPUTS[name] | (overrides or {})}})
    inputs.validate()
    return inputs.async_substitute()


def test_input_contracts():
    assert {p.stem for p in ROOT.glob('*.yaml')} == set(NAMES)
    docs = {name: yaml.load_yaml(str(ROOT / f'{name}.yaml')) for name in NAMES}
    low = docs[NAMES[0]]['blueprint']['input']
    assert low['threshold']['default'] == 2
    assert low['threshold']['selector']['number'] == {'min': 0, 'max': 5, 'step': 1}
    assert low['hold']['default'] == {'minutes': 5}
    reminder = docs[NAMES[2]]['blueprint']['input']
    assert reminder['daily_time']['default'] == '09:00:00'
    for key, default in [('refill_days', 7), ('cleaning_days', 30)]:
        assert reminder[key]['default'] == default
        assert reminder[key]['selector']['number'] == {'min': 1, 'max': 90, 'step': 1}
    for key in ['refill_helper', 'cleaning_helper']:
        assert reminder[key]['selector']['entity']['filter'] == [{'domain': 'input_datetime'}]
    for doc in docs.values():
        assert doc['mode'] in ('single', 'queued')
        for key, value in doc['blueprint']['input'].items():
            assert 'selector' in value
            if key.endswith('_action'):
                assert value['selector'] == {'action': {}}
                assert 'default' not in value


@pytest.mark.parametrize('name', NAMES)
async def test_schema(hass, name):
    config = expand(name)
    assert await async_validate_config_item(hass, 'automation', config)
    text = (ROOT / f'{name}.yaml').read_text()
    assert 'petsafe_freshfeed.command' not in text
    assert 'petsafe_freshfeed.recover' not in text
    assert 'petsafe_freshfeed.save_schedule' not in text
    def actions(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('action', 'service'):
                    assert value in ('test.notify', 'input_datetime.set_datetime')
                actions(value)
        elif isinstance(node, list):
            for value in node:
                actions(value)
    actions(config)


async def install(hass, name, overrides=None):
    calls = []
    hass.services.async_register('test', 'notify', lambda call: calls.append(call))
    assert await async_setup_component(hass, 'automation', {'automation': [expand(name, overrides)]})
    await hass.async_start()
    await hass.async_block_till_done()
    return calls


async def set_state(hass, entity, value, attrs=None):
    if entity.startswith('input_datetime.') and value not in ('unknown', 'unavailable') and attrs is None:
        attrs = {'has_date': True, 'has_time': True, 'timestamp': dt_util.parse_datetime(value).replace(tzinfo=dt_util.UTC).timestamp()}
    hass.states.async_set(entity, value, attrs)
    await hass.async_block_till_done()


async def advance(hass, seconds):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_low_estimate_crossing_and_hold(hass):
    calls = await install(hass, NAMES[0])
    await set_state(hass, 'sensor.meals', 'unknown')
    await set_state(hass, 'sensor.meals', '1')
    await advance(hass, 301)
    assert not calls
    await set_state(hass, 'sensor.meals', '4')
    await set_state(hass, 'sensor.meals', '1')
    await advance(hass, 299)
    assert not calls
    await advance(hass, 301)
    assert len(calls) == 1


async def test_attention_offline_fault_uncertain(hass):
    calls = await install(hass, NAMES[1], {'notification_action': [{'action': 'test.notify', 'data': {'reason': '{{ attention_reason }}'}}]})
    for entity, value in [('binary_sensor.connected', 'off'), ('binary_sensor.fault', 'on'), ('sensor.write_status', 'uncertain')]:
        await set_state(hass, entity, 'unknown')
        await set_state(hass, entity, value)
    await advance(hass, 901)
    assert not calls
    await set_state(hass, 'binary_sensor.connected', 'on')
    await set_state(hass, 'binary_sensor.connected', 'off')
    await advance(hass, 899)
    assert not calls
    await set_state(hass, 'binary_sensor.connected', 'on')
    await advance(hass, 901)
    assert not calls
    await set_state(hass, 'binary_sensor.connected', 'off')
    await advance(hass, 901)
    assert len(calls) == 1
    await set_state(hass, 'binary_sensor.fault', 'off')
    await set_state(hass, 'binary_sensor.fault', 'on')
    await set_state(hass, 'sensor.write_status', 'idle')
    await set_state(hass, 'sensor.write_status', 'uncertain')
    assert [call.data['reason'] for call in calls] == [
        'Feeder observations unavailable for 15 minutes',
        'Feeder fault reported',
        'Feeder confirmation required; inspect the feeder',
    ]


@pytest.mark.parametrize('previous', ['unknown', 'unavailable'])
async def test_first_refill_after_unknown_or_unavailable(hass, previous):
    updates = []
    hass.services.async_register('input_datetime', 'set_datetime', lambda call: updates.append(call))
    await install(hass, NAMES[2])
    old_stamp = (dt_util.utcnow() - timedelta(days=1)).isoformat()
    await set_state(hass, 'event.feeding', previous)
    await set_state(hass, 'event.feeding', old_stamp, {'event_type': 'refilled'})
    assert not updates  # A stale restored event is not a new observation.
    await set_state(hass, 'event.feeding', previous)
    stamp = dt_util.utcnow().isoformat()
    await set_state(hass, 'event.feeding', stamp, {'event_type': 'refilled'})
    assert len(updates) == 1
    assert updates[0].data['timestamp'] == dt_util.parse_datetime(stamp).timestamp()
    await set_state(hass, 'event.feeding', previous)
    await set_state(hass, 'event.feeding', dt_util.utcnow().isoformat(), {'event_type': 'dispensed'})
    assert len(updates) == 1
    await hass.services.async_call('automation', 'turn_off', {'entity_id': 'automation.refill_cleaning_reminder'}, blocking=True)


async def test_first_cleaning_press_after_unknown(hass):
    updates = []
    hass.services.async_register('input_datetime', 'set_datetime', lambda call: updates.append(call))
    await install(hass, NAMES[2])
    await set_state(hass, 'input_button.cleaned', 'unknown')
    await set_state(hass, 'input_button.cleaned', dt_util.utcnow().isoformat())
    await hass.services.async_call('automation', 'turn_off', {'entity_id': 'automation.refill_cleaning_reminder'}, blocking=True)
    assert len(updates) == 1
    assert updates[0].data['entity_id'] == ['input_datetime.cleaning']


@pytest.mark.parametrize('helper,interval,other', [
    ('input_datetime.refill', 3, 'input_datetime.cleaning'),
    ('input_datetime.cleaning', 11, 'input_datetime.refill'),
])
async def test_custom_reminder_boundaries_and_missing_helpers(hass, freezer, helper, interval, other):
    freezer.move_to('2026-11-18T09:00:00+00:00')
    await hass.config.async_set_time_zone('UTC')
    calls = await install(hass, NAMES[2], {
        'refill_days': 3, 'cleaning_days': 11,
        'refill_notification_action': [{'action': 'test.notify', 'data': {'kind': 'refill'}}],
        'cleaning_notification_action': [{'action': 'test.notify', 'data': {'kind': 'cleaning'}}],
    })
    kind = 'refill' if helper.endswith('refill') else 'cleaning'
    for missing in ('unknown', 'unavailable'):
        await set_state(hass, helper, missing)
        await set_state(hass, other, missing)
        freezer.tick(timedelta(days=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert not calls
    # At the next daily check, the known timestamp is one minute short.
    target = dt_util.utcnow() + timedelta(days=1)
    await set_state(hass, helper, (target - timedelta(days=interval) + timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'))
    freezer.move_to(target)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not calls
    # Set the timestamp exactly the configured interval before the next check.
    target += timedelta(days=1)
    await set_state(hass, helper, (target - timedelta(days=interval)).strftime('%Y-%m-%d %H:%M:%S'))
    freezer.move_to(target)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert [call.data['kind'] for call in calls] == [kind]
    await hass.services.async_call('automation', 'turn_off', {'entity_id': 'automation.refill_cleaning_reminder'}, blocking=True)


@pytest.mark.parametrize('missing_helper,overdue_helper,kind', [
    ('input_datetime.refill', 'input_datetime.cleaning', 'cleaning'),
    ('input_datetime.cleaning', 'input_datetime.refill', 'refill'),
])
@pytest.mark.parametrize('missing_state', ['unknown', 'unavailable'])
async def test_missing_helper_does_not_suppress_other_overdue_reminder(
    hass, freezer, missing_helper, overdue_helper, kind, missing_state
):
    freezer.move_to('2026-11-18T09:00:00+00:00')
    await hass.config.async_set_time_zone('UTC')
    calls = await install(hass, NAMES[2], {
        'refill_notification_action': [{'action': 'test.notify', 'data': {'kind': 'refill'}}],
        'cleaning_notification_action': [{'action': 'test.notify', 'data': {'kind': 'cleaning'}}],
    })
    await set_state(hass, missing_helper, missing_state)
    await set_state(hass, overdue_helper, (dt_util.utcnow() - timedelta(days=31)).strftime('%Y-%m-%d %H:%M:%S'))
    freezer.tick(timedelta(days=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert [call.data['kind'] for call in calls] == [kind]
    await hass.services.async_call('automation', 'turn_off', {'entity_id': 'automation.refill_cleaning_reminder'}, blocking=True)


@pytest.mark.parametrize('helper,kind', [('refill', 'refill'), ('cleaning', 'cleaning')])
@pytest.mark.parametrize('check_time', ['2026-02-18T14:00:00+00:00', '2026-03-09T13:00:00+00:00'])
async def test_real_helper_local_timezone_deadlines(hass, freezer, request, helper, kind, check_time):
    # The March case crosses the US spring DST transition within the interval.
    import time
    from datetime import datetime
    # Force the process timezone to differ from HA's configured timezone, so a
    # naive helper state parsed in process time would shift the deadline.
    import os
    previous_tz = os.environ.get('TZ')

    def restore_tz():
        if previous_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous_tz
        time.tzset()

    request.addfinalizer(restore_tz)
    os.environ['TZ'] = 'UTC'
    time.tzset()
    naive = datetime(2026, 2, 15, 9, 0, 0)
    assert naive.timestamp() == datetime.fromisoformat('2026-02-15T09:00:00+00:00').timestamp()
    check = datetime.fromisoformat(check_time)
    freezer.move_to(check - timedelta(days=1))
    await hass.config.async_set_time_zone('America/New_York')
    assert await async_setup_component(hass, 'input_datetime', {'input_datetime': {
        helper: {'has_date': True, 'has_time': True},
    }})
    calls = await install(hass, NAMES[2], {
        'refill_days': 3, 'cleaning_days': 3,
        'refill_notification_action': [{'action': 'test.notify', 'data': {'kind': 'refill'}}],
        'cleaning_notification_action': [{'action': 'test.notify', 'data': {'kind': 'cleaning'}}],
    })
    entity = f'input_datetime.{helper}'
    for delta, expected in [(1, []), (-1, [kind])]:
        stamp = (check - timedelta(days=3, seconds=-delta)).timestamp()
        await hass.services.async_call('input_datetime', 'set_datetime', {'entity_id': entity, 'timestamp': stamp}, blocking=True)
        freezer.move_to(check)
        async_fire_time_changed(hass, check)
        await hass.async_block_till_done()
        assert [call.data['kind'] for call in calls] == expected
        check += timedelta(days=1)
    await hass.services.async_call('automation', 'turn_off', {'entity_id': 'automation.refill_cleaning_reminder'}, blocking=True)


async def test_real_helpers_accept_observations(hass):
    assert await async_setup_component(hass, 'input_datetime', {'input_datetime': {
        'refill': {'has_date': True, 'has_time': True},
        'cleaning': {'has_date': True, 'has_time': True},
    }})
    assert await async_setup_component(hass, 'input_button', {'input_button': {'cleaned': {}}})
    await install(hass, NAMES[2])
    await set_state(hass, 'event.feeding', 'unknown')
    stamp = dt_util.utcnow().isoformat()
    await set_state(hass, 'event.feeding', stamp, {'event_type': 'refilled'})
    assert hass.states.get('input_datetime.refill').attributes['timestamp'] == dt_util.parse_datetime(stamp).timestamp()
    await hass.services.async_call('input_button', 'press', {'entity_id': 'input_button.cleaned'}, blocking=True)
    await hass.async_block_till_done()
    button_stamp = dt_util.parse_datetime(hass.states.get('input_button.cleaned').state)
    assert hass.states.get('input_datetime.cleaning').attributes['timestamp'] == button_stamp.timestamp()
    await hass.services.async_call('automation', 'turn_off', {'entity_id': 'automation.refill_cleaning_reminder'}, blocking=True)


async def test_refill_and_cleaning_reminders(hass):
    updates = []
    hass.services.async_register('input_datetime', 'set_datetime', lambda call: updates.append(call))
    calls = await install(hass, NAMES[2])
    stamps = [(dt_util.utcnow() - timedelta(seconds=0.1 * (6 - i))).isoformat() for i in range(6)]
    await set_state(hass, 'event.feeding', stamps[0], {'event_type': 'refilled', 'restored': True})
    await set_state(hass, 'event.feeding', stamps[1], {'event_type': 'refilled'})
    assert not updates
    await set_state(hass, 'event.feeding', stamps[2], {'event_type': 'refilled'})
    await set_state(hass, 'event.feeding', stamps[3], {'event_type': 'refilled'})
    assert len(updates) == 2
    assert all(c.data['entity_id'] == ['input_datetime.refill'] for c in updates)
    assert [c.data['timestamp'] for c in updates] == [dt_util.parse_datetime(s).timestamp() for s in stamps[2:4]]
    await set_state(hass, 'event.feeding', stamps[4], {'event_type': 'dispensed'})
    assert len(updates) == 2
    await set_state(hass, 'input_button.cleaned', stamps[0])
    await set_state(hass, 'input_button.cleaned', stamps[1])
    assert len(updates) == 3
    assert updates[-1].data['entity_id'] == ['input_datetime.cleaning']
    assert updates[-1].data['timestamp'] == dt_util.parse_datetime(stamps[1]).timestamp()
    # Trigger the actual daily time listener, not automation.trigger (which lacks its ID).
    tomorrow = dt_util.as_local(dt_util.utcnow() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    async_fire_time_changed(hass, tomorrow)
    await hass.async_block_till_done()
    assert not calls  # Unset helpers are not overdue.
    for entity, days in [('input_datetime.refill', 8), ('input_datetime.cleaning', 31)]:
        await set_state(hass, entity, (dt_util.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S'))
    async_fire_time_changed(hass, tomorrow + timedelta(days=1))
    await hass.async_block_till_done()
    assert len(calls) == 2
    await hass.services.async_call('automation', 'turn_off', {'entity_id': 'automation.refill_cleaning_reminder'}, blocking=True)
