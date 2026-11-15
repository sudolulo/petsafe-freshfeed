"""Strict operation boundary and actual-report semantic contracts."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.petsafe_freshfeed.tuya_bridge import DeviceReport, DispatchMark
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
    with pytest.raises(ServiceValidationError, match='admin_required'):
        await hass.services.async_call('petsafe_freshfeed', service, data, blocking=True,
                                       context=Context(user_id=None if caller == 'missing' else caller))
    adapter.async_submit.assert_not_awaited()
    adapter.async_recover.assert_not_awaited()


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
