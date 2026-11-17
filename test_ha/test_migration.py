"""Explicit staged migration of a legacy entry to the PetSafe-owned source.

Actual HA config entries, device registry and entity registry throughout. The
Tuya Local integration is a mock integration (no Tuya Local import: a
meta-path blocker refuses one), the borrowed client is the synthetic legacy
fake, and every owned connection (pre-move probe, post-move verification and
the migrated hub) runs the pinned TinyTuya client against an injected protocol
3.5 fake device. No network and no physical feeder.

A "restart" is simulated explicitly: every entry is shut down, the HA process
marker is replaced and entries are set up again as a new process would, from
their persisted configuration. Owner accounting counts the loaded Tuya Local
source plus every started, not yet drained OwnedSource; controlled migration
never exceeds one.

Limit: manually re-enabling another integration (Tuya Local) is outside any
universal lock guarantee. Owned admission refuses the known conflict and the
owned hub fences itself, but it cannot stop another integration's connection.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntryDisabler, ConfigEntryState, ConfigFlow
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry, MockModule, mock_config_flow, mock_integration, mock_platform,
)

import custom_components.petsafe_freshfeed as integration
from custom_components.petsafe_freshfeed import hub as hub_module, migration, migration_state, tuya_bridge
from custom_components.petsafe_freshfeed.const import (
    BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_DEVICE_ID, CONF_HOST, CONF_INTENT_BINDING_ID,
    CONF_INTENT_STORE_ID, CONF_LOCAL_KEY, CONF_MIGRATED_FROM, CONF_MIGRATION_STAGED_CONNECTION,
    CONF_PROTOCOL_VERSION, CONF_REGISTRY_DEVICE_ID, CONF_SOURCE_ENTRY_ID, DOMAIN,
    OWNED_SETTING_ENTITIES,
)
from custom_components.petsafe_freshfeed.migration_journal import MigrationJournal
from custom_components.petsafe_freshfeed.migration_state import (
    MigrationFault, MigrationPhase, MigrationStatus, PreviewReason,
)
from custom_components.petsafe_freshfeed.owned_source import OwnedSource
from custom_components.petsafe_freshfeed.sensor import SENSORS
from conftest import RAW_DPS
from test_owned_hub import HUB_DPS
from test_owned_source import (  # noqa: F401 - pytest fixtures by name
    DEVICE, FakeDevice, HOST, KEY, OTHER_KEY, READ_ONLY_CMDS, no_tuya_local, pinned,
)
from test_tuya_bridge import Api

SRC = "migrating_tuya_source_entry"
NEW_HOST = "192.168.210.88"
S = MigrationStatus


async def until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class _TuyaLocalFlow(ConfigFlow):
    VERSION = 1


@pytest.fixture
async def mig(hass, pinned, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    root = Path(hass.config.path(".storage"))
    root.mkdir(mode=0o700)
    ctx = SimpleNamespace(hass=hass, mp=monkeypatch, root=str(root), live=set(), max_live=0,
                          sources=[], refuse_unload=False, setups=0, before_start=None)
    ctx.device = FakeDevice(pinned, dps=HUB_DPS).install(monkeypatch)

    def up(token):
        ctx.live.add(token)
        ctx.max_live = max(ctx.max_live, len(ctx.live))

    def down(token):
        ctx.live.discard(token)

    class Recorded(OwnedSource):
        IDLE = 0.005
        BACKOFF = 0.05

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ctx.sources.append(self)

        async def async_start(self):
            if ctx.before_start is not None:
                ctx.before_start(len(ctx.sources))
            up(self)
            try:
                await super().async_start()
            except BaseException:
                down(self)
                raise

        async def async_stop(self, timeout=5.0):
            stopped = await super().async_stop(timeout)
            if stopped:
                down(self)
            return stopped

    monkeypatch.setattr(hub_module, "OwnedSource", Recorded)
    monkeypatch.setattr(migration, "OwnedSource", Recorded)
    monkeypatch.setattr(tuya_bridge, "check_loaded_source", lambda *args: True)
    ctx.wrapper = SimpleNamespace(_api=Api(), _api_lock=asyncio.Lock())

    async def tl_setup(hass_, entry_):
        hass_.data.setdefault("tuya_local", {})[DEVICE] = {"device": ctx.wrapper}
        up("tuya_local")
        return True

    async def tl_unload(hass_, entry_):
        if ctx.refuse_unload:
            return False
        hass_.data.get("tuya_local", {}).pop(DEVICE, None)
        down("tuya_local")
        return True

    mock_integration(hass, MockModule("tuya_local", async_setup_entry=tl_setup,
                                      async_unload_entry=tl_unload))
    mock_platform(hass, "tuya_local.config_flow", None)
    real_setup = integration.async_setup_entry

    async def counted_setup(hass_, entry_):
        ctx.setups += 1
        return await real_setup(hass_, entry_)
    monkeypatch.setattr(integration, "async_setup_entry", counted_setup)
    monkeypatch.setattr(migration_state, "PROCESS_MARKER", str(uuid4()))
    with mock_config_flow("tuya_local", _TuyaLocalFlow):
        yield ctx
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)
        for source in ctx.sources:
            assert await source.async_stop() is True


def source_data(**overrides):
    data = {"type": "petsafe_freshfeed_raw", "device_id": DEVICE, "local_key": KEY,
            "host": HOST, "protocol_version": 3.5}
    data.update(overrides)
    return data


async def provision(ctx, leaf_id, *, retained=None):
    from custom_components.petsafe_freshfeed.store_bootstrap import async_provision_store, open_store
    store_id = uuid4().hex
    await async_provision_store(ctx.root, binding_id=leaf_id, store_id=store_id)
    if retained is not None:
        await open_store(ctx.root, binding_id=leaf_id, store_id=store_id).async_prepare_intent(
            {"operation_id": retained, "operation": "feed", "state": "pending"})
    return store_id


async def legacy(ctx, *, store=False, retained=None, src=None, src_options=None, load=True):
    """A loaded legacy PetSafe entry over a loaded Tuya Local source and its leaf."""
    hass = ctx.hass
    source = MockConfigEntry(domain="tuya_local", entry_id=SRC, title="FreshFeed raw",
                             data=src or source_data(), options=src_options or {})
    source.add_to_hass(hass)
    assert await hass.config_entries.async_setup(source.entry_id)
    registry, entities = dr.async_get(hass), er.async_get(hass)
    leaf = registry.async_get_or_create(config_entry_id=SRC, identifiers={("tuya_local", DEVICE)},
                                        name="PetSafe FreshFeed")
    raw = {}
    for dp, (platform, state) in RAW_DPS.items():
        row = entities.async_get_or_create(
            platform, "tuya_local", f"{DEVICE}-{platform}_dp_{dp}_raw",
            suggested_object_id=f"petsafe_freshfeed_dp_{dp}_raw", config_entry=source,
            device_id=leaf.id)
        raw[dp] = row.entity_id
        hass.states.async_set(row.entity_id, state)
    data = {CONF_SOURCE_ENTRY_ID: SRC}
    store_id = None
    if store:
        store_id = await provision(ctx, leaf.id, retained=retained)
        data.update({CONF_INTENT_STORE_ID: store_id, CONF_INTENT_BINDING_ID: leaf.id})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SRC, title="FreshFeed", data=data)
    entry.add_to_hass(hass)
    if load:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
    ctx.src, ctx.entry, ctx.leaf, ctx.raw, ctx.store_id = source, entry, leaf, raw, store_id
    ctx.original_data = dict(entry.data)
    ctx.petsafe = {row.unique_id: row.entity_id for row in petsafe_rows(ctx)}
    if load:
        assert set(ctx.petsafe) >= {f"{entry.entry_id}_{key}" for key in SENSORS}
    return ctx


def petsafe_rows(ctx):
    return er.async_entries_for_config_entry(er.async_get(ctx.hass), ctx.entry.entry_id)


def preserved(ctx):
    """Pre-migration PetSafe rows (unique ID -> entity ID); only owned-only setting rows may be added."""
    rows = {row.unique_id: row.entity_id for row in petsafe_rows(ctx)}
    added = set(rows) - set(ctx.petsafe)
    assert added <= {f"{ctx.entry.entry_id}_{role}" for role in OWNED_SETTING_ENTITIES}, added
    return {unique_id: rows[unique_id] for unique_id in ctx.petsafe if unique_id in rows}


# Tuya Local profile rows for the four retiring setting/diagnostic entities a
# dashboard uses: (domain, Tuya Local unique-ID suffix, dashboard object ID).
SETTING_ROWS = {
    "stainless_inserts": ("switch", "switch_stainless_inserts", "petsafe_freshfeed_stainless_inserts"),
    "meal_counter": ("switch", "switch_meal_counter", "petsafe_freshfeed_meal_counter"),
    "brightness_setting": ("number", "number_brightness", "petsafe_freshfeed_brightness"),
    "quick_feed_state": ("sensor", "sensor_quick_feed_state", "petsafe_freshfeed_quick_feed_state"),
}
TARGETS = {role: f"{domain}.{object_id}" for role, (domain, _s, object_id) in SETTING_ROWS.items()}


def add_setting_rows(ctx):
    entities = er.async_get(ctx.hass)
    for role, (domain, suffix, object_id) in SETTING_ROWS.items():
        row = entities.async_get_or_create(
            domain, "tuya_local", f"{DEVICE}-{suffix}", suggested_object_id=object_id,
            config_entry=ctx.src, device_id=ctx.leaf.id)
        assert row.entity_id == TARGETS[role]
        ctx.hass.states.async_set(row.entity_id, "on")


def owned_setting_ids(ctx):
    entities = er.async_get(ctx.hass)
    return {role: entities.async_get_entity_id(domain, DOMAIN, f"{ctx.entry.entry_id}_{role}")
            for role, (domain, _s, _o) in SETTING_ROWS.items()}


def leaf_owner(ctx):
    return dr.async_get(ctx.hass).async_get(ctx.leaf.id).config_entry_id


async def journal(ctx):
    return await MigrationJournal(ctx.hass.config.config_dir, ctx.entry.entry_id).async_load()


async def restart(ctx):
    """Shut everything down, then start as a new HA process from persisted config."""
    hass = ctx.hass
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.NOT_LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
    for entry in hass.config_entries.async_entries("tuya_local"):
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # A new process starts with an empty state machine, and HA writes no
    # startup state for entities of a disabled config entry: drop them here.
    entities = er.async_get(hass)
    for row in list(entities.entities.values()):
        owner = hass.config_entries.async_get_entry(row.config_entry_id) if row.config_entry_id else None
        if owner is not None and owner.disabled_by is not None:
            hass.states.async_remove(row.entity_id)
    ctx.mp.setattr(migration_state, "PROCESS_MARKER", str(uuid4()))
    for entry in hass.config_entries.async_entries("tuya_local"):
        if entry.disabled_by is None and entry.source != SOURCE_IGNORE:
            await hass.config_entries.async_setup(entry.entry_id)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.disabled_by is None and entry.source != SOURCE_IGNORE:
            await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def prepare(ctx, **kwargs):
    status = await migration.async_prepare_migration(ctx.hass, ctx.entry, **kwargs)
    await ctx.hass.async_block_till_done()
    return status


async def resume(ctx, **kwargs):
    status = await migration.async_resume_migration(ctx.hass, ctx.entry, **kwargs)
    await ctx.hass.async_block_till_done()
    return status


async def cancel(ctx):
    status = await migration.async_cancel_migration(ctx.hass, ctx.entry)
    await ctx.hass.async_block_till_done()
    return status


async def migrate(ctx):
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    await ctx.hass.async_block_till_done()
    return ctx.entry.runtime_data


async def migrate_resumed(ctx):
    """The rest of ``migrate`` after a successful prepare."""
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    await ctx.hass.async_block_till_done()
    return ctx.entry.runtime_data


def assert_one_owner(ctx):
    assert ctx.max_live <= 1
    assert ctx.device.control_frames() == [] and ctx.device.controls == []
    assert set(ctx.device.commands) <= READ_ONLY_CMDS


# --------------------------------------------------------------------------


@pytest.mark.parametrize("crash", [None, MigrationPhase.OLD_DRAINED, MigrationPhase.DEVICE_MOVED,
                                   MigrationPhase.OWNED_CONFIGURED, MigrationPhase.VERIFIED])
async def test_migration_every_phase_restart(mig, crash):
    """Power loss before each journal write: startup fences, recovery is forward."""
    ctx = await legacy(mig)
    real_advance = MigrationJournal.async_advance
    crashed = []

    async def advance(self, expected, record):
        if record.phase is crash and not crashed:
            crashed.append(record.phase)
            raise MigrationFault()  # the write never happened
        return await real_advance(self, expected, record)
    ctx.mp.setattr(MigrationJournal, "async_advance", advance)

    status = await prepare(ctx)
    if crash is MigrationPhase.OLD_DRAINED:
        assert status is S.REFUSED and (await journal(ctx)).phase is MigrationPhase.PREPARED
    else:
        assert status is S.RESTART_REQUIRED
    assert ctx.sources == []  # never constructed in the disabling process
    assert ctx.src.disabled_by is ConfigEntryDisabler.USER
    assert ctx.src.state is ConfigEntryState.NOT_LOADED
    for _ in range(4):
        await restart(ctx)
        record = await journal(ctx)
        if record.phase is MigrationPhase.VERIFIED and record.verified_marker is not None \
                and ctx.entry.state is ConfigEntryState.LOADED:
            break
        # An unfinished journal fences startup before any hub or source.
        assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
        assert ctx.entry.reason == "migration_pending"
        assert all(not source.is_current() for source in ctx.sources)
        status = await resume(ctx)
        assert status in (S.COMPLETE, S.RESTART_REQUIRED, S.FORWARD_REPAIR_REQUIRED)
        if status is S.COMPLETE:
            await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
            break
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert (await journal(ctx)).phase is MigrationPhase.VERIFIED
    assert leaf_owner(ctx) == ctx.entry.entry_id
    assert preserved(ctx) == ctx.petsafe
    assert ctx.entry.data[CONF_CONNECTION_BACKEND] == BACKEND_OWNED
    assert ctx.entry.runtime_data.snapshot.available
    assert_one_owner(ctx)


async def test_disable_not_persisted_refuses_resume(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    # Same process: never resumes, whatever the observed state.
    assert await resume(ctx) is S.RESTART_REQUIRED
    assert ctx.sources == []
    # Power loss before the disabled flag reached storage: the new process
    # restores the source enabled and starts it.
    ctx.src.disabled_by = None
    await restart(ctx)
    assert ctx.src.state is ConfigEntryState.LOADED
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert await resume(ctx) is S.RESTART_REQUIRED  # repeats the checkpoint
    assert ctx.sources == [] and leaf_owner(ctx) == SRC
    assert ctx.src.disabled_by is ConfigEntryDisabler.USER
    assert (await journal(ctx)).phase is MigrationPhase.OLD_DRAINED
    await restart(ctx)
    assert await resume(ctx) is S.COMPLETE
    assert_one_owner(ctx)


async def test_config_save_lost_after_prepare_restages_and_repeats_checkpoint(mig):
    """Power loss within HA's delayed config-entries save: both the staged
    connection and the source's disabled flag are lost; only the fsynced journal
    (OLD_DRAINED) survives. The checkpoint repeats from the source entry."""
    ctx = await legacy(mig, store=True)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    assert await ctx.hass.config_entries.async_unload(ctx.entry.entry_id)
    ctx.hass.config_entries.async_update_entry(ctx.entry, data=ctx.original_data)
    ctx.src.disabled_by = None
    await restart(ctx)
    assert ctx.src.state is ConfigEntryState.LOADED
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY  # journal fences startup
    assert await resume(ctx) is S.RESTART_REQUIRED
    assert ctx.sources == [] and leaf_owner(ctx) == SRC
    assert ctx.src.disabled_by is ConfigEntryDisabler.USER
    assert ctx.src.state is ConfigEntryState.NOT_LOADED
    assert CONF_MIGRATION_STAGED_CONNECTION in ctx.entry.data
    await restart(ctx)
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert_moved(ctx)
    assert ctx.entry.data[CONF_INTENT_STORE_ID] == ctx.store_id


@pytest.mark.parametrize("refusal", ["source_unload", "petsafe_drain"])
async def test_unload_refusal_never_constructs_owned(mig, refusal):
    ctx = await legacy(mig)
    if refusal == "source_unload":
        ctx.refuse_unload = True
    else:
        hub = ctx.entry.runtime_data
        real_drain = hub.async_drain_controls
        refusing = [True]

        async def refuse():
            if refusing:
                return False
            return await real_drain()
        ctx.mp.setattr(hub, "async_drain_controls", refuse)
    assert await prepare(ctx) is S.REFUSED
    record = await journal(ctx)
    assert record.phase is MigrationPhase.PREPARED
    if refusal == "petsafe_drain":
        assert ctx.src.disabled_by is None and ctx.src.state is ConfigEntryState.LOADED
    assert await resume(ctx) in (S.REFUSED, S.RESTART_REQUIRED)
    assert ctx.sources == [] and leaf_owner(ctx) == SRC
    assert (await journal(ctx)).phase in (MigrationPhase.PREPARED, MigrationPhase.OLD_DRAINED)
    assert_one_owner(ctx)
    if refusal == "petsafe_drain":
        refusing.clear()  # the operation settled: cancellation restores legacy
        assert await cancel(ctx) is S.CANCELLED
        await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
        assert ctx.src.state is ConfigEntryState.LOADED


async def test_concurrent_migration_one_owner(mig):
    ctx = await legacy(mig)
    first, second = await asyncio.gather(
        migration.async_prepare_migration(ctx.hass, ctx.entry),
        migration.async_prepare_migration(ctx.hass, ctx.entry))
    assert sorted((first.value, second.value)) == sorted(
        (S.RESTART_REQUIRED.value, S.REFUSED.value))
    await restart(ctx)

    async def setup_retry():
        # HA's retry timer firing during the migration never admits a hub.
        await asyncio.sleep(0)
        return await integration.async_setup_entry(ctx.hass, ctx.entry)
    results = await asyncio.gather(
        migration.async_resume_migration(ctx.hass, ctx.entry),
        migration.async_resume_migration(ctx.hass, ctx.entry),
        migration.async_cancel_migration(ctx.hass, ctx.entry),
        setup_retry(), return_exceptions=True)
    assert [r for r in results[:3]].count(S.COMPLETE) == 1
    assert all(r is S.REFUSED for r in results[:3] if r is not S.COMPLETE)
    assert isinstance(results[3], Exception)
    await ctx.hass.async_block_till_done()
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert_one_owner(ctx)


async def test_registry_move_retains_leaf_and_entity_ids(mig, caplog):
    # INFO and above captured: DEBUG would (correctly) make owned admission refuse.
    caplog.set_level(logging.INFO)
    ctx = await legacy(mig, store=True)
    before = {row.unique_id: (row.entity_id, row.device_id) for row in petsafe_rows(ctx)}
    hub = await migrate(ctx)
    entry = ctx.entry
    assert hub.device_id == ctx.leaf.id
    assert [d.id for d in dr.async_entries_for_config_entry(dr.async_get(ctx.hass), entry.entry_id)] \
        == [ctx.leaf.id]
    assert {row.unique_id: (row.entity_id, row.device_id) for row in petsafe_rows(ctx)
            if row.unique_id in before} == before
    assert preserved(ctx) == ctx.petsafe
    data = entry.data
    assert data[CONF_INTENT_STORE_ID] == ctx.store_id and data[CONF_INTENT_BINDING_ID] == ctx.leaf.id
    assert data[CONF_REGISTRY_DEVICE_ID] == ctx.leaf.id and data[CONF_MIGRATED_FROM] == SRC
    assert data[CONF_SOURCE_ENTRY_ID] == SRC and CONF_MIGRATION_STAGED_CONNECTION not in data
    assert (data[CONF_DEVICE_ID], data[CONF_HOST], data[CONF_PROTOCOL_VERSION]) == (DEVICE, HOST, "3.5")
    assert data[CONF_LOCAL_KEY] == KEY
    # The old source is retained, disabled, for forward repair.
    assert ctx.src.disabled_by is ConfigEntryDisabler.USER
    assert ctx.hass.config_entries.async_get_entry(SRC) is ctx.src
    # No credential in logs or the journal file.
    assert KEY not in caplog.text
    journal_dir = Path(ctx.root) / "petsafe_freshfeed_migrations"
    for path in journal_dir.iterdir():
        assert KEY.encode() not in path.read_bytes()
    assert_one_owner(ctx)


async def test_migration_preserves_user_disabled_entities(mig):
    ctx = await legacy(mig)
    registry = er.async_get(ctx.hass)
    chosen = ctx.petsafe[f"{ctx.entry.entry_id}_history"]
    registry.async_update_entity(chosen, disabled_by=er.RegistryEntryDisabler.USER)
    await migrate(ctx)
    assert registry.async_get(chosen).disabled_by is er.RegistryEntryDisabler.USER
    others = [row for row in petsafe_rows(ctx) if row.entity_id != chosen]
    assert others and all(row.disabled_by is None for row in others)


async def test_source_disable_does_not_leave_petsafe_entities_disabled(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    # The checkpoint disables the old owner's device; PetSafe rows stay enabled.
    device = dr.async_get(ctx.hass).async_get(ctx.leaf.id)
    assert device.disabled_by is dr.DeviceEntryDisabler.CONFIG_ENTRY
    await restart(ctx)
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert dr.async_get(ctx.hass).async_get(ctx.leaf.id).disabled_by is None
    assert all(row.disabled_by is None for row in petsafe_rows(ctx))
    await ctx.hass.async_block_till_done()
    status = ctx.hass.states.get(ctx.petsafe[f"{ctx.entry.entry_id}_status"])
    assert status is not None and status.state not in ("unavailable", "unknown")


async def test_source_removal_keeps_petsafe_leaf(mig):
    ctx = await legacy(mig)
    await migrate(ctx)
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert await migration.async_removal_eligible(ctx.hass, ctx.entry)
    assert await migration.async_remove_source(ctx.hass, ctx.entry) is S.COMPLETE
    await ctx.hass.async_block_till_done()
    assert ctx.hass.config_entries.async_get_entry(SRC) is None
    device = dr.async_get(ctx.hass).async_get(ctx.leaf.id)
    assert device is not None and device.config_entry_id == ctx.entry.entry_id
    assert preserved(ctx) == ctx.petsafe
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert ctx.entry.runtime_data.device_id == ctx.leaf.id
    assert_one_owner(ctx)


async def test_move_retires_raw_entities_only(mig):
    ctx = await legacy(mig)
    entities = er.async_get(ctx.hass)
    # A source entity on no device is not part of the leaf move.
    loose = entities.async_get_or_create("sensor", "tuya_local", "loose-raw", config_entry=ctx.src)
    preview = await migration.async_preflight_migration(ctx.hass, ctx.entry)
    assert preview.eligible and preview.reason is PreviewReason.ELIGIBLE
    assert preview.retiring_entity_ids == tuple(sorted(ctx.raw.values()))
    assert KEY not in repr(preview) and HOST not in repr(preview)
    assert await prepare(ctx, acknowledged=preview.retiring_entity_ids) is S.RESTART_REQUIRED
    assert (await journal(ctx)).retiring == preview.retiring_entity_ids
    await restart(ctx)
    assert await resume(ctx) is S.COMPLETE
    for entity_id in ctx.raw.values():
        assert entities.async_get(entity_id) is None
    assert entities.async_get(loose.entity_id) is not None
    assert preserved(ctx) == ctx.petsafe


async def test_prepare_refuses_changed_acknowledgement(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx, acknowledged=tuple(sorted(ctx.raw.values()))[1:]) is S.REFUSED
    assert await journal(ctx) is None and ctx.src.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("case", ["moved_before_journal", "config_lost_after_verified",
                                  "journal_ahead_of_registry"])
async def test_cross_store_crash_facts_override_phase(mig, case):
    ctx = await legacy(mig)
    if case == "config_lost_after_verified":
        await migrate(ctx)
        assert await ctx.hass.config_entries.async_unload(ctx.entry.entry_id)
        # The PetSafe entry's saved owned fields were lost; the journal says VERIFIED.
        ctx.hass.config_entries.async_update_entry(ctx.entry, data=ctx.original_data)
        await restart(ctx)
        assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
        assert await resume(ctx) is S.COMPLETE  # forward repair, key re-read from the source
        await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
        assert ctx.entry.data[CONF_CONNECTION_BACKEND] == BACKEND_OWNED
        assert leaf_owner(ctx) == ctx.entry.entry_id
        # The repairing process is not the later process removal requires.
        assert not await migration.async_removal_eligible(ctx.hass, ctx.entry)
        await restart(ctx)
        assert ctx.entry.state is ConfigEntryState.LOADED
        assert await migration.async_removal_eligible(ctx.hass, ctx.entry)
        assert_one_owner(ctx)
        return
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    registry = dr.async_get(ctx.hass)
    record = await journal(ctx)
    if case == "moved_before_journal":
        # The registry move persisted; the DEVICE_MOVED journal write did not.
        registry.async_update_device(ctx.leaf.id, new_config_entry_id=ctx.entry.entry_id)
        await ctx.hass.async_block_till_done()
        await restart(ctx)
        assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
        assert await resume(ctx) is S.COMPLETE
        assert (await journal(ctx)).phase is MigrationPhase.VERIFIED
        assert_one_owner(ctx)
        return
    # Journal claims DEVICE_MOVED, but the actual leaf is still source-owned:
    # the fsynced journal write landed, the delayed registry save did not.
    await journal_ahead(ctx, record, MigrationPhase.DEVICE_MOVED)
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert await cancel(ctx) is S.REFUSED  # forward-only: never back to legacy
    assert leaf_owner(ctx) == SRC and ctx.sources == []
    assert await resume(ctx) is S.COMPLETE  # the move is redone forward
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert_moved(ctx)


async def journal_ahead(ctx, record, phase):
    """Persist journal phases up to `phase` (and, past DEVICE_MOVED, the owned
    configuration) while the actual registry leaf stays source-owned."""
    from dataclasses import replace
    store = MigrationJournal(ctx.hass.config.config_dir, ctx.entry.entry_id)
    order = (MigrationPhase.DEVICE_MOVED, MigrationPhase.OWNED_CONFIGURED, MigrationPhase.VERIFIED)
    for step in order[:order.index(phase) + 1]:
        marker = str(uuid4()) if step is MigrationPhase.VERIFIED else None
        nxt = replace(record, phase=step, revision=record.revision + 1, verified_marker=marker)
        await store.async_advance(record.phase, nxt)
        record = nxt
        if step is MigrationPhase.DEVICE_MOVED and phase is not step:
            migration._configure(ctx.hass, ctx.entry, record, migration._connection_from(
                ctx.entry.data[CONF_MIGRATION_STAGED_CONNECTION]))
    return record


def assert_moved(ctx):
    assert leaf_owner(ctx) == ctx.entry.entry_id
    assert preserved(ctx) == ctx.petsafe
    entities = er.async_get(ctx.hass)
    assert all(entities.async_get(entity_id) is None for entity_id in ctx.raw.values())
    assert ctx.entry.data[CONF_CONNECTION_BACKEND] == BACKEND_OWNED
    assert ctx.entry.data[CONF_INTENT_BINDING_ID if CONF_INTENT_BINDING_ID in ctx.entry.data
                          else CONF_REGISTRY_DEVICE_ID] == ctx.leaf.id
    assert ctx.entry.runtime_data.device_id == ctx.leaf.id
    assert_one_owner(ctx)


@pytest.mark.parametrize("phase", [MigrationPhase.OWNED_CONFIGURED, MigrationPhase.VERIFIED])
async def test_journal_and_config_ahead_of_registry_repair_forward(mig, phase):
    """Power loss after the journal and config entries saved but before the
    delayed registry save: fenced, never cancellable, the move is redone forward,
    and source removal still needs a later process than the one that moved."""
    ctx = await legacy(mig, store=True)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    await journal_ahead(ctx, await journal(ctx), phase)
    assert ctx.entry.data[CONF_CONNECTION_BACKEND] == BACKEND_OWNED
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert ctx.entry.reason == "migration_pending"
    assert ctx.sources == [] and leaf_owner(ctx) == SRC
    assert await migration.async_controls_fenced(ctx.hass, ctx.entry)
    assert not await migration.async_removal_eligible(ctx.hass, ctx.entry)
    assert await migration.async_options_choices(ctx.hass, ctx.entry) == ("resume",)
    assert await cancel(ctx) is S.REFUSED
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert_moved(ctx)
    assert ctx.entry.data[CONF_INTENT_STORE_ID] == ctx.store_id
    assert ctx.entry.data[CONF_INTENT_BINDING_ID] == ctx.leaf.id
    assert (await journal(ctx)).phase is MigrationPhase.VERIFIED
    # The repaired move is re-observed only by a later process.
    assert not await migration.async_removal_eligible(ctx.hass, ctx.entry)
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert await migration.async_removal_eligible(ctx.hass, ctx.entry)


async def test_leaf_moved_but_raw_rows_not_persisted_repairs_forward(mig):
    """Device registry saved the move, the entity registry did not: the
    acknowledged raw rows linger on the moved leaf after restart."""
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    record = await journal(ctx)
    entities = er.async_get(ctx.hass)
    ctx.mp.setattr(migration, "_SETTLE_TIMEOUT", 0.05)
    dr.async_get(ctx.hass).async_update_device(ctx.leaf.id, new_config_entry_id=ctx.entry.entry_id)
    await ctx.hass.async_block_till_done()
    assert all(entities.async_get(e) is None for e in ctx.raw.values())
    # Restored from the stale entity-registry file: the former owner's raw rows.
    for dp, (platform, _state) in RAW_DPS.items():
        ctx.hass.states.async_remove(ctx.raw[dp])
        row = entities.async_get_or_create(
            platform, "tuya_local", f"{DEVICE}-{platform}_dp_{dp}_raw",
            suggested_object_id=f"petsafe_freshfeed_dp_{dp}_raw", config_entry=ctx.src,
            device_id=ctx.leaf.id)
        assert row.entity_id == ctx.raw[dp]
    await journal_ahead(ctx, record, MigrationPhase.DEVICE_MOVED)
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert_moved(ctx)


async def test_unknown_intent_survives_handoff(mig):
    from custom_components.petsafe_freshfeed.store_bootstrap import open_store
    operation_id = uuid4().hex
    ctx = await legacy(mig, store=True, retained=operation_id)
    hub = await migrate(ctx)
    await until(lambda: hub.adapter is not None)
    view = hub.adapter.view
    assert view["state"] == "uncertain" and view["operation_id"] == operation_id
    ctx.device.push_status({"4": "standby", "145": 3, "140": "EAAA", "142": 0})
    await until(lambda: hub.snapshot.meals_left == 3)
    await asyncio.sleep(0.1)
    assert hub.adapter.view["state"] == "uncertain"
    record = await open_store(ctx.root, binding_id=ctx.leaf.id,
                              store_id=ctx.store_id).async_load_last_intent()
    assert record["operation_id"] == operation_id and record["state"] == "pending"
    assert_one_owner(ctx)


@pytest.mark.parametrize("same_process", [True, False])
async def test_cancel_before_move_restores_legacy(mig, same_process):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    if not same_process:
        await restart(ctx)
    assert await cancel(ctx) is S.CANCELLED
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert ctx.src.disabled_by is None and ctx.src.state is ConfigEntryState.LOADED
    assert dict(ctx.entry.data) == ctx.original_data
    assert (await journal(ctx)).phase is MigrationPhase.LEGACY_RESTORED
    assert leaf_owner(ctx) == SRC
    assert set(ctx.raw.values()) <= set(er.async_get(ctx.hass).entities)
    hub = ctx.entry.runtime_data
    assert hub.raw_entity_ids == ctx.raw and hub.device_id == ctx.leaf.id
    assert dr.async_get(ctx.hass).async_get(ctx.leaf.id).disabled_by is None
    assert ctx.sources == []
    # A cancelled (finished) journal admits the legacy entry on later starts.
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert_one_owner(ctx)


async def test_cancel_after_move_refused_without_registry_change(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    ctx.before_start = lambda count: setattr(ctx.device, "refuse", count >= 2)
    assert await resume(ctx) is S.FORWARD_REPAIR_REQUIRED
    assert leaf_owner(ctx) == ctx.entry.entry_id
    phase = (await journal(ctx)).phase
    assert await cancel(ctx) is S.REFUSED
    assert leaf_owner(ctx) == ctx.entry.entry_id
    assert (await journal(ctx)).phase is phase
    assert ctx.src.disabled_by is ConfigEntryDisabler.USER
    assert_one_owner(ctx)


@pytest.mark.parametrize("change", ["host", "key", "device_cid"])
async def test_source_fingerprint_changed_refuses_resume(mig, change):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    options = {"host": NEW_HOST} if change == "host" else (
        {"local_key": OTHER_KEY} if change == "key" else {"device_cid": "child"})
    ctx.hass.config_entries.async_update_entry(ctx.src, options=options)
    await restart(ctx)
    assert await resume(ctx) is S.REFUSED
    assert leaf_owner(ctx) == SRC and ctx.sources == []
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY


async def test_registry_collision_refuses_move(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    other = dr.async_get(ctx.hass).async_get_or_create(
        config_entry_id=ctx.entry.entry_id, identifiers={(DOMAIN, DEVICE)})
    assert await resume(ctx) is S.REFUSED
    assert leaf_owner(ctx) == SRC and ctx.sources == []
    assert set(ctx.raw.values()) <= set(er.async_get(ctx.hass).entities)
    dr.async_get(ctx.hass).async_remove_device(other.id)
    assert await resume(ctx) is S.COMPLETE


async def test_reenabled_legacy_source_fences_owned(mig):
    ctx = await legacy(mig)
    hub = await migrate(ctx)
    source = hub.bridge
    assert source is not None and source.is_current()
    # Manually re-enabling the retired source is an unsupported ownership
    # conflict: the owned hub fences and stops its own connection.
    await ctx.hass.config_entries.async_set_disabled_by(SRC, None)
    await ctx.hass.async_block_till_done()
    await until(lambda: hub.bridge is None and not source.is_current())
    assert hub.adapter is None
    await restart(ctx)
    assert ctx.src.state is ConfigEntryState.LOADED
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert all(not s.is_current() for s in ctx.sources)
    assert ctx.device.control_frames() == []


async def test_probe_failure_precedes_move_and_allows_cancel(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    ctx.device.refuse = True
    assert await resume(ctx) is S.REFUSED
    assert leaf_owner(ctx) == SRC
    assert set(ctx.raw.values()) <= set(er.async_get(ctx.hass).entities)
    assert (await journal(ctx)).phase is MigrationPhase.OLD_DRAINED
    assert ctx.live == set()
    assert await cancel(ctx) is S.CANCELLED
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert ctx.src.state is ConfigEntryState.LOADED
    assert_one_owner(ctx)


async def test_post_move_verify_failure_repairable(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    # The pre-move probe succeeds; the post-move verification cannot connect.
    ctx.before_start = lambda count: setattr(ctx.device, "refuse", count >= 2)
    assert await resume(ctx) is S.FORWARD_REPAIR_REQUIRED
    assert leaf_owner(ctx) == ctx.entry.entry_id
    assert (await journal(ctx)).phase is MigrationPhase.OWNED_CONFIGURED
    assert ctx.entry.data[CONF_CONNECTION_BACKEND] == BACKEND_OWNED
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY  # fenced until verified
    ctx.before_start = None
    ctx.device.refuse = False
    assert await resume(ctx, host=NEW_HOST) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert ctx.entry.data[CONF_HOST] == NEW_HOST
    assert leaf_owner(ctx) == ctx.entry.entry_id
    assert_one_owner(ctx)


async def test_removal_offer_requires_second_process_persistence_check(mig):
    ctx = await legacy(mig)
    await migrate(ctx)
    assert not await migration.async_removal_eligible(ctx.hass, ctx.entry)
    assert await migration.async_remove_source(ctx.hass, ctx.entry) is S.REFUSED
    assert ctx.hass.config_entries.async_get_entry(SRC) is not None
    assert "remove_source" not in await migration.async_options_choices(ctx.hass, ctx.entry)
    await restart(ctx)
    assert await migration.async_removal_eligible(ctx.hass, ctx.entry)
    assert "remove_source" in await migration.async_options_choices(ctx.hass, ctx.entry)
    # Another PetSafe entry still depending on the source blocks removal.
    other = MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE_ENTRY_ID: SRC},
                            disabled_by=ConfigEntryDisabler.USER)
    other.add_to_hass(ctx.hass)
    assert not await migration.async_removal_eligible(ctx.hass, ctx.entry)
    await ctx.hass.config_entries.async_remove(other.entry_id)
    assert await migration.async_removal_eligible(ctx.hass, ctx.entry)


async def test_owned_resolves_migrated_leaf_with_tuya_identifiers(mig):
    ctx = await legacy(mig)
    hub = await migrate(ctx)
    assert hub.device_id == ctx.leaf.id
    assert hub.device_identifiers == {("tuya_local", DEVICE)}
    registry = dr.async_get(ctx.hass)
    assert not [device for device in registry.devices
                if (DOMAIN, DEVICE) in device.identifiers]
    assert {row.device_id for row in petsafe_rows(ctx)} == {ctx.leaf.id}


async def test_migrated_unique_id_unchanged(mig):
    ctx = await legacy(mig)
    entry_id = ctx.entry.entry_id
    await migrate(ctx)
    assert ctx.entry.unique_id == SRC and ctx.entry.entry_id == entry_id
    assert ctx.hass.config_entries.async_entries(DOMAIN) == [ctx.entry]


@pytest.mark.parametrize("version", ["2026.9.4", "2026.11.0", "2027.10.1"])
async def test_migration_refused_outside_2026_10(mig, version):
    ctx = await legacy(mig)
    ctx.mp.setattr(migration, "HA_VERSION", version)
    preview = await migration.async_preflight_migration(ctx.hass, ctx.entry)
    assert not preview.eligible and preview.reason is PreviewReason.UNSUPPORTED_VERSION
    assert await migration.async_options_choices(ctx.hass, ctx.entry) == ()
    assert await prepare(ctx) is S.REFUSED
    assert await journal(ctx) is None
    assert ctx.src.state is ConfigEntryState.LOADED and dict(ctx.entry.data) == ctx.original_data
    # Prepared on the characterized version, then HA changed: no resume.
    ctx.mp.setattr(migration, "HA_VERSION", "2026.10.4")
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    ctx.mp.setattr(migration, "HA_VERSION", version)
    assert await resume(ctx) is S.REFUSED
    assert leaf_owner(ctx) == SRC and ctx.sources == []


async def test_checkpoint_entity_add_preserves_user_integration_disabled(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    entities = er.async_get(ctx.hass)
    added = {}
    for label, disabled in (("user", er.RegistryEntryDisabler.USER),
                            ("integration", er.RegistryEntryDisabler.INTEGRATION),
                            ("device", None)):
        row = entities.async_get_or_create(
            "sensor", DOMAIN, f"{ctx.entry.entry_id}_checkpoint_{label}",
            config_entry=ctx.entry, device_id=ctx.leaf.id, disabled_by=disabled)
        added[label] = row.entity_id
    # Added on the disabled device during the checkpoint, as a platform would.
    entities.async_update_entity(added["device"], disabled_by=er.RegistryEntryDisabler.DEVICE)
    await restart(ctx)
    assert await resume(ctx) is S.COMPLETE
    assert entities.async_get(added["user"]).disabled_by is er.RegistryEntryDisabler.USER
    assert entities.async_get(added["integration"]).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entities.async_get(added["device"]).disabled_by is None


@pytest.mark.parametrize("case", ["tl_disabled_same_device", "tl_ignored_same_device",
                                  "tl_other_profile_same_device", "petsafe_disabled_same_device",
                                  "petsafe_ignored_same_source", "tl_other_profile_other_device"])
async def test_duplicate_scan_disabled_ignored_other_profiles(mig, case):
    ctx = await legacy(mig)
    hass = ctx.hass
    if case.startswith("tl_"):
        MockConfigEntry(
            domain="tuya_local",
            data=source_data(type="other_profile" if "other_profile" in case else "petsafe_freshfeed_raw",
                             device_id=DEVICE if "same_device" in case else "bfotherdevice0001"),
            disabled_by=ConfigEntryDisabler.USER if "disabled" in case else None,
            source=SOURCE_IGNORE if "ignored" in case else "user").add_to_hass(hass)
    elif case == "petsafe_disabled_same_device":
        MockConfigEntry(domain=DOMAIN, unique_id=DEVICE,
                        data={CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: DEVICE},
                        disabled_by=ConfigEntryDisabler.USER).add_to_hass(hass)
    else:
        MockConfigEntry(domain=DOMAIN, data={CONF_SOURCE_ENTRY_ID: SRC},
                        source=SOURCE_IGNORE).add_to_hass(hass)
    preview = await migration.async_preflight_migration(hass, ctx.entry)
    if case == "tl_other_profile_other_device":
        assert preview.eligible
        return
    assert not preview.eligible and preview.reason is PreviewReason.AMBIGUOUS_SOURCE
    assert await prepare(ctx) is S.REFUSED
    assert await journal(ctx) is None and ctx.src.state is ConfigEntryState.LOADED


async def test_open_migration_updates_do_not_reload(mig):
    ctx = await legacy(mig)
    setups = ctx.setups
    assert await prepare(ctx) is S.RESTART_REQUIRED
    # Staging the connection during prepare did not reload the entry.
    assert ctx.setups == setups and ctx.entry.state is ConfigEntryState.LOADED
    assert CONF_MIGRATION_STAGED_CONNECTION in ctx.entry.data
    assert ctx.entry.data.get(CONF_CONNECTION_BACKEND) is None
    # Any later update while the migration is open does not reload either.
    ctx.hass.config_entries.async_update_entry(ctx.entry, title="Renamed")
    await ctx.hass.async_block_till_done()
    assert ctx.setups == setups
    await restart(ctx)
    assert ctx.setups == setups + 1  # the new process's fenced start
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    # One migration-requested reload, no listener double reload.
    assert ctx.setups == setups + 2
    # A later options update on the migrated entry uses the real listener reload.
    hub = ctx.entry.runtime_data
    result = await ctx.hass.config_entries.options.async_init(ctx.entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await ctx.hass.config_entries.options.async_configure(
        result["flow_id"], {"enable_controls": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await ctx.hass.async_block_till_done()
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED
                and ctx.entry.runtime_data is not hub)
    assert ctx.setups == setups + 3
    assert ctx.entry.data[CONF_INTENT_BINDING_ID] == ctx.leaf.id
    assert ctx.entry.data[CONF_CONNECTION_BACKEND] == BACKEND_OWNED
    assert_one_owner(ctx)


async def test_options_flow_drives_migration(mig):
    ctx = await legacy(mig)
    hass = ctx.hass
    options = hass.config_entries.options
    result = await options.async_init(ctx.entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["controls", "migrate"]
    result = await options.async_configure(result["flow_id"], {"next_step_id": "migrate"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "migrate"
    placeholders = result["description_placeholders"]
    assert placeholders["retiring"] == ", ".join(sorted(ctx.raw.values()))
    assert KEY not in str(result) and HOST not in str(result)
    result = await options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "migration_restart_required"
    await hass.async_block_till_done()
    await restart(ctx)
    result = await options.async_init(ctx.entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["resume", "cancel"]
    result = await options.async_configure(result["flow_id"], {"next_step_id": "resume"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "resume"
    result = await options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "migration_complete"
    await hass.async_block_till_done()
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    # Verified in this process: only the ordinary controls form, no removal yet.
    result = await options.async_init(ctx.entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    options.async_abort(result["flow_id"])
    await restart(ctx)
    result = await options.async_init(ctx.entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["controls", "remove_source"]
    result = await options.async_configure(result["flow_id"], {"next_step_id": "remove_source"})
    result = await options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "migration_complete"
    assert hass.config_entries.async_get_entry(SRC) is None
    assert_one_owner(ctx)


async def test_options_host_correction_for_hostname_auto(mig):
    ctx = await legacy(mig, src=source_data(host="feeder.local", protocol_version="auto"))
    preview = await migration.async_preflight_migration(ctx.hass, ctx.entry)
    assert not preview.eligible and preview.reason is PreviewReason.UNSUPPORTED_SHAPE
    options = ctx.hass.config_entries.options
    result = await options.async_init(ctx.entry.entry_id)
    assert result["menu_options"] == ["controls", "migrate"]
    result = await options.async_configure(result["flow_id"], {"next_step_id": "migrate"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "migrate_host"
    result = await options.async_configure(result["flow_id"], {"host": "feeder.local",
                                                               "protocol_version": "3.5"})
    assert result["step_id"] == "migrate_host" and result["errors"] == {"host": "invalid_host"}
    result = await options.async_configure(result["flow_id"], {"host": HOST,
                                                               "protocol_version": "3.5"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "migrate"
    result = await options.async_configure(result["flow_id"], {})
    assert result["reason"] == "migration_restart_required"
    staged = ctx.entry.data[CONF_MIGRATION_STAGED_CONNECTION]
    assert (staged[CONF_HOST], staged[CONF_PROTOCOL_VERSION]) == (HOST, "3.5")
    await restart(ctx)
    assert await resume(ctx) is S.COMPLETE
    assert ctx.entry.data[CONF_HOST] == HOST


@pytest.mark.parametrize("shape", [{"protocol_version": 3.3}, {"device_cid": "child"},
                                   {"local_key": "short"}, {"type": "other_profile"}])
async def test_unsupported_legacy_shape_refused(mig, shape):
    ctx = await legacy(mig, src=source_data(**shape))
    preview = await migration.async_preflight_migration(ctx.hass, ctx.entry)
    assert not preview.eligible
    assert await prepare(ctx, host=HOST, protocol="3.5") is S.REFUSED
    assert await journal(ctx) is None and ctx.src.state is ConfigEntryState.LOADED


# ----- terminal journals, deleted sources and lifecycle ---------------------


async def test_restart_after_cancel_with_changed_source_host_loads_legacy(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    assert await cancel(ctx) is S.CANCELLED
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    # The user later edits the Tuya Local entry (a new feeder address).
    ctx.hass.config_entries.async_update_entry(ctx.src, options={"host": NEW_HOST})
    await ctx.hass.async_block_till_done()
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert ctx.entry.runtime_data.raw_entity_ids == ctx.raw
    assert not await migration.async_controls_fenced(ctx.hass, ctx.entry)
    assert ctx.sources == []


async def test_remigration_after_legacy_restored(mig):
    ctx = await legacy(mig, store=True)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    assert await cancel(ctx) is S.CANCELLED
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    cancelled = await journal(ctx)
    assert cancelled.phase is MigrationPhase.LEGACY_RESTORED
    preview = await migration.async_preflight_migration(ctx.hass, ctx.entry)
    assert preview.eligible and preview.reason is PreviewReason.ELIGIBLE
    assert "migrate" in await migration.async_options_choices(ctx.hass, ctx.entry)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    fresh = await journal(ctx)
    assert fresh.phase is MigrationPhase.OLD_DRAINED
    assert fresh.revision > cancelled.revision
    await restart(ctx)
    assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert_moved(ctx)
    assert ctx.entry.data[CONF_INTENT_STORE_ID] == ctx.store_id


@pytest.mark.parametrize("change", ["changed", "deleted"])
async def test_verified_loads_owned_with_source_changed_or_deleted(mig, change):
    ctx = await legacy(mig)
    await migrate(ctx)
    if change == "changed":
        ctx.hass.config_entries.async_update_entry(ctx.src, options={"host": NEW_HOST})
    else:
        assert (await ctx.hass.config_entries.async_remove(SRC))["require_restart"] is False
    await ctx.hass.async_block_till_done()
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert ctx.entry.runtime_data.device_id == ctx.leaf.id
    assert ctx.entry.runtime_data.snapshot.available
    assert preserved(ctx) == ctx.petsafe
    assert not await migration.async_controls_fenced(ctx.hass, ctx.entry)
    assert "resume" not in await migration.async_options_choices(ctx.hass, ctx.entry)
    assert_one_owner(ctx)


@pytest.mark.parametrize("config", ["owned_kept", "owned_lost"])
async def test_post_move_source_deleted_offers_forward_repair(mig, config):
    ctx = await legacy(mig, store=True)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    # The move succeeds; the post-move verification cannot connect.
    ctx.before_start = lambda count: setattr(ctx.device, "refuse", count >= 2)
    assert await resume(ctx) is S.FORWARD_REPAIR_REQUIRED
    assert (await journal(ctx)).phase is MigrationPhase.OWNED_CONFIGURED
    assert leaf_owner(ctx) == ctx.entry.entry_id
    if config == "owned_lost":
        # The PetSafe entry's owned fields were lost (delayed config save).
        ctx.hass.config_entries.async_update_entry(ctx.entry, data=ctx.original_data)
    await ctx.hass.config_entries.async_remove(SRC)
    await ctx.hass.async_block_till_done()
    assert leaf_owner(ctx) == ctx.entry.entry_id  # the moved leaf stays
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.SETUP_RETRY
    assert ctx.entry.reason == "migration_pending"
    assert await migration.async_controls_fenced(ctx.hass, ctx.entry)
    assert await migration.async_options_choices(ctx.hass, ctx.entry) == ("resume",)
    assert await cancel(ctx) is S.REFUSED  # never back
    ctx.before_start = None
    ctx.device.refuse = False
    if config == "owned_lost":
        # Without the source nothing supplies the key: forward repair asks for it.
        assert await resume(ctx) is S.FORWARD_REPAIR_REQUIRED
        assert leaf_owner(ctx) == ctx.entry.entry_id
        assert await resume(ctx, host=HOST, local_key=KEY) is S.COMPLETE
    else:
        assert await resume(ctx) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert leaf_owner(ctx) == ctx.entry.entry_id
    assert ctx.entry.data[CONF_CONNECTION_BACKEND] == BACKEND_OWNED
    assert ctx.entry.data[CONF_LOCAL_KEY] == KEY
    assert ctx.entry.data[CONF_INTENT_STORE_ID] == ctx.store_id
    assert ctx.entry.data[CONF_INTENT_BINDING_ID] == ctx.leaf.id
    assert preserved(ctx) == ctx.petsafe
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert_one_owner(ctx)


async def test_post_move_changed_source_is_not_trusted(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    ctx.before_start = lambda count: setattr(ctx.device, "refuse", count >= 2)
    assert await resume(ctx) is S.FORWARD_REPAIR_REQUIRED
    ctx.hass.config_entries.async_update_entry(ctx.entry, data=ctx.original_data)
    # The retained source changed after the journal fixed its fingerprint:
    # its connection material is no longer what the journal acknowledged.
    ctx.hass.config_entries.async_update_entry(ctx.src, options={"host": NEW_HOST})
    await restart(ctx)
    ctx.before_start = None
    ctx.device.refuse = False
    assert await migration.async_options_choices(ctx.hass, ctx.entry) == ("resume",)
    assert await resume(ctx) is S.FORWARD_REPAIR_REQUIRED
    assert CONF_LOCAL_KEY not in ctx.entry.data
    assert await resume(ctx, host=HOST, local_key=KEY) is S.COMPLETE
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert (ctx.entry.data[CONF_LOCAL_KEY], ctx.entry.data[CONF_HOST]) == (KEY, HOST)


@pytest.mark.parametrize("terminal", ["verified", "legacy_restored"])
async def test_terminal_startup_does_not_load_witness(mig, terminal):
    ctx = await legacy(mig, store=True)
    if terminal == "verified":
        await migrate(ctx)
    else:
        assert await prepare(ctx) is S.RESTART_REQUIRED
        assert await cancel(ctx) is S.CANCELLED
        await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    calls = []

    async def witness(*args):
        calls.append(args)
        return False  # an unreadable store must not fence a finished migration
    ctx.mp.setattr(migration, "_async_witness_valid", witness)
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert calls == []


async def test_setup_checks_migration_admission_once(mig):
    ctx = await legacy(mig)
    await migrate(ctx)
    calls = []
    real = migration.async_startup_admits

    async def counted(hass_, entry_):
        calls.append(entry_.entry_id)
        return await real(hass_, entry_)
    ctx.mp.setattr(migration, "async_startup_admits", counted)
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert calls == [ctx.entry.entry_id]


async def test_remove_entry_removes_migration_journal(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    assert await cancel(ctx) is S.CANCELLED
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    journal_dir = Path(ctx.root) / "petsafe_freshfeed_migrations"
    assert len(list(journal_dir.iterdir())) == 1
    await ctx.hass.config_entries.async_remove(ctx.entry.entry_id)
    await ctx.hass.async_block_till_done()
    assert list(journal_dir.iterdir()) == []
    assert await MigrationJournal(ctx.hass.config.config_dir, ctx.entry.entry_id).async_load() is None


async def test_options_resume_form_accepts_forward_repair_key(mig, caplog):
    caplog.set_level(logging.INFO)  # DEBUG would (correctly) refuse owned admission
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await restart(ctx)
    ctx.before_start = lambda count: setattr(ctx.device, "refuse", count >= 2)
    assert await resume(ctx) is S.FORWARD_REPAIR_REQUIRED
    ctx.hass.config_entries.async_update_entry(ctx.entry, data=ctx.original_data)
    await ctx.hass.config_entries.async_remove(SRC)
    await restart(ctx)
    ctx.before_start = None
    ctx.device.refuse = False
    options = ctx.hass.config_entries.options
    result = await options.async_init(ctx.entry.entry_id)
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["resume"]
    result = await options.async_configure(result["flow_id"], {"next_step_id": "resume"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "resume"
    assert {str(key) for key in result["data_schema"].schema} == {CONF_HOST, CONF_LOCAL_KEY}
    result = await options.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_LOCAL_KEY: KEY})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "migration_complete"
    assert KEY not in str(result) and KEY not in caplog.text
    await until(lambda: ctx.entry.state is ConfigEntryState.LOADED)
    assert ctx.entry.data[CONF_LOCAL_KEY] == KEY


# --------------------------------------------------------------------------
# Retiring Tuya Local setting entities: owned replacements keep their entity IDs


async def test_setting_takeover_captures_and_reserves_only_matching_rows(hass):
    source = MockConfigEntry(domain="tuya_local", entry_id="source")
    entry = MockConfigEntry(domain=DOMAIN, entry_id="owned")
    source.add_to_hass(hass)
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    leaf = devices.async_get_or_create(config_entry_id=source.entry_id,
                                        identifiers={("tuya_local", DEVICE)})
    registry = er.async_get(hass)
    row = registry.async_get_or_create("switch", "tuya_local", f"{DEVICE}-switch_meal_counter",
                                       suggested_object_id="custom_meals", config_entry=source,
                                       device_id=leaf.id)
    retiring = (row.entity_id,)
    targets = migration._setting_targets(hass, source.entry_id, leaf.id, DEVICE, retiring)
    assert targets == (("meal_counter", 0),)
    record = SimpleNamespace(setting_targets=targets, retiring=retiring, device_id=leaf.id)
    migration._reserve_setting_targets(hass, entry, record)
    # The old row is still present: never take its registry identity.
    assert registry.async_get(row.entity_id).platform == "tuya_local"
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_meal_counter") is None
    registry.async_remove(row.entity_id)
    migration._reserve_setting_targets(hass, entry, record)
    assert registry.async_get(row.entity_id).unique_id == f"{entry.entry_id}_meal_counter"
    registry.async_update_entity(row.entity_id, new_entity_id="switch.user_choice")
    migration._reserve_setting_targets(hass, entry, record)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_meal_counter") == "switch.user_choice"


async def test_setting_takeover_does_not_replace_an_occupied_target(hass):
    entry = MockConfigEntry(domain=DOMAIN, entry_id="owned")
    entry.add_to_hass(hass)
    leaf = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                                                  identifiers={(DOMAIN, DEVICE)})
    registry = er.async_get(hass)
    other = registry.async_get_or_create("switch", "other", "other-id",
                                         suggested_object_id="claimed_meals")
    record = SimpleNamespace(setting_targets=(("meal_counter", 0),),
                             retiring=(other.entity_id,), device_id=leaf.id)
    migration._reserve_setting_targets(hass, entry, record)
    assert registry.async_get(other.entity_id).id == other.id
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_meal_counter") is None


@pytest.mark.parametrize("renamed_device", [False, True])
async def test_migration_preserves_setting_entity_ids(mig, renamed_device):
    ctx = await legacy(mig, store=True)
    add_setting_rows(ctx)
    if renamed_device:
        # Natural owned naming would now differ: only the journaled targets keep the IDs.
        dr.async_get(ctx.hass).async_update_device(ctx.leaf.id, name_by_user="Kitchen feeder")
    assert all(value is None for value in owned_setting_ids(ctx).values())
    assert await prepare(ctx) is S.RESTART_REQUIRED
    record = await journal(ctx)
    assert {role: record.retiring[index] for role, index in record.setting_targets} == TARGETS
    hub = await migrate_resumed(ctx)
    assert owned_setting_ids(ctx) == TARGETS
    entities = er.async_get(ctx.hass)
    for role, entity_id in TARGETS.items():
        row = entities.async_get(entity_id)
        assert row.platform == DOMAIN and row.config_entry_id == ctx.entry.entry_id
        assert row.device_id == ctx.leaf.id and row.unique_id == f"{ctx.entry.entry_id}_{role}"
        state = ctx.hass.states.get(entity_id)
        assert state is not None and state.state not in ("unavailable", "unknown")
    assert hub.device_id == ctx.leaf.id
    assert preserved(ctx) == ctx.petsafe
    # A later rename by the user is never undone by a restart.
    entities.async_update_entity(TARGETS["meal_counter"], new_entity_id="switch.custom_meals")
    await restart(ctx)
    assert ctx.entry.state is ConfigEntryState.LOADED
    assert owned_setting_ids(ctx)["meal_counter"] == "switch.custom_meals"
    assert_one_owner(ctx)


async def test_occupied_setting_target_is_left_alone(mig):
    ctx = await legacy(mig)
    add_setting_rows(ctx)
    dr.async_get(ctx.hass).async_update_device(ctx.leaf.id, name_by_user="Kitchen feeder")
    real_settled = migration._async_registry_settled
    occupier = []

    async def settled(hass, record):
        result = await real_settled(hass, record)
        if result and not occupier:
            # Another integration claims one target between the move and setup.
            occupier.append(er.async_get(hass).async_get_or_create(
                "switch", "other_integration", "occupier",
                suggested_object_id="petsafe_freshfeed_meal_counter"))
        return result
    ctx.mp.setattr(migration, "_async_registry_settled", settled)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    await migrate_resumed(ctx)
    row = er.async_get(ctx.hass).async_get(TARGETS["meal_counter"])
    assert row.platform == "other_integration" and row.unique_id == "occupier"
    assert row.id == occupier[0].id
    ids = owned_setting_ids(ctx)
    assert ids["meal_counter"] not in (None, TARGETS["meal_counter"])
    assert {role: ids[role] for role in TARGETS if role != "meal_counter"} == {
        role: TARGETS[role] for role in TARGETS if role != "meal_counter"}
    assert_one_owner(ctx)


async def test_fresh_owned_migration_without_setting_rows_uses_natural_names(mig):
    ctx = await legacy(mig)
    assert await prepare(ctx) is S.RESTART_REQUIRED
    assert (await journal(ctx)).setting_targets == ()
    await migrate_resumed(ctx)
    assert all(owned_setting_ids(ctx).values())
