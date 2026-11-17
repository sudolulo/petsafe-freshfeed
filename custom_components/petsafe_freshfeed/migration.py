"""Explicit, staged migration of a legacy entry to the PetSafe-owned source.

Public HA APIs only, plus the private PetSafe migration journal. Nothing here
edits HA storage directly or relies on a private flush: the journal records
intent and completed checks, and every decision is re-derived from actual HA
state (``migration_state.next_action``) rather than trusted from the phase.

Stages (each a separate, explicit call):

* ``async_preflight_migration``: inventory only. Exact selected source, its
  registry leaf, retiring raw entities; fixed reason, never a credential.
* ``async_prepare_migration`` (process A): journal PREPARED, then stage the
  validated local connection in the SAME PetSafe entry (active backend
  unchanged), drain PetSafe controls, disable the source through
  ``async_set_disabled_by`` and observe it NOT_LOADED, journal OLD_DRAINED.
  Returns RESTART_REQUIRED; no owned client is ever constructed here.
* ``async_resume_migration`` (a different process B): requires the source
  still disabled and unloaded; a transient authenticated protocol 3.5
  read-only probe, fully drained, precedes the irreversible registry move of
  the same leaf (``async_update_device(new_config_entry_id=...)``), which
  deliberately retires the former owner's raw entities. Then the owned
  connection is committed to the same entry, a transient owned start/
  snapshot/stop verifies it, and the journal reaches VERIFIED. One reload is
  requested afterwards; no normal hub runs before VERIFIED.
* ``async_cancel_migration``: only while the actual leaf is still source-owned;
  restores the legacy configuration and (if originally enabled) the source,
  after all I/O settled. The leaf is never moved back: the reverse registry
  cascade would delete PetSafe entities. After the move recovery is forward
  only (``async_resume_migration`` with an optional host correction re-reads
  the key from the retained legacy entry).
* Source removal is offered only in a later process than the one that marked
  VERIFIED, after re-observing the persisted owned configuration and leaf.

A per-entry transition (``TRANSITIONS_KEY`` shared with host reconfigure, plus
``MIGRATIONS_KEY``) serializes these calls, keeps the update listener from
reloading and keeps setup fenced while a migration call runs. Every result is a
fixed ``MigrationStatus``; no exception text, key or host is surfaced.

Limit: a user re-enabling the retired Tuya Local entry is an unsupported
ownership conflict. Owned startup refuses it and a running owned hub fences
itself, but this cannot stop another integration's own connection; it is not a
universal cross-integration lock.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import contextlib
from dataclasses import dataclass, replace
import hashlib
import hmac
import json

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import migration_state, observed
from .const import (
    BACKEND_LEGACY, BACKEND_OWNED, CONF_CONNECTION_BACKEND, CONF_DEVICE_ID, CONF_HOST,
    CONF_INTENT_BINDING_ID, CONF_INTENT_STORE_ID, CONF_LOCAL_KEY, CONF_MIGRATED_FROM,
    CONF_MIGRATION_STAGED_CONNECTION, CONF_PROTOCOL_VERSION, CONF_REGISTRY_DEVICE_ID,
    CONF_SOURCE_ENTRY_ID, DOMAIN, INTENT_STORE_ROOT, OWNED_SETTING_ENTITIES, SOURCE_DOMAIN,
    SOURCE_PROFILE, TRANSITIONS_KEY,
)
from .migration_journal import MigrationJournal
from .migration_state import (
    LeafOwner, MigrationAction, MigrationFacts, MigrationFault, MigrationPhase,
    MigrationPreview, MigrationRecord, MigrationStatus, PreviewReason, cancel_allowed,
    next_action, removal_allowed, status_for, terminal_action,
)
from .owned_source import LocalConnection, LocalConnectionError, OwnedSource
from .source_lookup import binding_id, owned_leaf_valid, profile_type

# hass.data[DOMAIN] key: entry IDs inside a migration call (setup stays fenced).
MIGRATIONS_KEY = "migrations"
# The HA Core minor release whose registry/config-entry semantics were characterized.
SUPPORTED_HA_MINOR = (2026, 10)
MIGRATION_PROTOCOL = "3.5"
_RETIRING_HUBS = "retiring"  # __init__._RETIRING: failed-setup hubs still draining
_SETTLE_TIMEOUT = 5.0
# (entry ID, process marker) pairs whose registry move ran in that process.
_MOVED_IN_PROCESS: set = set()

_A = MigrationAction
_P = MigrationPhase
_S = MigrationStatus
_R = PreviewReason
_SETTLED_STATES = frozenset((ConfigEntryState.NOT_LOADED, ConfigEntryState.SETUP_RETRY,
                             ConfigEntryState.SETUP_ERROR, ConfigEntryState.MIGRATION_ERROR))


def _version_supported() -> bool:
    parts = str(HA_VERSION).split(".")
    try:
        return (int(parts[0]), int(parts[1])) == SUPPORTED_HA_MINOR
    except (IndexError, ValueError):
        return False


def _marker() -> str:
    # Read at call time: one marker per HA process (tests replace it).
    return migration_state.PROCESS_MARKER


def _label(disabler):
    return None if disabler is None else disabler.value


def _journal(hass, entry) -> MigrationJournal:
    return MigrationJournal(hass.config.config_dir, entry.entry_id)


async def _async_load(hass, entry):
    """(ok, record). A fault or invalid root is not ok; never inferred absent."""
    try:
        return True, await _journal(hass, entry).async_load()
    except MigrationFault:
        return False, None


@contextlib.asynccontextmanager
async def _transition(hass, entry):
    domain = hass.data.setdefault(DOMAIN, {})
    transitions = domain.setdefault(TRANSITIONS_KEY, set())
    migrations = domain.setdefault(MIGRATIONS_KEY, set())
    if entry.entry_id in transitions or entry.entry_id in migrations:
        yield False
        return
    transitions.add(entry.entry_id)
    migrations.add(entry.entry_id)
    try:
        yield True
    finally:
        transitions.discard(entry.entry_id)
        migrations.discard(entry.entry_id)


def in_migration(hass, entry) -> bool:
    return entry.entry_id in hass.data.get(DOMAIN, {}).get(MIGRATIONS_KEY, ())


# ----- legacy source material (the only credential reader) ------------------


def _pick(source, key):
    """Tuya Local semantics: options win over data."""
    if key in source.options:
        return source.options[key]
    return source.data.get(key)


def _protocol(value):
    """'3.5' for an explicit 3.5, 'auto' for Auto, else None (unsupported)."""
    if (type(value) is float and value == 3.5) or (type(value) is str and value == "3.5"):
        return MIGRATION_PROTOCOL
    if type(value) is str and value == "auto":
        return "auto"
    return None


@dataclass(frozen=True, slots=True)
class _Material:
    connection: LocalConnection | None
    correctable: bool


def _material(source, host=None, protocol=None) -> _Material:
    """Validated local connection from the selected legacy entry, or why not.

    Reads only device_id, host, local_key, protocol_version (options over
    data). A subdevice, another profile or drifted shape is refused; a hostname
    or Auto protocol is correctable only by an explicit RFC 1918 host and
    protocol 3.5 selection, never by automatic downgrade.
    """
    if source is None or source.domain != SOURCE_DOMAIN or profile_type(source) != SOURCE_PROFILE:
        return _Material(None, False)
    if _pick(source, "device_cid") not in (None, ""):
        return _Material(None, False)
    device_id, key = _pick(source, CONF_DEVICE_ID), _pick(source, CONF_LOCAL_KEY)
    raw_host, raw_protocol = _pick(source, CONF_HOST), _protocol(_pick(source, CONF_PROTOCOL_VERSION))
    try:
        LocalConnection(device_id, "10.0.0.1", key, MIGRATION_PROTOCOL)
    except LocalConnectionError:
        return _Material(None, False)  # device ID or key itself is invalid
    if type(raw_host) is not str or not raw_host or raw_protocol is None:
        return _Material(None, False)
    if protocol is not None and protocol != MIGRATION_PROTOCOL:
        return _Material(None, False)
    chosen = MIGRATION_PROTOCOL if protocol is not None else raw_protocol
    try:
        connection = LocalConnection(device_id, host if host is not None else raw_host, key, chosen)
    except LocalConnectionError:
        return _Material(None, True)
    return _Material(connection, True)


def _source_digest(source) -> str:
    """SHA-256 of the selected source's non-secret identity fields (never the key)."""
    document = {"domain": source.domain, "entry_id": source.entry_id}
    for key in ("type", CONF_DEVICE_ID, CONF_HOST, CONF_PROTOCOL_VERSION, "device_cid"):
        value = _pick(source, key)
        document[key] = value if value is None or type(value) in (str, int, float, bool) \
            else "<" + type(value).__name__ + ">"
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _same_secret(left, right) -> bool:
    return (type(left) is str and type(right) is str
            and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8")))


def _staged(connection) -> dict:
    return {CONF_DEVICE_ID: connection.device_id, CONF_HOST: connection.host,
            CONF_LOCAL_KEY: connection.local_key, CONF_PROTOCOL_VERSION: connection.version}


def _connection_from(mapping, host=None, protocol=None):
    if not isinstance(mapping, Mapping):
        return None
    if protocol is not None and protocol != MIGRATION_PROTOCOL:
        return None
    try:
        return LocalConnection(mapping.get(CONF_DEVICE_ID),
                               host if host is not None else mapping.get(CONF_HOST),
                               mapping.get(CONF_LOCAL_KEY),
                               protocol if protocol is not None else mapping.get(CONF_PROTOCOL_VERSION))
    except LocalConnectionError:
        return None


# ----- registry inspection ----------------------------------------------------


def _physical_ids(entry) -> set:
    found = set()
    for container in (entry.data, entry.options, entry.data.get(CONF_MIGRATION_STAGED_CONNECTION)):
        if isinstance(container, Mapping):
            value = container.get(CONF_DEVICE_ID)
            if type(value) is str and value:
                found.add(value)
    return found


def _ambiguous(hass, entry, source, physical, leaf_id) -> bool:
    """Any other owner candidate for this feeder or source, in any state."""
    for other in hass.config_entries.async_entries(SOURCE_DOMAIN, include_ignore=True,
                                                   include_disabled=True):
        if other.entry_id != source.entry_id and physical in _physical_ids(other):
            return True
    for ours in hass.config_entries.async_entries(DOMAIN, include_ignore=True,
                                                  include_disabled=True):
        if ours.entry_id == entry.entry_id:
            continue
        if (ours.data.get(CONF_SOURCE_ENTRY_ID) == source.entry_id
                or ours.unique_id in (physical, source.entry_id)
                or physical in _physical_ids(ours)):
            return True
    identities = {(SOURCE_DOMAIN, physical), (DOMAIN, physical)}
    for device in dr.async_get(hass).devices:
        if device.id != leaf_id and identities & set(device.identifiers):
            return True
    return False


def _retiring(hass, source_entry_id, leaf_id) -> tuple:
    """The former owner's entities on the leaf: what the registry move removes."""
    rows = er.async_entries_for_device(er.async_get(hass), leaf_id, include_disabled_entities=True)
    return tuple(sorted(row.entity_id for row in rows if row.config_entry_id == source_entry_id))


def _setting_targets(hass, source_entry_id, leaf_id, physical_id, retiring) -> tuple:
    """Capture only exact profile identities from the acknowledged leaf inventory."""
    registry = er.async_get(hass)
    targets = []
    for role, (domain, _dp, suffix) in OWNED_SETTING_ENTITIES.items():
        entity_id = registry.async_get_entity_id(domain, SOURCE_DOMAIN, physical_id + suffix)
        row = registry.async_get(entity_id) if entity_id else None
        if (row is not None and row.config_entry_id == source_entry_id
                and row.device_id == leaf_id and entity_id in retiring):
            targets.append((role, retiring.index(entity_id)))
    return tuple(sorted(targets))


def _reserve_setting_targets(hass, entry, record):
    """Reserve vacant acknowledged IDs once, without renaming an existing owned row."""
    registry = er.async_get(hass)
    for role, index in record.setting_targets:
        domain = OWNED_SETTING_ENTITIES[role][0]
        target = record.retiring[index]
        unique_id = f"{entry.entry_id}_{role}"
        if (registry.async_get_entity_id(domain, DOMAIN, unique_id) is not None
                or registry.async_get(target) is not None or hass.states.get(target) is not None):
            continue
        registry.async_get_or_create(domain, DOMAIN, unique_id,
                                     suggested_object_id=target.split(".", 1)[1],
                                     config_entry=entry, device_id=record.device_id)


def _petsafe_labels(hass, entry) -> tuple:
    rows = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return tuple(sorted((row.entity_id, _label(row.disabled_by)) for row in rows))


def _store_ids(entry):
    data = entry.data
    if CONF_INTENT_STORE_ID not in data and CONF_INTENT_BINDING_ID not in data:
        return None
    return data.get(CONF_INTENT_STORE_ID), data.get(CONF_INTENT_BINDING_ID)


def _hub_state(hass, entry):
    """None when quiet; else a fixed PreviewReason for an active/faulted adapter."""
    if entry.entry_id in hass.data.get(DOMAIN, {}).get(_RETIRING_HUBS, {}):
        return _R.OPERATION_ACTIVE
    if entry.state is not ConfigEntryState.LOADED:
        return None if entry.state in _SETTLED_STATES else _R.OPERATION_ACTIVE
    adapter = getattr(getattr(entry, "runtime_data", None), "adapter", None)
    if adapter is None:
        return None
    try:
        view = adapter.view
    except Exception:  # noqa: BLE001 - unknown shape: refuse
        return _R.STORAGE_UNAVAILABLE
    if view.get("storage_fault") or view.get("integrity_fault"):
        return _R.STORAGE_UNAVAILABLE
    if view.get("state") == "pending":
        return _R.OPERATION_ACTIVE
    return None


@dataclass(frozen=True, slots=True)
class _Inspection:
    reason: PreviewReason
    correctable: bool = False
    source: object = None
    leaf_id: str | None = None
    connection: LocalConnection | None = None
    retiring: tuple = ()
    previous: MigrationRecord | None = None  # a finished cancellation to renew


async def _async_inspect(hass, entry, host, protocol, *, held=False) -> _Inspection:
    if not _version_supported():
        return _Inspection(_R.UNSUPPORTED_VERSION)
    data = entry.data
    if (data.get(CONF_CONNECTION_BACKEND, BACKEND_LEGACY) != BACKEND_LEGACY
            or CONF_MIGRATED_FROM in data or CONF_MIGRATION_STAGED_CONNECTION in data
            or (in_migration(hass, entry) and not held)):
        return _Inspection(_R.ALREADY_MIGRATING)
    ok, record = await _async_load(hass, entry)
    if not ok:
        return _Inspection(_R.STORAGE_UNAVAILABLE)
    if record is not None and record.phase is not _P.LEGACY_RESTORED:
        return _Inspection(_R.ALREADY_MIGRATING)
    source_id = data.get(CONF_SOURCE_ENTRY_ID)
    source = hass.config_entries.async_get_entry(source_id) if type(source_id) is str else None
    if source is None or source.domain != SOURCE_DOMAIN:
        return _Inspection(_R.AMBIGUOUS_SOURCE)
    material = _material(source, host, protocol)
    if material.connection is None:
        return _Inspection(_R.UNSUPPORTED_SHAPE, material.correctable)
    connection = material.connection
    leaf_id = binding_id(hass, source.entry_id)
    if leaf_id is None or _ambiguous(hass, entry, source, connection.device_id, leaf_id):
        return _Inspection(_R.AMBIGUOUS_SOURCE)
    ids = _store_ids(entry)
    if ids is not None and ids[1] != leaf_id:
        return _Inspection(_R.AMBIGUOUS_SOURCE)
    if dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        return _Inspection(_R.AMBIGUOUS_SOURCE)  # the move would collide
    busy = _hub_state(hass, entry)
    if busy is not None:
        return _Inspection(busy)
    return _Inspection(_R.ELIGIBLE, True, source, leaf_id, connection,
                       _retiring(hass, source.entry_id, leaf_id), record)


def _preview(inspection) -> MigrationPreview:
    eligible = inspection.reason is _R.ELIGIBLE
    try:
        return MigrationPreview(eligible, inspection.reason,
                                inspection.retiring if eligible else ())
    except MigrationFault:
        # An inventory beyond the bounds is refused, never truncated.
        return MigrationPreview(False, _R.UNSUPPORTED_SHAPE, ())


async def async_preflight_migration(hass, entry, *, host=None, protocol=None) -> MigrationPreview:
    """Redacted inventory of the exact selected source and its retiring raw entities."""
    return _preview(await _async_inspect(hass, entry, host, protocol))


async def async_migration_correctable(hass, entry) -> bool:
    """The legacy shape is unsupported only by host/protocol, fixable explicitly."""
    inspection = await _async_inspect(hass, entry, None, None)
    return inspection.reason is _R.UNSUPPORTED_SHAPE and inspection.correctable


# ----- actual facts -------------------------------------------------------------


def _leaf_owner(hass, entry, record) -> LeafOwner:
    device = dr.async_get(hass).async_get(record.device_id)
    if device is None or device.id != record.device_id or device.config_subentry_id is not None:
        return LeafOwner.OTHER
    if device.config_entry_id == record.source_entry_id:
        return LeafOwner.SOURCE
    if device.config_entry_id == entry.entry_id:
        return LeafOwner.PETSAFE
    return LeafOwner.OTHER


def _owned_config_valid(hass, entry, record) -> bool:
    data = entry.data
    if (data.get(CONF_CONNECTION_BACKEND) != BACKEND_OWNED
            or data.get(CONF_REGISTRY_DEVICE_ID) != record.device_id
            or data.get(CONF_MIGRATED_FROM) != record.source_entry_id
            or CONF_MIGRATION_STAGED_CONNECTION in data):
        return False
    connection = _connection_from(data)
    if connection is None:
        return False
    device = dr.async_get(hass).async_get(record.device_id)
    return owned_leaf_valid(device, entry.entry_id, connection.device_id)


async def _async_witness_valid(hass, entry, record) -> bool:
    """The existing store opens with its persisted identity, on the unchanged leaf.

    Read-only: loads the last intent and waits idle; never prepares, clears or
    provisions anything.
    """
    ids = _store_ids(entry)
    if ids is None:
        return True
    store_id, binding = ids
    if type(store_id) is not str or binding != record.device_id:
        return False
    from .store_bootstrap import open_store
    try:
        witness = open_store(hass.config.path(INTENT_STORE_ROOT), binding_id=binding,
                             store_id=store_id)
        await witness.async_load_last_intent()
        return await witness.async_wait_idle(_SETTLE_TIMEOUT) is True
    except Exception:  # noqa: BLE001 - any doubt is invalid
        return False


def _source_unchanged(hass, record) -> bool:
    """The selected source still exists with the journaled non-secret identity."""
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    return (source is not None and source.domain == SOURCE_DOMAIN
            and _source_digest(source) == record.source_config_digest)


async def _async_facts(hass, entry, record) -> MigrationFacts:
    """Actual facts. A deleted source is reported stopped with no digest.

    The witness is opened only when the reducer needs it: a finished migration
    (terminal_action) is decided without it, so a storage fault there degrades
    controls in the running hub instead of fencing startup.
    """
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    if source is None:
        # Deleted: the strongest form of stopped, and no longer a material source.
        disabled, loaded, digest = True, False, None
    elif source.domain == SOURCE_DOMAIN:
        disabled = source.disabled_by is not None
        loaded = source.state is not ConfigEntryState.NOT_LOADED
        digest = _source_digest(source)
    else:
        return MigrationFacts(process_marker=_marker())
    facts = MigrationFacts(
        process_marker=_marker(), source_disabled=disabled, source_loaded=loaded,
        leaf_owner=_leaf_owner(hass, entry, record), source_config_digest=digest,
        backend=entry.data.get(CONF_CONNECTION_BACKEND, BACKEND_LEGACY),
        owned_config_valid=_owned_config_valid(hass, entry, record))
    if terminal_action(record, facts) is not None:
        return facts
    return replace(facts, witness_valid=await _async_witness_valid(hass, entry, record))


async def async_startup_admits(hass, entry) -> bool:
    """Startup admission, before any hub or source is constructed.

    No journal: normal backend selection, unless the entry carries migration
    markers (fenced: never virgin-entry inference). A journal admits only a
    finished migration whose actual facts the reducer completes: VERIFIED with
    persisted owned configuration, or a cancellation back to legacy. Neither
    depends on the (possibly since edited or deleted) source entry's digest,
    nor on opening the witness.
    """
    if in_migration(hass, entry):
        return False
    data = entry.data
    ok, record = await _async_load(hass, entry)
    if not ok:
        return False
    if record is None:
        return CONF_MIGRATED_FROM not in data and CONF_MIGRATION_STAGED_CONNECTION not in data
    if CONF_MIGRATION_STAGED_CONNECTION in data:
        return False
    if next_action(record, await _async_facts(hass, entry, record)) is not _A.COMPLETE:
        return False
    if record.phase is _P.VERIFIED:
        return _owned_config_valid(hass, entry, record)
    if record.phase is _P.LEGACY_RESTORED:
        return (data.get(CONF_CONNECTION_BACKEND, BACKEND_LEGACY) == BACKEND_LEGACY
                and CONF_MIGRATED_FROM not in data)
    return False


async def async_controls_fenced(hass, entry) -> bool:
    """Controls provisioning is refused while a migration is open or unreadable."""
    if CONF_MIGRATION_STAGED_CONNECTION in entry.data or in_migration(hass, entry):
        return True
    ok, record = await _async_load(hass, entry)
    if not ok:
        return True
    if record is None:
        return False
    return next_action(record, await _async_facts(hass, entry, record)) is not _A.COMPLETE


# ----- shared steps -------------------------------------------------------------


async def _async_drain_petsafe(hass, entry) -> bool:
    """PetSafe controls drained: a loaded hub drains; anything unsettled refuses."""
    if entry.entry_id in hass.data.get(DOMAIN, {}).get(_RETIRING_HUBS, {}):
        return False
    if entry.state is ConfigEntryState.LOADED:
        hub = getattr(entry, "runtime_data", None)
        drain = getattr(hub, "async_drain_controls", None)
        if drain is None:
            return False
        return await drain() is True
    return entry.state in _SETTLED_STATES


async def _async_transient(hass, connection, *, require_dps) -> bool:
    """One transient owned source: authenticated read-only start, then drained stop."""
    source = OwnedSource(hass, connection)
    started = fresh = False
    try:
        await source.async_start()
        started = True
        dps = source.snapshot_dps()
        fresh = not require_dps or all(str(dp) in dps for dp in observed.REQUIRED_DPS)
    except Exception:  # noqa: BLE001 - fixed outcome only
        started = False
    stopped = await source.async_stop() is True
    return started and fresh and stopped


def _owned_claim_active(device_id) -> bool:
    from . import owned_source
    with owned_source._CLAIMS_GUARD:  # noqa: SLF001 - this package's own claim table
        return device_id in owned_source._CLAIMS  # noqa: SLF001


async def _async_advance(journal, record, phase, **changes):
    advanced = replace(record, phase=phase, revision=record.revision + 1, **changes)
    await journal.async_advance(record.phase, advanced)
    return advanced


def _request_reload(hass, entry):
    """One migration-requested reload; never awaited on this entry's setup lock."""
    if entry.disabled_by is None and hass.config_entries.async_get_entry(entry.entry_id) is entry:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def _async_checkpoint(hass, entry, journal, record) -> MigrationStatus:
    """Drain, disable the source and observe it unloaded; then a restart is required."""
    if not isinstance(entry.data.get(CONF_MIGRATION_STAGED_CONNECTION), Mapping):
        if not _restage(hass, entry, record):
            return _S.REFUSED  # cannot re-derive it: cancellation remains available
    if not await _async_drain_petsafe(hass, entry):
        return _S.REFUSED
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    if source is None:
        return _S.REFUSED
    try:
        await hass.config_entries.async_set_disabled_by(source.entry_id, ConfigEntryDisabler.USER)
    except Exception:  # noqa: BLE001 - fixed outcome only
        return _S.REFUSED
    if source.disabled_by is None or source.state is not ConfigEntryState.NOT_LOADED:
        return _S.REFUSED
    try:
        await _async_advance(journal, record, _P.OLD_DRAINED, origin_marker=_marker())
    except MigrationFault:
        return _S.REFUSED
    return _S.RESTART_REQUIRED


def _restage(hass, entry, record) -> bool:
    """Re-stage from the unchanged source after a lost config-entries save.

    Only for a still-legacy entry before the move, and only from the source's
    own validated fields (a lost host/protocol override is not guessed: then
    cancellation remains the way out).
    """
    if (record.phase not in (_P.PREPARED, _P.OLD_DRAINED)
            or entry.data.get(CONF_CONNECTION_BACKEND, BACKEND_LEGACY) != BACKEND_LEGACY
            or CONF_MIGRATED_FROM in entry.data
            or entry.data.get(CONF_SOURCE_ENTRY_ID) != record.source_entry_id):
        return False
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    if (source is None or source.domain != SOURCE_DOMAIN
            or _source_digest(source) != record.source_config_digest
            or binding_id(hass, source.entry_id) != record.device_id):
        return False
    connection = _material(source).connection
    if connection is None or _ambiguous(hass, entry, source, connection.device_id,
                                        record.device_id):
        return False
    hass.config_entries.async_update_entry(entry, data={
        **entry.data, CONF_MIGRATION_STAGED_CONNECTION: _staged(connection)})
    return True


# ----- prepare ------------------------------------------------------------------


async def async_prepare_migration(hass, entry, *, host=None, protocol=None,
                                  acknowledged=None) -> MigrationStatus:
    """Journal, stage, drain and disable the old owner; never constructs the new client."""
    async with _transition(hass, entry) as held:
        if not held:
            return _S.REFUSED
        inspection = await _async_inspect(hass, entry, host, protocol, held=True)
        if inspection.reason is not _R.ELIGIBLE or _preview(inspection).eligible is not True:
            return _S.REFUSED
        if acknowledged is not None and tuple(acknowledged) != inspection.retiring:
            return _S.REFUSED  # the inventory changed since it was acknowledged
        source, previous = inspection.source, inspection.previous
        device = dr.async_get(hass).async_get(inspection.leaf_id)
        try:
            record = MigrationRecord(
                entry_id=entry.entry_id, source_entry_id=source.entry_id,
                device_id=inspection.leaf_id, source_config_digest=_source_digest(source),
                source_disabled_by=_label(source.disabled_by),
                device_disabled_by=_label(device.disabled_by),
                entity_disabled_by=_petsafe_labels(hass, entry), retiring=inspection.retiring,
                origin_marker=_marker(), phase=_P.PREPARED,
                revision=1 if previous is None else previous.revision + 1,
                setting_targets=_setting_targets(hass, source.entry_id, inspection.leaf_id,
                                                 inspection.connection.device_id,
                                                 inspection.retiring))
        except MigrationFault:
            return _S.REFUSED
        journal = _journal(hass, entry)
        try:
            if previous is None:
                await journal.async_create(record)
            else:
                # A fresh migration after a finished cancellation.
                await journal.async_renew(record)
        except MigrationFault:
            return _S.REFUSED
        # The journal now fences startup: only then stage the validated
        # connection in the same entry. The active backend is unchanged.
        hass.config_entries.async_update_entry(entry, data={
            **entry.data, CONF_MIGRATION_STAGED_CONNECTION: _staged(inspection.connection)})
        return await _async_checkpoint(hass, entry, journal, record)


# ----- resume -------------------------------------------------------------------


def _pre_move_connection(entry, source, host, protocol):
    """The staged connection (with explicit overrides), still matching the source.

    A repeated move after a lost registry save may find the owned fields
    already committed instead of the staged copy; they must match the source too.
    """
    data = entry.data
    if CONF_MIGRATION_STAGED_CONNECTION in data:
        connection = _connection_from(data.get(CONF_MIGRATION_STAGED_CONNECTION), host, protocol)
    elif data.get(CONF_CONNECTION_BACKEND) == BACKEND_OWNED:
        connection = _connection_from(data, host, protocol)
    else:
        connection = None
    if connection is None or source is None:
        return None
    if (_pick(source, CONF_DEVICE_ID) != connection.device_id
            or not _same_secret(_pick(source, CONF_LOCAL_KEY), connection.local_key)):
        return None
    return connection


def _leaf_physical(device):
    """The single physical device ID the moved leaf's identifiers name, or None."""
    found = {value for domain, value in getattr(device, "identifiers", ())
             if domain in (DOMAIN, SOURCE_DOMAIN) and type(value) is str and value}
    return found.pop() if len(found) == 1 else None


def _forward_connection(hass, entry, record, host, protocol, local_key=None):
    """Forward-repair connection, with explicit host/protocol/key overrides.

    Base: the staged connection, else the persisted owned fields, else the
    retained legacy entry only while it is unchanged since the journal (a
    changed or deleted source supplies nothing), else the moved leaf's own
    physical identifier, which needs an explicit host and key. The device ID is
    never overridden and must match the moved leaf.
    """
    device = dr.async_get(hass).async_get(record.device_id)
    base = None
    staged = entry.data.get(CONF_MIGRATION_STAGED_CONNECTION)
    if isinstance(staged, Mapping) and type(staged.get(CONF_DEVICE_ID)) is str:
        base = staged
    elif (entry.data.get(CONF_CONNECTION_BACKEND) == BACKEND_OWNED
          and type(entry.data.get(CONF_DEVICE_ID)) is str):
        base = entry.data
    elif _source_unchanged(hass, record):
        source = hass.config_entries.async_get_entry(record.source_entry_id)
        material = _material(source, host, protocol).connection
        if material is not None:
            base = _staged(material)
    if base is None:
        physical = _leaf_physical(device)
        if physical is None:
            return None
        base = {CONF_DEVICE_ID: physical, CONF_PROTOCOL_VERSION: MIGRATION_PROTOCOL}
    if local_key is not None:
        base = {**base, CONF_LOCAL_KEY: local_key}
    connection = _connection_from(base, host, protocol)
    if connection is None:
        return None
    return connection if owned_leaf_valid(device, entry.entry_id, connection.device_id) else None


def _source_rows_left(hass, record) -> bool:
    rows = er.async_entries_for_device(er.async_get(hass), record.device_id,
                                       include_disabled_entities=True)
    return any(row.config_entry_id == record.source_entry_id for row in rows)


def _petsafe_rows_survive(hass, entry, record) -> bool:
    registry = er.async_get(hass)
    for entity_id, _label_value in record.entity_disabled_by:
        row = registry.async_get(entity_id)
        if row is None or row.config_entry_id != entry.entry_id:
            return False
    return True


async def _async_registry_settled(hass, record) -> bool:
    """Await HA's registry handlers: the former owner's rows on the leaf are gone.

    If the device-registry move persisted but the entity-registry removal did
    not (separate delayed saves), no handler fires again after a restart: the
    acknowledged retiring rows are then removed through the public registry
    API, exactly as the move would have. Any unacknowledged row refuses.
    """
    loop = hass.loop
    deadline = loop.time() + _SETTLE_TIMEOUT
    while _source_rows_left(hass, record):
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.01)
    else:
        return True
    registry = er.async_get(hass)
    rows = [row for row in er.async_entries_for_device(registry, record.device_id,
                                                       include_disabled_entities=True)
            if row.config_entry_id == record.source_entry_id]
    if not {row.entity_id for row in rows} <= set(record.retiring):
        return False
    for row in rows:
        registry.async_remove(row.entity_id)
    return not _source_rows_left(hass, record)


def _source_stopped(hass, record) -> bool:
    """Synchronous re-check right before constructing an owned client."""
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    if source is None:
        return True  # deleted: it cannot run a connection any more
    return source.disabled_by is not None and source.state is ConfigEntryState.NOT_LOADED


def _moved_here(entry) -> bool:
    """This HA process performed a registry move for the entry."""
    return (entry.entry_id, _marker()) in _MOVED_IN_PROCESS


def _restore_leaf(hass, entry, record):
    """Undo migration-caused disablement only; USER/INTEGRATION are preserved."""
    devices = dr.async_get(hass)
    device = devices.async_get(record.device_id)
    if device is None:
        return
    if device.disabled_by is dr.DeviceEntryDisabler.CONFIG_ENTRY:
        # The former owner's config-entry disablement no longer applies.
        device = devices.async_update_device(device.id, disabled_by=None)
    if device is None or device.disabled_by is not None:
        return
    entities = er.async_get(hass)
    for row in er.async_entries_for_device(entities, record.device_id,
                                           include_disabled_entities=True):
        if (row.config_entry_id == entry.entry_id
                and row.disabled_by is er.RegistryEntryDisabler.DEVICE):
            entities.async_update_entity(row.entity_id, disabled_by=None)


async def _async_move(hass, entry, journal, record, host, protocol):
    """Probe, then move the same leaf. Returns (status, None) or (None, moved record)."""
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    connection = _pre_move_connection(entry, source, host, protocol)
    if connection is None:
        return _S.REFUSED, None
    devices = dr.async_get(hass)
    if (binding_id(hass, source.entry_id) != record.device_id
            or dr.async_entries_for_config_entry(devices, entry.entry_id)
            or not set(_retiring(hass, source.entry_id, record.device_id)) <= set(record.retiring)
            or _ambiguous(hass, entry, source, connection.device_id, record.device_id)):
        return _S.REFUSED, None
    # Authenticated read-only probe, closed and drained before the move.
    if not _source_stopped(hass, record):
        return _S.REFUSED, None
    if not await _async_transient(hass, connection, require_dps=False):
        return _S.REFUSED, None
    facts = await _async_facts(hass, entry, record)
    if (facts.leaf_owner is not LeafOwner.SOURCE or facts.source_disabled is not True
            or facts.source_loaded is not False or _owned_claim_active(connection.device_id)):
        return _S.REFUSED, None
    device = devices.async_get(record.device_id)
    changes = {"new_config_entry_id": entry.entry_id}
    if device.disabled_by is dr.DeviceEntryDisabler.CONFIG_ENTRY:
        changes["disabled_by"] = None
    try:
        moved = devices.async_update_device(device.id, **changes)
    except Exception:  # noqa: BLE001 - HA validates before mutating: still pre-move
        moved = None
    if _leaf_owner(hass, entry, record) is not LeafOwner.PETSAFE:
        return _S.REFUSED, None
    # Removal must be re-observed by a later process than any that moved.
    _MOVED_IN_PROCESS.add((entry.entry_id, _marker()))
    if moved is None or moved.id != record.device_id:
        return _S.FORWARD_REPAIR_REQUIRED, None
    if not await _async_registry_settled(hass, record):
        return _S.FORWARD_REPAIR_REQUIRED, None
    if record.phase is not _P.OLD_DRAINED:
        # A repeated move after the journal got ahead of a lost registry save.
        return None, record
    try:
        return None, await _async_advance(journal, record, _P.DEVICE_MOVED)
    except MigrationFault:
        return _S.FORWARD_REPAIR_REQUIRED, None


def _configure(hass, entry, record, connection):
    data = {key: value for key, value in entry.data.items()
            if key != CONF_MIGRATION_STAGED_CONNECTION}
    data.update({
        CONF_CONNECTION_BACKEND: BACKEND_OWNED, CONF_DEVICE_ID: connection.device_id,
        CONF_HOST: connection.host, CONF_LOCAL_KEY: connection.local_key,
        CONF_PROTOCOL_VERSION: connection.version, CONF_REGISTRY_DEVICE_ID: record.device_id,
        CONF_MIGRATED_FROM: record.source_entry_id, CONF_SOURCE_ENTRY_ID: record.source_entry_id,
    })
    hass.config_entries.async_update_entry(entry, data=data)


async def _async_forward(hass, entry, journal, record, host, protocol,
                         local_key=None) -> MigrationStatus:
    """Forward-only after the move: configure the same entry, verify, VERIFIED."""
    try:
        # Also covers a persisted leaf move whose raw-row removal was lost.
        if not await _async_registry_settled(hass, record):
            return _S.FORWARD_REPAIR_REQUIRED
        if record.phase is _P.OLD_DRAINED:
            # The registry move persisted; its journal write did not.
            record = await _async_advance(journal, record, _P.DEVICE_MOVED)
        _restore_leaf(hass, entry, record)
        if not _petsafe_rows_survive(hass, entry, record):
            return _S.FORWARD_REPAIR_REQUIRED
        _reserve_setting_targets(hass, entry, record)
        if (record.phase is _P.DEVICE_MOVED or host is not None or protocol is not None
                or local_key is not None or not _owned_config_valid(hass, entry, record)):
            connection = _forward_connection(hass, entry, record, host, protocol, local_key)
            if connection is None:
                return _S.FORWARD_REPAIR_REQUIRED
            # Flip the active backend only here, keeping store/binding IDs. A
            # repairing process is not the later process removal requires.
            _MOVED_IN_PROCESS.add((entry.entry_id, _marker()))
            _configure(hass, entry, record, connection)
            if record.phase is _P.DEVICE_MOVED:
                record = await _async_advance(journal, record, _P.OWNED_CONFIGURED)
        connection = _connection_from(entry.data)
        if connection is None or not _owned_config_valid(hass, entry, record):
            return _S.FORWARD_REPAIR_REQUIRED
        # Fresh post-move read-only verification on a transient owned source.
        if not _source_stopped(hass, record):
            return _S.FORWARD_REPAIR_REQUIRED
        if not await _async_transient(hass, connection, require_dps=True):
            return _S.FORWARD_REPAIR_REQUIRED
        if record.phase is _P.OWNED_CONFIGURED:
            record = await _async_advance(journal, record, _P.VERIFIED, verified_marker=_marker())
    except MigrationFault:
        return _S.FORWARD_REPAIR_REQUIRED
    return _S.COMPLETE if record.phase is _P.VERIFIED else _S.FORWARD_REPAIR_REQUIRED


async def async_resume_migration(hass, entry, *, host=None, protocol=None,
                                 local_key=None) -> MigrationStatus:
    """Continue in a new process; forward-only once the leaf has moved.

    ``local_key`` (with ``host``) is a forward-repair input only: it is used
    after the move, when the source entry was deleted or changed and the
    owned fields were lost. It is never used for the pre-move probe or move.
    """
    status = _S.REFUSED
    async with _transition(hass, entry) as held:
        if not held or not _version_supported():
            return _S.REFUSED
        journal = _journal(hass, entry)
        ok, record = await _async_load(hass, entry)
        if not ok or record is None:
            return _S.REFUSED
        facts = await _async_facts(hass, entry, record)
        action = next_action(record, facts)
        if action is _A.CHECKPOINT:
            return await _async_checkpoint(hass, entry, journal, record)
        if action is _A.CANCEL:
            status = await _async_finish_cancel(hass, entry, journal, record)
        elif action is _A.COMPLETE:
            return status_for(record, action)
        elif action is _A.REFUSE:
            return _S.REFUSED
        elif (entry.state not in _SETTLED_STATES
              or entry.entry_id in hass.data.get(DOMAIN, {}).get(_RETIRING_HUBS, {})):
            return _S.REFUSED  # never overlap a running PetSafe hub
        else:
            if facts.leaf_owner is LeafOwner.SOURCE:
                if local_key is not None:
                    return _S.REFUSED  # the move only uses the acknowledged source key
                status, moved = await _async_move(hass, entry, journal, record, host, protocol)
                if moved is None:
                    return status
                record = moved
            status = await _async_forward(hass, entry, journal, record, host, protocol,
                                          local_key)
    if status in (_S.COMPLETE, _S.CANCELLED):
        _request_reload(hass, entry)
    return status


# ----- cancel -------------------------------------------------------------------


async def _async_finish_cancel(hass, entry, journal, record) -> MigrationStatus:
    """Restore legacy configuration and (if originally enabled) the source."""
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    if source is None or not await _async_drain_petsafe(hass, entry):
        return _S.REFUSED
    physical = _pick(source, CONF_DEVICE_ID)
    if type(physical) is str and _owned_claim_active(physical):
        return _S.REFUSED  # an owned connection has not settled yet
    try:
        if record.phase is not _P.CANCEL_PREPARED:
            record = await _async_advance(journal, record, _P.CANCEL_PREPARED)
        if CONF_MIGRATION_STAGED_CONNECTION in entry.data:
            hass.config_entries.async_update_entry(entry, data={
                key: value for key, value in entry.data.items()
                if key != CONF_MIGRATION_STAGED_CONNECTION})
        if record.source_disabled_by is None and source.disabled_by is not None:
            try:
                await hass.config_entries.async_set_disabled_by(source.entry_id, None)
            except Exception:  # noqa: BLE001 - fixed outcome only
                return _S.REFUSED
            if source.disabled_by is not None:
                return _S.REFUSED
        await _async_advance(journal, record, _P.LEGACY_RESTORED)
    except MigrationFault:
        return _S.REFUSED
    return _S.CANCELLED


async def async_cancel_migration(hass, entry) -> MigrationStatus:
    """Cancel only while the actual leaf is still legacy-owned; no witness mutation."""
    async with _transition(hass, entry) as held:
        if not held:
            return _S.REFUSED
        ok, record = await _async_load(hass, entry)
        if not ok or record is None:
            return _S.REFUSED
        if not cancel_allowed(record, await _async_facts(hass, entry, record)):
            return _S.REFUSED  # moved (forward-only) or inconsistent: no registry change
        status = await _async_finish_cancel(hass, entry, _journal(hass, entry), record)
    if status is _S.CANCELLED:
        _request_reload(hass, entry)
    return status


# ----- removal ------------------------------------------------------------------


async def async_removal_eligible(hass, entry) -> bool:
    """Old-source removal: VERIFIED, re-observed by a later process, no dependants."""
    if _moved_here(entry):
        return False
    ok, record = await _async_load(hass, entry)
    if not ok or record is None or record.phase is not _P.VERIFIED:
        return False
    source = hass.config_entries.async_get_entry(record.source_entry_id)
    if source is None or source.disabled_by is None:
        return False
    if not removal_allowed(record, await _async_facts(hass, entry, record)):
        return False
    if not _owned_config_valid(hass, entry, record):
        return False
    for ours in hass.config_entries.async_entries(DOMAIN, include_ignore=True,
                                                  include_disabled=True):
        if ours.entry_id != entry.entry_id and ours.data.get(CONF_SOURCE_ENTRY_ID) == source.entry_id:
            return False
    return True


async def async_remove_source(hass, entry) -> MigrationStatus:
    """Remove the retired Tuya Local entry (never its package) once eligible."""
    async with _transition(hass, entry) as held:
        if not held or not await async_removal_eligible(hass, entry):
            return _S.REFUSED
        source_id = entry.data.get(CONF_MIGRATED_FROM)
        try:
            await hass.config_entries.async_remove(source_id)
        except Exception:  # noqa: BLE001 - fixed outcome only
            return _S.REFUSED
    return _S.COMPLETE


# ----- options-flow choices ---------------------------------------------------------


async def async_options_choices(hass, entry) -> tuple:
    """Applicable migration choices: migrate | resume, cancel | remove_source."""
    ok, record = await _async_load(hass, entry)
    if not ok:
        return ()
    if record is None or record.phase is _P.LEGACY_RESTORED:
        # No migration, or a finished cancellation: a fresh one may start.
        inspection = await _async_inspect(hass, entry, None, None)
        if inspection.reason in (_R.UNSUPPORTED_VERSION, _R.ALREADY_MIGRATING):
            return ()
        if inspection.reason is _R.UNSUPPORTED_SHAPE and not inspection.correctable:
            return ()
        return ("migrate",)
    facts = await _async_facts(hass, entry, record)
    choices = []
    if next_action(record, facts) is not _A.COMPLETE:
        choices.append("resume")
    if cancel_allowed(record, facts):
        choices.append("cancel")
    if await async_removal_eligible(hass, entry):
        choices.append("remove_source")
    return tuple(choices)
