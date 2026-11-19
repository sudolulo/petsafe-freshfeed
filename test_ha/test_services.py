"""Strict operation boundary and actual-report semantic contracts."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.petsafe_freshfeed.local_source import DeviceReport, DispatchMark
from custom_components.petsafe_freshfeed.write_adapter import IntentContext, RecoveryEvidence


def report(dps, sequence=2, epoch=1):
    return DeviceReport(epoch, sequence, datetime.now(timezone.utc), dps)


@pytest.mark.parametrize('operation,dps,decision', [
    ('schedule', {'1': '/w=='}, 'observed'),
    ('schedule', {'1': 'fwgAAQE='}, 'ignore'),
    ('feed', {'140': 'EAEAAA=='}, 'observed'),
    ('feed', {'140': 'EAIAAA=='}, 'observed'),
    ('feed', {'140': 'EAMAAA=='}, 'observed'),
    ('feed', {'140': 'EAEAAQ=='}, 'ignore'),
    ('feed', {'140': 'EAEA'}, 'observed'),
    ('feed', {'140': 'EAIA'}, 'observed'),
    ('feed', {'140': 'EAMA'}, 'observed'),
    ('feed', {'140': 'EAQA'}, 'observed_failure'),
    ('feed', {'140': 'EAAA'}, 'ignore'),
    ('cancel', {'140': 'IAAA'}, 'observed'),
    ('cancel', {'140': 'IAEA'}, 'ignore'),
    ('cancel', {'140': 'EAAA'}, 'ignore'),
    ('cancel', {'140': 'IAQA'}, 'observed_failure'),
    ('defrost', {'142': 1}, 'observed'),
    ('defrost', {'142': True}, 'ignore'),
    ('refilled', {'145': 1}, 'observed'),
    ('refilled', {'145': True}, 'ignore'),
])
def test_acknowledgement(operation, dps, decision):
    from custom_components.petsafe_freshfeed.services import acknowledgement
    ctx = IntentContext(operation, 'op', 'session', 1, {'1': '/w=='})
    mark = DispatchMark('op', 1, 1)
    assert acknowledgement(ctx, mark, report(dps)) == decision
    assert acknowledgement(ctx, mark, report(dps, sequence=1)) == 'ignore'
    assert acknowledgement(ctx, mark, report(dps, epoch=2)) == 'ignore'
    assert acknowledgement(ctx, DispatchMark('other', 1, 1), report(dps)) == 'ignore'


@pytest.mark.parametrize('operation,extra', [('schedule', {'1': '/w=='}),
    ('feed', {'140': 'EAAA'}), ('feed', {'140': 'EAAAAA=='}),
    ('feed', {'140': 'EAMAAA=='}), ('defrost', {'142': 0}), ('refilled', {}),
    ('cancel', {'140': 'IAAA', '142': 0})])
def test_recovery_combines_only_fresh_safe_evidence(operation, extra):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = IntentContext(operation, 'op', 'session', 1, {})
    rows = (report({'4': 'standby'}, 2), report({'145': 2, **extra}, 3))
    evidence = RecoveryEvidence(ctx, 1, 3, rows)
    assert recovery_allowed(ctx, evidence)
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 3, 3, rows))
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 4, rows + (report({'4': 'feeding'}, 4),)))
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 4, rows + (report({'140': 'EAIA'}, 4),)))
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 4, rows + (report({'142': 1}, 4),)))
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 2, rows[:1]))
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 3, (rows[0], report({'145': 2, **extra}, 3, epoch=2))))
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 4, rows + (report({'140': 'EAIAAA=='}, 4),)))
    for invalid in ({'140': 'EA=='}, {'142': -1}, {'4': 'enum_4'}):
        assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 4, rows + (report(invalid, 4),)))


@pytest.mark.parametrize('status', ['standby', 'done', 'last_meal', 'no_food',
    'feeding', 'defrost', 'defrosting', 'enum_4', 'idle', 'ready', 'empty', 'run_out', 'unknown', None])
def test_recovery_status_matrix(status):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = IntentContext('cancel', 'op', 'session', 1, {})
    dps = {'4': status, '145': 6, '140': 'IAAA', '142': 0}
    assert recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 2, (report(dps),))) == (status in ('standby', 'done', 'last_meal', 'no_food'))


@pytest.mark.parametrize('invalid', [{'4': 'feeding'}, {'4': None}, {'4': []}, {'4': {}}, {'140': 'EAEA'},
    {'140': 'EAIA'}, {'140': 'bad'}, {'142': True}, {'142': -1}, {'142': 1},
    {'145': True}, {'145': 0}, {'145': 7}])
def test_unsafe_then_safe_reports_latch_recovery_veto(invalid):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = IntentContext('cancel', 'op', 'session', 1, {})
    safe = {'4': 'standby', '145': 6, '140': 'IAAA', '142': 0}
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 3, (report(invalid), report(safe, 3))))


@pytest.mark.parametrize('operation', ['feed', 'cancel', 'defrost', 'refilled', 'schedule', 'settings'])
def test_local_abandonment_requires_fresh_triple(operation):
    from custom_components.petsafe_freshfeed import services
    predicate = getattr(services, 'local_abandonment_allowed', lambda *args: False)
    ctx = IntentContext(operation, 'op', 'session', 1, {'110': True} if operation == 'settings' else {})
    fresh = {'4': 'no_food', '142': 0, '145': 6}
    assert predicate(ctx, RecoveryEvidence(ctx, 1, 2, (report(fresh),)))
    for missing in fresh:
        assert not predicate(ctx, RecoveryEvidence(ctx, 1, 2, (report({k: v for k, v in fresh.items() if k != missing}),)))
    if operation == 'cancel':
        for quick in ('EAEA', 'EAIA', 'EAMA', 'EAQA', 'bad'):
            assert not predicate(ctx, RecoveryEvidence(ctx, 1, 2, (report(fresh | {'140': quick}),)))


@pytest.mark.parametrize('operation', ['schedule', 'settings'])
@pytest.mark.parametrize('quick', ['IAAA', 'EAMA', 'EAQA'])
@pytest.mark.parametrize('in_baseline', [False, True])
def test_reported_dp140_cannot_abandon_missing_operation_readback(operation, quick, in_baseline):
    """The missing-140 route cannot bypass schedule/setting readback requirements."""
    from custom_components.petsafe_freshfeed.services import local_abandonment_allowed, recovery_allowed
    ctx = IntentContext(operation, 'op', 'session', 1, {'110': True} if operation == 'settings' else {})
    fresh = {'4': 'standby', '142': 0, '145': 2}
    baseline = {'140': quick} if in_baseline else {}
    if not in_baseline:
        fresh['140'] = quick
    evidence = RecoveryEvidence(ctx, 1, 2, (report(fresh),),
        baseline_epoch=1, baseline_dps=baseline)
    assert not recovery_allowed(ctx, evidence)
    assert not local_abandonment_allowed(ctx, evidence)


def test_recovery_observed_baseline_only_safety_keys():
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = IntentContext('cancel', 'op', 'session', 1, {})
    baseline = {'4': 'standby', '145': 6, '140': 'IAAA', '142': 0}
    evidence = SimpleNamespace(context=ctx, start_sequence=1, end_sequence=2,
        reports=(report({'110': True}),), baseline_epoch=1, baseline_dps=baseline)
    assert recovery_allowed(ctx, evidence)
    evidence.baseline_epoch = 2
    assert not recovery_allowed(ctx, evidence)
    evidence.baseline_epoch = 1
    for operation, request in [('schedule', {}), ('settings', {'111': 4})]:
        evidence.context = IntentContext(operation, 'op', 'session', 1, request)
        evidence.baseline_dps = baseline | {'1': '/w==', '111': 4}
        assert not recovery_allowed(evidence.context, evidence)


def test_no_food_safe_for_recovery_only():
    from custom_components.petsafe_freshfeed.services import operation_reason, recovery_allowed
    ctx = IntentContext('cancel', 'op', 'session', 1, {})
    dps = {'4': 'no_food', '145': 6, '140': 'IAAA', '142': 0}
    assert recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 2, (report(dps),)))
    for operation in ('feed', 'cancel', 'settings'):
        assert operation_reason(operation, dps) == 'status_unavailable'
    settings = IntentContext('settings', 'op', 'session', 1, {})
    assert recovery_allowed(settings, RecoveryEvidence(settings, 1, 2, (report(dps),)))


@pytest.mark.parametrize('operation,dps,reason', [
    ('cancel', {'4': 'standby','145': 1,'140': 'EAEA','142': 0}, None),
    ('cancel', {'4': 'standby','145': 6,'140': 'EAEA','142': 0}, None),
    ('cancel', {'4': 'standby','145': 1,'140': 'EAAA','142': 0}, 'cancel_not_pending'),
    ('cancel', {'4': 'standby','145': 1,'140': 'EAIA','142': 0}, 'quick_feed_active'),
    ('cancel', {'4': 'standby','145': 1,'140': 'EAEA'}, 'status_unavailable'),
    ('cancel', {'4': 'standby','145': 1,'140': 'EAEA','142': 1}, 'defrost_active'),
    ('feed', {}, 'status_unavailable'),
    ('feed', {'4': 'standby','145': 2,'140': 'EAEA','142': 0}, 'quick_feed_active'),
    ('feed', {'4': 'standby','145': 2,'140': 'EAIA','142': 0}, 'quick_feed_active'),
    ('feed', {'4': 'standby','145': 2,'140': 'EAEFAA==','142': 0}, 'quick_feed_active'),
    ('feed', {'4': 'standby','145': 2,'140': 'EAIAAA==','142': 0}, 'quick_feed_active'),
    ('feed', {'4': 'standby','145': 6,'140': 'EAAA','142': 0}, 'empty_tray'),
    ('defrost', {'4': 'standby','145': 2,'140': 'EAAA','142': 1}, 'defrost_active'),
    ('defrost', {'4': 'defrost','145': 2,'140': 'EAAA','142': 0}, 'defrost_active'),
    ('refilled', {'4': 'standby','145': 1,'140': 'EAAA','142': 0}, 'already_refilled'),
    ('feed', {'4': 'standby','145': 2,'140': 'EAAA','142': 0}, None),
])
def test_interlock(operation, dps, reason):
    from custom_components.petsafe_freshfeed.services import operation_reason
    assert operation_reason(operation, dps) == reason


@pytest.mark.parametrize('operation,quick', [
    ('feed', 'EAAA'), ('feed', 'EAMA'), ('feed', 'EAQA'),
    ('feed', 'EAEA'), ('feed', 'bad'), ('cancel', 'EAEA'),
    ('cancel', 'EAAA'), ('defrost', 'EAIA'), ('refilled', 'EAAA'),
])
def test_source_decision_local_dp140_uses_operation_reason(operation, quick):
    from custom_components.petsafe_freshfeed.const import FEED_NOW_COMMAND, FEED_START_COMMAND
    from custom_components.petsafe_freshfeed.services import operation_reason, source_operation_decision
    dps = {'4': 'standby', '142': 0, '145': 2, '140': quick}
    reason, command = source_operation_decision(operation, dps, {}, 100, None, None)
    assert reason == operation_reason(operation, dps)
    expected = FEED_NOW_COMMAND if quick == 'EAMA' else FEED_START_COMMAND
    assert command == (expected if operation == 'feed' and reason is None else None)


@pytest.mark.parametrize('quick,command_name', [
    ('EAAA', 'FEED_START_COMMAND'), ('EAMA', 'FEED_NOW_COMMAND'),
    ('EAQA', 'FEED_START_COMMAND'),
])
@pytest.mark.parametrize('operation', ['feed', 'defrost', 'refilled', 'settings'])
def test_source_decision_inactive_cloud_with_fresh_local_triple(operation, quick, command_name):
    from custom_components.petsafe_freshfeed import const, services
    dps = {'4': 'standby', '142': 0, '145': 2}
    times = dict.fromkeys(dps, 100)
    reason, command = services.source_operation_decision(operation, dps, times, 100, quick, 0)
    assert reason is None
    assert command == (getattr(const, command_name) if operation == 'feed' else None)


@pytest.mark.parametrize('quick', ['EAEA', 'EAIA', 'invalid'])
@pytest.mark.parametrize('age', [0, 60, 1000])
@pytest.mark.parametrize('operation', ['feed', 'defrost', 'refilled', 'settings', 'cancel'])
def test_source_decision_active_or_malformed_cloud_vetoes_even_expired(operation, quick, age):
    from custom_components.petsafe_freshfeed.services import source_operation_decision
    dps = {'4': 'standby', '142': 0, '145': 2}
    assert source_operation_decision(operation, dps, dict.fromkeys(dps, 100), 100, quick, age)[0] is not None


@pytest.mark.parametrize('cloud,age', [(None, None), ('EAAA', 60), ('EAMA', 61), ('EAQA', 1000)])
@pytest.mark.parametrize('operation', ['feed', 'defrost', 'refilled', 'settings'])
def test_source_decision_absent_or_expired_inactive_uses_fresh_local_exception(operation, cloud, age):
    from custom_components.petsafe_freshfeed.const import FEED_START_COMMAND
    from custom_components.petsafe_freshfeed.services import source_operation_decision
    dps = {'4': 'standby', '142': 0, '145': 2}
    assert source_operation_decision(operation, dps, dict.fromkeys(dps, 100), 100, cloud, age) == (
        None, FEED_START_COMMAND if operation == 'feed' else None)
    dps['4'] = 'no_food'
    reason, command = source_operation_decision(operation, dps, dict.fromkeys(dps, 100), 100, cloud, age)
    assert (reason is None) == (operation != 'feed')
    assert command is None


@pytest.mark.parametrize('key', ['4', '142', '145'])
@pytest.mark.parametrize('age,allowed', [(35, True), (35.001, False), (-0.001, False)])
def test_source_decision_local_freshness_boundary(key, age, allowed):
    from custom_components.petsafe_freshfeed.services import source_operation_decision
    dps = {'4': 'standby', '142': 0, '145': 2}
    times = dict.fromkeys(dps, 100)
    times[key] = 100 - age
    assert (source_operation_decision('feed', dps, times, 100, 'EAAA', 0)[0] is None) == allowed
    del times[key]
    assert source_operation_decision('feed', dps, times, 100, 'EAAA', 0)[0] is not None


@pytest.mark.parametrize('status', [[], {}, ['standby']])
@pytest.mark.parametrize('cloud,age', [(None, None), ('EAAA', 0)])
@pytest.mark.parametrize('operation', ['feed', 'defrost', 'refilled', 'settings'])
def test_source_decision_unhashable_status_fails_closed(status, cloud, age, operation):
    from custom_components.petsafe_freshfeed.services import source_operation_decision
    dps = {'4': status, '142': 0, '145': 2}
    assert source_operation_decision(operation, dps, dict.fromkeys(dps, 100), 100,
                                     cloud, age) == ('status_unavailable', None)


def test_source_decision_accepts_decoded_cloud_quick_feed():
    from custom_components.petsafe_freshfeed.quick_feed import decode_quick_feed
    from custom_components.petsafe_freshfeed.const import FEED_NOW_COMMAND
    from custom_components.petsafe_freshfeed.services import source_operation_decision
    dps = {'4': 'standby', '142': 0, '145': 2}
    assert source_operation_decision('feed', dps, dict.fromkeys(dps, 100), 100,
                                     decode_quick_feed('EAMA'), 0) == (None, FEED_NOW_COMMAND)


def test_source_decision_cloud_freshness_boundary_and_veto():
    from custom_components.petsafe_freshfeed.const import FEED_NOW_COMMAND, FEED_START_COMMAND
    from custom_components.petsafe_freshfeed.services import source_operation_decision
    dps = {'4': 'standby', '142': 0, '145': 2}
    times = dict.fromkeys(dps, 100)
    assert source_operation_decision('feed', dps, times, 100, 'EAMA', 59.999) == (None, FEED_NOW_COMMAND)
    assert source_operation_decision('feed', dps, times, 100, 'EAMA', 60) == (None, FEED_START_COMMAND)
    assert source_operation_decision('feed', dps, times, 100, None, None, cloud_veto=True)[0] is not None


def test_source_decision_cancel_requires_local_pending_schedule_ignores_dp140():
    from custom_components.petsafe_freshfeed.services import source_operation_decision
    dps = {'4': 'standby', '142': 0, '145': 2}
    times = dict.fromkeys(dps, 100)
    for cloud, age in [(None, None), ('EAEA', 0), ('EAAA', 0)]:
        assert source_operation_decision('cancel', dps, times, 100, cloud, age)[0] is not None
        assert source_operation_decision('schedule', dps, {}, 100, cloud, age, cloud_veto=True) == (None, None)
    assert source_operation_decision('cancel', dps | {'140': 'EAEA'}, times, 100, None, None) == (None, None)
    assert source_operation_decision('schedule', {'140': 'invalid'}, {}, 100, 'invalid', 999, cloud_veto=True) == (None, None)


@pytest.mark.parametrize('changes', [
    {'140': 'EAEA'}, {'140': 'EAIA'}, {'140': 'EAMA'}, {'140': 'EAQA'},
    {'140': 'bad'}, {'140': None}, {'142': None}, {'142': True},
    {'145': 0}, {'145': True}, {'4': 'feeding'},
])
def test_cancel_recovery_requires_fresh_idle_and_all_safe_prerequisites(changes):
    from custom_components.petsafe_freshfeed.services import recovery_allowed
    ctx = IntentContext('cancel', 'op', 'session', 1, {})
    dps = {'4': 'standby', '145': 1, '140': 'IAAA', '142': 0} | changes
    assert not recovery_allowed(ctx, RecoveryEvidence(ctx, 1, 2, (report(dps),)))


@pytest.mark.parametrize('service,data', [
    ('command', {'entry_id': 'a', 'operation': 'feed', 'confirmed': True}),
    ('command', {'entry_id': 'a', 'operation': 'cancel', 'confirmed': True}),
    ('save_schedule', {'entry_id': 'a', 'entries': [], 'base_revision': 'revision'}),
    ('recover', {'entry_id': 'a', 'operation_id': 'op', 'session_id': 'session', 'confirmed': True}),
])
@pytest.mark.parametrize('caller', ['missing', 'unknown', 'nonadmin'])
async def test_write_services_reject_non_admin_callers(hass, service, data, caller):
    from custom_components.petsafe_freshfeed.services import async_register
    adapter = SimpleNamespace(async_submit=AsyncMock(return_value={'admitted': True}),
                              async_recover=AsyncMock(return_value=True))
    async_register(hass, SimpleNamespace(entry_id='a', runtime_data=SimpleNamespace(adapter=adapter)))
    from homeassistant.core import Context
    user = None if caller == 'unknown' else SimpleNamespace(is_admin=False)
    hass.auth.async_get_user = AsyncMock(return_value=user)
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call('petsafe_freshfeed', service, data, blocking=True,
                                       context=Context(user_id=None if caller == 'missing' else caller))
    assert caught.value.translation_key == 'admin_required'
    assert caught.value.translation_domain == 'petsafe_freshfeed'
    adapter.async_submit.assert_not_awaited()
    adapter.async_recover.assert_not_awaited()


@pytest.mark.parametrize('reason,expected', [
    ('stale_schedule', 'Reload the schedule before saving again.'),
    ('schedule_conflict', 'Reload the schedule before saving again.'),
    ('storage_fault', 'Check storage and write status; do not delete retained intent.'),
    ('integrity_fault', 'Check storage and write status; do not delete retained intent.'),
    ('intent_uncertain', 'Inspect the feeder and use the existing recovery controls.'),
    ('recovery_in_progress', 'Inspect the feeder and use the existing recovery controls.'),
    ('recovery_rejected', 'Inspect the feeder and use the existing recovery controls.'),
    ('admin_required', 'Use an administrator account.'),
    ('busy', 'Check the feeder connection, controls and write status before trying again.'),
    ('controls_disabled', 'Check the feeder connection, controls and write status before trying again.'),
    ('SENTINEL-private-value', 'The request could not be confirmed. Check device and write status before trying again.'),
])
async def test_service_refusals_use_fixed_translations(hass, reason, expected):
    from homeassistant.core import Context
    from custom_components.petsafe_freshfeed.services import async_register
    adapter = SimpleNamespace(async_submit=AsyncMock(return_value={'admitted': False, 'reason': reason}))
    async_register(hass, SimpleNamespace(entry_id='a', runtime_data=SimpleNamespace(adapter=adapter)))
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call('petsafe_freshfeed', 'save_schedule',
            {'entry_id': 'a', 'entries': [], 'base_revision': 'SENTINEL-revision'},
            blocking=True, context=Context(user_id='admin'))
    assert caught.value.translation_key == (reason if not reason.startswith('SENTINEL') else 'request_refused')
    assert caught.value.translation_domain == 'petsafe_freshfeed'
    assert not caught.value.translation_placeholders
    assert await resolved_exception_text(hass, caught.value) == expected
    assert 'SENTINEL' not in str(caught.value)
    adapter.async_submit.assert_awaited_once_with('schedule', {'1': '/w=='}, base_revision='SENTINEL-revision')


async def resolved_exception_text(hass, error):
    """Resolve the exception using Home Assistant's translation catalog."""
    from homeassistant.helpers.translation import async_get_translations
    translations = await async_get_translations(hass, 'en', 'exceptions',
                                                integrations={error.translation_domain})
    key = f'component.{error.translation_domain}.exceptions.{error.translation_key}.message'
    return translations[key].format(**(error.translation_placeholders or {}))


@pytest.mark.parametrize('key,placeholders,expected', [
    ('setting_refused', {'reason': 'Check the feeder connection, controls and write status before trying again.'},
     'Setting refused: Check the feeder connection, controls and write status before trying again.'),
    ('setting_uncertain', None, 'The setting could not be confirmed. Inspect the feeder and write status.'),
    ('invalid_setting', None, 'The setting value is invalid.'),
    ('controls_disabled', None, 'Check the feeder connection, controls and write status before trying again.'),
])
async def test_setting_exceptions_resolve_fixed_text(hass, key, placeholders, expected):
    from homeassistant.exceptions import HomeAssistantError
    error = HomeAssistantError(translation_domain='petsafe_freshfeed', translation_key=key,
                               translation_placeholders=placeholders)
    assert await resolved_exception_text(hass, error) == expected


async def test_validation_precedes_admin_and_does_not_leak(hass):
    from custom_components.petsafe_freshfeed.services import async_register
    adapter = SimpleNamespace(async_submit=AsyncMock())
    async_register(hass, SimpleNamespace(entry_id='a', runtime_data=SimpleNamespace(adapter=adapter)))
    hass.auth.async_get_user = AsyncMock()
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call('petsafe_freshfeed', 'command',
            {'entry_id': 'SENTINEL', 'operation': 'feed', 'confirmed': True, 'extra': 'SENTINEL'}, blocking=True)
    assert caught.value.translation_key == 'invalid_request'
    assert 'SENTINEL' not in str(caught.value)
    hass.auth.async_get_user.assert_not_awaited()
    adapter.async_submit.assert_not_awaited()


@pytest.mark.parametrize('operation', ['feed', 'cancel'])
async def test_services_strict_payload_routing_and_entry_lifetime(hass, operation):
    from custom_components.petsafe_freshfeed.services import async_register, async_unregister
    adapters = [SimpleNamespace(async_submit=AsyncMock(return_value={'admitted': True}),
                                async_recover=AsyncMock(return_value=True)) for _ in range(2)]
    for name, adapter in zip(('a', 'b'), adapters):
        async_register(hass, SimpleNamespace(entry_id=name, runtime_data=SimpleNamespace(adapter=adapter)))
    domain = 'petsafe_freshfeed'
    from homeassistant.core import Context
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    admin_context = Context(user_id='admin')
    payload = {'entry_id': 'a', 'operation': operation, 'confirmed': True}
    await hass.services.async_call(domain, 'command', payload, blocking=True, context=admin_context)
    adapters[0].async_submit.assert_awaited_once_with(
        operation, {'140': 'IA==' if operation == 'cancel' else 'EA=='}, confirmed=True)
    assert adapters[1].async_submit.await_count == 0
    await hass.services.async_call(domain, 'command', payload | {'operation': 'cancel'}, blocking=True, context=admin_context)
    adapters[0].async_submit.assert_awaited_with('cancel', {'140': 'IA=='}, confirmed=True)
    for change in ({'confirmed': False}, {'confirmed': None}, {'confirmed': 1},
                   {'confirmed': 'true'}, {'extra': 1}, {'dps': {'140': 'IA=='}},
                   {'operation': 'raw'}, {'operation': True}, {'entry_id': 'missing'}):
        with pytest.raises((ServiceValidationError, __import__('voluptuous').Invalid)):
            await hass.services.async_call(domain, 'command', payload | change, blocking=True, context=admin_context)
    await hass.services.async_call(domain, 'save_schedule', {'entry_id': 'b', 'entries': [], 'base_revision': 'digest'}, blocking=True, context=admin_context)
    adapters[1].async_submit.assert_awaited_once_with('schedule', {'1': '/w=='}, base_revision='digest')
    for entries in ([{'weekmask': 127,'hour': 8,'minute': 0,'enabled': 1,'audible': True}], [{}]*11):
        with pytest.raises((ServiceValidationError, __import__('voluptuous').Invalid)):
            await hass.services.async_call(domain, 'save_schedule', {'entry_id':'b','entries':entries,'base_revision':'digest'}, blocking=True, context=admin_context)
    await hass.services.async_call(domain, 'recover', {'entry_id':'a','operation_id':'op','session_id':'session','confirmed':True}, blocking=True, context=admin_context)
    adapters[0].async_recover.assert_awaited_once_with('op', 'session', confirmed=True)
    async_unregister(hass, 'a')
    assert hass.services.has_service(domain, 'command')
    async_unregister(hass, 'b')
    assert not hass.services.has_service(domain, 'command')


async def test_near_meal_schedule_admitted_under_normal_rules(hass, freezer):
    """A meal two minutes away is advisory in the card only; the service applies no timing rule."""
    from homeassistant.core import Context
    from custom_components.petsafe_freshfeed.services import async_register
    await hass.config.async_set_time_zone('UTC')
    freezer.move_to('2026-11-18T07:58:00+00:00')
    adapter = SimpleNamespace(async_submit=AsyncMock(return_value={'admitted': True}))
    async_register(hass, SimpleNamespace(entry_id='a', runtime_data=SimpleNamespace(adapter=adapter)))
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    entries = [{'weekmask': 127, 'hour': 8, 'minute': 0, 'enabled': True, 'audible': False}]
    await hass.services.async_call('petsafe_freshfeed', 'save_schedule',
        {'entry_id': 'a', 'entries': entries, 'base_revision': 'digest'},
        blocking=True, context=Context(user_id='admin'))
    adapter.async_submit.assert_awaited_once()
    operation, dps = adapter.async_submit.await_args.args
    assert operation == 'schedule'
    assert set(dps) == {'1'}
    assert adapter.async_submit.await_args.kwargs == {'base_revision': 'digest'}


@pytest.mark.parametrize('status', [[], {}, ['standby'], {'status': 'standby'}, None, True, 4])
@pytest.mark.parametrize('operation', ['cancel', 'feed', 'defrost', 'refilled', 'settings'])
def test_malformed_dp4_refuses_every_operation_without_raising(operation, status):
    """An unhashable or non-string DP4 is unavailable evidence on every branch."""
    from custom_components.petsafe_freshfeed.services import operation_reason
    dps = {'4': status, '145': 2, '140': 'EAEA' if operation == 'cancel' else 'EAAA', '142': 0}
    assert operation_reason(operation, dps) == 'status_unavailable'


@pytest.mark.parametrize('status', [[], {}, ['defrost'], {'status': 'defrost'}, True, 4])
def test_malformed_dp4_never_acknowledges_defrost_and_does_not_raise(status):
    from custom_components.petsafe_freshfeed.services import acknowledgement
    ctx = IntentContext('defrost', 'op', 'session', 1, {'142': 1})
    assert acknowledgement(ctx, DispatchMark('op', 1, 1), report({'4': status, '142': 0})) == 'ignore'
