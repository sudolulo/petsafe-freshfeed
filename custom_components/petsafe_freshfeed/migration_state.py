"""Pure migration value types and the conservative recovery reducer.

No Home Assistant import and no I/O. The reducer derives the next action from
typed facts about the actual source, registry leaf, configuration, witness and
HA process; the journal phase records intent and completed checks, never
atomicity across HA stores, so it never overrides an observed owner. The
reducer only names an action: it never starts a client or changes state.

PROCESS_MARKER is one fresh random UUID generated when this module is first
imported in an HA Python process. It is not derived from a PID, entry, source
or reload, so a restart checkpoint can require a marker different from the one
journaled by the process that disabled the old source. Tests simulate process
identity explicitly through MigrationFacts.process_marker.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import enum
import re
import uuid

from .const import BACKEND_LEGACY, BACKEND_OWNED, OWNED_SETTING_ENTITIES

PROCESS_MARKER = str(uuid.uuid4())

SCHEMA = 1
MAX_ID_CHARS = 128
MAX_ENTITY_ID_CHARS = 255
# Shared bounds for MigrationPreview and MigrationRecord, chosen so that every
# valid record encodes within the journal's 16 KiB load bound (worst case is
# tested): IDs escape to at most 12 bytes per character, each inventory to at
# most MAX_INVENTORY_CHARS plus 19 bytes per item. Refuse beyond; never truncate.
MAX_INVENTORY = 128  # bound on retiring-entity and entity-label inventories each
MAX_INVENTORY_CHARS = 4096  # total entity-ID characters per inventory
MAX_REVISION = 2**31 - 1

_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_MARKER = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.ASCII)
_ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+", re.ASCII)
# HA config-entry, device-registry and entity-registry disabler values.
DISABLED_LABELS = frozenset((None, "config_entry", "device", "hass", "integration", "user"))


class MigrationFault(RuntimeError):
    """Fixed public message: migration_fault. Raised without original context."""

    def __init__(self):
        super().__init__("migration_fault")


class MigrationStatus(enum.Enum):
    RESTART_REQUIRED = "restart_required"
    READY_TO_RESUME = "ready_to_resume"
    FORWARD_REPAIR_REQUIRED = "forward_repair_required"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    REFUSED = "refused"


class MigrationAction(enum.Enum):
    CHECKPOINT = "checkpoint"
    RESUME = "resume"
    VERIFY = "verify"
    CANCEL = "cancel"
    COMPLETE = "complete"
    REFUSE = "refuse"


class MigrationPhase(enum.Enum):
    PREPARED = "prepared"
    OLD_DRAINED = "old_drained"
    DEVICE_MOVED = "device_moved"
    OWNED_CONFIGURED = "owned_configured"
    VERIFIED = "verified"
    CANCEL_PREPARED = "cancel_prepared"
    LEGACY_RESTORED = "legacy_restored"


class LeafOwner(enum.Enum):
    """Actual config entry owning the registry leaf, as observed in HA."""

    SOURCE = "source"    # still the selected Tuya Local entry only
    PETSAFE = "petsafe"  # moved to the unchanged PetSafe entry only
    OTHER = "other"      # anything else: missing, shared, composite, foreign


class PreviewReason(enum.Enum):
    ELIGIBLE = "eligible"
    ALREADY_MIGRATING = "already_migrating"
    AMBIGUOUS_SOURCE = "ambiguous_source"
    UNSUPPORTED_SHAPE = "unsupported_shape"
    UNSUPPORTED_VERSION = "unsupported_version"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    OPERATION_ACTIVE = "operation_active"


PRE_MOVE = frozenset((MigrationPhase.PREPARED, MigrationPhase.OLD_DRAINED,
                      MigrationPhase.CANCEL_PREPARED, MigrationPhase.LEGACY_RESTORED))
POST_MOVE = frozenset((MigrationPhase.DEVICE_MOVED, MigrationPhase.OWNED_CONFIGURED,
                       MigrationPhase.VERIFIED))
CANCELLABLE = frozenset((MigrationPhase.PREPARED, MigrationPhase.OLD_DRAINED,
                         MigrationPhase.CANCEL_PREPARED))
# Allowed journal advances; same-phase advances record a repeated checkpoint
# with a new revision (a VERIFIED repeat keeps its verified_marker unchanged).
TRANSITIONS = {
    MigrationPhase.PREPARED: frozenset((MigrationPhase.PREPARED, MigrationPhase.OLD_DRAINED,
                                        MigrationPhase.CANCEL_PREPARED)),
    MigrationPhase.OLD_DRAINED: frozenset((MigrationPhase.OLD_DRAINED, MigrationPhase.DEVICE_MOVED,
                                           MigrationPhase.CANCEL_PREPARED)),
    MigrationPhase.DEVICE_MOVED: frozenset((MigrationPhase.DEVICE_MOVED,
                                            MigrationPhase.OWNED_CONFIGURED)),
    MigrationPhase.OWNED_CONFIGURED: frozenset((MigrationPhase.OWNED_CONFIGURED,
                                                MigrationPhase.VERIFIED)),
    MigrationPhase.VERIFIED: frozenset((MigrationPhase.VERIFIED,)),
    MigrationPhase.CANCEL_PREPARED: frozenset((MigrationPhase.CANCEL_PREPARED,
                                               MigrationPhase.LEGACY_RESTORED)),
    MigrationPhase.LEGACY_RESTORED: frozenset(),
}


def valid_entry_id(value) -> bool:
    return (type(value) is str and 0 < len(value) <= MAX_ID_CHARS and "\x00" not in value)


def _valid_marker(value) -> bool:
    return type(value) is str and _MARKER.fullmatch(value) is not None


def _valid_entity_id(value) -> bool:
    return (type(value) is str and len(value) <= MAX_ENTITY_ID_CHARS
            and _ENTITY_ID.fullmatch(value) is not None)


def _valid_label(value) -> bool:
    return (value is None or type(value) is str) and value in DISABLED_LABELS


def _valid_entities(value) -> bool:
    return (type(value) is tuple and len(value) <= MAX_INVENTORY
            and all(_valid_entity_id(item) for item in value)
            and len(set(value)) == len(value)
            and sum(len(item) for item in value) <= MAX_INVENTORY_CHARS)


def _valid_entity_labels(value) -> bool:
    if type(value) is not tuple or len(value) > MAX_INVENTORY:
        return False
    for item in value:
        if (type(item) is not tuple or len(item) != 2 or not _valid_entity_id(item[0])
                or not _valid_label(item[1])):
            return False
    names = [item[0] for item in value]
    return (len(set(names)) == len(names)
            and sum(len(name) for name in names) <= MAX_INVENTORY_CHARS)


def _valid_setting_targets(value, retiring) -> bool:
    """(role, index into retiring) pairs: fixed roles, unique, domain-matching rows."""
    if type(value) is not tuple or type(retiring) is not tuple:
        return False
    roles, indexes = set(), set()
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            return False
        role, index = item
        if (type(role) is not str or role not in OWNED_SETTING_ENTITIES or role in roles
                or type(index) is not int or not 0 <= index < len(retiring) or index in indexes
                or not retiring[index].startswith(OWNED_SETTING_ENTITIES[role][0] + ".")):
            return False
        roles.add(role)
        indexes.add(index)
    return list(value) == sorted(value)


def _check(valid):
    # Raised outside any handler: fixed message and no attached context.
    if not valid:
        raise MigrationFault()


@dataclass(frozen=True, slots=True)
class MigrationPreview:
    """Redacted preflight summary: fixed reason and retiring entity IDs only."""

    eligible: bool
    reason: PreviewReason
    retiring_entity_ids: tuple

    def __post_init__(self):
        _check(type(self.eligible) is bool and type(self.reason) is PreviewReason
               and self.eligible == (self.reason is PreviewReason.ELIGIBLE)
               and _valid_entities(self.retiring_entity_ids))


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """Durable journal content. Identifiers, digest, labels and markers only.

    device_id is the existing HA device-registry leaf ID; entity_disabled_by
    holds the original PetSafe (entity_id, disabler-label) pairs; retiring is
    the acknowledged raw-entity inventory the registry move will remove.
    setting_targets maps each owned setting role (const.OWNED_SETTING_ENTITIES)
    to the index of the retiring row that held it, sorted by role, so the owned
    replacement can take over that entity ID after the move; empty when none.
    """

    entry_id: str
    source_entry_id: str
    device_id: str
    source_config_digest: str
    source_disabled_by: str | None
    device_disabled_by: str | None
    entity_disabled_by: tuple
    retiring: tuple
    origin_marker: str
    phase: MigrationPhase
    revision: int
    verified_marker: str | None = None
    schema: int = SCHEMA
    setting_targets: tuple = ()

    def __post_init__(self):
        _check(self.schema == SCHEMA and type(self.schema) is int
               and valid_entry_id(self.entry_id) and valid_entry_id(self.source_entry_id)
               and valid_entry_id(self.device_id) and self.entry_id != self.source_entry_id
               and type(self.source_config_digest) is str
               and _DIGEST.fullmatch(self.source_config_digest) is not None
               and _valid_label(self.source_disabled_by) and _valid_label(self.device_disabled_by)
               and _valid_entity_labels(self.entity_disabled_by)
               and _valid_entities(self.retiring)
               and _valid_marker(self.origin_marker)
               and type(self.phase) is MigrationPhase
               and type(self.revision) is int and 1 <= self.revision <= MAX_REVISION
               # verified_marker is present exactly in VERIFIED; the journal
               # fixes it on OWNED_CONFIGURED->VERIFIED and never changes it.
               and (self.verified_marker is None) == (self.phase is not MigrationPhase.VERIFIED)
               and (self.verified_marker is None or _valid_marker(self.verified_marker))
               and _valid_setting_targets(self.setting_targets, self.retiring))


# origin_marker may change only on a repeated restart checkpoint.
CHECKPOINT_PHASES = frozenset((MigrationPhase.PREPARED, MigrationPhase.OLD_DRAINED))
IMMUTABLE_FIELDS = ("schema", "entry_id", "source_entry_id", "device_id", "source_config_digest",
                    "source_disabled_by", "device_disabled_by", "entity_disabled_by", "retiring",
                    "setting_targets")
RECORD_FIELDS = tuple(item.name for item in fields(MigrationRecord))


@dataclass(frozen=True, slots=True)
class MigrationFacts:
    """Actual observations for one reducer call. Any None field means missing.

    backend is the PetSafe entry's persisted connection backend. owned_config_valid
    states that persisted owned connection fields validate; witness_valid that the
    existing intent store opened with its persisted store/binding identity.

    source_config_digest None means the selected source entry no longer exists
    (then it must be reported disabled and unloaded: the strongest form of
    stopped). witness_valid None means the witness was not checked; only the
    terminal decisions (a finished cancellation, a verified handoff with its
    persisted owned configuration) are made without it.
    """

    process_marker: str | None = None
    source_disabled: bool | None = None
    source_loaded: bool | None = None
    leaf_owner: LeafOwner | None = None
    source_config_digest: str | None = None
    backend: str | None = None
    owned_config_valid: bool | None = None
    witness_valid: bool | None = None


def _facts_complete(facts) -> bool:
    return (type(facts) is MigrationFacts and _valid_marker(facts.process_marker)
            and type(facts.source_disabled) is bool and type(facts.source_loaded) is bool
            and type(facts.leaf_owner) is LeafOwner
            and (facts.source_config_digest is None
                 or (type(facts.source_config_digest) is str
                     and _DIGEST.fullmatch(facts.source_config_digest) is not None))
            and (facts.source_config_digest is not None
                 or (facts.source_disabled and not facts.source_loaded))
            and facts.backend in (BACKEND_LEGACY, BACKEND_OWNED) and type(facts.backend) is str
            and type(facts.owned_config_valid) is bool
            and (facts.witness_valid is None or type(facts.witness_valid) is bool))


def terminal_action(record, facts) -> MigrationAction | None:
    """COMPLETE for a finished migration, decided without source digest or witness.

    LEGACY_RESTORED is a finished cancellation: the legacy entry starts (its own
    setup validates the source it borrows), whatever later happened to the
    source entry, as long as the entry is still legacy and the leaf is not
    PetSafe-owned. VERIFIED completes over the persisted owned configuration on
    the PetSafe-owned leaf while the old source is stopped or gone. None: not
    terminal, use the full reducer.
    """
    if type(record) is not MigrationRecord or not _facts_complete(facts):
        return None
    if record.phase is MigrationPhase.LEGACY_RESTORED:
        if facts.backend == BACKEND_LEGACY and facts.leaf_owner is not LeafOwner.PETSAFE:
            return MigrationAction.COMPLETE
        return None
    if (record.phase is MigrationPhase.VERIFIED and facts.leaf_owner is LeafOwner.PETSAFE
            and facts.source_disabled and not facts.source_loaded
            and facts.backend == BACKEND_OWNED and facts.owned_config_valid):
        return MigrationAction.COMPLETE
    return None


def next_action(record, facts) -> MigrationAction:
    """Next migration action from a journal record and actual facts.

    A missing record (None) is not a migration: callers use normal backend
    selection and never infer a virgin witness from it; the reducer refuses to
    drive anything. Missing or inconsistent facts refuse.

    Source drift (a changed or missing source entry) gates only decisions that
    rely on the source: resuming before the move (and redoing a move the
    registry lost). Once the leaf moved, recovery stays forward-only and the
    coordinator no longer trusts a drifted source's connection material.
    """
    if type(record) is not MigrationRecord or not _facts_complete(facts):
        return MigrationAction.REFUSE
    terminal = terminal_action(record, facts)
    if terminal is not None:
        return terminal
    if facts.witness_valid is not True:
        return MigrationAction.REFUSE
    phase = record.phase
    same_process = facts.process_marker == record.origin_marker
    old_stopped = facts.source_disabled and not facts.source_loaded
    source_unchanged = facts.source_config_digest == record.source_config_digest

    if facts.leaf_owner is LeafOwner.OTHER:
        return MigrationAction.REFUSE

    if facts.leaf_owner is LeafOwner.PETSAFE:
        # Moved: forward-only, never reassigned back. OLD_DRAINED is the crash
        # window where the registry move persisted before the DEVICE_MOVED
        # journal write; it resumes forward like DEVICE_MOVED. Other pre-move
        # phases cannot explain a moved leaf.
        if not old_stopped or same_process:
            return MigrationAction.REFUSE
        if phase in (MigrationPhase.OLD_DRAINED, MigrationPhase.DEVICE_MOVED):
            return MigrationAction.RESUME
        if phase in PRE_MOVE:
            return MigrationAction.REFUSE
        if facts.backend != BACKEND_OWNED or not facts.owned_config_valid:
            # Owned configuration did not persist: forward repair, never back.
            return MigrationAction.RESUME
        if phase is MigrationPhase.OWNED_CONFIGURED:
            return MigrationAction.VERIFY
        return MigrationAction.COMPLETE  # VERIFIED with persisted owned config

    # Leaf still owned by the selected legacy source: every remaining action
    # relies on that source being the one the journal acknowledged.
    if not source_unchanged:
        return MigrationAction.REFUSE
    if phase in POST_MOVE:
        # The fsynced journal (and possibly the owned configuration) is ahead
        # of HA's delayed registry save: the move did not persist. Forward-only:
        # redo the same move in a new process that observes the old source
        # stopped; never cancel back to legacy once the backend may have flipped.
        if same_process or not old_stopped:
            return MigrationAction.REFUSE
        return MigrationAction.RESUME
    if phase not in PRE_MOVE or facts.backend != BACKEND_LEGACY:
        return MigrationAction.REFUSE
    if phase is MigrationPhase.LEGACY_RESTORED:
        return MigrationAction.REFUSE  # terminal cases returned above
    if phase is MigrationPhase.CANCEL_PREPARED:
        return MigrationAction.CANCEL
    if phase is MigrationPhase.PREPARED:
        # PREPARED never implies the old owner stopped: finish disable/drain.
        return MigrationAction.CHECKPOINT
    # OLD_DRAINED: resume only in a different process that observes the old
    # source disabled and unloaded; otherwise fence and repeat the checkpoint.
    if same_process or not old_stopped:
        return MigrationAction.CHECKPOINT
    return MigrationAction.RESUME


def cancel_allowed(record, facts) -> bool:
    """Cancellation only while the actual leaf is still legacy-owned."""
    return (type(record) is MigrationRecord and _facts_complete(facts)
            and facts.leaf_owner is LeafOwner.SOURCE and record.phase in CANCELLABLE
            and facts.backend == BACKEND_LEGACY)


def removal_allowed(record, facts) -> bool:
    """Old-source removal only in a later process than the one that verified."""
    return (next_action(record, facts) is MigrationAction.COMPLETE
            and record.phase is MigrationPhase.VERIFIED and record.verified_marker is not None
            and record.verified_marker != facts.process_marker)


_P = MigrationPhase
_A = MigrationAction
_S = MigrationStatus
# Every (action, phase) pair next_action can return, mapped explicitly:
# - CHECKPOINT: a restart (new HA process) must observe the old source stopped.
# - RESUME from OLD_DRAINED: the planned next step; continue the migration.
# - RESUME after the move: owned setup or verification must be repaired forward.
# - VERIFY: owned configuration persisted; continue with verification.
# - CANCEL: a prepared cancellation; continue restoring the legacy owner. There
#   is no separate cancelling status, and CANCELLED is reserved for completion.
# - COMPLETE: migrated (VERIFIED) or cancelled (LEGACY_RESTORED).
# Any other pair is not a reducer output and is REFUSED.
_STATUS = {
    (_A.CHECKPOINT, _P.PREPARED): _S.RESTART_REQUIRED,
    (_A.CHECKPOINT, _P.OLD_DRAINED): _S.RESTART_REQUIRED,
    (_A.RESUME, _P.OLD_DRAINED): _S.READY_TO_RESUME,
    (_A.RESUME, _P.DEVICE_MOVED): _S.FORWARD_REPAIR_REQUIRED,
    (_A.RESUME, _P.OWNED_CONFIGURED): _S.FORWARD_REPAIR_REQUIRED,
    (_A.RESUME, _P.VERIFIED): _S.FORWARD_REPAIR_REQUIRED,
    (_A.VERIFY, _P.OWNED_CONFIGURED): _S.READY_TO_RESUME,
    (_A.CANCEL, _P.CANCEL_PREPARED): _S.READY_TO_RESUME,
    (_A.COMPLETE, _P.VERIFIED): _S.COMPLETE,
    (_A.COMPLETE, _P.LEGACY_RESTORED): _S.CANCELLED,
}


def status_for(record, action) -> MigrationStatus:
    """Fixed user-facing status for a reducer action; see _STATUS."""
    if type(record) is not MigrationRecord or type(action) is not MigrationAction:
        return MigrationStatus.REFUSED
    return _STATUS.get((action, record.phase), MigrationStatus.REFUSED)
