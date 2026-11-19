"""Fail-closed provenance for the PetSafe-owned TinyTuya client.

TinyTuya only: the loaded library is checked against ``owned_fingerprints.json``
(public TinyTuya files and version), never against another integration's files.
``check_loaded_client`` reads files and must run in an executor;
``check_runtime_owner`` and ``vendor_logging_safe`` are synchronous and do no
file I/O. Hooks the owned source installs on its client are registered here,
so each admission sees only its own explicitly owned instance attributes.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from pathlib import Path
import sys
import struct
import threading
from types import CodeType

from .critical_transport import _socket_open, underlying_socket

FAILURE = "owned_source_incompatible"
_MANIFEST = "owned_fingerprints.json"
# Instance hooks the owned source installs (socket lifecycle, pre-cache reports).
OWNED_HOOKS = ("_cache_response", "_get_socket", "_check_socket_close",
               "set_socketPersistent", "close")
# Every client callsite the owned source, its monitor or the critical exchange
# reaches. Each must be the audited function its defining TinyTuya class declares.
API_METHODS = OWNED_HOOKS + (
    "_send_receive", "_send_receive_quick", "_receive", "_recv_all", "_process_message",
    "_process_response", "_decode_payload", "_encode_message", "_get_retcode",
    "_negotiate_session_key", "_negotiate_session_key_generate_step_1",
    "_negotiate_session_key_generate_step_3", "_negotiate_session_key_generate_finalize",
    "generate_payload", "status", "receive", "heartbeat", "set_socketRetryLimit", "cache_clear",
)
_CLIENT_CLASS = ("tinytuya.core.Device", "Device")
# Module names of every fingerprinted file (tested equal to the manifest), so
# the synchronous logger gate needs no file I/O.
VENDOR_MODULES = (
    "tinytuya", "tinytuya.core", "tinytuya.core.Device", "tinytuya.core.Monitor",
    "tinytuya.core.XenonDevice", "tinytuya.core.command_types", "tinytuya.core.const",
    "tinytuya.core.core", "tinytuya.core.crypto_helper", "tinytuya.core.error_helper",
    "tinytuya.core.exceptions", "tinytuya.core.header", "tinytuya.core.message_helper",
    "tinytuya.core.udp_helper",
)

def _constant_signature(value):
    """Represent compiler constants exactly, including signed IEEE zero."""
    kind = type(value)
    if kind is CodeType:
        return ("code", _code_signature(value))
    if kind is float:
        return ("float", struct.pack('!d', value))
    if kind is complex:
        return ("complex", struct.pack('!dd', value.real, value.imag))
    if kind is slice:
        return ("slice", *(_constant_signature(item) for item in
                           (value.start, value.stop, value.step)))
    if kind is tuple:
        return ("tuple", tuple(_constant_signature(item) for item in value))
    if kind is frozenset:
        return ("frozenset", frozenset(_constant_signature(item) for item in value))
    if kind in (type(None), bool, int, str, bytes, type(Ellipsis)):
        return (kind.__name__, value)
    raise ValueError(FAILURE)


def _code_signature(code):
    """Compare executable semantics, including nested code, not locations or caches."""
    constants = tuple(_constant_signature(value) for value in code.co_consts)
    return (code.co_code, constants, code.co_names, code.co_varnames,
            code.co_freevars, code.co_cellvars, code.co_argcount,
            code.co_posonlyargcount, code.co_kwonlyargcount, code.co_nlocals,
            code.co_stacksize, code.co_flags, code.co_exceptiontable)


def _source_code_matches(function, owner, root):
    """Locate a class method in compiled verified source; never execute it."""
    current = root
    for part in owner.__qualname__.split('.'):
        candidates = [item for item in current.co_consts
                      if isinstance(item, CodeType) and item.co_name == part]
        if len(candidates) != 1:
            return False
        current = candidates[0]
    candidates = [item for item in current.co_consts
                  if isinstance(item, CodeType) and item.co_name == function.__name__]
    return (len(candidates) == 1 and
            _code_signature(function.__code__) == _code_signature(candidates[0]))


_HOOKS = {}
_HOOKS_GUARD = threading.Lock()


def own_hook(api: object, name: str, hook: object, original: object) -> None:
    """Record an owned-source hook and the bound method it shadows."""
    if name not in OWNED_HOOKS:
        raise ValueError(FAILURE)
    with _HOOKS_GUARD:
        entry = _HOOKS.setdefault(id(api), (api, {}))
        if entry[0] is not api or name in entry[1]:
            raise ValueError(FAILURE)
        entry[1][name] = (hook, original)


def disown_hook(api: object, name: str, hook: object) -> None:
    """Forget one owned hook; a mismatched hook is left untouched."""
    with _HOOKS_GUARD:
        entry = _HOOKS.get(id(api))
        if entry is None or entry[0] is not api or entry[1].get(name, (None,))[0] is not hook:
            return
        del entry[1][name]
        if not entry[1]:
            del _HOOKS[id(api)]


def hook_original(api: object, name: str):
    """Original method shadowed by OUR installed hook, else None."""
    with _HOOKS_GUARD:
        entry = _HOOKS.get(id(api))
        if entry is None or entry[0] is not api or name not in entry[1]:
            return None
        hook, original = entry[1][name]
    return original if getattr(api, "__dict__", {}).get(name) is hook else None


def hooks_intact(api: object) -> bool:
    """Every owned hook installed and registered; no other callable override."""
    try:
        attributes = vars(api)
        for name in OWNED_HOOKS:
            if name not in attributes or hook_original(api, name) is None:
                return False
        return not any(callable(value) for name, value in attributes.items()
                       if name not in OWNED_HOOKS)
    except TypeError:
        return False


def _resolved(api, name):
    """The callsite a caller reaches, seen through our own hooks only."""
    if name in getattr(api, "__dict__", {}):
        original = hook_original(api, name)
        if original is None:
            raise ValueError(FAILURE)
        return original
    return getattr(api, name)


def _manifest():
    return json.loads(Path(__file__).with_name(_MANIFEST).read_text(encoding="utf-8"))


def _module_names(files):
    names = set()
    for suffix in files:
        name = suffix[:-3].replace("/", ".")
        names.add(name[:-9] if name.endswith(".__init__") else name)
    return names


def _debug_enabled(name):
    """Effective DEBUG for a logger name, without creating or changing loggers."""
    manager = logging.Logger.manager
    if manager.disable >= logging.DEBUG:
        return False
    existing = manager.loggerDict.get(name)
    if isinstance(existing, logging.Logger):
        return existing.isEnabledFor(logging.DEBUG)
    current = name
    while "." in current:
        current = current.rpartition(".")[0]
        ancestor = manager.loggerDict.get(current)
        if isinstance(ancestor, logging.Logger):
            return ancestor.getEffectiveLevel() <= logging.DEBUG
    return logging.getLogger().getEffectiveLevel() <= logging.DEBUG


def vendor_logging_safe() -> bool:
    """No TinyTuya logger, configured or implied, would emit DEBUG records.

    TinyTuya debug lines carry keys, payloads and frames, and its debug mode
    also fixes the GCM nonce. Never changes any logger: a configuration that
    could expose values is refused, not silenced.
    """
    try:
        names = set(VENDOR_MODULES)
        names |= {name for name in tuple(logging.Logger.manager.loggerDict)
                  if type(name) is str and name.startswith("tinytuya.")}
        return not any(_debug_enabled(name) for name in names)
    except (TypeError, ValueError, KeyError, RuntimeError):
        return False


def check_loaded_client(api: object) -> bool:
    """Executor: loaded TinyTuya files, version, classes and bound callsites."""
    try:
        manifest = _manifest()
        files = manifest["files"]
        version = manifest["tinytuya_version"]
        if (type(files) is not dict or not files or type(version) is not str
                or any(not key.startswith("tinytuya/") for key in files)):
            return False
        checked = {}

        def verified(filename):
            path = Path(filename).resolve(strict=True)
            if path in checked:
                return True
            matches = [key for key in files if path.as_posix().endswith("/" + key)]
            source = path.read_bytes()
            if len(matches) != 1 or hashlib.sha256(source).hexdigest() != files[matches[0]]:
                return False
            checked[path] = compile(source, str(path), "exec", dont_inherit=True,
                                    optimize=sys.flags.optimize)
            return True

        package = sys.modules.get("tinytuya")
        core = sys.modules.get("tinytuya.core.core")
        if (package is None or core is None or getattr(package, "__version__", None) != version
                or getattr(core, "__version__", None) != version):
            return False
        if _module_names(files) != set(VENDOR_MODULES):
            return False
        for name in VENDOR_MODULES:
            module = sys.modules.get(name)
            if module is None or not verified(module.__file__):
                return False
        cls = type(api)
        if (cls.__module__, cls.__qualname__) != _CLIENT_CLASS:
            return False
        classes = [item for item in cls.__mro__ if item is not object]
        if any(not item.__module__.startswith("tinytuya.")
               or sys.modules.get(item.__module__) is None for item in classes):
            return False
        if not all(verified(inspect.getfile(item)) for item in classes):
            return False
        for name in API_METHODS:
            method = _resolved(api, name)
            owner = next(item for item in cls.__mro__ if name in item.__dict__)
            expected = owner.__dict__[name]
            if (not inspect.isfunction(expected) or expected.__name__ != name
                    or expected.__qualname__ != f"{owner.__qualname__}.{name}"
                    or expected.__module__ != owner.__module__):
                return False
            path = Path(expected.__code__.co_filename).resolve(strict=True)
            if (not inspect.ismethod(method) or method.__self__ is not api
                    or method.__func__ is not expected or not verified(path)
                    or path != Path(inspect.getfile(owner)).resolve(strict=True)
                    or not _source_code_matches(expected, owner, checked[path])):
                return False
        # Unknown instance callables would shadow audited methods.
        if any(callable(value) and name not in OWNED_HOOKS for name, value in vars(api).items()):
            return False
        return vendor_logging_safe()
    except (AttributeError, OSError, TypeError, ValueError, KeyError, StopIteration, SyntaxError):
        return False


def check_runtime_owner(owner: object, api: object, lock: asyncio.Lock) -> bool:
    """Synchronous, no file I/O: identity, currency, socket, hooks and loggers."""
    try:
        return (isinstance(lock, asyncio.Lock)
                and getattr(owner, "api", None) is api and getattr(owner, "lock", None) is lock
                and owner.is_current() is True
                and getattr(api, "parent", None) is None and not getattr(api, "children", None)
                and _socket_open(underlying_socket(api.socket))
                and hooks_intact(api) and vendor_logging_safe())
    except Exception:  # noqa: BLE001 - unknown shape is incompatibility
        return False
