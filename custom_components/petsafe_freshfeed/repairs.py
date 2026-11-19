"""Read-only, per-entry Repairs issue lifecycle."""
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .const import BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_PROTOCOL_VERSION, DOMAIN

_KINDS = ("uncertain_intent", "offline", "unsupported_protocol", "cloud_auth")


def async_cloud_auth_issue(hass, entry, active):
    """Fixed notice; no account data enters the issue registry."""
    _set(hass, entry, "cloud_auth", active)


def _set(hass, entry, kind, active):
    issue_id = f"{entry.entry_id}_{kind}"
    if active:
        ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=False,
                              severity=ir.IssueSeverity.ERROR if kind == "unsupported_protocol" else ir.IssueSeverity.WARNING,
                              translation_key=kind, data={"config_entry_id": entry.entry_id}, is_persistent=True)
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


def async_reconcile_protocol_issue(hass, entry):
    data = entry.data
    unsupported = (data.get(CONF_CONNECTION_BACKEND) == BACKEND_OWNED
                   and CONF_PROTOCOL_VERSION in data
                   and data[CONF_PROTOCOL_VERSION] != "3.5")
    _set(hass, entry, "unsupported_protocol", unsupported)


def async_clear_offline_issue(hass, entry):
    _set(hass, entry, "offline", False)


def async_remove_entry_issues(hass, entry_id):
    for kind in _KINDS:
        ir.async_delete_issue(hass, DOMAIN, f"{entry_id}_{kind}")


class FreshFeedRepairs:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self._cancel = None
        self._offline = False
        # Offline duration cannot be carried across an unloaded/restarted hub.
        async_clear_offline_issue(hass, entry)

    def update(self, *, connected, write_view, unsupported_protocol=False):
        if unsupported_protocol:
            _set(self.hass, self.entry, "unsupported_protocol", True)
        state = write_view.get("state")
        phase = write_view.get("recovery_phase")
        if state == "uncertain" or (phase in ("awaiting_user_confirmation", "failed") and write_view.get("operation")):
            _set(self.hass, self.entry, "uncertain_intent", True)
        elif connected and state in ("idle", "verified") and phase in (None, "idle", "resolved"):
            _set(self.hass, self.entry, "uncertain_intent", False)
        if connected:
            if self._cancel:
                self._cancel()
                self._cancel = None
            self._offline = False
            _set(self.hass, self.entry, "offline", False)
        elif not self._offline and self._cancel is None:
            self._cancel = async_call_later(self.hass, timedelta(minutes=15), self._deadline)

    @callback
    def _deadline(self, _now):
        self._cancel = None
        self._offline = True
        _set(self.hass, self.entry, "offline", True)

    def stop(self):
        if self._cancel:
            self._cancel()
            self._cancel = None
        async_clear_offline_issue(self.hass, self.entry)
