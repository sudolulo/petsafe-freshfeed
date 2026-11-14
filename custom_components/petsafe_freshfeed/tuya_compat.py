"""Fail-closed, offline provenance checks for the single loaded Tuya client.

Call from an executor: checking provenance reads public source files, never
configuration, credentials, payloads, or package version declarations.
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

FAILURE = "tuya_source_incompatible"
_API_METHODS = (
    "_cache_response", "_get_socket", "_check_socket_close",
    "set_socketPersistent", "close", "_send_receive", "_receive",
    "_process_message", "_decode_payload", "_encode_message",
    "generate_payload", "status", "receive", "send", "set_value",
)
# Wrapper control flow the receive takeover replicates or interleaves with.
# Fingerprinted by exact source text so a synthetic or test-patched provenance
# check can never admit a takeover of unknown upstream monitor logic.
RECEIVE_CONTROL_FLOW = (
    "async_receive", "receive_loop", "actually_start", "async_stop",
    "_retry_on_failed_connection", "should_poll", "has_returned_state",
)
# Group 0 is required: the critical exchange, and the client's own decoder for
# its response (decode_response=False never drops the frame; it is decoded by
# exactly this allowlisted bound callsite).
_LOGGED_METHODS = (
    ("generate_payload", "_send_receive", "_decode_payload"),
    ("_encode_message", "_receive", "_recv_all", "_get_socket", "_check_socket_close",
     "_get_retcode", "set_socketRetryLimit", "receive", "heartbeat", "status", "updatedps"),
)
# Instance attributes installed by this integration's own bridge, keyed by the
# owning object's id while installed (the entry keeps a strong reference to the
# object, so the id cannot be recycled). Any other instance override is unknown.
_OWNED = {}
_OWNED_GUARD = threading.Lock()


def own_attribute(obj: object, name: str, hook: object, original: object) -> None:
    """Record a bridge-installed instance attribute and the method it shadows."""
    with _OWNED_GUARD:
        entry = _OWNED.setdefault(id(obj), (obj, {}))
        if entry[0] is not obj or name in entry[1]:
            raise ValueError(FAILURE)
        entry[1][name] = (hook, original)


def disown_attribute(obj: object, name: str, hook: object) -> None:
    """Forget one owned attribute; a mismatched hook is left untouched."""
    with _OWNED_GUARD:
        entry = _OWNED.get(id(obj))
        if entry is None or entry[0] is not obj or entry[1].get(name, (None,))[0] is not hook:
            return
        del entry[1][name]
        if not entry[1]:
            del _OWNED[id(obj)]


def owned_original(obj: object, name: str):
    """Original method shadowed by OUR installed hook, else None (unknown/absent)."""
    with _OWNED_GUARD:
        entry = _OWNED.get(id(obj))
        if entry is None or entry[0] is not obj or name not in entry[1]:
            return None
        hook, original = entry[1][name]
    return original if getattr(obj, "__dict__", {}).get(name) is hook else None


def _resolved(obj, name):
    """The callsite a caller reaches, seen through our own owned hooks only."""
    if name in getattr(obj, "__dict__", {}):
        original = owned_original(obj, name)
        if original is None:
            raise ValueError(FAILURE)
        return original
    return getattr(obj, name)


def logging_safe(api: object) -> bool:
    """No logger reachable from the client's I/O callsites is DEBUG-enabled."""
    try:
        for index, names in enumerate(_LOGGED_METHODS):
            for name in names:
                try:
                    method = _resolved(api, name)
                except AttributeError:
                    if index == 0:
                        return False
                    continue
                namespace = getattr(getattr(method, "__func__", method), "__globals__", None)
                if namespace is None:
                    return False
                loggers = [value for value in namespace.values()
                           if isinstance(value, logging.Logger)]
                if index == 0 and not loggers:
                    return False
                if any(logger.isEnabledFor(logging.DEBUG) for logger in loggers):
                    return False
        # Helpers such as tinytuya.core.error_helper log payloads from modules
        # no callsite above names; any DEBUG-enabled TinyTuya logger rejects.
        for name, logger in tuple(logging.Logger.manager.loggerDict.items()):
            if (isinstance(logger, logging.Logger)
                    and (name == "tinytuya" or name.startswith("tinytuya."))
                    and logger.isEnabledFor(logging.DEBUG)):
                return False
        return True
    except (TypeError, ValueError):
        return False


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


def check_receive_control_flow(wrapper: object) -> bool:
    """Exact pinned source of the wrapper monitor logic; call from an executor."""
    try:
        manifest = json.loads(Path(__file__).with_name("tuya_fingerprints.json").read_text())
        expected = manifest["receive_control_flow"]
        if type(expected) is not dict or set(expected) != set(RECEIVE_CONTROL_FLOW):
            return False
        cls = type(wrapper)
        path = Path(inspect.getfile(cls)).resolve(strict=True)
        source = path.read_bytes()
        files = manifest['files']
        matches = [key for key in files if path.as_posix().endswith('/' + key)]
        if len(matches) != 1 or hashlib.sha256(source).hexdigest() != files[matches[0]]:
            return False
        root = compile(source, str(path), 'exec', dont_inherit=True,
                       optimize=sys.flags.optimize)
        for name in RECEIVE_CONTROL_FLOW:
            if name in getattr(wrapper, "__dict__", {}) and owned_original(wrapper, name) is None:
                return False
            static = inspect.getattr_static(cls, name)
            function = static.fget if isinstance(static, property) else static
            if (not inspect.isfunction(function) or function.__name__ != name
                    or function.__qualname__ != f"{cls.__qualname__}.{name}"
                    or function.__module__ != cls.__module__):
                return False
            digest = hashlib.sha256(inspect.getsource(function).encode("utf-8")).hexdigest()
            if digest != expected[name] or not _source_code_matches(function, cls, root):
                return False
        return True
    except (AttributeError, OSError, TypeError, ValueError, KeyError, SyntaxError):
        return False


def check_loaded_source(wrapper: object, api: object, lock: asyncio.Lock) -> bool:
    """Validate loaded classes, dependencies and bound callsites without mutation."""
    try:
        if not isinstance(lock, asyncio.Lock) or wrapper._api_lock is not lock or wrapper._api is not api:
            return False
        manifest = json.loads(Path(__file__).with_name("tuya_fingerprints.json").read_text())
        files = manifest["files"]
        checked = {}
        compiled = {}

        def compiled_source(path):
            if path not in compiled:
                compiled[path] = compile(checked[path], str(path), 'exec',
                                         dont_inherit=True, optimize=sys.flags.optimize)
            return compiled[path]

        def verified(filename):
            path = Path(filename).resolve(strict=True)
            if path in checked:
                return True
            matches = [key for key in files if path.as_posix().endswith("/" + key)]
            source = path.read_bytes()
            if len(matches) != 1 or hashlib.sha256(source).hexdigest() != files[matches[0]]:
                return False
            checked[path] = source
            return True

        if type(wrapper).__module__ != "custom_components.tuya_local.device":
            return False
        if not verified(inspect.getfile(type(wrapper))):
            return False
        classes = [cls for cls in type(api).__mro__ if cls is not object]
        if not classes or any(not cls.__module__.startswith("tinytuya.") for cls in classes):
            return False
        if not all(verified(inspect.getfile(cls)) for cls in classes):
            return False
        # Check every dependency in the pinned manifest, not just a version string.
        for suffix in files:
            if not suffix.startswith("tinytuya/"):
                continue
            name = suffix[:-3].replace("/", ".")
            if name.endswith(".__init__"):
                name = name[:-9]
            module = sys.modules.get(name)
            if module is None or not verified(module.__file__):
                return False
        for name in _API_METHODS:
            # Each callsite must resolve to the function its defining class declares
            # under that name: not an instance override and not a class-level alias
            # of another (even allowlisted) method.
            # Our own bridge hooks are seen through to the method they shadow;
            # any other instance override is unknown code.
            method = _resolved(api, name)
            owner = next(cls for cls in type(api).__mro__ if name in cls.__dict__)
            expected = owner.__dict__[name]
            if (not inspect.isfunction(expected) or expected.__name__ != name
                    or expected.__qualname__ != f"{owner.__qualname__}.{name}"
                    or expected.__module__ != owner.__module__):
                return False
            if (not inspect.ismethod(method) or method.__self__ is not api
                    or method.__func__ is not expected
                    or not verified(expected.__code__.co_filename)
                    or Path(expected.__code__.co_filename).resolve(strict=True)
                    != Path(inspect.getfile(owner)).resolve(strict=True)):
                return False
            if not _source_code_matches(expected, owner, compiled_source(Path(expected.__code__.co_filename).resolve(strict=True))):
                return False
        # A patched wrapper can supply a different client or optimistic receive path.
        for name, static in inspect.getmembers_static(type(wrapper)):
            # The pinned wrapper has read-only properties and no classmethods.
            # Reject new descriptor shapes instead of silently ignoring their code.
            if isinstance(static, property):
                if static.fset is not None or static.fdel is not None or static.fget is None:
                    return False
                value = static.fget
                owner = next(cls for cls in type(wrapper).__mro__ if name in cls.__dict__)
                path = Path(value.__code__.co_filename).resolve(strict=True)
                if (not inspect.isfunction(value) or value.__name__ != name
                        or not verified(path) or path != Path(inspect.getfile(owner)).resolve(strict=True)
                        or not _source_code_matches(value, owner, compiled_source(path))):
                    return False
            elif isinstance(static, classmethod):
                return False
            elif inspect.isfunction(static) or isinstance(static, staticmethod):
                value = static.__func__ if isinstance(static, staticmethod) else static
                bound = _resolved(wrapper, name)
                if isinstance(inspect.getattr_static(type(wrapper), name), staticmethod):
                    if bound is not value:
                        return False
                elif not inspect.ismethod(bound) or bound.__func__ is not value:
                    return False
                if not verified(value.__code__.co_filename):
                    return False
                owner = next(cls for cls in type(wrapper).__mro__ if name in cls.__dict__)
                path = Path(value.__code__.co_filename).resolve(strict=True)
                if path != Path(inspect.getfile(owner)).resolve(strict=True) or not _source_code_matches(value, owner, compiled_source(path)):
                    return False
        return True
    except (AttributeError, OSError, TypeError, ValueError, KeyError, StopIteration, SyntaxError):
        return False
