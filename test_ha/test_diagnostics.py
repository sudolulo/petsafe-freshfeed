"""Diagnostics exposes only bounded public status, including failed setup."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from homeassistant.const import __version__ as HA_VERSION


async def test_allowlist_excludes_nested_sentinels(hass, caplog):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics
    secrets = {name: f"synthetic-private-{name}" for name in (
        'local_key', 'host', 'account', 'identifier', 'title', 'staged', 'path',
        'session', 'operation', 'store', 'binding', 'unknown_option')}
    hub = SimpleNamespace(snapshot=SimpleNamespace(available=True),
                          schedule=SimpleNamespace(source_state='current', entries=(1, 2)),
                          _controls_reason=secrets['operation'], nested=secrets)
    entry = SimpleNamespace(data={'connection_backend': 'owned', 'protocol_version': '3.5',
                                  'nested': secrets}, options={'nested': secrets},
                            title=secrets['title'], runtime_data=hub)
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert set(result) == {'component_version', 'ha_version', 'backend', 'protocol_version',
                           'connected', 'admission_reason', 'schedule_source_state',
                           'schedule_count', 'write_state', 'recovery_phase',
                           'storage_fault', 'integrity_fault', 'cloud_mode', 'cloud_auth', 'cloud_outcome'}
    assert result['admission_reason'] == 'unknown'
    assert result['connected'] is True and result['schedule_count'] == 2
    for value in secrets.values():
        assert value not in json.dumps(result)
        assert value not in caplog.text


async def test_cloud_diagnostics_fixed_labels_only(hass):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics
    secrets = {key: 'synthetic-private-' + key for key in ('sid', 'ecode', 'local_key', 'password')}
    entry = SimpleNamespace(data=secrets, options={'cloud_read_enabled': True},
        runtime_data=SimpleNamespace(cloud_read=SimpleNamespace(
            last_outcome=secrets['sid'], auth_state=secrets['ecode'], secrets=secrets)))
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result.get('cloud_mode') == 'enabled'
    assert result['cloud_auth'] == 'unknown'
    assert result['cloud_outcome'] == 'unknown'
    assert 'synthetic-private-' not in json.dumps(result)


async def test_diagnostics_setup_failure_no_runtime(hass):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics
    entry = SimpleNamespace(data={'connection_backend': 'owned', 'protocol_version': '3.5'})
    result = await async_get_config_entry_diagnostics(hass, entry)
    manifest = json.loads((Path(__file__).parents[1] / 'custom_components/petsafe_freshfeed/manifest.json').read_text())
    assert result == {'component_version': manifest['version'], 'ha_version': HA_VERSION,
                      'backend': 'owned', 'protocol_version': '3.5', 'connected': None,
                      'admission_reason': 'unknown', 'schedule_source_state': 'unknown',
                      'schedule_count': None, 'write_state': 'unknown',
                      'recovery_phase': 'unknown', 'storage_fault': None, 'integrity_fault': None,
                      'cloud_mode': 'fully_local', 'cloud_auth': 'unknown', 'cloud_outcome': 'unknown'}


async def test_diagnostics_never_io_or_mutates(hass):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics
    spy = Mock(side_effect=AssertionError('unexpected IO'))
    retained = {'nested': ['retained', {'intent': 'unchanged'}]}
    original = copy.deepcopy(retained)
    class Hub:
        snapshot = SimpleNamespace(available=False)
        schedule = SimpleNamespace(source_state='last_known', entries=())
        _controls_reason = 'controls_disabled'
        _store = SimpleNamespace(async_load=spy)
        journal = SimpleNamespace(read=spy)
        @property
        def write_view(self):
            return spy()
    hub = Hub()
    hub._controls_reason = 'controls_disabled'
    hub.retained = retained
    entry = SimpleNamespace(data={}, runtime_data=hub)
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result['admission_reason'] == 'controls_disabled'
    assert result['backend'] == 'unknown'
    assert spy.call_count == 0
    assert retained == original


async def test_failed_runtime_exception_is_not_logged(hass, caplog):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics
    class FailedHub:
        @property
        def snapshot(self):
            raise ValueError('synthetic-private-exception')
    result = await async_get_config_entry_diagnostics(hass, SimpleNamespace(data={}, runtime_data=FailedHub()))
    assert result['connected'] is None
    assert 'synthetic-private-exception' not in caplog.text


@pytest.mark.parametrize(('changes', 'expected'), [
    ({}, ('idle', 'idle', 'unknown', False, False)),
    ({'_submitting': object()}, ('pending', 'idle', 'unknown', False, False)),
    ({'_ack_wait': object(), '_current': object()}, ('pending', 'idle', 'unknown', False, False)),
    ({'_result': 'verified', '_current': object()}, ('verified', 'idle', 'unknown', False, False)),
    ({'_blocked': True}, ('uncertain', 'awaiting_user_confirmation', 'unknown', False, False)),
    ({'_blocked': True, '_phase': 'collecting'}, ('uncertain', 'collecting', 'unknown', False, False)),
    ({'_blocked': True, '_phase': 'failed'}, ('uncertain', 'failed', 'unknown', False, False)),
    ({'_phase': 'resolved'}, ('idle', 'resolved', 'unknown', False, False)),
    ({'_storage_fault': True}, ('blocked', 'idle', 'storage_fault', True, False)),
    ({'_integrity_fault': True}, ('blocked', 'idle', 'integrity_fault', False, True)),
    ({'_reason': 'source_incompatible'}, ('idle', 'idle', 'source_incompatible', False, False)),
    ({'_stopped': True}, ('blocked', 'idle', 'unknown', False, False)),
])
async def test_loaded_status_without_eligibility_side_effects(hass, changes, expected):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics
    from custom_components.petsafe_freshfeed.write_adapter import WriteAdapter
    from custom_components.petsafe_freshfeed.critical_transport import CriticalTransport

    # Real classes, bypassing constructors that need a source. Any accidental
    # view/eligibility read fails; only stored status is available.
    adapter = object.__new__(WriteAdapter)
    transport = object.__new__(CriticalTransport)
    vars(transport).update(_blocked=False, _storage_fault=False, _integrity_fault=False,
                           _pending={'operation_id': 'synthetic-private-operation'})
    vars(adapter).update(transport=transport, _submitting=None, _ack_wait=None,
                         _current=None, _stopped=False, _result=None, _reason=None, _phase='idle')
    for key, value in changes.items():
        vars(transport if key in vars(transport) else adapter)[key] = value
    spy = Mock(side_effect=AssertionError('eligibility must not run'))
    adapter._compatible_now = spy
    adapter._gate_reason = spy
    before_transport = copy.deepcopy(vars(transport))
    before_adapter = dict(vars(adapter))
    result = await async_get_config_entry_diagnostics(
        hass, SimpleNamespace(data={}, runtime_data=SimpleNamespace(adapter=adapter)))
    assert tuple(result[key] for key in ('write_state', 'recovery_phase', 'admission_reason',
                                        'storage_fault', 'integrity_fault')) == expected
    assert spy.call_count == 0
    assert vars(transport) == before_transport
    assert vars(adapter) == before_adapter
    assert 'synthetic-private' not in json.dumps(result)


async def test_loaded_hub_without_adapter_reports_blocked_and_idle(hass):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics

    hub = SimpleNamespace(adapter=None, _controls_reason='controls_disabled')
    result = await async_get_config_entry_diagnostics(hass, SimpleNamespace(data={}, runtime_data=hub))
    assert result['write_state'] == 'blocked'
    assert result['recovery_phase'] == 'idle'
    assert result['admission_reason'] == 'controls_disabled'


async def test_diagnostics_rejects_unbounded_or_wrong_types(hass):
    from custom_components.petsafe_freshfeed.diagnostics import async_get_config_entry_diagnostics
    hub = SimpleNamespace(snapshot=SimpleNamespace(available='private'),
                          schedule=SimpleNamespace(source_state='private', entries=list(range(100))),
                          _controls_reason='private')
    entry = SimpleNamespace(data={'connection_backend': 'private', 'protocol_version': 'private'}, runtime_data=hub)
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert 'private' not in json.dumps(result)
    assert result['connected'] is None and result['schedule_count'] is None
    assert result['storage_fault'] is None and result['integrity_fault'] is None
