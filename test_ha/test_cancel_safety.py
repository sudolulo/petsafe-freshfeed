"""Cancel regression matrix using the real adapter and owned fake transport."""
import asyncio

import pytest
from homeassistant.util import dt as dt_util

from test_write_adapter import (  # noqa: F401
    adapters, owned, store, writes, GatedWitness, _adapter, _provision,
    _queue_reports, _push, _open, until,
)

PENDING = {'4': 'standby', '145': 1, '140': 'EAEA', '142': 0}
PAYLOAD = {'140': 'IA=='}


@pytest.mark.parametrize('operation', ['cancel', 'feed', 'defrost', 'refilled', 'schedule', 'settings'])
@pytest.mark.parametrize('evidence', [
    'safe', 'missing_receipt', 'unknown', 'attempted', 'client_returned',
    'pending', 'storage', 'integrity', 'mark', 'context_dispatch',
])
async def test_finish_submit_rejects_only_proven_unsent_cancel(adapters, store, evidence, operation):
    from dataclasses import replace
    from custom_components.petsafe_freshfeed.critical_transport import SendReceipt
    from custom_components.petsafe_freshfeed.write_adapter import IntentContext
    adapter = await _adapter(adapters, await _provision(store))
    receipt = SendReceipt('op', 'not_sent', False, False, 'no_socket')
    adapter._submitting = IntentContext(operation, '', 'session', adapters.bridge.epoch, PAYLOAD)
    if evidence == 'missing_receipt':
        receipt = None
    elif evidence == 'unknown':
        receipt = replace(receipt, outcome='unknown')
    elif evidence in ('attempted', 'client_returned'):
        receipt = replace(receipt, **{evidence: True})
    elif evidence == 'pending':
        adapter.transport._blocked = True
        adapter.transport._pending = {'operation_id': 'op', 'operation': 'cancel', 'state': 'unknown'}
    elif evidence == 'storage':
        adapter.transport._storage_fault = True
    elif evidence == 'integrity':
        adapter.transport._integrity_fault = True
    elif evidence == 'mark':
        adapter._marks['op'] = object()
    elif evidence == 'context_dispatch':
        adapter._submitting = replace(adapter._submitting, operation_id='op')
    result = await adapter._finish_submit(receipt)
    assert result['admitted'] is (operation != 'cancel' or evidence != 'safe')
    if evidence == 'safe':
        assert result['reason'] == 'no_socket'


@pytest.mark.parametrize('stage', ['lock', 'witness'])
@pytest.mark.parametrize('failure', ['pre_dispatch', 'attempted', 'pending_changed'])
async def test_cancel_real_service_response_drives_dom_latch(
        adapters, store, writes, stage, failure):
    """Real transport -> adapter -> HA service/WS error mapping -> card DOM."""
    import json
    import logging
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from homeassistant.core import Context
    from homeassistant.components.websocket_api.commands import handle_call_service
    from custom_components.petsafe_freshfeed.services import async_register

    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    adapter.entry.runtime_data.adapter = adapter
    async_register(ctx.hass, adapter.entry)
    ctx.hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    responses = []
    connection = SimpleNamespace(
        context=lambda msg: Context(user_id='admin'), logger=logging.getLogger(__name__),
        send_result=lambda *args: responses.append({'success': True}),
        send_error=lambda ident, code, message, **kwargs: responses.append(
            {'success': False, 'error': {'code': code, 'message': message}}))
    lock = adapter.transport._lock
    gate = asyncio.Event()
    if stage == 'lock':
        await lock.acquire()
    else:
        witness.gate = gate
    _queue_reports(ctx, PENDING)
    initial = adapter.view
    task = ctx.hass.async_create_task(handle_call_service.__wrapped__(ctx.hass, connection, {
        'id': 1, 'type': 'call_service', 'domain': 'petsafe_freshfeed',
        'service': 'command', 'return_response': False,
        'service_data': {'entry_id': 'synthetic', 'operation': 'cancel', 'confirmed': True},
    }))
    try:
        if stage == 'lock':
            await until(lambda: adapter.transport._busy)
        else:
            await asyncio.wait_for(witness.entered.wait(), 3)
        if failure != 'attempted':
            _queue_reports(ctx, {'140': 'EAAA'})
        if failure == 'pre_dispatch':
            # Real socket loss makes the transport reject BEFORE executor dispatch.
            ctx.device.refuse = True
            ctx.api.socket.close()
    finally:
        if stage == 'lock':
            lock.release()
        gate.set()
    await asyncio.wait_for(task, 5)
    assert len(responses) == 1
    assert len(writes) == (1 if failure == 'attempted' else 0)
    safe = failure == 'pre_dispatch'
    assert responses[0]['success'] is (not safe)
    if safe:
        assert responses[0]['error']['code'] == 'service_validation_error'
        assert not adapter.transport.needs_reconciliation
    else:
        assert adapter.transport.needs_reconciliation
        assert not (await adapter.async_submit('cancel', PAYLOAD, confirmed=True))['admitted']
        assert len(writes) == (1 if failure == 'attempted' else 0)
    env = dict(os.environ, PETSAFE_CANCEL_SERVICE_RESULT=json.dumps({
        'initial': initial, 'response': responses[0], 'safe': safe}))
    proc = await asyncio.create_subprocess_exec(
        'node', '--test', '--test-name-pattern=real HA service Cancel response',
        'test/freshfeed-dom.test.mjs', cwd=Path(__file__).parents[1], env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(proc.communicate(), 15)
    assert proc.returncode == 0, output.decode()


@pytest.mark.parametrize('change', [
    {'140': 'EAAA'}, {'140': 'EAMA'}, {'140': 'EAQA'}, {'140': 'EAIA'},
    {'140': 'bad'}, {'140': None}, {'145': None}, {'145': True},
    {'145': 0}, {'145': 7}, {'142': None}, {'142': True}, {'142': 1},
    {'4': None}, {'4': 'feeding'}, {'4': 'defrost'},
])
async def test_cancel_unsafe_admission_never_prepares(adapters, store, writes, change):
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(adapters, witness)
    _queue_reports(adapters, PENDING | change)
    assert not adapter.view['can_cancel']
    result = await adapter.async_submit('cancel', PAYLOAD, confirmed=True)
    assert not result['admitted']
    assert not witness.prepares and not writes


@pytest.mark.parametrize('stage', ['provenance', 'lock', 'witness'])
@pytest.mark.parametrize('change', ['feeding', 'idle', 'disconnect'])
async def test_cancel_rechecks_wait_boundaries(adapters, store, writes, monkeypatch, stage, change):
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness)
    gate, entered = asyncio.Event(), asyncio.Event()
    lock = adapter.transport._lock
    original = ctx.hass.async_add_executor_job
    if stage == 'provenance':
        def gated(func, *args):
            if func != ctx.bridge.code_ok:
                return original(func, *args)
            async def run():
                entered.set()
                await gate.wait()
                return await original(func, *args)
            return asyncio.create_task(run())
        monkeypatch.setattr(ctx.hass, 'async_add_executor_job', gated)
    elif stage == 'witness':
        witness.gate = gate
        entered = witness.entered
    else:
        await lock.acquire()
    _queue_reports(ctx, PENDING)
    task = ctx.hass.async_create_task(adapter.async_submit('cancel', PAYLOAD, confirmed=True))
    try:
        if stage == 'lock':
            await until(lambda: adapter.transport._busy)
        else:
            await asyncio.wait_for(entered.wait(), 3)
        if change == 'disconnect':
            adapter._on_connection(ctx.bridge.epoch, False)
        else:
            _queue_reports(ctx, {'140': 'EAIA' if change == 'feeding' else 'EAAA'})
    finally:
        if stage == 'lock':
            lock.release()
        gate.set()
    result = await task
    assert not writes
    if stage == 'provenance':
        assert not result['admitted'] and not witness.prepares
    else:
        # Executor dispatch happened even though the final send hook refused:
        # no bytes alone is NOT proof of a pre-dispatch rejection.
        assert result['admitted'] and adapter.transport.needs_reconciliation


async def test_cancel_duplicate_uncertainty_and_current_session_recovery(adapters, store, writes):
    from custom_components.petsafe_freshfeed.services import acknowledgement, recovery_allowed
    ctx = adapters
    witness = GatedWitness(await _provision(store))
    adapter = await _adapter(ctx, witness, ack=acknowledgement, recovery=recovery_allowed)
    witness.gate = asyncio.Event()
    _queue_reports(ctx, PENDING)
    task = ctx.hass.async_create_task(adapter.async_submit('cancel', PAYLOAD, confirmed=True))
    await asyncio.wait_for(witness.entered.wait(), 3)
    assert (await adapter.async_submit('cancel', PAYLOAD, confirmed=True))['reason'] == 'busy'
    witness.gate.set()
    assert (await task)['state'] == 'uncertain'
    context = adapter._current
    assert dict(context.dps) == PAYLOAD
    assert (await adapter.async_submit('cancel', PAYLOAD, confirmed=True))['reason'] == 'intent_uncertain'
    assert not await adapter.async_recover(context.operation_id, 'old-session', confirmed=True)
    _queue_reports(ctx, {'140': 'EAAA'})  # not a Cancel acknowledgement
    request = ctx.hass.async_create_task(adapter.async_recover(
        context.operation_id, context.session_id, confirmed=True), eager_start=True)
    _queue_reports(ctx, {'4': 'standby', '145': 6, '140': 'EAAA'})
    assert not request.done()  # pre-request DP142 is not fresh evidence
    _queue_reports(ctx, {'142': 0})
    assert await request
    assert adapter.view['recovery_phase'] == 'resolved'
    assert adapter._current is context
    assert len(writes) == len(witness.prepares) == 1
    assert (await _open(store).async_load_last_intent())['operation'] == 'cancel'


@pytest.mark.parametrize('final,expected', [('IAAA', 'verified'), ('IAQA', 'idle')])
async def test_cancel_ack_origin_epoch_sequence_and_command(adapters, store, writes, final, expected):
    from custom_components.petsafe_freshfeed.services import acknowledgement
    from custom_components.petsafe_freshfeed.local_source import DeviceReport
    ctx = adapters
    adapter = await _adapter(ctx, await _provision(store), ack=acknowledgement)
    _queue_reports(ctx, PENDING)
    from owned_test_support import _frame
    seen = []
    ctx.bridge.subscribe(seen.append, lambda *args: None)
    ctx.device.response = _frame({'140': 'IAAA'})  # uncorrelated exchange is not an ack
    assert (await adapter.async_submit('cancel', PAYLOAD, confirmed=True))['state'] == 'uncertain'
    context = adapter._current
    (mark,) = adapter._marks.values()
    matching = [report for report in seen if report.dps == {'140': 'IAAA'}]
    assert len(matching) == 1
    assert matching[0].sequence > mark.sequence and matching[0].ack_eligible is False
    for report in (DeviceReport(mark.epoch, mark.sequence, dt_util.utcnow(), {'140': 'IAAA'}),
                   DeviceReport(mark.epoch + 1, mark.sequence + 50, dt_util.utcnow(), {'140': 'IAAA'}),
                   DeviceReport(mark.epoch, mark.sequence + 1, dt_util.utcnow(),
                                {'140': 'IAAA'}, ack_eligible=False)):
        adapter.on_report(report)
    _queue_reports(ctx, {'140': 'EAEA'}, {'140': 'EAAA'})
    await asyncio.sleep(0)
    assert adapter.view['state'] == 'uncertain'
    _push(ctx, {'140': final})
    await until(lambda: adapter.view['state'] == expected)
    assert adapter._current is context
    if final == 'IAAA':
        assert adapter.view['can_defrost'] is True
    assert len(writes) == 1
    if final == 'IAQA':
        assert adapter.view['reason'] == 'observed_failure'
        # Preserve the existing terminal shape; add correlation, not success.
        assert adapter.view['operation_id'] is None
        assert adapter.view['operation'] is None
        import json
        from pathlib import Path
        fixture = json.loads((Path(__file__).parents[1] / 'test/fixtures/cancel-observed-failure.json').read_text())
        actual = adapter.view
        actual['session_id'] = 'session'
        assert actual.get('observed_failure') == {
            'operation_id': context.operation_id, 'operation': 'cancel',
            'session_id': context.session_id,
        }
        actual['observed_failure'] = dict(actual['observed_failure'], operation_id='cancel-op', session_id='session')
        expected_view = dict(fixture['attributes'], state=fixture['state'], can_feed=True,
                             can_defrost=True, cancel_reason='cancel_not_pending',
                             blocked_reason='already_refilled')
        assert actual == expected_view
        # A later admitted operation cannot carry this terminal outcome forward.
        _queue_reports(ctx, PENDING)
        await adapter.async_submit('cancel', PAYLOAD, confirmed=True)
        assert adapter.view['observed_failure'] is None
