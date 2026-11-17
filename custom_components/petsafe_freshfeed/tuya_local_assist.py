"""Create the Tuya Local source entry through Tuya Local's own config flow.

The assist never writes Tuya Local's entries itself. It checks that the pinned
Tuya Local release is installed and loaded, installs the profile copy shipped
inside this component when it is absent (never overwriting), and then drives
Tuya Local's real manual flow ``user`` -> ``local`` -> ``select_type`` (or
``select_type_auto_detected``) -> ``choose_entities`` step by step. Any other
step, any form carrying errors, or any abort ends the drive with a fixed
``AssistError`` reason, and the Tuya Local flow is aborted on every exit that
is not ``create_entry`` (including cancellation).

Privacy: the local key, device ID and host are passed only to Tuya Local's
flow. They are never logged, returned, stored here or put in an exception;
intermediate flow results are never logged or returned.

Documented limits (never retried): the pre-abort of an in-progress Tuya Local
flow for the device can cancel a flow the user opened by hand. Tuya Local's LAN
discovery raises at most one discovery flow per device per Home Assistant run
(``discovery.py`` ``_discover_new`` skips device IDs already in ``_discovered``),
so an aborted discovery flow is not raised again until restart; a flow for the
device that claims its unique id while the drive runs still ends in
``already_in_progress``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from homeassistant.config_entries import SOURCE_IGNORE, SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData, UnknownFlow
from homeassistant.helpers.selector import SelectSelector
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .const import SOURCE_DOMAIN, SOURCE_PROFILE
from .source_lookup import binding_id, profile_type

_COMPONENT = Path(__file__).resolve().parent
PROFILE_FILENAME = f"{SOURCE_PROFILE}.yaml"
PROFILE_SOURCE = _COMPONENT / "profile" / PROFILE_FILENAME
_FINGERPRINTS = _COMPONENT / "tuya_fingerprints.json"
_TL_PREFIX = "custom_components/tuya_local/"
_TL_PINNED_FILES = ("device.py", "config_flow.py", "discovery.py", "helpers/device_config.py")
_PROFILE_MAX_BYTES = 1 << 20

# Pinned Tuya Local flow contract (test_pinned_tl_flow_contract checks these
# against the pinned public source).
STEP_USER = "user"
STEP_LOCAL = "local"
STEP_SELECT_TYPE = "select_type"
STEP_SELECT_TYPE_AUTO = "select_type_auto_detected"
STEP_CHOOSE_ENTITIES = "choose_entities"
STEP_IDS = (STEP_USER, STEP_LOCAL, STEP_SELECT_TYPE, STEP_SELECT_TYPE_AUTO, STEP_CHOOSE_ENTITIES)
SETUP_MODE_FIELD = "setup_mode"
SETUP_MODE_MANUAL = "manual"
LOCAL_FIELDS = ("device_id", "host", "local_key", "protocol_version", "poll_only", "device_cid")
TYPE_FIELD = "type"
TYPE_PREFIX = f"{SOURCE_PROFILE}||"
NAME_FIELD = "name"
PROTOCOL_AUTO = "auto"

# Bounded wait for the new entry to be loaded and its DP145 raw entity registered.
ENTITY_WAIT_S = 10.0
_ENTITY_POLL_S = 0.1
# States in which the new entry will not become loaded within the wait.
_SETTLED_FAILURE_STATES = frozenset({
    ConfigEntryState.SETUP_RETRY,
    ConfigEntryState.SETUP_ERROR,
    ConfigEntryState.FAILED_UNLOAD,
})
# Upper bound for driving Tuya Local's flow; its auto protocol mode can try up to
# eight protocol versions, each with its own connection test.
DRIVE_TIMEOUT_S = 60.0

REASONS = frozenset({
    "unexpected_step",
    "connection_failed",
    "not_supported",
    "already_configured_other",
    "already_in_progress",
    "entry_not_loaded",
    "aborted",
    "profile_install_failed",
})
_ABORT_REASONS = {
    "already_in_progress": "already_in_progress",
    "not_supported": "not_supported",
    "already_configured": "already_configured_other",
}


class AssistStatus(enum.Enum):
    """Preflight result."""

    READY = "ready"
    PROFILE_NEEDED = "profile_needed"
    TUYA_LOCAL_MISSING = "tuya_local_missing"
    TUYA_LOCAL_UNSUPPORTED = "tuya_local_unsupported"
    PROFILE_CONFLICT = "profile_conflict"


class AssistError(Exception):
    """Fixed-reason failure. Carries only a reason from the closed set."""

    def __init__(self, reason: str) -> None:
        if type(reason) is not str or reason not in REASONS:
            raise ValueError("unknown assist reason")
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason

    def __repr__(self) -> str:
        return f"AssistError({self.reason!r})"


# --- preflight -------------------------------------------------------------------


def _load_pins() -> tuple[str, dict[str, str]]:
    """Pinned release and the four pinned Tuya Local file hashes (blocking)."""
    manifest = json.loads(_FINGERPRINTS.read_text(encoding="utf-8"))
    release = manifest["provenance"]["tuya_local_release"]
    files = manifest["files"]
    pins = {_TL_PREFIX + name: files[_TL_PREFIX + name] for name in _TL_PINNED_FILES}
    if type(release) is not str or not all(type(v) is str and len(v) == 64 for v in pins.values()):
        raise ValueError("malformed pins")
    return release, pins


def _read_regular(path: Path, limit: int) -> bytes | None:
    """Bytes of a regular, non-symlink file, or None if absent. Raises OSError otherwise.

    ``O_NONBLOCK`` keeps a FIFO at ``path`` from blocking the open (and the executor);
    the ``S_ISREG`` check then rejects it.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError("file too large")
    return data


def _check_install(root: Path, pins: dict[str, str]) -> AssistStatus:
    """Hash the pinned files and compare the installed profile (blocking)."""
    for key, expected in pins.items():
        try:
            data = _read_regular(root / key[len(_TL_PREFIX):], 16 << 20)
        except OSError:
            return AssistStatus.TUYA_LOCAL_UNSUPPORTED
        if data is None or hashlib.sha256(data).hexdigest() != expected:
            return AssistStatus.TUYA_LOCAL_UNSUPPORTED
    devices = root / "devices"
    if not devices.is_dir() or devices.is_symlink():
        return AssistStatus.TUYA_LOCAL_UNSUPPORTED
    bundled = _bundled_profile()
    try:
        installed = _read_regular(devices / PROFILE_FILENAME, _PROFILE_MAX_BYTES)
    except OSError:
        return AssistStatus.PROFILE_CONFLICT
    if installed is None:
        if os.path.lexists(devices / PROFILE_FILENAME):
            return AssistStatus.PROFILE_CONFLICT
        return AssistStatus.PROFILE_NEEDED
    return AssistStatus.READY if installed == bundled else AssistStatus.PROFILE_CONFLICT


def _bundled_profile() -> bytes:
    data = _read_regular(PROFILE_SOURCE, _PROFILE_MAX_BYTES)
    if not data:
        raise OSError("bundled profile missing")
    return data


async def _tuya_local_root(hass) -> tuple[AssistStatus, Path | None, dict[str, str]]:
    try:
        integration = await async_get_integration(hass, SOURCE_DOMAIN)
    except IntegrationNotFound:
        return AssistStatus.TUYA_LOCAL_MISSING, None, {}
    try:
        release, pins = await hass.async_add_executor_job(_load_pins)
    except (OSError, ValueError, KeyError, TypeError):
        return AssistStatus.TUYA_LOCAL_UNSUPPORTED, None, {}
    # The version HA loaded (cached at first resolution): an in-place update
    # under running old code still reports the old version until restart.
    version = integration.manifest.get("version")
    if type(version) is not str or version != release or integration.file_path is None:
        return AssistStatus.TUYA_LOCAL_UNSUPPORTED, None, {}
    return AssistStatus.READY, Path(integration.file_path), pins


async def async_preflight(hass) -> AssistStatus:
    """Check the pinned Tuya Local install and the profile; changes nothing."""
    status, root, pins = await _tuya_local_root(hass)
    if status is not AssistStatus.READY:
        return status
    try:
        return await hass.async_add_executor_job(_check_install, root, pins)
    except OSError:  # the profile copy shipped in this component is unreadable
        pass
    raise AssistError("profile_install_failed")  # outside the except: no __context__


# --- profile install -------------------------------------------------------------


def _install(devices: Path, data: bytes) -> None:
    """Exclusive atomic create: temp file, fsync, then link (fails if target exists).

    Limits: the directory is not fsynced, so a power loss right after the link can
    lose the new name; and there is no fallback where hard links are unsupported
    (``os.link`` raises OSError, reported as ``profile_install_failed``).
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{SOURCE_PROFILE}.", suffix=".tmp", dir=devices)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.link(tmp, devices / PROFILE_FILENAME, follow_symlinks=False)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


async def async_install_profile(hass) -> None:
    """Install the bundled profile into Tuya Local's devices dir; only from PROFILE_NEEDED.

    No restart is needed: Tuya Local rescans its devices directory on every lookup.
    """
    if await async_preflight(hass) is not AssistStatus.PROFILE_NEEDED:
        raise AssistError("profile_install_failed")
    _status, root, _pins = await _tuya_local_root(hass)
    try:
        data = await hass.async_add_executor_job(_bundled_profile)
        await hass.async_add_executor_job(_install, root / "devices", data)
        return
    except OSError:
        pass
    raise AssistError("profile_install_failed")  # outside the except: no __context__


# --- existing entries ------------------------------------------------------------


def _matches(entry, device_id: str) -> bool:
    return (entry.unique_id == device_id
            or entry.data.get("device_id") == device_id
            or entry.options.get("device_id") == device_id)


def _matching_entries(hass, device_id: str) -> list:
    """Every Tuya Local entry for the device, including disabled and ignored ones."""
    return [entry for entry in hass.config_entries.async_entries(
        SOURCE_DOMAIN, include_ignore=True, include_disabled=True) if _matches(entry, device_id)]


async def async_existing_source(hass, device_id: str) -> str | None:
    """Entry ID of a loaded petsafe_freshfeed_raw Tuya Local entry for the device, or None.

    Includes disabled and ignored entries; any other match raises
    ``already_configured_other``.
    """
    matches = _matching_entries(hass, device_id)
    if not matches:
        return None
    if len(matches) != 1:
        raise AssistError("already_configured_other")
    entry = matches[0]
    cid = entry.options.get("device_cid", entry.data.get("device_cid"))
    if (entry.source == SOURCE_IGNORE or entry.disabled_by is not None
            or entry.state is not ConfigEntryState.LOADED
            or profile_type(entry) != SOURCE_PROFILE
            or cid not in (None, "")):
        raise AssistError("already_configured_other")
    return entry.entry_id


# --- driver ----------------------------------------------------------------------


def _abort_tl_flows_for(hass, device_id: str) -> None:
    """Abort in-progress Tuya Local flows (e.g. LAN discovery) whose unique id is the device."""
    manager = hass.config_entries.flow
    for flow in manager.async_progress_by_handler(
            SOURCE_DOMAIN, include_uninitialized=True, match_context={"unique_id": device_id}):
        with contextlib.suppress(UnknownFlow):
            manager.async_abort(flow["flow_id"])


def _expect(result: Any, step_ids: tuple[str, ...]) -> None:
    """Accept only a clean form for one of ``step_ids``; else raise a fixed reason."""
    kind = result.get("type")
    if kind is FlowResultType.ABORT:
        raise AssistError(_ABORT_REASONS.get(result.get("reason"), "aborted"))
    if kind is not FlowResultType.FORM:
        raise AssistError("unexpected_step")
    if result.get("errors"):
        raise AssistError("connection_failed")
    if result.get("step_id") not in step_ids or result.get("data_schema") is None:
        raise AssistError("unexpected_step")


def _fields(result: Any) -> dict[str, Any]:
    schema = getattr(result["data_schema"], "schema", None)
    if not isinstance(schema, dict):
        raise AssistError("unexpected_step")
    return {str(key): value for key, value in schema.items()}


def _option_values(selector: Any) -> list[Any]:
    if not isinstance(selector, SelectSelector):
        raise AssistError("unexpected_step")
    options = selector.config.get("options")
    if not isinstance(options, (list, tuple)):
        raise AssistError("unexpected_step")
    return [option.get("value") if isinstance(option, dict) else option for option in options]


def _check_user_form(result: Any) -> None:
    fields = _fields(result)
    if set(fields) != {SETUP_MODE_FIELD} or SETUP_MODE_MANUAL not in _option_values(
            fields[SETUP_MODE_FIELD]):
        raise AssistError("unexpected_step")


def _check_local_form(result: Any) -> None:
    if tuple(_fields(result)) != LOCAL_FIELDS:
        raise AssistError("unexpected_step")


def _profile_option(result: Any) -> str:
    fields = _fields(result)
    if set(fields) != {TYPE_FIELD}:
        raise AssistError("unexpected_step")
    values = [value for value in _option_values(fields[TYPE_FIELD])
              if type(value) is str and value.startswith(TYPE_PREFIX)]
    if len(values) != 1:
        raise AssistError("not_supported")
    return values[0]


def _check_name_form(result: Any) -> None:
    if set(_fields(result)) != {NAME_FIELD}:
        raise AssistError("unexpected_step")


async def _wait_loaded(hass, entry_id: str) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ENTITY_WAIT_S
    while True:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state in _SETTLED_FAILURE_STATES:
            return False
        if entry.state is ConfigEntryState.LOADED and binding_id(hass, entry_id) is not None:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_ENTITY_POLL_S)


def _tl_flow_ids(manager, *, include_uninitialized: bool) -> set[str]:
    return {flow["flow_id"] for flow in manager.async_progress_by_handler(
        SOURCE_DOMAIN, include_uninitialized=include_uninitialized)}


def _abort_own_flows(manager, before: set[str], flow_ids: list[str]) -> None:
    """Abort the flow this drive started, also when ``async_init`` never returned its ID.

    HA registers a flow before running its first step, so a first step that raises,
    is cancelled or times out leaves an uninitialized flow whose ID was never
    returned. With no recorded ID, every new uninitialized Tuya Local flow since
    ``before`` is aborted; initialized flows started meanwhile (a discovery or a
    hand-opened flow) are left alone.
    """
    targets = set(flow_ids)
    if not targets:
        targets = (_tl_flow_ids(manager, include_uninitialized=True)
                   - _tl_flow_ids(manager, include_uninitialized=False)) - before
    for flow_id in targets:
        with contextlib.suppress(UnknownFlow):
            manager.async_abort(flow_id)


async def _drive(hass, manager, flow_ids: list[str], local: dict[str, Any], name: str,
                 final_sent: list[bool]) -> str:
    result = await manager.async_init(SOURCE_DOMAIN, context={"source": SOURCE_USER})
    if result.get("flow_id"):
        flow_ids.append(result["flow_id"])
    _expect(result, (STEP_USER,))
    _check_user_form(result)
    flow_id = result["flow_id"]

    result = await manager.async_configure(flow_id, {SETUP_MODE_FIELD: SETUP_MODE_MANUAL})
    _expect(result, (STEP_LOCAL,))
    _check_local_form(result)

    result = await manager.async_configure(flow_id, local)
    _expect(result, (STEP_SELECT_TYPE, STEP_SELECT_TYPE_AUTO))
    choice = _profile_option(result)

    result = await manager.async_configure(flow_id, {TYPE_FIELD: choice})
    _expect(result, (STEP_CHOOSE_ENTITIES,))
    _check_name_form(result)

    # Only this submit lets Tuya Local's flow create an entry.
    final_sent.append(True)
    result = await manager.async_configure(flow_id, {NAME_FIELD: name})
    if result.get("type") is not FlowResultType.CREATE_ENTRY:
        _expect(result, ())
    entry = result.get("result")
    entry_id = getattr(entry, "entry_id", None)
    if type(entry_id) is not str:
        raise AssistError("unexpected_step")
    return entry_id


async def async_create_source_entry(hass, feeder, located, host, *, name: str) -> str:
    """Create the Tuya Local entry for ``feeder`` through Tuya Local's flow; return its ID.

    ``located`` (from the LAN locator) supplies the host and protocol version;
    otherwise ``host`` is used with protocol ``auto``. Raises ``AssistError``.
    The created Tuya Local entry is kept even when ``entry_not_loaded`` is raised.
    Any other drive failure after the final submit is also reported as
    ``entry_not_loaded`` when a matching Tuya Local entry that did not exist before
    the drive is now present (a ``create_entry`` result without a usable entry,
    or the drive timeout firing while Tuya Local sets up the entry it added).

    Any existing Tuya Local entry for the device (reusable or not) is refused with
    ``already_configured_other`` before any flow is touched: an earlier
    ``async_existing_source`` check can be stale, and Tuya Local's connection test
    pauses an existing device for 5 s before its own unique-id abort. The whole
    drive is bounded by ``DRIVE_TIMEOUT_S`` (``connection_failed``).
    """
    if located is not None:
        address, protocol = located.host, located.version
    elif host is not None:
        address, protocol = host, PROTOCOL_AUTO
    else:
        raise ValueError("located or host required")
    local = {
        "device_id": feeder.device_id,
        "host": address,
        "local_key": feeder.local_key,
        "protocol_version": protocol,
        "poll_only": False,
    }
    if _matching_entries(hass, feeder.device_id):
        raise AssistError("already_configured_other")
    manager = hass.config_entries.flow
    _abort_tl_flows_for(hass, feeder.device_id)
    before = _tl_flow_ids(manager, include_uninitialized=True)
    entries_before = {entry.entry_id for entry in _matching_entries(hass, feeder.device_id)}
    flow_ids: list[str] = []
    final_sent: list[bool] = []
    entry_id = None
    # Each handler records only a fixed reason; the AssistError is raised after the
    # except block so it carries no __context__ (whose traceback frames hold the key).
    reason = None
    try:
        try:
            async with asyncio.timeout(DRIVE_TIMEOUT_S):
                entry_id = await _drive(hass, manager, flow_ids, local, name, final_sent)
        except AssistError as err:
            reason = err.reason
        except TimeoutError:
            reason = "connection_failed"
        except InvalidData:
            reason = "unexpected_step"
        except UnknownFlow:
            reason = "aborted"
        except Exception:  # noqa: BLE001 - Tuya Local internals; never surface their text
            reason = "aborted"
    finally:
        if entry_id is None:
            _abort_own_flows(manager, before, flow_ids)
    if reason is not None:
        if final_sent and {entry.entry_id for entry in _matching_entries(
                hass, feeder.device_id)} - entries_before:
            reason = "entry_not_loaded"  # Tuya Local's flow created it: kept, reported
        raise AssistError(reason)
    if not await _wait_loaded(hass, entry_id):
        raise AssistError("entry_not_loaded")
    return entry_id
