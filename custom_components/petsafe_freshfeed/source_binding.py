"""Transient identity binding to an existing, loaded tuya_local source.

Pure and duck-typed: no Home Assistant import, no client, lock, SDK, network or
HA method call, and no mutation of any source object. Entry and registry objects
(including framework MappingProxyType views) are trusted HA metadata, not
arbitrary hostile getters; every scalar and key is still type-checked as an
exact builtin before any equality or hash, so str subclasses never get to run
their own comparison code. Credential values are never read: mappings are
walked by key only, and only device_id, device_cid and type are looked up.

Identity binding proves the same in-memory source objects are still in place.
It does not establish loaded-code compatibility, authentication or safety.
"""

import asyncio
import re
import threading
from types import MappingProxyType
import weakref

_DOMAIN = "tuya_local"
_PROFILE = "petsafe_freshfeed_raw"
_SELECTED = ("type", "device_id", "device_cid")
_LEAF_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
_MISSING = object()

# First admitted loop per asyncio.Lock, held weakly on both sides so neither the
# lock nor the loop is retained. The creator loop of a never-used unbound Lock
# is not knowable; this only fences reuse after the first admission here.
_LOOPS = weakref.WeakKeyDictionary()
_LOOPS_GUARD = threading.Lock()


class SourceBindingError(RuntimeError):
    """Fixed public message: source_binding_fault."""

    def __init__(self):
        super().__init__("source_binding_fault")


class _Reject(Exception):
    """Private validation failure; never escapes this module."""


def _require(condition):
    if not condition:
        raise _Reject()


def _exact_str_keys(mapping):
    # Exact dict or exact MappingProxyType only; keys checked before any lookup.
    _require(type(mapping) is dict or type(mapping) is MappingProxyType)
    for key in mapping:
        _require(type(key) is str)
    return mapping


def _selected(entry):
    data = _exact_str_keys(entry.data)
    options = _exact_str_keys(entry.options)
    values = {}
    for name in _SELECTED:
        # Options take precedence over data, per selected field only.
        if name in options:
            values[name] = options[name]
        elif name in data:
            values[name] = data[name]
        else:
            values[name] = _MISSING
    return values


def _snapshot(entry, registry_device, domain_data):
    """Validate everything and return (identities, values); raises _Reject."""
    domain = entry.domain
    _require(type(domain) is str and domain == _DOMAIN)
    state = entry.state.value
    _require(type(state) is str and state == "loaded")
    entry_id = entry.entry_id
    _require(type(entry_id) is str and 1 <= len(entry_id) <= 128)

    selected = _selected(entry)
    profile = selected["type"]
    _require(type(profile) is str and profile == _PROFILE)
    device_id = selected["device_id"]
    _require(type(device_id) is str and 1 <= len(device_id) <= 128
             and "\x00" not in device_id)
    cid = selected["device_cid"]
    if cid is _MISSING:
        cid_state = "missing"
    elif cid is None:
        cid_state = "none"
    else:
        _require(type(cid) is str and cid == "")
        cid_state = "empty"

    _require(type(domain_data) is dict)
    _exact_str_keys(domain_data)
    _require(device_id in domain_data)
    record = domain_data[device_id]
    _require(type(record) is dict)
    _exact_str_keys(record)
    _require("device" in record and "tuyadevice" in record
             and "tuyadevicelock" in record)
    wrapper = record["device"]
    api = record["tuyadevice"]
    lock = record["tuyadevicelock"]
    _require(type(lock) is asyncio.Lock)
    _require(wrapper._api is api and wrapper._api_lock is lock)
    _require(api.parent is None and wrapper.dev_cid is None)
    for value in (api.id, wrapper.dev_id, wrapper.unique_id):
        _require(type(value) is str and value == device_id)

    # Source-owned main leaf; the deprecated config_entries is never read.
    leaf_id = registry_device.id
    _require(type(leaf_id) is str and _LEAF_ID.fullmatch(leaf_id) is not None)
    leaf_entry = registry_device.config_entry_id
    _require(type(leaf_entry) is str and leaf_entry == entry_id)
    _require(registry_device.config_subentry_id is None)
    _require(registry_device.is_composite_device is False)
    identifiers = registry_device.identifiers
    _require(type(identifiers) is set)
    for member in identifiers:
        _require(type(member) is tuple and len(member) == 2
                 and type(member[0]) is str and type(member[1]) is str)
    _require((_DOMAIN, device_id) in identifiers)

    identities = (entry, registry_device, record, wrapper, api, lock)
    values = (entry_id, profile, device_id, cid_state, leaf_id,
              frozenset(identifiers))
    return identities, values


def _lock_loop_ok(lock, loop):
    bound = lock._loop
    if bound is not None and bound is not loop:
        return False
    admitted = _LOOPS.get(lock)
    return admitted is None or admitted() is loop


class SourceBinding:
    """Opaque holder; repr discloses no wrapper, API, UID or configuration."""

    __slots__ = ("_identities", "_values", "_loop")

    def __init__(self):
        self._identities: tuple | None = None
        self._values: tuple | None = None
        self._loop: weakref.ref | None = None

    def __repr__(self):
        return "<SourceBinding>"

    __str__ = __repr__

    def is_current(self, *, entry, registry_device, domain_data: dict) -> bool:
        try:
            if self._identities is None or self._loop is None:
                return False
            loop = asyncio.get_running_loop()
            if loop is not self._loop():
                return False
            identities, values = _snapshot(entry, registry_device, domain_data)
            if any(a is not b for a, b in zip(identities, self._identities)):
                return False
            if values != self._values:
                return False
            with _LOOPS_GUARD:
                admitted = _LOOPS.get(identities[-1])
                if admitted is None or admitted() is not loop:
                    return False
            bound = identities[-1]._loop
            return bound is None or bound is loop
        except Exception:
            return False


def bind_source(*, entry, registry_device, domain_data: dict) -> SourceBinding:
    binding = SourceBinding()
    try:
        loop = asyncio.get_running_loop()
        identities, values = _snapshot(entry, registry_device, domain_data)
        lock = identities[-1]
        with _LOOPS_GUARD:
            # A lock whose first admitted loop is gone stays refused: its
            # owner cannot be re-proved, so a fresh source must supply a new lock.
            _require(_lock_loop_ok(lock, loop))
            _LOOPS[lock] = weakref.ref(loop)
        binding._identities = identities
        binding._values = values
        binding._loop = weakref.ref(loop)
    except Exception:
        binding = None
    # Raised outside the handler so no private cause or context is attached.
    if binding is None:
        raise SourceBindingError()
    return binding
