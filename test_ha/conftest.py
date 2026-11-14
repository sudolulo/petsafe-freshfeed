"""Shared fixtures for the petsafe_freshfeed Home Assistant tests."""

from pathlib import Path

import custom_components
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

# pytest-homeassistant-custom-component ships a regular `custom_components` package in its
# testing_config that shadows this repo's namespace package. Put the repo directory first so
# Home Assistant's loader (which scans custom_components.__path__) finds petsafe_freshfeed.
_REPO_CUSTOM_COMPONENTS = str(Path(__file__).resolve().parent.parent / "custom_components")
if _REPO_CUSTOM_COMPONENTS not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CUSTOM_COMPONENTS)

SOURCE_ENTRY_ID = "synthetic_tuya_source_entry"
SOURCE_TITLE = "PetSafe FreshFeed"
DEVICE_ID = "fakeid"

# DP -> (platform, live state) as observed on the real device (spec Evidence table).
RAW_DPS = {
    4: ("sensor", "standby"),
    14: ("sensor", "9"),
    110: ("binary_sensor", "on"),
    111: ("sensor", "10"),
    125: ("binary_sensor", "on"),
    130: ("sensor", "2.5.18"),
    131: ("sensor", "7.1.0.0"),
    142: ("sensor", "0"),
    145: ("sensor", "6"),
    150: ("sensor", "0"),
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/ in every test."""
    yield


def make_source_entry(hass, *, entry_id=SOURCE_ENTRY_ID, title=SOURCE_TITLE,
                      data_type="petsafe_freshfeed_raw", options=None):
    """Add a mock tuya_local entry in LOADED state without setting tuya_local up."""
    entry = MockConfigEntry(
        domain="tuya_local",
        title=title,
        entry_id=entry_id,
        data={"type": data_type, "device_id": DEVICE_ID, "local_key": "SECRET"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


@pytest.fixture
def source_entry(hass: HomeAssistant) -> MockConfigEntry:
    return make_source_entry(hass)


@pytest.fixture
def raw_entities(hass: HomeAssistant, source_entry) -> dict[int, str]:
    """Register the raw device, its ten raw entities and their live states."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("tuya_local", DEVICE_ID)},
        name="PetSafe FreshFeed",
    )
    result: dict[int, str] = {}
    for dp, (platform, state) in RAW_DPS.items():
        entry = ent_reg.async_get_or_create(
            platform,
            "tuya_local",
            f"{DEVICE_ID}-{platform}_dp_{dp}_raw",
            suggested_object_id=f"petsafe_freshfeed_dp_{dp}_raw",
            config_entry=source_entry,
            device_id=device.id,
        )
        result[dp] = entry.entity_id
        hass.states.async_set(entry.entity_id, state)
    return result


@pytest.fixture
def loaded_source(hass, source_entry, raw_entities, monkeypatch):
    """Synthetic shared client; provenance is tested separately with temp files."""
    import asyncio
    from types import SimpleNamespace
    from test_tuya_bridge import Api
    from custom_components.petsafe_freshfeed import tuya_bridge
    api = Api()
    wrapper = SimpleNamespace(_api=api, _api_lock=asyncio.Lock())
    hass.data.setdefault("tuya_local", {})[DEVICE_ID] = {"device": wrapper}
    monkeypatch.setattr(tuya_bridge, "check_loaded_source", lambda *args: True)
    return wrapper


def set_raw(hass: HomeAssistant, entity_id: str, state: str) -> None:
    """Set a raw entity state, as tuya_local would after a DP update."""
    hass.states.async_set(entity_id, state)
