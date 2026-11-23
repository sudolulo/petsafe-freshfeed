"""Literal synthetic 0.5.0 inventory, not a captured live registry snapshot.

Migration-journal admission is removed by Tasks 3-4, not this freeze. The
marker-preservation test bypasses that admission only while it still exists.
"""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock

import attr
import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.petsafe_freshfeed.const import DOMAIN
from custom_components.petsafe_freshfeed.sensor import SENSORS
from custom_components.petsafe_freshfeed.store_bootstrap import async_provision_store, open_store
from test_owned_hub import make_entry, owned, until  # noqa: F401
from test_owned_source import KEY, no_tuya_local, pinned  # noqa: F401


def inventory():
    return json.loads((Path(__file__).parent / "fixtures/owned_upgrade_050.json").read_text())


def test_owned_upgrade_inventory_matches_baseline_platforms():
    rows = inventory()["rows"]
    assert len(rows) == 27
    for column in (2, 3, 4):
        assert len({row[column] for row in rows}) == 27
    assert {r[1] for r in rows if r[0] == "sensor"} == set(SENSORS) | {"quick_feed_state"}
    assert {r[1] for r in rows if r[0] == "binary_sensor"} == {
        "connected", "defrosting", "fault", "out_of_food"}
    assert {(r[0], r[1]) for r in rows if r[0] not in {"sensor", "binary_sensor"}} == {
        ("event", "feeding"), ("switch", "meal_counter"),
        ("switch", "stainless_inserts"), ("number", "brightness_setting")}


async def seed(ctx, monkeypatch, *, disabled=False):
    fixture = inventory()
    data = dict(fixture["data"])
    data.pop("migrated_from")
    # Match the existing injected protocol-3.5 device's synthetic key.
    data["local_key"] = KEY
    entry = make_entry(ctx, data, entry_id=fixture["entry_id"], options=fixture["options"])
    leaf = dr.DeviceEntry(config_entry_id=entry.entry_id, id=fixture["leaf_id"],
                          identifiers={("tuya_local", fixture["device_id"])})
    dr.async_get(ctx.hass)._devices[leaf.id] = leaf
    entities = er.async_get(ctx.hass)
    for domain, key, unique_id, entity_id, row_id, name in fixture["rows"]:
        # Registry-generated row IDs are replaced only during fixture creation,
        # before any production setup, to freeze literal pre-upgrade identities.
        row = entities.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry, device_id=leaf.id,
            suggested_object_id=entity_id.split(".", 1)[1])
        entities.entities[row.entity_id] = attr.evolve(row, id=row_id)
        entities.async_update_entity(row.entity_id, name=name,
            disabled_by=er.RegistryEntryDisabler.USER if disabled and key == "brightness" else None)
    await async_provision_store(ctx.root, binding_id=leaf.id, store_id=data["intent_store_id"])
    witness = open_store(ctx.root, binding_id=leaf.id, store_id=data["intent_store_id"])
    record = {"operation_id": "1234567890abcdef1234567890abcdef", "operation": "feed", "state": "pending"}
    await witness.async_prepare_intent(record)
    return entry, record


def assert_identity(ctx, entry, *, disabled=False):
    fixture = inventory()
    registry = er.async_get(ctx.hass)
    frozen_entity_ids = {r[3] for r in fixture["rows"]}
    frozen_unique_ids = {r[2] for r in fixture["rows"]}
    rows = [r for r in registry.entities.values()
            if r.config_entry_id == entry.entry_id or r.device_id == fixture["leaf_id"]
            or r.entity_id in frozen_entity_ids
            or (r.platform == DOMAIN and r.unique_id in frozen_unique_ids)]
    # The only entity added since the frozen baseline is the refill button,
    # created fresh on the existing leaf; every frozen row stays exact.
    added = [r for r in rows if r.unique_id == f"{entry.entry_id}_refill_reset"]
    assert [(r.domain, r.platform, r.device_id, r.name, r.disabled_by, r.config_entry_id)
            for r in added] == [("button", DOMAIN, fixture["leaf_id"], None, None, entry.entry_id)]
    assert added[0].entity_id not in frozen_entity_ids and added[0].id not in {r[4] for r in fixture["rows"]}
    rows = [r for r in rows if r not in added]
    actual = {(r.entity_id, r.unique_id, r.id, r.name, r.device_id, r.disabled_by,
               r.config_entry_id) for r in rows}
    expected = {(entity_id, unique_id, row_id, name, fixture["leaf_id"],
                 er.RegistryEntryDisabler.USER if disabled and key == "brightness" else None,
                 entry.entry_id)
                for domain, key, unique_id, entity_id, row_id, name in fixture["rows"]}
    assert actual == expected
    leaves = dr.async_entries_for_config_entry(dr.async_get(ctx.hass), entry.entry_id)
    assert [(d.id, d.identifiers) for d in leaves] == [
        (fixture["leaf_id"], {("tuya_local", fixture["device_id"])})]
    matching_leaves = [d for d in dr.async_get(ctx.hass).devices
                       if ("tuya_local", fixture["device_id"]) in d.identifiers]
    assert [d.id for d in matching_leaves] == [fixture["leaf_id"]]


async def assert_retained(ctx, entry, record):
    await until(lambda: entry.runtime_data.adapter is not None)
    view = entry.runtime_data.adapter.view
    assert view["state"] == "uncertain"
    assert view["operation_id"] == record["operation_id"]
    assert view["recovery_phase"] == "awaiting_user_confirmation"
    witness = open_store(ctx.root, binding_id=entry.data["intent_binding_id"],
                         store_id=entry.data["intent_store_id"])
    assert await witness.async_load_last_intent() == record
    assert (Path(ctx.root) / DOMAIN / entry.data["intent_binding_id"] / "intent.sqlite").is_file()


@pytest.mark.parametrize("disabled", [False, True])
async def test_upgrade_keeps_27_rows_and_historical_identifier(owned, monkeypatch, disabled):
    entry, record = await seed(owned, monkeypatch, disabled=disabled)
    # Provisioning is permitted only above, never in setup/reload/reconfigure.
    from custom_components.petsafe_freshfeed import store_bootstrap
    monkeypatch.setattr(store_bootstrap, "async_provision_store", AsyncMock(side_effect=AssertionError("startup provisioned")))
    before = dict(entry.data)
    assert await owned.hass.config_entries.async_setup(entry.entry_id)
    await owned.hass.async_block_till_done()
    for iteration in range(2):
        assert_identity(owned, entry, disabled=disabled)
        await assert_retained(owned, entry, record)
        assert dict(entry.data) == before
        assert dict(entry.options) == {**inventory()["options"], "cloud_read_enabled": False}
        if iteration == 0:
            assert await owned.hass.config_entries.async_reload(entry.entry_id)
            await owned.hass.async_block_till_done()
    assert owned.device.control_frames() == []


async def test_migrated_from_survives_setup_reload_and_reconfigure(owned, monkeypatch):
    entry, record = await seed(owned, monkeypatch)
    data = {**entry.data, "migrated_from": inventory()["data"]["migrated_from"]}
    owned.hass.config_entries.async_update_entry(entry, data=data)
    # Until Tasks 3-4 remove journal admission, isolate field preservation from
    # that separate gate. Once removed, this exercises the unmodified path.
    from custom_components.petsafe_freshfeed import hub, config_flow
    if hasattr(hub, "async_migration_admits"):
        monkeypatch.setattr(hub, "async_migration_admits", AsyncMock(return_value=True))
    before = dict(entry.data)
    assert await owned.hass.config_entries.async_setup(entry.entry_id)
    await owned.hass.async_block_till_done()
    assert dict(entry.data) == before
    assert_identity(owned, entry)
    assert await owned.hass.config_entries.async_reload(entry.entry_id)
    await owned.hass.async_block_till_done()
    assert dict(entry.data) == before
    monkeypatch.setattr(config_flow, "_async_probe", AsyncMock(return_value=True))
    result = await owned.hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    result = await owned.hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.210.78"})
    assert result["reason"] == "reconfigure_successful"
    await owned.hass.async_block_till_done()
    assert dict(entry.data) == {**before, "host": "192.168.210.78"}
    assert dict(entry.options) == {**inventory()["options"], "cloud_read_enabled": False}
    assert_identity(owned, entry)
    await assert_retained(owned, entry, record)
    assert owned.device.control_frames() == []


@pytest.mark.parametrize("version", ["list", "v2", "v3", "corrupt-v3"])
async def test_owned_upgrade_loads_stores_without_conversion(owned, monkeypatch, hass_storage, version):
    entry, record = await seed(owned, monkeypatch)
    now = dt_util.utcnow()
    event = {"kind": "dispensed", "compartment": 2, "at": (now - timedelta(minutes=5)).isoformat()}
    from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
    schedule = ScheduleState().with_report("fwgAAQE=", now)
    payload = {"version": 3, "events": [event], "schedule": schedule.to_dict(),
               "observed_meals_dispensed_total": 42, "counting_since": now.isoformat()}
    if version == "list":
        payload = [event]
    elif version == "v2":
        payload = {"version": 2, "events": [event], "schedule": schedule.to_dict()}
    elif version == "corrupt-v3":
        payload.update(observed_meals_dispensed_total={"opaque": [1]}, counting_since="bad")
    original = deepcopy(payload)
    key = f"petsafe_freshfeed.{entry.entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": payload}
    owned.device.dps.pop("1", None)  # disk schedule is not replaced by a new report
    assert await owned.hass.config_entries.async_setup(entry.entry_id)
    for iteration in range(2):
        hub = entry.runtime_data
        await assert_retained(owned, entry, record)
        assert [e.to_dict() for e in hub.events] == [event]
        if version != "list":
            assert hub.schedule.entries == schedule.entries
            assert hub.schedule.source_state == "last_known"
        if version == "corrupt-v3":
            assert hub.observed_meals_dispensed_total is None
            assert hub.statistics_reason == "invalid_statistics_payload"
        else:
            assert hub.observed_meals_dispensed_total == (42 if version == "v3" else 0)
            assert hub.counting_since is not None
            if version == "v3":
                assert hub.counting_since.isoformat() == original["counting_since"]
        assert await owned.hass.config_entries.async_unload(entry.entry_id)
        saved = hass_storage[key]["data"]
        if version == "corrupt-v3":
            for field in ("observed_meals_dispensed_total", "counting_since"):
                assert saved[field] == original[field]
        assert saved["events"] == [event]
        if version != "list":
            assert saved["schedule"] == schedule.to_dict()
        if version == "v3":
            assert saved["observed_meals_dispensed_total"] == 42
            assert saved["counting_since"] == original["counting_since"]
        if iteration == 0:
            assert await owned.hass.config_entries.async_setup(entry.entry_id)
    assert owned.device.control_frames() == []


async def test_host_reconfigure_keeps_identity_and_unknown_fields(owned, monkeypatch):
    entry, record = await seed(owned, monkeypatch)
    assert await owned.hass.config_entries.async_setup(entry.entry_id)
    await owned.hass.async_block_till_done()
    before = dict(entry.data)
    from custom_components.petsafe_freshfeed import config_flow
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(config_flow, "_async_probe", probe)
    result = await owned.hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    result = await owned.hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.210.78"})
    assert result["reason"] == "reconfigure_successful"
    await owned.hass.async_block_till_done()
    probe.assert_awaited_once()
    assert dict(entry.data) == {**before, "host": "192.168.210.78"}
    assert dict(entry.options) == {**inventory()["options"], "cloud_read_enabled": False}
    assert_identity(owned, entry)
    await assert_retained(owned, entry, record)
    assert owned.device.control_frames() == []


@pytest.mark.parametrize("case", ["missing", "foreign_owner", "foreign_identifier"])
async def test_persisted_leaf_refusal_never_creates_or_adopts(owned, monkeypatch, case):
    entry, record = await seed(owned, monkeypatch)
    registry = dr.async_get(owned.hass)
    leaf_id = inventory()["leaf_id"]
    if case == "missing":
        del registry._devices[leaf_id]
    elif case == "foreign_owner":
        registry._devices[leaf_id] = dr.DeviceEntry(
            config_entry_id="foreign-entry", id=leaf_id,
            identifiers={("tuya_local", inventory()["device_id"])})
    else:
        registry._devices[leaf_id] = dr.DeviceEntry(
            config_entry_id=entry.entry_id, id=leaf_id, identifiers={(DOMAIN, "foreign-device")})
    before = dict(registry._devices)
    rows = dict(er.async_get(owned.hass).entities)
    assert not await owned.hass.config_entries.async_setup(entry.entry_id)
    assert dict(registry._devices) == before
    assert dict(er.async_get(owned.hass).entities) == rows
    assert owned.device.control_frames() == []
