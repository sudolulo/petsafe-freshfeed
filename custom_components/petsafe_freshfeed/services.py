"""Strict local services and pure actual-report operation predicates.

Observed acknowledgements describe device status, never physical success.
Schedule admission compares the last pushed revision; an external writer's
approximately one-second push latency remains a race, not a conditional write.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.exceptions import ServiceValidationError

from .const import (
    BRIGHTNESS_MAX, BRIGHTNESS_MIN, DOMAIN, FEED_CANCEL_COMMAND, SETTING_BOOL_DPS,
    SETTING_BRIGHTNESS_DP, WRITE_OPERATION_DPS,
)
from .quick_feed import decode_quick_feed
from .schedule_codec import MealEntry, ScheduleCodecError, decode_base64, encode_base64

PUBLIC_REASONS = frozenset('invalid_request confirmation_required invalid_operation admin_required unknown_entry source_unavailable recovery_rejected stopped storage_fault integrity_fault busy recovery_in_progress intent_uncertain source_incompatible schedule_unknown stale_schedule quick_feed_active defrost_active feeding_active status_unavailable cancel_not_pending empty_tray already_refilled controls_disabled source_not_ready schedule_conflict'.split())


def friendly_reason(reason: str) -> str:
    """Fixed public copy only; never expose an adapter or transport value."""
    if reason not in PUBLIC_REASONS:
        return 'The request could not be confirmed. Check device and write status before trying again.'
    if reason == 'admin_required':
        return 'Use an administrator account.'
    if reason in {'stale_schedule', 'schedule_conflict'}:
        return 'Reload the schedule before saving again.'
    if reason in {'storage_fault', 'integrity_fault'}:
        return 'Check storage and write status; do not delete retained intent.'
    if reason in {'intent_uncertain', 'recovery_in_progress', 'recovery_rejected'}:
        return 'Inspect the feeder and use the existing recovery controls.'
    return 'Check the feeder connection, controls and write status before trying again.'


def service_error(reason: str) -> "ServiceValidationError":
    """Translate public refusal codes without retaining arbitrary error data."""
    from homeassistant.exceptions import ServiceValidationError
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=reason if reason in PUBLIC_REASONS else 'request_refused')


_SAFE_STATUS = frozenset(('standby', 'done', 'last_meal'))
_DEFROST_STATUS = frozenset(('defrost', 'defrosting'))
_COMMANDS = {'feed': 'EA==', 'cancel': FEED_CANCEL_COMMAND, 'defrost': 1, 'refilled': True}


def _counter(value):
    return type(value) is int and 1 <= value <= 6


def setting_value_ok(dp, value):
    """Exact value domain of one writable setting DP; no coercion."""
    if dp in SETTING_BOOL_DPS:
        return type(value) is bool
    if dp == SETTING_BRIGHTNESS_DP:
        return type(value) is int and BRIGHTNESS_MIN <= value <= BRIGHTNESS_MAX
    return False


def setting_request(dps):
    """The single (dp, value) of a valid settings request, else None."""
    if type(dps) is not dict or len(dps) != 1:
        return None
    (dp, value), = dps.items()
    if type(dp) is not str or not setting_value_ok(dp, value):
        return None
    return dp, value


def unsafe_reason(dps):
    """Known unsafe evidence only; missing evidence does not bar collection."""
    if decode_quick_feed(dps.get('140')).status in ('pending', 'feeding'):
        return 'quick_feed_active'
    if dps.get('4') in _DEFROST_STATUS or (type(dps.get('142')) is int and dps['142'] > 0):
        return 'defrost_active'
    if dps.get('4') == 'feeding':
        return 'feeding_active'
    return None


def operation_reason(operation, dps):
    if operation == 'schedule':
        return None
    if operation == 'cancel':
        status = decode_quick_feed(dps.get('140')).status
        if status == 'feeding':
            return 'quick_feed_active'
        if dps.get('4') in _DEFROST_STATUS or (type(dps.get('142')) is int and dps['142'] > 0):
            return 'defrost_active'
        if dps.get('4') == 'feeding':
            return 'feeding_active'
        if (dps.get('4') not in _SAFE_STATUS or not _counter(dps.get('145'))
                or status is None or type(dps.get('142')) is not int or dps['142'] != 0):
            return 'status_unavailable'
        return None if status == 'pending' else 'cancel_not_pending'
    unsafe = unsafe_reason(dps)
    if unsafe:
        return unsafe
    if (dps.get('4') not in _SAFE_STATUS or not _counter(dps.get('145'))
            or decode_quick_feed(dps.get('140')).status is None
            or type(dps.get('142')) is not int or dps['142'] != 0):
        return 'status_unavailable'
    if operation == 'feed' and dps['145'] == 6:
        return 'empty_tray'
    if operation == 'refilled' and dps['145'] == 1:
        return 'already_refilled'
    return None


def acknowledgement(context, mark, report):
    if (context.operation_id != mark.operation_id or context.epoch != mark.epoch
            or report.epoch != mark.epoch or report.sequence <= mark.sequence):
        return 'ignore'
    dps = report.dps
    if context.operation == 'schedule':
        if '1' in dps and dps['1'] == context.dps.get('1'):
            try:
                decode_base64(dps['1'])
            except (ScheduleCodecError, TypeError):
                return 'ignore'
            return 'observed'
    elif context.operation == 'feed':
        status = decode_quick_feed(dps.get('140')).status
        if status == 'failed':
            return 'observed_failure'
        if status in ('pending', 'feeding', 'ready'):
            return 'observed'
    elif context.operation == 'cancel':
        quick = decode_quick_feed(dps.get('140'))
        if quick.status == 'failed':
            return 'observed_failure'
        if quick.command == 'cancel' and quick.status == 'idle':
            return 'observed'
    elif context.operation == 'defrost':
        if (type(dps.get('142')) is int and dps['142'] > 0) or dps.get('4') in _DEFROST_STATUS:
            return 'observed'
    elif context.operation == 'refilled':
        if type(dps.get('145')) is int and dps['145'] == 1:
            return 'observed'
    elif context.operation == 'settings':
        # Same DP, same exact type and value (False is not 0, 4 is not True).
        request = setting_request(context.dps)
        if request is not None and request[0] in dps:
            value = dps[request[0]]
            if type(value) is type(request[1]) and value == request[1]:
                return 'observed'
    return 'ignore'


def recovery_allowed(context, evidence):
    if context != evidence.context or not evidence.reports:
        return False
    dps = {}
    sequence = evidence.start_sequence
    for report in evidence.reports:
        if report.epoch != context.epoch or report.sequence <= sequence:
            return False
        sequence = report.sequence
        dps.update(report.dps)
    if sequence != evidence.end_sequence or unsafe_reason(dps):
        return False
    if dps.get('4') not in _SAFE_STATUS or not _counter(dps.get('145')):
        return False
    if ('140' in dps and decode_quick_feed(dps['140']).status is None
            or '142' in dps and (type(dps['142']) is not int or dps['142'] != 0)):
        return False
    if context.operation == 'schedule':
        try:
            decode_base64(dps.get('1'))
        except (ScheduleCodecError, TypeError):
            return False
        return True
    if context.operation == 'feed':
        return decode_quick_feed(dps.get('140')).status in ('idle', 'ready', 'failed')
    if context.operation == 'cancel':
        return (type(dps.get('142')) is int and dps['142'] == 0
                and decode_quick_feed(dps.get('140')).status == 'idle')
    if context.operation == 'defrost':
        return type(dps.get('142')) is int and dps['142'] == 0
    if context.operation == 'settings':
        if operation_reason('settings', dps) is not None:
            return False
        # A retained startup intent (no in-memory DP) needs only safe status;
        # a known request needs a post-request report of that setting DP.
        if not context.dps:
            return True
        request = setting_request(context.dps)
        return (request is not None and request[0] in dps
                and setting_value_ok(request[0], dps[request[0]]))
    return context.operation == 'refilled'


def _validate(service, data):
    """Fixed failures, exact JSON types, no coercion or partial vector writes."""
    fields = {'save_schedule': {'entry_id', 'entries', 'base_revision'},
              'command': {'entry_id', 'operation', 'confirmed'},
              'recover': {'entry_id', 'operation_id', 'session_id', 'confirmed'}}[service]
    if set(data) != fields or type(data.get('entry_id')) is not str:
        raise service_error('invalid_request')
    if service == 'save_schedule':
        if type(data['base_revision']) is not str or type(data['entries']) is not list or len(data['entries']) > 10:
            raise service_error('invalid_request')
        try:
            rows = []
            for row in data['entries']:
                if type(row) is not dict or set(row) != {'weekmask','hour','minute','enabled','audible'}:
                    raise ValueError
                rows.append(MealEntry(**row))
            return encode_base64(rows)
        except (ValueError, TypeError):
            raise service_error('invalid_request') from None
    if data['confirmed'] is not True:
        raise service_error('confirmation_required')
    if service == 'command' and (type(data['operation']) is not str or data['operation'] not in _COMMANDS):
        raise service_error('invalid_operation')
    if service == 'recover' and any(type(data[key]) is not str for key in ('operation_id', 'session_id')):
        raise service_error('invalid_request')
    return None


def async_register(hass, entry):
    """Register domain services once; route only to installed entry runtimes."""
    from homeassistant.core import SupportsResponse
    entries = hass.data.setdefault(DOMAIN, {}).setdefault('service_entries', {})
    entries[entry.entry_id] = entry

    async def handle(call):
        value = _validate(call.service, call.data)
        caller_id = call.context.user_id
        if not caller_id:
            raise service_error('admin_required')
        caller = await hass.auth.async_get_user(caller_id)
        if caller is None or not caller.is_admin:
            raise service_error('admin_required')
        target = entries.get(call.data['entry_id'])
        if target is None:
            raise service_error('unknown_entry')
        adapter = getattr(target.runtime_data, 'adapter', None)
        if adapter is None:
            raise service_error('source_unavailable')
        if call.service == 'save_schedule':
            result = await adapter.async_submit('schedule', {'1': value}, base_revision=call.data['base_revision'])
        elif call.service == 'command':
            operation = call.data['operation']
            result = await adapter.async_submit(operation, {WRITE_OPERATION_DPS[operation]: _COMMANDS[operation]}, confirmed=True)
        else:
            resolved = await adapter.async_recover(call.data['operation_id'], call.data['session_id'], confirmed=True)
            if not resolved:
                raise service_error('recovery_rejected')
            return {'resolved': True}
        if not result['admitted']:
            raise service_error(result['reason'])
        return result

    for service in ('save_schedule', 'command', 'recover'):
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handle, supports_response=SupportsResponse.OPTIONAL)


def async_unregister(hass, entry_id):
    entries = hass.data.get(DOMAIN, {}).get('service_entries', {})
    entries.pop(entry_id, None)
    if not entries:
        for service in ('save_schedule', 'command', 'recover'):
            hass.services.async_remove(DOMAIN, service)
