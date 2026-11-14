"""Schedule registry binding must not use unrelated source entities."""

from homeassistant.helpers import entity_registry as er

from test_hub import make_hub


async def test_schedule_registry_binds_profile_suffix_on_selected_device(hass, source_entry, raw_entities):
    registry = er.async_get(hass)
    device_id = registry.async_get(raw_entities[145]).device_id
    schedule = registry.async_get_or_create(
        "text", "tuya_local", "fakeid-text_meal_plan",
        config_entry=source_entry, device_id=device_id,
    )
    quick = registry.async_get_or_create(
        "sensor", "tuya_local", "fakeid-sensor_quick_feed_state",
        config_entry=source_entry, device_id=device_id,
    )
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        assert hub.quick_feed_entity_id == quick.entity_id
        assert hub.schedule_entity_id == schedule.entity_id
        registry.async_update_entity(schedule.entity_id, new_entity_id="text.renamed_plan")
        await hass.async_block_till_done()
        assert hub.schedule_entity_id == "text.renamed_plan"
        from unittest.mock import AsyncMock
        bridge = AsyncMock()
        hub.bridge = bridge
        registry.async_remove("text.renamed_plan")
        await hass.async_block_till_done()
        assert hub.schedule_entity_id is None
        bridge.async_detach.assert_awaited_once()
        assert hub.bridge is None
    finally:
        await hub.async_stop()


async def test_raw_rename_keeps_state_listener(hass, source_entry, raw_entities):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        er.async_get(hass).async_update_entity(raw_entities[145], new_entity_id="sensor.renamed_raw")
        await hass.async_block_till_done()
        hass.states.async_set("sensor.renamed_raw", "2")
        await hass.async_block_till_done()
        assert hub.snapshot.compartment == 2
    finally:
        await hub.async_stop()


async def test_wrong_device_schedule_not_bound(hass, source_entry, raw_entities):
    from homeassistant.helpers import device_registry as dr
    registry = er.async_get(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id, identifiers={("tuya_local", "other")})
    registry.async_get_or_create("text", "tuya_local", "other-text_meal_plan",
                                 config_entry=source_entry, device_id=other.id)
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        assert hub.schedule_entity_id is None
    finally:
        await hub.async_stop()


async def test_loaded_receive_not_optimistic_state(hass, source_entry, raw_entities, loaded_source):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        assert hub.bridge is not None
        hass.states.async_set('text.optimistic', '/w==')
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'unknown'
        loaded_source._api._cache_response({'dps': {'1': '/w=='}})
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'current'
        loaded_source._api.close()
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'last_known'
    finally:
        await hub.async_stop()


async def test_queued_report_survives_reconnect_as_last_known(hass, source_entry, raw_entities, loaded_source):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        api = loaded_source._api
        api._cache_response({'dps': {'1': '/w=='}})
        api.close()
        api._get_socket()
        await hass.async_block_till_done()
        assert hub.schedule.entries == ()
        assert hub.schedule.source_state == 'last_known'
    finally:
        await hub.async_stop()


async def test_api_replacement_demotes_without_report(hass, source_entry, raw_entities, loaded_source):
    from datetime import datetime, timezone
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        loaded_source._api._cache_response({'dps': {'1': '/w=='}})
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'current'
        from test_tuya_bridge import Api
        old_bridge = hub.bridge
        loaded_source._api = Api()
        hub._tick(datetime.now(timezone.utc))
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'last_known'
        assert hub.bridge is not None and hub.bridge is not old_bridge
        assert hub.bridge.api is loaded_source._api
    finally:
        await hub.async_stop()


async def test_nonpersistent_close_before_cache_retains_last_known(hass, source_entry, raw_entities, loaded_source):
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        loaded_source._api.close()
        loaded_source._api._cache_response({'dps': {'1': '/w=='}})
        await hass.async_block_till_done()
        assert hub.schedule.entries == ()
        assert hub.schedule.source_state == 'last_known'
    finally:
        await hub.async_stop()


async def test_unload_during_attachment_detaches_result(hass, source_entry, raw_entities):
    import asyncio
    from unittest.mock import AsyncMock, patch
    started, resume = asyncio.Event(), asyncio.Event()
    bridge = AsyncMock()
    async def attach(*args):
        started.set()
        await resume.wait()
        return bridge
    hub = make_hub(hass, source_entry)
    with patch('custom_components.petsafe_freshfeed.hub.async_attach', attach):
        task = asyncio.create_task(hub.async_start())
        async with asyncio.timeout(5):
            await started.wait()
        await hub.async_stop()
        resume.set()
        await task
    assert hub.bridge is None
    bridge.async_detach.assert_awaited_once()
    bridge.subscribe.assert_not_called()


async def test_registry_removal_invalidates_before_lock_released(hass, source_entry, raw_entities, loaded_source):
    import asyncio
    registry = er.async_get(hass)
    device_id = registry.async_get(raw_entities[145]).device_id
    schedule = registry.async_get_or_create('text', 'tuya_local', 'fakeid-text_meal_plan',
                                           config_entry=source_entry, device_id=device_id)
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    api, lock = loaded_source._api, loaded_source._api_lock
    try:
        api._cache_response({'dps': {'1': '/w=='}})
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'current'
        await lock.acquire()
        registry.async_remove(schedule.entity_id)
        for _ in range(10):
            await asyncio.sleep(0)
        assert hub.bridge is None
        assert hub.schedule.source_state == 'last_known'
        api._cache_response({'dps': {'1': '/w=='}})
        for _ in range(10):
            await asyncio.sleep(0)
        assert hub.schedule.source_state == 'last_known'
    finally:
        if lock.locked():
            lock.release()
        await hass.async_block_till_done()
        await hub.async_stop()


async def test_source_unload_clears_quick_feed(hass, source_entry, raw_entities, loaded_source):
    from homeassistant.config_entries import ConfigEntryState
    from custom_components.petsafe_freshfeed.sensor import FreshFeedSensor
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, "quick_feed")
        loaded_source._api._cache_response({'dps': {'140': 'EAIA'}})
        await hass.async_block_till_done()
        assert sensor.native_value == 'feeding'
        source_entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
        await hass.async_block_till_done()
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {'command': None, 'minutes': None}
    finally:
        await hub.async_stop()


async def test_source_unload_detaches(hass, source_entry, raw_entities):
    from unittest.mock import AsyncMock
    from homeassistant.config_entries import ConfigEntryState
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    bridge = AsyncMock()
    hub.bridge = bridge
    source_entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
    await hass.async_block_till_done()
    try:
        bridge.async_detach.assert_awaited_once()
        assert hub.bridge is None
    finally:
        await hub.async_stop()


async def test_schedule_only_reports_promote_and_disconnect_demotes(hass, source_entry, raw_entities):
    from datetime import datetime, timezone
    from custom_components.petsafe_freshfeed.tuya_bridge import DeviceReport
    from custom_components.petsafe_freshfeed.sensor import FreshFeedSensor
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        sensor = FreshFeedSensor(hub, hub.entry.entry_id, "schedule")
        assert sensor.native_value is None
        hass.states.async_set(raw_entities[145], "5")
        await hass.async_block_till_done()
        assert hub.schedule.source_state == "unknown"
        hub._device_report(DeviceReport(1, 1, datetime.now(timezone.utc), {"1": "/w=="}))
        assert sensor.native_value == 0
        assert sensor.extra_state_attributes["entries"] == []
        assert set(sensor.extra_state_attributes) == {
            "entries", "source_state", "observed_at", "revision", "source_entity_id", "error"}
        assert hub.schedule.source_state == "current"
        hub._device_report(DeviceReport(1, 2, datetime.now(timezone.utc), {"1": "invalid"}))
        assert hub.schedule.entries == ()
        assert hub.schedule.error == "invalid_schedule"
        hub._connection_changed(2, False)
        assert hub.schedule.source_state == "last_known"
        assert sensor.available
    finally:
        await hub.async_stop()


async def test_source_reload_reattaches_new_client(hass, source_entry, raw_entities, loaded_source):
    import asyncio
    from types import SimpleNamespace
    from homeassistant.config_entries import ConfigEntryState
    from test_tuya_bridge import Api
    from conftest import DEVICE_ID
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        old_bridge, old_api = hub.bridge, loaded_source._api
        assert old_bridge is not None
        source_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        await hass.async_block_till_done()
        assert hub.bridge is None
        new_api = Api()
        new_wrapper = SimpleNamespace(_api=new_api, _api_lock=asyncio.Lock())
        hass.data["tuya_local"][DEVICE_ID] = {"device": new_wrapper}
        source_entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.async_block_till_done()
        assert hub.bridge is not None and hub.bridge is not old_bridge
        assert hub.bridge.api is new_api
        old_api._cache_response({'dps': {'1': '/w=='}})
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'unknown'
        new_api._cache_response({'dps': {'1': '/w=='}})
        await hass.async_block_till_done()
        assert hub.schedule.source_state == 'current'
        # Repeated LOADED notifications never install a second receive owner.
        source_entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.async_block_till_done()
        assert hub.bridge.api is new_api and hub.schedule.error is None
    finally:
        await hub.async_stop()
    assert '_cache_response' not in new_api.__dict__


async def test_unload_during_reattachment_detaches_result(hass, source_entry, raw_entities):
    import asyncio
    from unittest.mock import AsyncMock, patch
    from homeassistant.config_entries import ConfigEntryState
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    started, resume = asyncio.Event(), asyncio.Event()
    bridge = AsyncMock()
    async def attach(*args):
        started.set()
        await resume.wait()
        return bridge
    try:
        with patch('custom_components.petsafe_freshfeed.hub.async_attach', attach):
            source_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
            await hass.async_block_till_done()
            source_entry.mock_state(hass, ConfigEntryState.LOADED)
            async with asyncio.timeout(5):
                await started.wait()
            source_entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
            for _ in range(10):
                await asyncio.sleep(0)
            resume.set()
            await hass.async_block_till_done()
        assert hub.bridge is None
        bridge.async_detach.assert_awaited_once()
        bridge.subscribe.assert_not_called()
    finally:
        await hub.async_stop()


async def test_invalidated_binding_not_reattached_on_reload(hass, source_entry, raw_entities, loaded_source):
    from homeassistant.config_entries import ConfigEntryState
    registry = er.async_get(hass)
    device_id = registry.async_get(raw_entities[145]).device_id
    schedule = registry.async_get_or_create('text', 'tuya_local', 'fakeid-text_meal_plan',
                                           config_entry=source_entry, device_id=device_id)
    hub = make_hub(hass, source_entry)
    await hub.async_start()
    try:
        registry.async_remove(schedule.entity_id)
        await hass.async_block_till_done()
        assert hub.bridge is None
        source_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        await hass.async_block_till_done()
        source_entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.async_block_till_done()
        assert hub.bridge is None
    finally:
        await hub.async_stop()
