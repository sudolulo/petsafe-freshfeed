"""Backend-neutral hub: the owned source observes directly, without raw entities.

Standalone tests never import Tuya Local: the owned source runs over the
pinned TinyTuya client against an injected 3.5 fake device (no network, no
physical feeder), and a meta-path blocker refuses any Tuya Local import.
Legacy fixtures appear only in ``test_legacy_backend_default_unchanged``.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.petsafe_freshfeed import hub as hub_module
from custom_components.petsafe_freshfeed.const import (
    BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_DEVICE_ID, CONF_HOST,
    CONF_INTENT_BINDING_ID, CONF_INTENT_STORE_ID, CONF_LOCAL_KEY, CONF_MIGRATED_FROM,
    CONF_PROTOCOL_VERSION, CONF_REGISTRY_DEVICE_ID,
    CONF_SOURCE_ENTRY_ID, DOMAIN,
)
from custom_components.petsafe_freshfeed.owned_source import OwnedSource
from custom_components.petsafe_freshfeed.sensor import SENSORS
from test_owned_source import (  # noqa: F401 - pytest fixtures by name
    BASE_DPS, DEVICE, FakeDevice, HOST, KEY, READ_ONLY_CMDS, no_tuya_local, pinned,
)

SCHEDULE = "fwgAAQE="
OTHER_SCHEDULE = "fwkAAQE="
HUB_DPS = {**BASE_DPS, "1": SCHEDULE, "140": "EAAA", "145": 2, "14": 9}


def test_constant_names():
    assert (CONF_CONNECTION_BACKEND, BACKEND_OWNED) == (
        "connection_backend", "owned")
    assert (CONF_DEVICE_ID, CONF_LOCAL_KEY, CONF_HOST, CONF_PROTOCOL_VERSION) == (
        "device_id", "local_key", "host", "protocol_version")
    assert (CONF_REGISTRY_DEVICE_ID, CONF_MIGRATED_FROM) == (
        "registry_device_id", "migrated_from")


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class FastSource(OwnedSource):
    IDLE = 0.005
    BACKOFF = 0.05


def owned_data(**extra):
    return {CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE, CONF_HOST: HOST,
            CONF_LOCAL_KEY: KEY, CONF_PROTOCOL_VERSION: "3.5", **extra}


@pytest.fixture
async def owned(hass, pinned, monkeypatch, tmp_path):
    """Owned hub context: fake 3.5 device, private config root, recorded sources."""
    hass.config.config_dir = str(tmp_path)
    root = Path(hass.config.path(".storage"))
    root.mkdir(mode=0o700)
    ctx = SimpleNamespace(hass=hass, root=str(root), entries=[], sources=[], tinytuya=pinned)
    ctx.device = FakeDevice(pinned, dps=HUB_DPS).install(monkeypatch)

    class Recorded(FastSource):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ctx.sources.append(self)
    monkeypatch.setattr(hub_module, "OwnedSource", Recorded)
    yield ctx
    for entry in reversed(ctx.entries):
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
        elif getattr(entry, "runtime_data", None) is not None:
            assert await entry.runtime_data.async_stop()
    for source in ctx.sources:
        assert await source.async_stop() is True


def make_entry(ctx, data, **kwargs):
    entry = MockConfigEntry(domain=DOMAIN, data=data, **kwargs)
    entry.add_to_hass(ctx.hass)
    entry.runtime_data = None
    ctx.entries.append(entry)
    return entry


async def setup(ctx, data, *, expect=True, **kwargs):
    entry = make_entry(ctx, data, **kwargs)
    assert await ctx.hass.config_entries.async_setup(entry.entry_id) is expect
    await ctx.hass.async_block_till_done()
    return entry


def leaf_for(ctx, entry, identifiers=None):
    return dr.async_get(ctx.hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers=identifiers or {(DOMAIN, DEVICE)})


async def provision(ctx, leaf_id, *, retained=None, operation="feed"):
    from custom_components.petsafe_freshfeed.store_bootstrap import async_provision_store, open_store
    store_id = uuid4().hex
    await async_provision_store(ctx.root, binding_id=leaf_id, store_id=store_id)
    if retained is not None:
        await open_store(ctx.root, binding_id=leaf_id, store_id=store_id).async_prepare_intent(
            {"operation_id": retained, "operation": operation, "state": "pending"})
    return store_id


def petsafe_entities(ctx, entry):
    return er.async_entries_for_config_entry(er.async_get(ctx.hass), entry.entry_id)


def tuya_local_imported():
    return any(name == "custom_components.tuya_local" or name.startswith("custom_components.tuya_local.")
               for name in sys.modules)


def capture(hub):
    reports = []
    hub.bridge.subscribe(reports.append, lambda *args: None)
    return reports


# --------------------------------------------------------------------------


async def test_owned_hub_without_raw_entities(owned):
    ctx = owned
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    assert not tuya_local_imported() and "tuya_local" not in ctx.hass.data
    assert ctx.hass.config_entries.async_entries("tuya_local") == []
    assert not hasattr(hub, 'raw_entity_ids')
    assert hub.schedule_entity_id is None and hub.quick_feed_entity_id is None
    # One PetSafe-owned leaf, created under this entry before platforms.
    devices = dr.async_entries_for_config_entry(dr.async_get(ctx.hass), entry.entry_id)
    assert [device.id for device in devices] == [hub.device_id]
    assert devices[0].identifiers == {(DOMAIN, DEVICE)}
    assert hub.device_identifiers == {(DOMAIN, DEVICE)}
    # Direct observation from the admission status report.
    assert hub.snapshot.available and hub.snapshot.meals_left == 4
    assert hub.snapshot.status == "standby" and hub.snapshot.feed_code == 9
    assert hub.snapshot.firmware_a == "2.5.18" and hub.snapshot.brightness == 10
    assert hub.schedule.source_state == "current" and hub.schedule.entries is not None
    assert hub.quick_feed.command == "start"
    assert hub.events == [] and hub.last_fed is None
    assert hub.last_report is not None
    # PetSafe entry-based unique IDs on the same leaf; no raw entity anywhere.
    entities = petsafe_entities(ctx, entry)
    assert {e.unique_id for e in entities} >= {f"{entry.entry_id}_{key}" for key in SENSORS}
    assert {e.device_id for e in entities} == {hub.device_id}
    assert all(e.platform == DOMAIN for e in er.async_get(ctx.hass).entities.values())
    schedule = ctx.hass.states.get(next(e.entity_id for e in entities
                                        if e.unique_id == f"{entry.entry_id}_schedule"))
    assert schedule.attributes["source_entity_id"] is None
    assert schedule.attributes["source_state"] == "current"
    # Read-only: no witness provisioned, no controls, no CONTROL frame.
    assert not (Path(ctx.root) / "petsafe_freshfeed").exists()
    assert hub.witness is None and hub.adapter is None
    assert set(ctx.device.commands) <= READ_ONLY_CMDS and ctx.device.control_frames() == []
    assert len(ctx.sources) == 1 and ctx.device.max_active <= 1


async def test_read_only_connection_and_reconnect_demand_missing_dp140(owned, monkeypatch):
    from custom_components.petsafe_freshfeed import cloud_read
    from custom_components.petsafe_freshfeed.account import DPRead
    from custom_components.petsafe_freshfeed.quick_feed import decode_quick_feed

    ctx = owned
    ctx.device.dps = {k: v for k, v in HUB_DPS.items() if k != "140"}
    attempts = []
    budget = {}

    async def fetch():
        attempts.append(True)
        return DPRead("ok", None, decode_quick_feed("IAEA"), True)

    monkeypatch.setattr(cloud_read, "create_entry_reader",
                        lambda hass, entry: cloud_read.CloudRead(fetch, budget=budget))
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    await until(lambda: len(attempts) == 1)
    assert hub.adapter is None
    assert hub.cloud_read.evidence("140", sequences=hub._dp_sequences) is not None
    epoch = hub.bridge.epoch
    ctx.device.refuse = True
    ctx.device.sockets[-1].drop()
    await until(lambda: not hub.snapshot.available)
    budget.clear()  # Advance past the shared budget for the reconnect demand.
    ctx.device.refuse = False
    await until(lambda: hub.snapshot.available and hub.bridge.epoch > epoch and len(attempts) == 2)
    assert hub.cloud_read.evidence("140", sequences=hub._dp_sequences) is not None
    assert ctx.device.control_frames() == []


async def test_schedule_refresh_does_not_start_cloud_during_recovery(owned, monkeypatch):
    from custom_components.petsafe_freshfeed import cloud_read
    from custom_components.petsafe_freshfeed.account import DPRead

    ctx = owned
    ctx.device.dps = {k: v for k, v in HUB_DPS.items() if k != '1'}
    calls = []
    async def fetch():
        calls.append(True)
        return DPRead('unavailable')
    monkeypatch.setattr(cloud_read, 'create_entry_reader',
                        lambda hass, entry: cloud_read.CloudRead(fetch))
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    original = hub.adapter
    hub.adapter = SimpleNamespace(_submitting=None, _ack_wait=None, _collector=object())
    try:
        await hub._demand_cloud_schedule(hub.bridge.epoch, hub.bridge, hub._binding_generation)
        assert calls == []
        hub.adapter._collector = None
        await hub._demand_cloud_schedule(hub.bridge.epoch, hub.bridge, hub._binding_generation)
        assert calls == [True]
    finally:
        hub.adapter = original


async def test_owned_history_and_optional_reports(owned, hass_storage):
    ctx = owned
    ctx.device.dps = {k: v for k, v in HUB_DPS.items() if k not in {"1", "14", "140", "150"}}
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    # Optional DPs absent: observation stays available, optional reads unknown.
    assert hub.snapshot.available and hub.snapshot.feed_code is None
    assert hub.schedule.entries is None and hub.schedule.source_state == "unknown"
    assert hub.quick_feed.status is None
    reports = capture(hub)
    ctx.device.push_status({"145": 3})
    await until(lambda: hub.events)
    dispensed = [r for r in reports if r.dps.get("145") == 3][0]
    assert [e.to_dict() for e in hub.events] == [
        {"kind": "dispensed", "compartment": 3, "at": dispensed.at.isoformat()}]
    assert hub.last_fed == dispensed.at and hub.last_report == dispensed.at
    ctx.device.push_status({"145": 1})
    await until(lambda: len(hub.events) == 2)
    assert hub.events[-1].kind == "refilled"
    ctx.device.push_status({"1": OTHER_SCHEDULE})
    await until(lambda: hub.schedule.source_state == "current")
    schedule_report = [r for r in reports if "1" in r.dps][0]
    assert hub.schedule.observed_at == schedule_report.at
    ctx.device.push_status({"140": "IAEA"})
    await until(lambda: hub.quick_feed.command == "cancel")
    latest = reports[-1].at
    assert hub.last_report == latest
    # A time tick is not a report: freshness never advances without one.
    async_fire_time_changed(ctx.hass, dt_util.utcnow() + timedelta(seconds=61))
    await ctx.hass.async_block_till_done()
    assert hub.last_report == latest
    assert await ctx.hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored == {"version": 3, "events": [e.to_dict() for e in hub.events],
                      "schedule": hub.schedule.to_dict(),
                      "observed_meals_dispensed_total": 1,
                      "counting_since": hub.counting_since.isoformat()}
    assert stored["schedule"]["value"] == OTHER_SCHEDULE
    assert ctx.device.control_frames() == []


async def test_owned_missing_required_dps_unavailable(owned):
    ctx = owned
    ctx.device.dps = {k: v for k, v in HUB_DPS.items() if k != "145"}
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    assert not hub.snapshot.available
    status = next(e.entity_id for e in petsafe_entities(ctx, entry)
                  if e.unique_id == f"{entry.entry_id}_status")
    assert ctx.hass.states.get(status).state == "unavailable"
    ctx.device.push_status({"145": 3})
    await until(lambda: hub.snapshot.available)
    await ctx.hass.async_block_till_done()
    assert ctx.hass.states.get(status).state == "standby"
    assert hub.events == []  # no transition from an absent value
    assert ctx.device.control_frames() == []


async def test_owned_no_event_across_disconnect(owned):
    ctx = owned
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    source = hub.bridge
    epoch = source.epoch
    ctx.device.refuse = True
    ctx.device.sockets[-1].drop()
    # Disconnected values are unavailable, never last-known freshness.
    await until(lambda: not hub.snapshot.available)
    assert hub.schedule.source_state == "last_known"
    assert hub.quick_feed.status is None
    ctx.device.dps.update({"145": 3, "14": 1, "150": 1})
    ctx.device.refuse = False
    await until(lambda: hub.snapshot.available and source.epoch > epoch)
    assert hub.snapshot.meals_left == 3
    # The first observation of a new connection seeds; it is never a transition.
    assert hub.events == [] and hub.last_fed is None
    assert hub.observed_meals_dispensed_total == 0
    ctx.device.push_status({"145": 4})
    await until(lambda: hub.events)
    assert [e.kind for e in hub.events] == ["dispensed"]
    assert hub.observed_meals_dispensed_total == 1
    assert hub.bridge is source and len(ctx.sources) == 1
    assert ctx.device.control_frames() == []


async def test_owned_dp14_both_classifiers(owned):
    ctx = owned
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    seen = []
    hub.async_add_event_listener(seen.append)
    for value, count in ((1, 1), (1, 1), (9, 2), (10, 3), (150, 4)):
        if value == 150:
            ctx.device.push_status({"150": 1})
        else:
            ctx.device.push_status({"14": value})
        await until(lambda count=count: len(hub.events) >= count)
        await asyncio.sleep(0.05)
    # Report classifier (DP14 meal report, DP150 interrupt) and state classifier
    # (DP14 skip codes) each record exactly once; a repeated value records nothing.
    assert [(e.kind, e.source_dp, e.report_value) for e in hub.events] == [
        ("meal_report", 14, 1), ("skipped_empty", None, None),
        ("skipped_fault", None, None), ("interrupted", 150, 1)]
    assert seen == hub.events
    assert ctx.device.control_frames() == []


async def test_owned_detach_refusal_keeps_group_parked(owned, monkeypatch):
    ctx = owned
    entry = make_entry(ctx, owned_data())
    leaf = leaf_for(ctx, entry)
    store_id = await provision(ctx, leaf.id)
    ctx.hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: leaf.id})
    assert await ctx.hass.config_entries.async_setup(entry.entry_id)
    hub = entry.runtime_data
    await until(lambda: hub.adapter is not None)
    old_adapter, old_source = hub.adapter, hub.bridge
    real_detach = old_source.async_detach
    allow = asyncio.Event()
    attempts = []

    async def refuse_until_released():
        attempts.append(True)
        if not allow.is_set():
            return False
        return await real_detach()

    monkeypatch.setattr(old_source, "async_detach", refuse_until_released)
    assert not await ctx.hass.config_entries.async_unload(entry.entry_id)
    await asyncio.sleep(0.2)
    # Never pop a live source and never start a second one meanwhile.
    assert attempts and hub._parked and hub._parked[0][0] is old_source
    assert len(ctx.sources) == 1 and hub.bridge is None
    allow.set()
    await until(lambda: hub.adapter is not None and hub.adapter is not old_adapter, timeout=10)
    assert not hub._parked and len(ctx.sources) == 2 and hub.bridge is ctx.sources[1]
    assert old_source._state == "stopped"
    assert ctx.device.control_frames() == []
    assert await hub.async_stop()


async def test_owned_retained_intent_requires_explicit_recovery(owned):
    ctx = owned
    from custom_components.petsafe_freshfeed.store_bootstrap import open_store
    entry = make_entry(ctx, owned_data())
    leaf = leaf_for(ctx, entry)
    operation_id = uuid4().hex
    store_id = await provision(ctx, leaf.id, retained=operation_id)
    ctx.hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: leaf.id})
    assert await ctx.hass.config_entries.async_setup(entry.entry_id)
    hub = entry.runtime_data
    await until(lambda: hub.adapter is not None)
    view = hub.adapter.view
    assert view["state"] == "uncertain" and view["operation_id"] == operation_id
    # Cached/current state that looks settled never auto-releases the intent.
    ctx.device.push_status({"4": "standby", "145": 3, "140": "EAAA", "142": 0})
    await until(lambda: hub.snapshot.meals_left == 3)
    await asyncio.sleep(0.1)
    assert hub.adapter.view["state"] == "uncertain"
    assert hub.adapter.view["recovery_phase"] == "awaiting_user_confirmation"
    record = await open_store(ctx.root, binding_id=leaf.id,
                              store_id=store_id).async_load_last_intent()
    assert record["operation_id"] == operation_id
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


async def test_statistic_store_never_changes_witness(owned, hass_storage):
    ctx = owned
    entry = make_entry(ctx, owned_data())
    leaf = leaf_for(ctx, entry)
    store_id = await provision(ctx, leaf.id, retained=uuid4().hex)
    ctx.hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: leaf.id})
    assert await ctx.hass.config_entries.async_setup(entry.entry_id)
    hub = entry.runtime_data
    await until(lambda: hub.adapter is not None)
    def witness_bytes():
        return {str(path.relative_to(ctx.root)): path.read_bytes()
                for path in Path(ctx.root).rglob('*') if path.is_file()}
    before = witness_bytes()
    assert before
    ctx.device.push_status({'145': 3})
    await until(lambda: hub.observed_meals_dispensed_total == 1)
    ctx.device.push_status({'145': 3, '14': 1})
    await until(lambda: any(e.kind == 'meal_report' for e in hub.events))
    assert hub.observed_meals_dispensed_total == 1
    assert await ctx.hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[f'{DOMAIN}.{entry.entry_id}']['data']['observed_meals_dispensed_total'] == 1
    assert witness_bytes() == before
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


async def test_migrated_entities_no_second_device(owned, monkeypatch):
    ctx = owned
    hass = ctx.hass
    source = MockConfigEntry(domain="tuya_local", entry_id="retired_tuya_source",
                             data={"device_id": DEVICE},
                             disabled_by=ConfigEntryDisabler.USER)
    source.add_to_hass(hass)
    entry = make_entry(ctx, {CONF_SOURCE_ENTRY_ID: source.entry_id})
    registry = dr.async_get(hass)
    leaf = registry.async_get_or_create(config_entry_id=source.entry_id,
                                        identifiers={("tuya_local", DEVICE)})
    # Existing PetSafe entities on the borrowed leaf, then the leaf moves.
    entity_registry = er.async_get(hass)
    before = {key: entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry, device_id=leaf.id).entity_id
        for key in SENSORS}
    registry.async_update_device(leaf.id, new_config_entry_id=entry.entry_id)
    migrated = owned_data(**{CONF_SOURCE_ENTRY_ID: source.entry_id,
                             CONF_REGISTRY_DEVICE_ID: leaf.id,
                             CONF_MIGRATED_FROM: source.entry_id})
    hass.config_entries.async_update_entry(entry, data=migrated)

    # Historical provenance is inert: no source/journal admission is required.
    assert await hass.config_entries.async_setup(entry.entry_id)
    hub = entry.runtime_data
    assert hub.device_id == leaf.id
    assert hub.device_identifiers == {("tuya_local", DEVICE)}
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    assert [device.id for device in devices] == [leaf.id]
    assert {key: entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}").entity_id for key in SENSORS} == before
    assert {e.device_id for e in petsafe_entities(ctx, entry)} == {leaf.id}
    assert hub.snapshot.available and len(ctx.sources) == 1
    assert ctx.device.control_frames() == []


@pytest.mark.parametrize("enabled_at_start", [True, False])
async def test_owned_fences_former_source_for_different_device(owned, monkeypatch, enabled_at_start):
    ctx = owned
    source = MockConfigEntry(
        domain="tuya_local", entry_id="former_source",
        data={"device_id": "different_device"},
        disabled_by=None if enabled_at_start else ConfigEntryDisabler.USER,
    )
    source.add_to_hass(ctx.hass)
    entry = make_entry(ctx, {})
    leaf = leaf_for(ctx, entry)
    store_id = await provision(ctx, leaf.id)
    ctx.hass.config_entries.async_update_entry(entry, data=owned_data(**{
        CONF_SOURCE_ENTRY_ID: source.entry_id, CONF_REGISTRY_DEVICE_ID: leaf.id,
        CONF_INTENT_BINDING_ID: leaf.id, CONF_INTENT_STORE_ID: store_id,
    }))
    assert await ctx.hass.config_entries.async_setup(entry.entry_id) is (not enabled_at_start)
    await ctx.hass.async_block_till_done()
    if enabled_at_start:
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert entry.reason == hub_module.SOURCE_CONFLICT
        assert ctx.sources == []
    else:
        hub = entry.runtime_data
        assert hub.snapshot.available and len(ctx.sources) == 1
        assert not hub._source_conflict()
        await until(lambda: hub.adapter is not None)
        adapter = hub.adapter
        monkeypatch.setattr(source, "disabled_by", None)
        source._async_set_state(ctx.hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
        # Submit immediately, without running the one-second periodic check.
        assert not hub._binding_valid
        result = await adapter.async_submit("feed", {"140": "EA=="}, confirmed=True)
        assert result["admitted"] is False
        await ctx.hass.async_block_till_done()
        assert not hub._binding_valid
        assert hub.bridge is None
    assert ctx.device.control_frames() == []


async def test_owned_missing_former_source_does_not_block_setup(owned):
    ctx = owned
    entry = await setup(ctx, owned_data(**{CONF_SOURCE_ENTRY_ID: "absent_source"}))
    assert ctx.hass.config_entries.async_get_entry("absent_source") is None
    assert not entry.runtime_data._source_conflict()
    assert entry.runtime_data.snapshot.available and len(ctx.sources) == 1
    assert ctx.device.control_frames() == []


@pytest.mark.parametrize("location", ["data", "options"])
async def test_owned_fences_actual_device_conflict_without_provenance(owned, location):
    ctx = owned
    entry = await setup(ctx, owned_data())
    hub = entry.runtime_data
    source = MockConfigEntry(domain="tuya_local", **{location: {"device_id": DEVICE}})
    source.add_to_hass(ctx.hass)
    assert hub._source_conflict()
    hub._check_owned()
    await ctx.hass.async_block_till_done()
    assert not hub._binding_valid
    assert hub.bridge is None
    assert ctx.device.control_frames() == []


@pytest.mark.parametrize("case", ["wrong_leaf", "other_owner", "foreign_identifier",
                                  "binding_mismatch", "source_enabled", "bad_backend"])
async def test_owned_leaf_and_conflict_admission(owned, monkeypatch, case):
    ctx = owned
    hass = ctx.hass

    registry = dr.async_get(hass)
    source = MockConfigEntry(domain="tuya_local", entry_id="legacy_source", data={"device_id": DEVICE},
                             disabled_by=None if case == "source_enabled" else ConfigEntryDisabler.USER)
    source.add_to_hass(hass)
    entry = make_entry(ctx, {})
    data = owned_data(**{CONF_SOURCE_ENTRY_ID: source.entry_id})
    if case == "wrong_leaf":
        leaf_for(ctx, entry)
        data[CONF_REGISTRY_DEVICE_ID] = "not-a-device"
    elif case == "other_owner":
        data[CONF_REGISTRY_DEVICE_ID] = registry.async_get_or_create(
            config_entry_id=source.entry_id, identifiers={("tuya_local", DEVICE)}).id
    elif case == "foreign_identifier":
        data[CONF_REGISTRY_DEVICE_ID] = leaf_for(ctx, entry, {(DOMAIN, "another_device")}).id
    elif case == "binding_mismatch":
        leaf = leaf_for(ctx, entry)
        data.update({CONF_REGISTRY_DEVICE_ID: leaf.id, CONF_INTENT_STORE_ID: uuid4().hex,
                     CONF_INTENT_BINDING_ID: "another_leaf"})
    elif case == "bad_backend":
        data[CONF_CONNECTION_BACKEND] = "automatic"
    count = len(registry.devices)
    hass.config_entries.async_update_entry(entry, data=data)
    if case == "binding_mismatch":
        # Observation loads; controls stay fenced against the wrong binding.
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.runtime_data.write_view["reason"] == "binding_mismatch"
        assert entry.runtime_data.witness is None
    else:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        expected = ConfigEntryState.SETUP_ERROR if case == "bad_backend" else ConfigEntryState.SETUP_RETRY
        assert entry.state is expected
        assert ctx.sources == [] and ctx.device.addresses == []
    assert len(registry.devices) == count  # never another leaf
    assert ctx.device.control_frames() == []


async def test_legacy_backend_never_constructs_hub(hass, source_entry, raw_entities, monkeypatch):
    from unittest.mock import Mock
    construct = Mock(side_effect=AssertionError("legacy hub constructed"))
    monkeypatch.setattr(hub_module, "FreshFeedHub", construct)
    for data in ({CONF_SOURCE_ENTRY_ID: source_entry.entry_id},
                 {CONF_SOURCE_ENTRY_ID: source_entry.entry_id,
                  CONF_CONNECTION_BACKEND: "legacy"}):
        entry = MockConfigEntry(domain=DOMAIN, data=data)
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) == []
        construct.assert_not_called()


async def test_hub_reports_dp140_seen_this_epoch_for_adapter_seeding(owned):
    from test_hub import owned_hub, report
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        epoch = hub.bridge.epoch
        assert '140' not in hub.reported_dps(epoch)
        await report(owned, hub, {'140': 'EAEA'})
        value, sequence = hub.reported_dps(epoch)['140']
        assert value == 'EAEA' and sequence == hub._dp_sequences['140']
        assert hub.reported_dps(epoch + 1) == {}
    finally:
        await hub.async_stop()


async def test_refused_second_shutdown_drain_resumes_cloud_reader(owned, monkeypatch):
    """A refused async_stop must leave cloud reads working after resume."""
    from test_hub import owned_hub
    from custom_components.petsafe_freshfeed.cloud_read import CloudRead
    hub = owned_hub(owned)
    calls, completed = [], []

    async def fetch():
        calls.append(True)
        return SimpleNamespace(outcome='ok', dp1=None, dp140='EAAA', dp140_present=True)

    now = [0.0]
    hub.cloud_read = CloudRead(fetch, clock=lambda: now[0])
    hub.cloud_read.subscribe(completed.append)
    await hub.async_start()
    real_drain = hub._async_drain
    drains = []

    async def drain():
        drains.append(True)
        # The first (controls) drain succeeds; the second shutdown drain is refused.
        return await real_drain() if len(drains) == 1 else False

    monkeypatch.setattr(hub, '_async_drain', drain)
    try:
        assert await hub.async_stop() is False
        hub.resume_after_failed_unload()
        monkeypatch.setattr(hub, '_async_drain', real_drain)
        now[0] = 120.0
        await hub.cloud_read.demand(epoch=7, sequences={})
        assert calls == [True] and completed == [7]
        assert hub.cloud_read.evidence('140').value == 'EAAA'
    finally:
        await hub.async_stop()
    now[0] = 240.0
    await hub.cloud_read.demand(epoch=8, sequences={})
    assert calls == [True]  # a successful stop closes the reader for good


@pytest.mark.parametrize('active', ['EAEA', 'EAIA', 'invalid'])
async def test_hub_reports_latest_same_epoch_dp140_for_adapter_seeding(owned, active):
    """Only the latest local DP140 and sequence are seeded."""
    from test_hub import owned_hub, report
    hub = owned_hub(owned)
    await hub.async_start()
    try:
        epoch = hub.bridge.epoch
        await report(owned, hub, {'140': 'EAAA'})
        await report(owned, hub, {'140': active})
        await report(owned, hub, {'140': 'EAAA'})
        seed = hub.reported_dps(epoch)
        assert set(seed) == {'140'} and seed['140'][0] == 'EAAA'
        hub._connection_changed(epoch, False)
        assert hub.reported_dps(epoch) == {}
    finally:
        await hub.async_stop()
