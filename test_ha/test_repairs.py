"""Repairs lifecycle uses only synthetic observations and registry state."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.petsafe_freshfeed import repairs as module
from custom_components.petsafe_freshfeed.const import (
    DOMAIN, BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_PROTOCOL_VERSION,
)


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, entry_id="synthetic_entry", data={})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0
        timers = []

        def later(self, hass, delay, action):
            timer = [self.now + delay.total_seconds(), action, True]
            self.timers.append(timer)
            def cancel():
                timer[2] = False
            return cancel

        def advance(self, seconds):
            self.now += seconds
            for timer in self.timers[:]:
                if timer[2] and timer[0] <= self.now:
                    timer[2] = False
                    timer[1](None)

    clock = Clock()
    monkeypatch.setattr(module, "async_call_later", clock.later)
    return clock


def issue(hass, entry, kind):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_{kind}")


def test_retained_uncertainty_not_auto_cleared_while_collecting_or_offline(hass, entry, clock):
    repairs = module.FreshFeedRepairs(hass, entry)
    repairs.update(connected=True, write_view={"state": "uncertain", "operation": "private-operation"})
    original = issue(hass, entry, "uncertain_intent")
    repairs.update(connected=True, write_view={"state": "uncertain"})
    assert issue(hass, entry, "uncertain_intent") is original
    for connected, view in [(False, {"state": "idle"}),
                            (True, {"state": "idle", "recovery_phase": "collecting"}),
                            (True, {"state": "blocked", "recovery_phase": "failed", "operation": "private-operation"})]:
        repairs.update(connected=connected, write_view=view)
        assert issue(hass, entry, "uncertain_intent") is not None
    repairs.stop()
    repairs = module.FreshFeedRepairs(hass, entry)
    repairs.update(connected=True, write_view={"state": "blocked", "recovery_phase": "collecting"})
    assert issue(hass, entry, "uncertain_intent") is not None
    repairs.update(connected=True, write_view={"state": "idle", "recovery_phase": "resolved"})
    assert issue(hass, entry, "uncertain_intent") is None
    repairs.stop()


def test_offline_15_minute_grace_and_reconnect(hass, entry, clock):
    repairs = module.FreshFeedRepairs(hass, entry)
    repairs.update(connected=True, write_view={"state": "blocked", "reason": "controls_disabled"})
    clock.advance(1000)
    assert issue(hass, entry, "offline") is None
    repairs.update(connected=False, write_view={})
    clock.advance(899)
    repairs.update(connected=False, write_view={})
    assert issue(hass, entry, "offline") is None
    clock.advance(1)
    assert issue(hass, entry, "offline") is not None
    repairs.update(connected=True, write_view={})
    assert issue(hass, entry, "offline") is None
    repairs.update(connected=False, write_view={})
    clock.advance(899)
    repairs.update(connected=True, write_view={})
    clock.advance(1)
    assert issue(hass, entry, "offline") is None
    repairs.stop()


def test_restart_gets_fresh_offline_grace(hass, entry, clock):
    repairs = module.FreshFeedRepairs(hass, entry)
    repairs.update(connected=False, write_view={})
    clock.advance(900)
    assert issue(hass, entry, "offline") is not None
    repairs.stop()
    repairs = module.FreshFeedRepairs(hass, entry)
    repairs.update(connected=False, write_view={})
    assert issue(hass, entry, "offline") is None
    clock.advance(899)
    assert issue(hass, entry, "offline") is None
    clock.advance(1)
    assert issue(hass, entry, "offline") is not None
    repairs.stop()


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("existing", [True, False])
async def test_fenced_unsupported_entry_admission_first(hass, entry, monkeypatch, tmp_path, active, existing):
    from uuid import uuid4
    from custom_components.petsafe_freshfeed import async_setup_entry, hub, migration
    from custom_components.petsafe_freshfeed.migration_journal import MigrationJournal
    from custom_components.petsafe_freshfeed.migration_state import MigrationPhase, MigrationRecord

    hass.config.config_dir = str(tmp_path)
    (tmp_path / ".storage").mkdir()
    journal = MigrationJournal(str(tmp_path), entry.entry_id)
    record = MigrationRecord(entry_id=entry.entry_id, source_entry_id="synthetic_source",
        device_id="c" * 32, source_config_digest="d" * 64, source_disabled_by=None,
        device_disabled_by=None, entity_disabled_by=(), retiring=(),
        origin_marker=str(uuid4()), phase=MigrationPhase.PREPARED, revision=1)
    await journal.async_create(record)
    if active:
        hass.data.setdefault(DOMAIN, {}).setdefault(migration.MIGRATIONS_KEY, set()).add(entry.entry_id)
    hass.config_entries.async_update_entry(entry, data={CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_PROTOCOL_VERSION: "3.3"})
    kinds = ("uncertain_intent", "offline", "unsupported_protocol")
    if existing:
        for kind in kinds:
            module._set(hass, entry, kind, True)
    original = {kind: issue(hass, entry, kind) for kind in kinds}
    reconcile = Mock(wraps=module.async_reconcile_protocol_issue)
    monkeypatch.setattr(module, "async_reconcile_protocol_issue", reconcile)
    construct = Mock(side_effect=RuntimeError("constructed"))
    monkeypatch.setattr(hub, "FreshFeedHub", construct)
    with pytest.raises(ConfigEntryNotReady, match=hub.MIGRATION_PENDING):
        await async_setup_entry(hass, entry)
    assert await journal.async_load() == record
    reconcile.assert_not_called()
    construct.assert_not_called()
    assert all(issue(hass, entry, kind) is original[kind] for kind in kinds)
    hass.data.get(DOMAIN, {}).get(migration.MIGRATIONS_KEY, set()).discard(entry.entry_id)
    await journal.async_remove()
    def check_before_construction(*args):
        assert issue(hass, entry, "unsupported_protocol") is not None
        raise RuntimeError("constructed")
    construct.side_effect = check_before_construction
    with pytest.raises(RuntimeError, match="constructed"):
        await async_setup_entry(hass, entry)
    reconcile.assert_called_once()


async def test_failed_retiring_drain_preserves_protocol_issue(hass, entry, monkeypatch):
    from custom_components.petsafe_freshfeed import async_setup_entry, hub
    monkeypatch.setattr(hub, "async_migration_admits", AsyncMock(return_value=True))
    previous = SimpleNamespace(async_stop=AsyncMock(return_value=False))
    hass.data.setdefault(DOMAIN, {})["retiring"] = {entry.entry_id: previous}
    kinds = ("uncertain_intent", "offline", "unsupported_protocol")
    for kind in kinds:
        module._set(hass, entry, kind, True)
    original = {kind: issue(hass, entry, kind) for kind in kinds}
    reconcile = Mock()
    monkeypatch.setattr(module, "async_reconcile_protocol_issue", reconcile)
    construct = Mock()
    monkeypatch.setattr(hub, "FreshFeedHub", construct)
    with pytest.raises(ConfigEntryNotReady, match="previous_coordinator_draining"):
        await async_setup_entry(hass, entry)
    reconcile.assert_not_called()
    construct.assert_not_called()
    assert all(issue(hass, entry, kind) is original[kind] for kind in kinds)


async def test_unsupported_protocol_issue_before_hub_setup(hass, entry, monkeypatch):
    from custom_components.petsafe_freshfeed import async_setup_entry, hub
    monkeypatch.setattr(hub, "async_migration_admits", AsyncMock(return_value=True))
    source = Mock(side_effect=AssertionError("must not construct source"))
    monkeypatch.setattr(hub, "OwnedSource", source)
    hass.config_entries.async_update_entry(entry, data={CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_PROTOCOL_VERSION: "3.3"})
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert issue(hass, entry, "unsupported_protocol") is not None
    source.assert_not_called()
    hass.config_entries.async_update_entry(entry, data={CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_PROTOCOL_VERSION: "3.5"})
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert issue(hass, entry, "unsupported_protocol") is None
    # A fingerprint failure or legacy entry is not a protocol issue.
    hass.config_entries.async_update_entry(entry, data={"fingerprint": "incompatible", CONF_PROTOCOL_VERSION: "3.3"})
    module.async_reconcile_protocol_issue(hass, entry)
    assert issue(hass, entry, "unsupported_protocol") is None


@pytest.mark.parametrize("refusal", ["drain", "platforms", "stop"])
async def test_unload_refusal_keeps_timer_successful_unload_stops_timer(hass, entry, clock, monkeypatch, refusal):
    from custom_components.petsafe_freshfeed import async_setup_entry, async_unload_entry, hub
    listeners = []
    fake = SimpleNamespace(snapshot=SimpleNamespace(available=False), write_view={},
        async_start=AsyncMock(), async_stop=AsyncMock(return_value=True),
        async_drain_controls=AsyncMock(return_value=False), resume_after_failed_unload=Mock())
    def subscribe(listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)
    fake.async_add_listener = subscribe
    monkeypatch.setattr(hub, "FreshFeedHub", lambda *args: fake)
    monkeypatch.setattr(hub, "async_migration_admits", AsyncMock(return_value=True))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    unload = AsyncMock(return_value=refusal != "platforms")
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    assert await async_setup_entry(hass, entry)
    fake.async_drain_controls.return_value = refusal != "drain"
    fake.async_stop.return_value = refusal != "stop"
    assert not await async_unload_entry(hass, entry)
    assert len(listeners) == 1
    clock.advance(900)
    assert issue(hass, entry, "offline") is not None
    fake.async_drain_controls.return_value = True
    fake.async_stop.return_value = True
    unload.return_value = True
    assert await async_unload_entry(hass, entry)
    assert not listeners
    assert issue(hass, entry, "offline") is None
    clock.advance(900)
    assert issue(hass, entry, "offline") is None


@pytest.mark.parametrize("failure", ["admission", "start"])
async def test_restart_failed_setup_clears_stale_offline_issue(hass, entry, clock, monkeypatch, failure):
    from custom_components.petsafe_freshfeed import async_setup_entry, hub
    module._set(hass, entry, "offline", True)
    module._set(hass, entry, "uncertain_intent", True)
    monkeypatch.setattr(hub, "async_migration_admits", AsyncMock(return_value=failure != "admission"))
    fake = SimpleNamespace(async_start=AsyncMock(side_effect=ConfigEntryNotReady("start failed")),
                           async_stop=AsyncMock(return_value=True))
    monkeypatch.setattr(hub, "FreshFeedHub", lambda *args: fake)
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert (issue(hass, entry, "offline") is not None) == (failure == "admission")
    assert issue(hass, entry, "uncertain_intent") is not None
    assert not clock.timers


async def test_setup_failure_does_not_start_repairs_timer(hass, entry, clock, monkeypatch):
    from custom_components.petsafe_freshfeed import async_setup_entry, hub
    fake = SimpleNamespace(async_start=AsyncMock(), async_stop=AsyncMock(return_value=False),
                           async_add_listener=Mock())
    monkeypatch.setattr(hub, "FreshFeedHub", lambda *args: fake)
    monkeypatch.setattr(hub, "async_migration_admits", AsyncMock(return_value=True))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups",
                        AsyncMock(side_effect=RuntimeError("synthetic platform failure")))
    with pytest.raises(RuntimeError, match="synthetic platform failure"):
        await async_setup_entry(hass, entry)
    assert hass.data[DOMAIN]["retiring"][entry.entry_id] is fake
    fake.async_add_listener.assert_not_called()
    assert not clock.timers


async def test_delete_removes_entry_issues_only(hass, entry, clock, tmp_path):
    hass.config.config_dir = str(tmp_path)
    from custom_components.petsafe_freshfeed import async_remove_entry
    from custom_components.petsafe_freshfeed.store_bootstrap import async_provision_store
    from custom_components.petsafe_freshfeed.const import CONF_INTENT_STORE_ID, CONF_INTENT_BINDING_ID
    store_id = "a" * 32
    binding_id = "b" * 32
    descriptor = await async_provision_store(hass.config.config_dir, binding_id=binding_id, store_id=store_id)
    retained = Path(descriptor.path)
    before = retained.read_bytes()
    hass.config_entries.async_update_entry(entry, data={CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: binding_id})
    other = MockConfigEntry(domain=DOMAIN, entry_id="other", data={})
    for target in (entry, other):
        for kind in ("uncertain_intent", "offline", "unsupported_protocol"):
            module._set(hass, target, kind, True)
    await async_remove_entry(hass, entry)
    for kind in ("uncertain_intent", "offline", "unsupported_protocol"):
        assert issue(hass, entry, kind) is None
        assert issue(hass, other, kind) is not None
    assert retained.read_bytes() == before


async def test_offline_timer_runs_on_event_loop(hass, entry):
    from homeassistant.core import is_callback
    repairs = module.FreshFeedRepairs(hass, entry)
    # HA schedules plain synchronous jobs in the executor; registry access must
    # instead remain on the event loop, like every other hub notification.
    assert is_callback(repairs._deadline)
    repairs.stop()


def test_explicit_unsupported_protocol_notification(hass, entry, clock):
    repairs = module.FreshFeedRepairs(hass, entry)
    repairs.update(connected=False, write_view={}, unsupported_protocol=True)
    assert issue(hass, entry, "unsupported_protocol") is not None
    repairs.stop()
    assert issue(hass, entry, "unsupported_protocol") is not None


def test_fixed_issue_contract_and_privacy(hass, entry, clock):
    hass.config_entries.async_update_entry(entry, data={CONF_CONNECTION_BACKEND: BACKEND_OWNED,
        CONF_PROTOCOL_VERSION: "private-protocol", "host": "private-host", "nested": {"key": "private-key"}})
    module.async_reconcile_protocol_issue(hass, entry)
    repairs = module.FreshFeedRepairs(hass, entry)
    repairs.update(connected=False, write_view={"state": "uncertain", "operation": "private-operation"})
    clock.advance(900)
    translations = json.loads(Path("custom_components/petsafe_freshfeed/strings.json").read_text())
    titles = {"uncertain_intent": "Feeder confirmation required", "offline": "Feeder observations unavailable",
              "unsupported_protocol": "Unsupported local protocol"}
    for kind, title in titles.items():
        item = issue(hass, entry, kind)
        assert item.translation_key == kind
        assert item.is_fixable is False
        assert item.severity == (ir.IssueSeverity.ERROR if kind == "unsupported_protocol" else ir.IssueSeverity.WARNING)
        assert item.data == {"config_entry_id": entry.entry_id}
        assert not item.translation_placeholders
        assert translations["issues"][kind]["title"] == title
        assert "private-" not in json.dumps(translations["issues"][kind])
    repairs.stop()
