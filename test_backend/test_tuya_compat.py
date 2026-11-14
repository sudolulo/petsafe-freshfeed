"""Synthetic provenance checks for the loaded tuya_local/TinyTuya source.

Temporary public-shaped modules only: no credentials, network or real device.
"""

import ast
import hashlib
import inspect
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import FunctionType, ModuleType
import unittest
from unittest import mock

import asyncio

from custom_components.petsafe_freshfeed import tuya_compat as compat

_WRAPPER_SOURCE = '''\
import logging
_LOGGER = logging.getLogger("synthetic.tuya_local.device")


class TuyaLocalDevice:
    @property
    def should_poll(self):
        return False

    @property
    def has_returned_state(self):
        return True

    def actually_start(self, event=None):
        return None

    async def async_stop(self, event=None):
        return None

    async def receive_loop(self):
        async for poll in self.async_receive():
            pass

    async def async_receive(self):
        def nested():
            return 0.0
        yield {"value": nested()}

    async def _retry_on_failed_connection(self, func, error_message):
        return func()
'''

_API_SOURCE = 'import logging\nlog = logging.getLogger("synthetic.tinytuya")\nclass Device:\n' + ''.join(
    f'    def {name}(self, *args, **kwargs): return None\n'
    for name in compat._API_METHODS + ("heartbeat", "updatedps"))

_DEPENDENCIES = ('tinytuya/__init__.py', 'tinytuya/core/__init__.py',
                 'tinytuya/core/XenonDevice.py', 'tinytuya/core/core.py',
                 'tinytuya/core/message_helper.py', 'tinytuya/core/header.py',
                 'tinytuya/core/crypto_helper.py')


def _fingerprints(cls):
    result = {}
    for name in compat.RECEIVE_CONTROL_FLOW:
        static = inspect.getattr_static(cls, name)
        function = static.fget if isinstance(static, property) else static
        result[name] = hashlib.sha256(inspect.getsource(function).encode()).hexdigest()
    return result


class SyntheticSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.files = {}
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        self.addCleanup(self._tmp.cleanup)
        self.device = self._module('custom_components.tuya_local.device',
                                   'custom_components/tuya_local/device.py', _WRAPPER_SOURCE)
        self.tinytuya = self._module('tinytuya.core.Device', 'tinytuya/core/Device.py', _API_SOURCE)
        for suffix in _DEPENDENCIES:
            self._module(suffix[:-3].replace('/', '.').removesuffix('.__init__'), suffix, '')
        self.manifest = {'files': self.files,
                         'receive_control_flow': _fingerprints(self.device.TuyaLocalDevice)}
        loads = mock.patch.object(compat.json, 'loads', side_effect=lambda _: self.manifest)
        loads.start()
        self.addCleanup(loads.stop)
        self.wrapper = self.device.TuyaLocalDevice()
        self.api = self.tinytuya.Device()
        self.lock = asyncio.Lock()
        self.wrapper._api, self.wrapper._api_lock = self.api, self.lock
        self.addCleanup(self._disown_all)
        self._owned = []

    def _module(self, name, suffix, text):
        path = self.root / suffix
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.files[suffix] = hashlib.sha256(path.read_bytes()).hexdigest()
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(text, str(path), 'exec'), module.__dict__)
        return module

    def _own(self, obj, name, hook):
        original = getattr(obj, name)
        compat.own_attribute(obj, name, hook, original)
        setattr(obj, name, hook)
        self._owned.append((obj, name, hook))
        return original

    def _disown_all(self):
        for obj, name, hook in self._owned:
            compat.disown_attribute(obj, name, hook)

    def check(self):
        return compat.check_loaded_source(self.wrapper, self.api, self.lock)


class ExecutableProvenance(SyntheticSource):
    def _replacement(self, name, body):
        source = _API_SOURCE.replace(
            f'    def {name}(self, *args, **kwargs): return None\n',
            f'    def {name}(self, *args, **kwargs): {body}\n')
        namespace = {'__name__': self.tinytuya.__name__}
        exec(compile(source, self.tinytuya.__file__, 'exec'), namespace)
        return getattr(namespace['Device'], name)

    def test_same_metadata_class_replacement_rejected(self):
        self.tinytuya.Device.close = self._replacement('close', 'return 42')
        self.assertFalse(self.check())

    def test_in_place_code_replacement_rejected(self):
        function = self.tinytuya.Device.close
        original = function.__code__
        self.addCleanup(setattr, function, '__code__', original)
        function.__code__ = self._replacement('close', 'return 42').__code__
        self.assertFalse(self.check())

    def test_nested_code_alteration_rejected(self):
        function = self.device.TuyaLocalDevice.async_receive
        original = function.__code__
        self.addCleanup(setattr, function, '__code__', original)
        nested = next(c for c in original.co_consts if inspect.iscode(c))
        altered = nested.replace(co_consts=tuple(-0.0 if type(c) is float else c for c in nested.co_consts))
        function.__code__ = original.replace(co_consts=tuple(altered if c is nested else c for c in original.co_consts))
        self.assertFalse(self.check())

    def test_wrapper_control_flow_code_replacement_rejected(self):
        function = self.device.TuyaLocalDevice.async_receive
        original = function.__code__
        self.addCleanup(setattr, function, '__code__', original)
        changed = _WRAPPER_SOURCE.replace('yield {"value": nested()}', 'yield {"optimistic": True}')
        namespace = {'__name__': self.device.__name__}
        exec(compile(changed, self.device.__file__, 'exec'), namespace)
        function.__code__ = namespace['TuyaLocalDevice'].async_receive.__code__
        self.assertFalse(self.check())
        self.assertFalse(compat.check_receive_control_flow(self.wrapper))


    def test_signed_zero_and_compound_constants_are_distinct(self):
        code = (lambda: None).__code__
        for left, right in [(0.0, -0.0), (0j, complex(-0.0, 0.0)),
                            ((0.0,), (-0.0,)), (frozenset({0.0}), frozenset({-0.0}))]:
            self.assertNotEqual(compat._code_signature(code.replace(co_consts=(left,))),
                                compat._code_signature(code.replace(co_consts=(right,))))

    def test_checks_compile_but_never_execute_verified_source(self):
        with mock.patch('builtins.exec', side_effect=AssertionError('source executed')):
            self.assertTrue(self.check())
            self.assertTrue(compat.check_receive_control_flow(self.wrapper))

    def test_classmethod_and_property_setter_are_rejected(self):
        cls = self.device.TuyaLocalDevice
        original = cls.should_poll
        self.addCleanup(setattr, cls, 'should_poll', original)
        cls.should_poll = original.setter(lambda self, value: None)
        self.assertFalse(self.check())
        cls.should_poll = original
        cls.extra = classmethod(lambda cls: None)
        self.addCleanup(delattr, cls, 'extra')
        self.assertFalse(self.check())

    def test_property_getter_code_replacement_rejected_by_loaded_check(self):
        function = self.device.TuyaLocalDevice.should_poll.fget
        original = function.__code__
        self.addCleanup(setattr, function, '__code__', original)
        function.__code__ = original.replace(co_consts=(None, True))
        self.assertFalse(self.check())


class PinnedWrapperAcceptance(SyntheticSource):
    def test_public_pinned_wrapper_compiled_without_importing_dependencies(self):
        path = Path('~/.local/share/petsafe-validation/local-validation-snapshot/tuya-local/custom_components/tuya_local/device.py')
        if not path.is_file():
            self.skipTest('public pinned research source unavailable')
        source = path.read_bytes()
        pinned = json.JSONDecoder().decode(Path(compat.__file__).with_name('tuya_fingerprints.json').read_text())
        suffix = 'custom_components/tuya_local/device.py'
        self.assertEqual(hashlib.sha256(source).hexdigest(), pinned['files'][suffix])
        tree = ast.parse(source)
        cls_ast = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TuyaLocalDevice')
        root = compile(source, str(path), 'exec', dont_inherit=True, optimize=sys.flags.optimize)
        class_code = next(c for c in root.co_consts if inspect.iscode(c) and c.co_name == 'TuyaLocalDevice')
        namespace = {'__name__': self.device.__name__}
        members = {'__module__': self.device.__name__}
        for node in cls_ast.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            code = next(c for c in class_code.co_consts if inspect.iscode(c) and c.co_name == node.name)
            function = FunctionType(code, namespace)
            decorators = [ast.unparse(d) for d in node.decorator_list]
            self.assertIn(decorators, ([], ['property'], ['staticmethod'], ['callback']))
            members[node.name] = property(function) if decorators == ['property'] else staticmethod(function) if decorators == ['staticmethod'] else function
        cls = type('TuyaLocalDevice', (), members)
        self.device.__file__ = str(path)
        self.device.TuyaLocalDevice = cls
        self.wrapper = object.__new__(cls)
        self.wrapper._api, self.wrapper._api_lock = self.api, self.lock
        self.files[suffix] = pinned['files'][suffix]
        self.manifest['receive_control_flow'] = pinned['receive_control_flow']
        self.assertTrue(self.check())
        self.assertTrue(compat.check_receive_control_flow(self.wrapper))


class OwnedHooks(SyntheticSource):
    def test_baseline_accepts_pinned_shape(self):
        self.assertTrue(self.check())
        self.assertTrue(compat.check_receive_control_flow(self.wrapper))
        self.assertTrue(compat.logging_safe(self.api))

    def test_owned_api_hook_is_seen_through_to_its_original(self):
        def hook(*args, **kwargs):
            return None
        self._own(self.api, '_cache_response', hook)
        self.assertTrue(self.check())
        # An unowned override of a different method stays unknown code.
        self.api.close = lambda: None
        self.assertFalse(self.check())

    def test_replaced_owned_hook_or_released_registration_rejects(self):
        def hook(*args, **kwargs):
            return None
        self._own(self.api, '_get_socket', hook)
        self.api._get_socket = lambda *args: None  # someone replaced our hook
        self.assertFalse(self.check())
        self.api._get_socket = hook
        self.assertTrue(self.check())
        compat.disown_attribute(self.api, '_get_socket', hook)
        self.assertFalse(self.check())  # still installed, no longer owned
        del self.api._get_socket
        self.assertTrue(self.check())

    def test_owned_hook_cannot_shadow_an_aliased_original(self):
        def hook(*args, **kwargs):
            return None
        compat.own_attribute(self.api, 'close', hook, self.api._check_socket_close)
        self._owned.append((self.api, 'close', hook))
        self.api.close = hook
        self.assertFalse(self.check())

    def test_duplicate_ownership_is_refused(self):
        def hook():
            return None
        self._own(self.api, 'close', hook)
        with self.assertRaises(ValueError):
            compat.own_attribute(self.api, 'close', hook, self.api.close)

    def test_owned_wrapper_receive_adapter_is_accepted(self):
        def adapter():
            return None
        self.wrapper.async_receive = adapter
        self.assertFalse(self.check())
        self.assertFalse(compat.check_receive_control_flow(self.wrapper))
        del self.wrapper.async_receive
        self._own(self.wrapper, 'async_receive', adapter)
        self.assertTrue(self.check())
        self.assertTrue(compat.check_receive_control_flow(self.wrapper))


class ControlFlow(SyntheticSource):
    def test_changed_monitor_source_fails_closed(self):
        class Patched(self.device.TuyaLocalDevice):
            async def async_receive(self):
                yield {'optimistic': True}
        self.wrapper.__class__ = Patched
        self.assertFalse(compat.check_receive_control_flow(self.wrapper))

    def test_same_names_with_changed_body_fail_on_digest_alone(self):
        # Identical class/qualname/module; only the async_receive text differs,
        # so only the source digest can reject it.
        changed = self._module(
            'custom_components.tuya_local.device', 'changed/custom_components/tuya_local/device.py',
            _WRAPPER_SOURCE.replace('        yield {"value": nested()}\n', '        yield {"optimistic": True}\n'))
        wrapper = changed.TuyaLocalDevice()
        self.assertEqual(type(wrapper).async_receive.__qualname__,
                         self.device.TuyaLocalDevice.async_receive.__qualname__)
        self.assertFalse(compat.check_receive_control_flow(wrapper))
        self.manifest['receive_control_flow'] = _fingerprints(changed.TuyaLocalDevice)
        # Only one manifest suffix may identify the file.
        del self.files['changed/custom_components/tuya_local/device.py']
        self.files['custom_components/tuya_local/device.py'] = hashlib.sha256(
            Path(changed.__file__).read_bytes()).hexdigest()
        self.assertTrue(compat.check_receive_control_flow(wrapper))

    def test_debug_enabled_helper_logger_rejects(self):
        helper = logging.getLogger('tinytuya.core.error_helper')
        self.addCleanup(helper.setLevel, helper.level)
        helper.setLevel(logging.DEBUG)
        self.assertFalse(compat.logging_safe(self.api))

    def test_changed_fingerprint_or_manifest_keys_fail_closed(self):
        self.manifest['receive_control_flow']['receive_loop'] = '0' * 64
        self.assertFalse(compat.check_receive_control_flow(self.wrapper))
        self.manifest['receive_control_flow'] = _fingerprints(self.device.TuyaLocalDevice)
        del self.manifest['receive_control_flow']['should_poll']
        self.assertFalse(compat.check_receive_control_flow(self.wrapper))
        del self.manifest['receive_control_flow']
        self.assertFalse(compat.check_receive_control_flow(self.wrapper))

    def test_missing_member_fails_closed(self):
        class Bare:
            pass
        self.assertFalse(compat.check_receive_control_flow(Bare()))

    def test_checked_in_manifest_pins_every_control_flow_member(self):
        # json.loads is patched for the synthetic layout; decode the real file directly.
        text = Path(compat.__file__).with_name('tuya_fingerprints.json').read_text()
        manifest = json.JSONDecoder().decode(text)
        pinned = manifest['receive_control_flow']
        self.assertEqual(set(pinned), set(compat.RECEIVE_CONTROL_FLOW))
        self.assertTrue(all(len(value) == 64 for value in pinned.values()))


class Logging(SyntheticSource):
    def test_debug_logger_rejects(self):
        logger = logging.getLogger('synthetic.tinytuya')
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.DEBUG)
        self.assertFalse(compat.logging_safe(self.api))
        logger.setLevel(logging.INFO)
        self.assertTrue(compat.logging_safe(self.api))

    def test_owned_hook_is_checked_through_its_original(self):
        def hook(*args, **kwargs):  # this module's globals hold no tinytuya logger
            return None
        self._own(self.api, '_send_receive', hook)
        self.assertTrue(compat.logging_safe(self.api))
        logger = logging.getLogger('synthetic.tinytuya')
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.DEBUG)
        self.assertFalse(compat.logging_safe(self.api))

    def test_unowned_override_or_missing_required_rejects(self):
        self.api._send_receive = lambda *args: None
        self.assertFalse(compat.logging_safe(self.api))
        del self.api._send_receive

        class NoPayload:
            pass
        self.assertFalse(compat.logging_safe(NoPayload()))


if __name__ == '__main__':
    unittest.main()
