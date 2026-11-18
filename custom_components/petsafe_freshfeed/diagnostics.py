"""Privacy-first diagnostics; never collect configuration or device payloads."""
from homeassistant.const import __version__ as HA_VERSION

from .const import BACKEND_LEGACY, BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_PROTOCOL_VERSION

# Kept in sync with manifest.json when releasing the integration.
_COMPONENT_VERSION = "0.5.0"
_REASONS = frozenset(("controls_disabled", "source_not_ready", "source_incompatible",
                      "storage_fault", "integrity_fault", "migration_pending"))


def _choice(value, choices, fallback="unknown"):
    return value if type(value) is str and value in choices else fallback


def _bool(value):
    return value if type(value) is bool else None


def _fields(value):
    """Read stored fields only, never status/eligibility properties."""
    try:
        return vars(value)
    except (TypeError, AttributeError):
        return {}


def _write_status(fields, result):
    """Project stored adapter status, without computing command eligibility.

    Mirrors the status precedence in WriteAdapter.view/_phase_view, but never
    calls the bridge, reads retained intent, or promises current admission.
    Missing initialization fields leave the corresponding status unknown.
    """
    if "adapter" in fields and fields["adapter"] is None:
        result.update(write_state="blocked", recovery_phase="idle")
        return
    adapter = _fields(fields.get("adapter"))
    transport = _fields(adapter.get("transport"))
    storage = _bool(transport.get("_storage_fault"))
    integrity = _bool(transport.get("_integrity_fault"))
    result.update(storage_fault=storage, integrity_fault=integrity)
    reason = _choice(adapter.get("_reason"), _REASONS)
    if reason != "unknown":
        result["admission_reason"] = reason
    if storage is True or integrity is True:
        result["write_state"] = "blocked"
        result["admission_reason"] = "storage_fault" if storage else "integrity_fault"
    required = {"_submitting", "_ack_wait", "_current", "_stopped", "_result", "_phase"}
    blocked = _bool(transport.get("_blocked"))
    if not required <= adapter.keys() or blocked is None:
        return
    submitting = adapter["_submitting"] is not None
    waiting = adapter["_ack_wait"] is not None
    current = adapter["_current"] is not None
    if storage is False and integrity is False:
        if submitting or (waiting and current):
            result["write_state"] = "pending"
        elif blocked:
            result["write_state"] = "uncertain"
        elif adapter["_stopped"] is True:
            result["write_state"] = "blocked"
        elif adapter["_stopped"] is False:
            result["write_state"] = ("verified" if current and
                _choice(adapter["_result"], {"verified"}) == "verified" else "idle")
    phase = _choice(adapter["_phase"], {"idle", "collecting", "failed", "resolved"})
    if not blocked:
        result["recovery_phase"] = "resolved" if phase == "resolved" else "idle"
    elif submitting or waiting:
        result["recovery_phase"] = "idle"
    else:
        result["recovery_phase"] = (phase if phase in {"collecting", "failed"}
                                    else "awaiting_user_confirmation")


async def async_get_config_entry_diagnostics(hass, entry):
    """Return fixed, bounded status only; no refreshes or storage access."""
    data = entry.data
    backend = _choice(data.get(CONF_CONNECTION_BACKEND, BACKEND_LEGACY),
                      {BACKEND_LEGACY, BACKEND_OWNED})
    protocol = _choice(data.get(CONF_PROTOCOL_VERSION), {"3.5"}, None)
    result = {"component_version": _COMPONENT_VERSION, "ha_version": HA_VERSION,
              "backend": backend, "protocol_version": protocol, "connected": None,
              "admission_reason": "unknown", "schedule_source_state": "unknown",
              "schedule_count": None, "write_state": "unknown", "recovery_phase": "unknown",
              "storage_fault": None, "integrity_fault": None}
    hub = getattr(entry, "runtime_data", None)
    if hub is None:
        return result
    # Do not evaluate properties: write_view performs live eligibility checks
    # that can refresh connection state. A partial hub may also have properties
    # that raise. Only already-stored fields are safe to inspect here.
    fields = _fields(hub)
    snapshot = _fields(fields.get("snapshot"))
    schedule = _fields(fields.get("schedule"))
    result["connected"] = _bool(snapshot.get("available"))
    result["schedule_source_state"] = _choice(schedule.get("source_state"),
                                                {"unknown", "current", "last_known"})
    entries = schedule.get("entries")
    if type(entries) in (list, tuple) and len(entries) <= 10:
        result["schedule_count"] = len(entries)
    result["admission_reason"] = _choice(fields.get("_controls_reason"), _REASONS)
    _write_status(fields, result)
    return result
