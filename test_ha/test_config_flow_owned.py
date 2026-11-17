"""Standalone account onboarding: owned entries, explicit controls, host reconfigure.

No network, no physical feeder and no Tuya Local import: the account client and
the LAN locator are patched at the names ``config_flow`` uses, and every local
connection (the flow's transient admission probe and the entry's own source)
runs the pinned TinyTuya client against an injected protocol 3.5 fake device.
A meta-path blocker refuses any Tuya Local import for each test.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.petsafe_freshfeed as integration
from custom_components.petsafe_freshfeed import config_flow, hub as hub_module
from custom_components.petsafe_freshfeed.const import (
    BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_DEVICE_ID, CONF_HOST, CONF_INTENT_BINDING_ID,
    CONF_INTENT_STORE_ID, CONF_LOCAL_KEY, CONF_PROTOCOL_VERSION, CONF_REGISTRY_DEVICE_ID,
    CONF_SOURCE_ENTRY_ID, DOMAIN,
)
from custom_components.petsafe_freshfeed.owned_source import OwnedSource
from test_owned_source import (  # noqa: F401 - pytest fixtures by name
    DEVICE, FakeDevice, HOST, KEY, OTHER_KEY, READ_ONLY_CMDS, no_tuya_local, pinned,
)

TL_DOMAIN = "tuya_local"
EMAIL = "synthetic-owned-mail@example.invalid"
PASSWORD = "synthetic-owned-pass-0001"  # noqa: S105 - synthetic sentinel
NAME = "SYNTH Owned Feeder"
OTHER_DEVICE = "bfsyntheticsecond0001"
NEW_HOST = "192.168.210.78"
PROFILE = object()
OWNED_KEYS = {CONF_CONNECTION_BACKEND, CONF_DEVICE_ID, CONF_LOCAL_KEY, CONF_HOST,
              CONF_PROTOCOL_VERSION}


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _feeder(device_id=DEVICE, name=NAME, key=KEY):
    from custom_components.petsafe_freshfeed.account.client import Feeder
    return Feeder._make(device_id, key, name)


def _ok(*feeders):
    from custom_components.petsafe_freshfeed import account
    return account.Discovery(account.Outcome.OK, feeders)


def _located(host=HOST, version="3.5"):
    from custom_components.petsafe_freshfeed.lan_locate import Located
    return Located(host=host, version=version)


@pytest.fixture
async def ctx(hass, pinned, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    root = Path(hass.config.path(".storage"))
    root.mkdir(mode=0o700, exist_ok=True)
    state = SimpleNamespace(hass=hass, root=root, sources=[], probes=[], setups=0, unloads=0,
                            discovery=_ok(_feeder()), located=_located(), locate_calls=[],
                            discover_calls=0, now=1000.0)
    state.device = FakeDevice(pinned).install(monkeypatch)

    class Recorded(OwnedSource):
        IDLE = 0.005
        BACKOFF = 0.05

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.sources.append(self)

    class Probe(Recorded):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.probes.append(self)

    monkeypatch.setattr(hub_module, "OwnedSource", Recorded)
    monkeypatch.setattr(config_flow, "OwnedSource", Probe)

    real_setup, real_unload = integration.async_setup_entry, integration.async_unload_entry

    async def counted_setup(hass_, entry):
        state.setups += 1
        return await real_setup(hass_, entry)

    async def counted_unload(hass_, entry):
        state.unloads += 1
        return await real_unload(hass_, entry)
    monkeypatch.setattr(integration, "async_setup_entry", counted_setup)
    monkeypatch.setattr(integration, "async_unload_entry", counted_unload)

    def discover(request, *, profile, deadline_s=30.0):
        assert profile is PROFILE
        state.discover_calls += 1
        return state.discovery

    def locate(device_id, **kwargs):
        state.locate_calls.append(device_id)
        return state.located

    with patch(f"{config_flow.__name__}.account.load_app_profile", return_value=PROFILE), \
            patch(f"{config_flow.__name__}.account.discover", new=discover), \
            patch(f"{config_flow.__name__}.lan_locate.locate", new=locate), \
            patch(f"{config_flow.__name__}._monotonic", side_effect=lambda: state.now):
        yield state
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)
        for source in state.sources:
            assert await source.async_stop() is True


async def _configure(hass, result, data):
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def _to_confirm(ctx):
    hass = ctx.hass
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["account", "existing"]
    result = await _configure(hass, result, {"next_step_id": "account"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "account"
    ctx.now += 120.0
    return await _configure(hass, result, {"country": "1", "email": EMAIL, "password": PASSWORD})


async def _create(ctx):
    result = await _to_confirm(ctx)
    assert result["step_id"] == "confirm"
    result = await _configure(ctx.hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await ctx.hass.async_block_till_done()
    return result, result["result"]


def _tuya_local_imported():
    import sys
    return any(name == "custom_components.tuya_local"
               or name.startswith("custom_components.tuya_local.") for name in sys.modules)


def _leaves(ctx, entry):
    return dr.async_entries_for_config_entry(dr.async_get(ctx.hass), entry.entry_id)


def _store_dir(ctx):
    return ctx.root / DOMAIN


def _flow(hass, result):
    return hass.config_entries.flow._progress[result["flow_id"]]


# --------------------------------------------------------------------------


async def test_account_creates_owned_entry_without_tuya_local(ctx):
    hass = ctx.hass
    result = await _to_confirm(ctx)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"name": NAME}
    assert [str(key) for key in result["data_schema"].schema] == []
    # Nothing connected yet: the located host is only advisory until confirmed.
    assert ctx.device.addresses == [] and ctx.probes == []
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE,
                              CONF_LOCAL_KEY: KEY, CONF_HOST: HOST, CONF_PROTOCOL_VERSION: "3.5"}
    assert result["title"] == NAME
    assert result["result"].unique_id == DEVICE
    # The completion text explains the separate, explicit controls step.
    assert result["description"] == "owned_read_only"
    assert result["description_placeholders"] == {"name": NAME}
    await hass.async_block_till_done()
    # One transient authenticated probe, fully drained before the entry's own source.
    assert len(ctx.probes) == 1 and ctx.probes[0]._state == "stopped"
    assert len(ctx.sources) == 2 and ctx.sources[1] is not ctx.probes[0]
    assert ctx.device.addresses[0] == (HOST, 6668)
    assert ctx.device.max_active <= 1
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.snapshot.available
    assert not _tuya_local_imported() and TL_DOMAIN not in hass.data
    assert hass.config_entries.async_entries(TL_DOMAIN) == []
    assert ctx.locate_calls == [DEVICE] and ctx.discover_calls == 1
    assert set(ctx.device.commands) <= READ_ONLY_CMDS and ctx.device.control_frames() == []


async def test_owned_manual_host_fallback(ctx):
    hass = ctx.hass
    ctx.located = None
    result = await _to_confirm(ctx)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "host"
    for bad in ("8.8.8.8", "feeder.local", "fd00::1", "192.168.1"):
        result = await _configure(hass, result, {"host": bad})
        assert result["step_id"] == "host" and result["errors"] == {"host": "invalid_host"}
    assert ctx.device.addresses == []
    result = await _configure(hass, result, {"host": NEW_HOST})
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == NEW_HOST
    await hass.async_block_till_done()
    assert ctx.device.addresses[0] == (NEW_HOST, 6668)
    assert set(ctx.device.addresses) == {(NEW_HOST, 6668)}
    assert ctx.device.control_frames() == []


async def test_located_unsupported_protocol_aborts_without_probe(ctx):
    ctx.located = _located(version="3.3")
    result = await _to_confirm(ctx)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_local_configuration"
    assert ctx.probes == [] and ctx.device.addresses == []
    assert ctx.hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("case", ["wrong_key", "stale_host", "forged_discovery", "other_device"])
async def test_owned_wrong_key_or_host_creates_no_entry(ctx, pinned, monkeypatch, case):
    hass = ctx.hass
    if case == "wrong_key":
        ctx.device = FakeDevice(pinned, key=OTHER_KEY).install(monkeypatch)
    elif case == "stale_host":
        ctx.device.refuse = True
    elif case == "forged_discovery":
        # A forged broadcast points at a host that does not hold the local key.
        ctx.located = _located(host="192.168.210.99")
        ctx.device = FakeDevice(pinned, key=OTHER_KEY).install(monkeypatch)
    else:
        ctx.device = FakeDevice(pinned, status_extra={"devId": OTHER_DEVICE}).install(monkeypatch)
    result = await _to_confirm(ctx)
    assert result["step_id"] == "confirm"
    result = await _configure(hass, result, {})
    # Fixed error; the user may correct the host. Nothing was created.
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "host"
    assert result["errors"] == {"base": "local_admission_failed"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert len(dr.async_get(hass).devices) == 0
    assert not _store_dir(ctx).exists()
    assert all(probe._state == "stopped" for probe in ctx.probes) and len(ctx.probes) == 1
    assert ctx.device.control_frames() == [] and ctx.device.controls == []
    assert KEY not in repr(result) and PASSWORD not in repr(result)
    if case == "stale_host":
        # Correcting the host keeps the account identity: same device, same key.
        ctx.device.refuse = False
        result = await _configure(hass, result, {"host": NEW_HOST})
        assert result["step_id"] == "confirm"
        result = await _configure(hass, result, {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_HOST] == NEW_HOST
        assert result["data"][CONF_DEVICE_ID] == DEVICE and result["result"].unique_id == DEVICE
        assert ctx.discover_calls == 1
        await hass.async_block_till_done()
    assert ctx.device.control_frames() == []


async def test_owned_setup_no_control(ctx):
    hass = ctx.hass
    _result, entry = await _create(ctx)
    hub = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    # Setup created the single PetSafe-owned leaf; the flow created none itself.
    leaves = _leaves(ctx, entry)
    assert [leaf.id for leaf in leaves] == [hub.device_id]
    assert leaves[0].identifiers == {(DOMAIN, DEVICE)}
    assert leaves[0].config_entry_id == entry.entry_id
    assert CONF_INTENT_STORE_ID not in entry.data and CONF_INTENT_BINDING_ID not in entry.data
    assert hub.witness is None and hub.adapter is None
    assert not _store_dir(ctx).exists()
    await asyncio.sleep(0.1)
    assert set(ctx.device.commands) <= READ_ONLY_CMDS
    assert ctx.device.control_frames() == [] and ctx.device.controls == []
    assert ctx.setups == 1


async def test_owned_entry_contains_no_account_credentials(ctx, caplog):
    hass = ctx.hass
    # Never the root logger: vendor DEBUG logging correctly refuses admission.
    caplog.set_level(logging.DEBUG, logger="custom_components.petsafe_freshfeed")
    # Capture may leave the root at DEBUG; TinyTuya is explicitly admitted at WARNING.
    caplog.set_level(logging.WARNING, logger="tinytuya")
    ctx.discovery = _ok(_feeder(), _feeder(OTHER_DEVICE, "SYNTH Second"))
    results = [await _to_confirm(ctx)]
    assert results[-1]["step_id"] == "feeder"
    flow = _flow(hass, results[-1])
    results.append(await _configure(hass, results[-1], {"feeder": "0"}))
    assert results[-1]["step_id"] == "confirm"
    flow_state = repr(vars(flow)) + repr(flow.context)
    results.append(await _configure(hass, results[-1], {}))
    assert results[-1]["type"] is FlowResultType.CREATE_ENTRY, results[-1].get("errors")
    await hass.async_block_till_done()
    entry = results[-1]["result"]
    assert set(entry.data) == OWNED_KEYS and dict(entry.options) == {}
    stored = repr(dict(entry.data)) + repr(dict(entry.options)) + entry.title + repr(entry.unique_id)
    for secret in (EMAIL, PASSWORD):
        assert secret not in stored and secret not in flow_state and secret not in caplog.text
    assert "country" not in entry.data and "email" not in entry.data
    for result in results:
        visible = repr({key: value for key, value in result.items()
                        if key not in ("data", "result")})
        for secret in (EMAIL, PASSWORD, KEY, HOST):
            assert secret not in visible, secret
    assert KEY not in flow_state and KEY not in caplog.text and KEY not in repr(entry)
    assert KEY not in repr(entry.runtime_data.bridge)
    # Account-path material is dropped once the flow finishes.
    assert flow._feeders == () and flow._feeder is None and flow._host is None
    assert flow._located is None


@pytest.mark.parametrize("case", [
    "legacy_enabled", "legacy_disabled", "legacy_ignored", "legacy_options", "owned",
    "owned_disabled", "migrated", "tuya_local_enabled_unused",
])
async def test_owned_duplicate_of_legacy_refused(ctx, case):
    hass = ctx.hass
    if case.startswith("legacy"):
        tl = MockConfigEntry(
            domain=TL_DOMAIN, entry_id="synthetic_legacy_tl",
            data={"type": "petsafe_freshfeed_raw"} if case == "legacy_options"
            else {"type": "petsafe_freshfeed_raw", "device_id": DEVICE},
            options={"device_id": DEVICE} if case == "legacy_options" else {},
            disabled_by=ConfigEntryDisabler.USER if case == "legacy_disabled" else None,
            source=config_entries.SOURCE_IGNORE if case == "legacy_ignored" else "user")
        tl.add_to_hass(hass)
        # The legacy PetSafe entry's unique ID is its source entry ID, not the device.
        MockConfigEntry(domain=DOMAIN, unique_id=tl.entry_id,
                        data={CONF_SOURCE_ENTRY_ID: tl.entry_id}).add_to_hass(hass)
        expected = "already_configured"
    elif case in ("owned", "owned_disabled"):
        MockConfigEntry(domain=DOMAIN, unique_id=DEVICE,
                        disabled_by=ConfigEntryDisabler.USER if case == "owned_disabled" else None,
                        data={CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE,
                              CONF_HOST: HOST, CONF_LOCAL_KEY: KEY,
                              CONF_PROTOCOL_VERSION: "3.5"}).add_to_hass(hass)
        expected = "already_configured"
    elif case == "migrated":
        # Migration keeps its old unique ID; the physical device field identifies it.
        MockConfigEntry(domain=DOMAIN, unique_id="old_source_entry_id",
                        data={CONF_SOURCE_ENTRY_ID: "old_source_entry_id",
                              CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE,
                              CONF_HOST: HOST, CONF_LOCAL_KEY: KEY,
                              CONF_PROTOCOL_VERSION: "3.5"}).add_to_hass(hass)
        expected = "already_configured"
    else:
        # An enabled Tuya Local entry already holds the feeder's one local connection.
        MockConfigEntry(domain=TL_DOMAIN, data={"type": "other_profile", "device_id": DEVICE}
                        ).add_to_hass(hass)
        expected = "tuya_local_conflict"
    before = len(hass.config_entries.async_entries(DOMAIN))
    result = await _to_confirm(ctx)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == expected
    assert len(hass.config_entries.async_entries(DOMAIN)) == before
    assert ctx.probes == [] and ctx.device.addresses == [] and ctx.locate_calls == []


async def test_disabled_unused_tuya_local_entry_does_not_block(ctx):
    MockConfigEntry(domain=TL_DOMAIN, data={"type": "other_profile", "device_id": DEVICE},
                    disabled_by=ConfigEntryDisabler.USER).add_to_hass(ctx.hass)
    result = await _to_confirm(ctx)
    assert result["step_id"] == "confirm"


async def test_ignored_owned_entry_does_not_block(ctx):
    MockConfigEntry(domain=DOMAIN, unique_id=DEVICE, source=config_entries.SOURCE_IGNORE,
                    data={}).add_to_hass(ctx.hass)
    result, entry = await _create(ctx)
    assert entry.unique_id == DEVICE
    assert [e.source for e in ctx.hass.config_entries.async_entries(DOMAIN)] == ["user"]


async def test_legacy_pick_refuses_owned_duplicate(ctx):
    hass = ctx.hass
    MockConfigEntry(domain=DOMAIN, unique_id=DEVICE,
                    data={CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE,
                          CONF_HOST: HOST, CONF_LOCAL_KEY: KEY,
                          CONF_PROTOCOL_VERSION: "3.5"}).add_to_hass(hass)
    tl = MockConfigEntry(domain=TL_DOMAIN, data={"type": "petsafe_freshfeed_raw", "device_id": DEVICE})
    tl.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await _configure(hass, result, {"next_step_id": "existing"})
    if result["type"] is FlowResultType.FORM:
        result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_owned_controls_explicit_only(ctx):
    hass = ctx.hass
    _result, entry = await _create(ctx)
    leaf = _leaves(ctx, entry)[0]
    before = dict(entry.data)
    # Opening options and declining enables nothing and provisions nothing.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.data) == before and not _store_dir(ctx).exists()
    assert ctx.setups == 1
    # The explicit second step provisions once, under the setup-created leaf.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.data) == {**before, CONF_INTENT_STORE_ID: entry.data[CONF_INTENT_STORE_ID],
                                CONF_INTENT_BINDING_ID: leaf.id}
    assert _store_dir(ctx).is_dir()
    assert [d.id for d in _leaves(ctx, entry)] == [leaf.id]
    # The update listener reloaded exactly once.
    assert ctx.setups == 2 and ctx.unloads == 1
    assert entry.state is ConfigEntryState.LOADED
    await until(lambda: entry.runtime_data.adapter is not None)
    assert entry.runtime_data.witness is not None
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "controls_already_enabled"
    assert ctx.device.control_frames() == [] and ctx.device.controls == []


async def test_enable_owned_controls_preserves_connection_config(ctx):
    hass = ctx.hass
    source = MockConfigEntry(domain=TL_DOMAIN, entry_id="retired_source",
                             data={"device_id": DEVICE}, disabled_by=ConfigEntryDisabler.USER)
    source.add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="retired_source", data={})
    entry.add_to_hass(hass)
    leaf = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(TL_DOMAIN, DEVICE)})
    data = {CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE, CONF_HOST: HOST,
            CONF_LOCAL_KEY: KEY, CONF_PROTOCOL_VERSION: "3.5",
            CONF_SOURCE_ENTRY_ID: source.entry_id, CONF_REGISTRY_DEVICE_ID: leaf.id}
    hass.config_entries.async_update_entry(entry, data=data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.data) == {**data, CONF_INTENT_STORE_ID: entry.data[CONF_INTENT_STORE_ID],
                                CONF_INTENT_BINDING_ID: leaf.id}
    assert (_store_dir(ctx)).is_dir()
    assert ctx.device.addresses == []


@pytest.mark.parametrize("case", ["no_leaf", "two_leaves", "foreign_leaf", "wrong_persisted"])
async def test_owned_controls_refuse_unresolved_leaf(ctx, case):
    hass = ctx.hass
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE, data={})
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    data = {CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE, CONF_HOST: HOST,
            CONF_LOCAL_KEY: KEY, CONF_PROTOCOL_VERSION: "3.5"}
    if case == "two_leaves":
        for ident in (DEVICE, OTHER_DEVICE):
            registry.async_get_or_create(config_entry_id=entry.entry_id,
                                         identifiers={(DOMAIN, ident)})
    elif case == "foreign_leaf":
        registry.async_get_or_create(config_entry_id=entry.entry_id,
                                     identifiers={(DOMAIN, OTHER_DEVICE)})
    elif case == "wrong_persisted":
        registry.async_get_or_create(config_entry_id=entry.entry_id,
                                     identifiers={(DOMAIN, DEVICE)})
        data[CONF_REGISTRY_DEVICE_ID] = "not-a-device"
    hass.config_entries.async_update_entry(entry, data=data)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "binding_unavailable"
    assert dict(entry.data) == data and not _store_dir(ctx).exists()


async def _reconfigure(ctx, entry):
    return await ctx.hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})


async def _with_controls(ctx):
    hass = ctx.hass
    _result, entry = await _create(ctx)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {"enable_controls": True})
    await hass.async_block_till_done()
    await until(lambda: entry.runtime_data.adapter is not None)
    return entry


async def test_owned_host_reconfigure_preserves_store_and_leaf(ctx):
    hass = ctx.hass
    entry = await _with_controls(ctx)
    entry_id, leaf_id = entry.entry_id, entry.runtime_data.device_id
    before = dict(entry.data)
    entity_ids = sorted(e.entity_id for e in er.async_entries_for_config_entry(
        er.async_get(hass), entry_id))
    setups, unloads, probes = ctx.setups, ctx.unloads, len(ctx.probes)
    result = await _reconfigure(ctx, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    assert [str(key) for key in result["data_schema"].schema] == ["host"]
    assert HOST not in repr(result) and KEY not in repr(result)
    result = await _configure(hass, result, {"host": "8.8.8.8"})
    assert result["errors"] == {"host": "invalid_host"}
    assert (ctx.setups, ctx.unloads) == (setups, unloads)
    result = await _configure(hass, result, {"host": NEW_HOST})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    # Unloaded once, probed the new host once, set up exactly once (no listener reload).
    assert (ctx.setups, ctx.unloads, len(ctx.probes)) == (setups + 1, unloads + 1, probes + 1)
    assert ctx.probes[-1]._connection.host == NEW_HOST and ctx.probes[-1]._state == "stopped"
    assert dict(entry.data) == {**before, CONF_HOST: NEW_HOST}
    assert entry.entry_id == entry_id and entry.unique_id == DEVICE
    assert entry.state is ConfigEntryState.LOADED
    hub = entry.runtime_data
    assert hub.device_id == leaf_id and [d.id for d in _leaves(ctx, entry)] == [leaf_id]
    assert hub.bridge._connection.host == NEW_HOST
    assert sorted(e.entity_id for e in er.async_entries_for_config_entry(
        er.async_get(hass), entry_id)) == entity_ids
    await until(lambda: hub.adapter is not None)
    assert ctx.device.addresses[-1] == (NEW_HOST, 6668)
    assert ctx.device.max_active <= 1
    assert ctx.device.control_frames() == [] and ctx.device.controls == []
    assert entry_id not in hass.data[DOMAIN].get("transitions", set())


async def test_owned_reconfigure_probe_failure_sets_up_old_host_once(ctx, monkeypatch):
    hass = ctx.hass
    _result, entry = await _create(ctx)
    before = dict(entry.data)
    setups = ctx.setups
    real_probe = config_flow._async_probe

    async def refused_probe(hass_, connection):
        ctx.device.refuse = True
        try:
            return await real_probe(hass_, connection)
        finally:
            ctx.device.refuse = False
    monkeypatch.setattr(config_flow, "_async_probe", refused_probe)
    result = await _reconfigure(ctx, entry)
    result = await _configure(hass, result, {"host": NEW_HOST})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "local_admission_failed"}
    await hass.async_block_till_done()
    assert dict(entry.data) == before
    assert ctx.setups == setups + 1 and entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.bridge._connection.host == HOST
    assert ctx.device.control_frames() == []


async def test_reconfigure_refuses_unload_failure_and_legacy(ctx, source_entry):
    hass = ctx.hass
    _result, entry = await _create(ctx)
    hub = entry.runtime_data

    async def refuse():
        return False
    hub.async_drain_controls = refuse
    setups, probes = ctx.setups, len(ctx.probes)
    result = await _reconfigure(ctx, entry)
    result = await _configure(hass, result, {"host": NEW_HOST})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "unload_failed"
    assert ctx.setups == setups and len(ctx.probes) == probes
    assert entry.data[CONF_HOST] == HOST
    assert entry.entry_id not in hass.data[DOMAIN].get("transitions", set())
    # The refused unload left the hub running; it was never replaced.
    assert entry.runtime_data is hub and hub.bridge is not None
    del hub.async_drain_controls
    assert await hub.async_stop()
    legacy = MockConfigEntry(domain=DOMAIN, unique_id=source_entry.entry_id,
                             data={CONF_SOURCE_ENTRY_ID: source_entry.entry_id})
    legacy.add_to_hass(hass)
    result = await _reconfigure(ctx, legacy)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_unsupported"


async def test_update_listener_respects_transition_gate(ctx):
    hass = ctx.hass
    _result, entry = await _create(ctx)
    setups = ctx.setups
    hass.data[DOMAIN].setdefault("transitions", set()).add(entry.entry_id)
    try:
        hass.config_entries.async_update_entry(entry, title="SYNTH renamed")
        await hass.async_block_till_done()
        assert ctx.setups == setups
    finally:
        hass.data[DOMAIN]["transitions"].discard(entry.entry_id)
    hass.config_entries.async_update_entry(entry, title="SYNTH renamed again")
    await hass.async_block_till_done()
    assert ctx.setups == setups + 1
