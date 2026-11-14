"""Report history and schedule projections."""
import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from custom_components.petsafe_freshfeed.schedule_codec import MealEntry, encode

from custom_components.petsafe_freshfeed.tuya_bridge import DeviceReport
from custom_components.petsafe_freshfeed.schedule_state import ScheduleState
from test_hub import make_hub
from custom_components.petsafe_freshfeed.observed import FeedEvent
from custom_components.petsafe_freshfeed.sensor import FreshFeedSensor


async def test_retention_on_load_preserves_schedule_and_last_fed(hass, source_entry, raw_entities, hass_storage):
    hub = make_hub(hass, source_entry)
    now = datetime.now(timezone.utc)
    expired = FeedEvent("dispensed", 2, now - timedelta(days=8))
    recent = [FeedEvent("meal_report", None, now - timedelta(seconds=i), 14, 1)
              for i in range(501)]
    cached = ScheduleState().with_report("fwgAAQE=", now)
    key = f"petsafe_freshfeed.{hub.entry.entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {
        "version": 2, "events": [e.to_dict() for e in [expired, *recent]],
        "schedule": cached.to_dict(),
    }}
    await hub.async_start()
    try:
        assert len(hub.events) == 500
        assert hub.events == list(reversed(recent[:500]))
        assert hub.last_fed == expired.at
        assert hub.schedule.entries == cached.entries
        assert hub.schedule.source_state == "last_known"
    finally:
        await hub.async_stop()
    assert len(hass_storage[key]["data"]["events"]) == 500
    persisted = ScheduleState.from_dict(hass_storage[key]["data"]["schedule"])
    assert persisted.entries == cached.entries
    assert persisted.observed_at == cached.observed_at


async def test_quiet_tick_expires_history_and_persists_without_new_event(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    now = datetime.now(timezone.utc)
    event = FeedEvent("dispensed", 2, now - timedelta(days=7) + timedelta(seconds=30))
    hub._record_event(event)
    sensor = FreshFeedSensor(hub, hub.entry.entry_id, "history")
    notified = []
    hub.async_add_listener(lambda: notified.append(sensor.native_value))
    try:
        assert sensor.native_value == 1
        with patch.object(hub._store, "async_delay_save") as save:
            hub._tick(now + timedelta(seconds=60))
            assert sensor.native_value == 0
            assert notified[-1] == 0
            save.assert_called_once()
            assert save.call_args.args[0]()["events"] == []
        assert hub.last_fed == event.at
    finally:
        await hub.async_stop()


async def test_reports_are_transitions_with_observation_timestamp(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    at = datetime.now(timezone.utc)
    for seq, dps in enumerate(({"14": 0, "150": 0}, {"14": 1}, {"14": 1}, {"14": 4}, {"14": 5}, {"150": 1})):
        hub._device_report(DeviceReport(1, seq, at, dps))
    assert [(e.kind, e.source_dp, e.report_value, e.at) for e in hub.events] == [
        ("meal_report", 14, 1, at), ("meal_report", 14, 4, at),
        ("all_meals_report", 14, 5, at), ("interrupted", 150, 1, at)]
    await hub.async_stop()
    restarted = type(hub)(hass, hub.entry)
    await restarted.async_start()
    assert [event.kind for event in restarted.events] == ["meal_report", "meal_report", "all_meals_report", "interrupted"]
    restarted._device_report(DeviceReport(2, 1, at, {"14": 2}))
    assert len(restarted.events) == 4
    await restarted.async_stop()


async def test_replacement_bridge_first_reports_do_not_create_transitions(hass, source_entry, raw_entities, loaded_source):
    import asyncio
    from types import SimpleNamespace
    from homeassistant.config_entries import ConfigEntryState
    from conftest import DEVICE_ID
    from test_tuya_bridge import Api

    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        old_bridge = hub.bridge
        loaded_source._api._cache_response({'dps': {'14': 0, '150': 0}})
        await hass.async_block_till_done()
        assert hub._report_epoch == 1
        source_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        await hass.async_block_till_done()
        new_api = Api()
        hass.data['tuya_local'][DEVICE_ID] = {'device': SimpleNamespace(_api=new_api, _api_lock=asyncio.Lock())}
        source_entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.async_block_till_done()
        assert hub.bridge is not old_bridge
        new_api._cache_response({'dps': {'14': 1, '150': 1}})
        await hass.async_block_till_done()
        assert hub._report_epoch == 1
        assert hub.events == []
        new_api._cache_response({'dps': {'14': 2, '150': 0}})
        await hass.async_block_till_done()
        assert [(event.kind, event.report_value) for event in hub.events] == [('meal_report', 2)]
    finally:
        await hub.async_stop()


@pytest.mark.parametrize(("hour", "minute", "now", "expected"), [
    (2, 30, "2026-03-08T09:00:00+00:00", "2026-03-09T09:30:00+00:00"),
    (1, 30, "2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00"),
])
async def test_next_meal_dst_in_ha_timezone(hass, source_entry, raw_entities, hour, minute, now, expected):
    await hass.config.async_set_time_zone("America/Los_Angeles")
    hass.states.async_set(raw_entities[145], "unknown")
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        at = datetime.fromisoformat(now)
        hub.schedule = ScheduleState().with_report(base64.b64encode(encode((MealEntry(127, hour, minute, True, True),))).decode(), at)
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, "next_meal")
        with patch("custom_components.petsafe_freshfeed.hub.dt_util.utcnow", return_value=at):
            assert sensor.native_value == datetime.fromisoformat(expected)
            assert sensor.extra_state_attributes == {
                "basis": "schedule", "source_state": "current", "observed_at": at.isoformat(), "reason": None}
            hub.schedule = hub.schedule.disconnected()
            assert sensor.native_value == datetime.fromisoformat(expected)
            assert sensor.extra_state_attributes["source_state"] == "last_known"
    finally:
        await hub.async_stop()


async def test_history_schema_and_report_does_not_double_count_dispense(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        at = datetime.now(timezone.utc)
        hub._device_report(DeviceReport(1, 1, at, {"14": 0}))
        hub._device_report(DeviceReport(1, 2, at, {"14": 1}))
        legacy = FeedEvent("dispensed", 2, at)
        hub._record_event(legacy)
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, "history")
        assert sensor.unique_id == f"{hub.entry.entry_id}_history"
        assert sensor.native_value == 2
        assert sensor.extra_state_attributes == {
            "basis": "ha_observed", "dp151_supported": False, "events": [
                {"kind": "meal_report", "compartment": None, "at": at.isoformat(), "source_dp": 14, "report_value": 1},
                {"kind": "dispensed", "compartment": 2, "at": at.isoformat(), "source_dp": None, "report_value": None},
            ]}
        assert hub.meals_today == 1
        assert hub.last_fed == at
    finally:
        await hub.async_stop()


@pytest.mark.parametrize("wire", [None, "/w==", "fwgAAAE="])
async def test_no_planned_occurrence_for_unknown_empty_disabled(hass, source_entry, raw_entities, wire):
    hass.states.async_set(raw_entities[145], "unknown")
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        if wire is not None:
            hub.schedule = ScheduleState().with_report(wire, datetime.now(timezone.utc))
        assert hub.next_meal is None
        assert FreshFeedSensor(hub, hub.entry.entry_id, "next_meal").extra_state_attributes["reason"] is None
    finally:
        await hub.async_stop()


async def test_next_meal_uses_cached_schedule_not_history(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    hub.schedule = ScheduleState().with_report("fwgAAQE=", datetime(2026, 1, 1, tzinfo=timezone.utc)).disconnected()
    hass.states.async_set(raw_entities[145], "unknown")
    await hass.async_block_till_done()
    with patch("custom_components.petsafe_freshfeed.hub.dt_util.utcnow", return_value=datetime(2026, 1, 1, 7, tzinfo=timezone.utc)):
        assert hub.next_meal == datetime(2026, 1, 1, 16, tzinfo=timezone.utc)
        before = hub.schedule
        hub._record_event(FeedEvent("dispensed", 2, datetime.now(timezone.utc)))
        assert hub.schedule == before
        assert hub.next_meal == datetime(2026, 1, 1, 16, tzinfo=timezone.utc)
        hass.states.async_set(raw_entities[145], "6")
        await hass.async_block_till_done()
        assert hub.next_meal is None
        assert FreshFeedSensor(hub, hub.entry.entry_id, "next_meal").extra_state_attributes == {
            "basis": "schedule", "source_state": "last_known",
            "observed_at": before.observed_at.isoformat(), "reason": "empty_tray"}
    await hub.async_stop()
