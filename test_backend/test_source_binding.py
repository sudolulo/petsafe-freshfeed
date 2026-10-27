"""Synthetic duck-typed source binding checks; no HA install, device or credentials."""

import asyncio
from asyncio import Lock
from collections import OrderedDict
import gc
import threading
from types import MappingProxyType, SimpleNamespace
import unittest
import weakref

from custom_components.petsafe_freshfeed import source_binding as sb
from custom_components.petsafe_freshfeed.source_binding import (
    SourceBinding, SourceBindingError, bind_source)

DEVICE_ID = "synthetic-device"
LEAF_ID = "a" * 32
HITS = []


class Tripwire(str):
    """Equal to everything; records any equality or hash use."""

    def __eq__(self, other):
        HITS.append("eq")
        return True

    def __ne__(self, other):
        HITS.append("ne")
        return False

    def __hash__(self):
        HITS.append("hash")
        return str.__hash__(self)


class Canary:
    """Credential stand-in that records any use of its value."""

    def _hit(self, *args):
        HITS.append("canary")
        raise AssertionError("credential value used")

    __eq__ = __hash__ = __repr__ = __str__ = __bool__ = __len__ = _hit
    __reduce__ = __reduce_ex__ = __format__ = __iter__ = _hit


class Api(SimpleNamespace):
    """Synthetic SDK object; every method and credential read is a tripwire."""

    @property
    def local_key(self):
        HITS.append("api.local_key")
        raise AssertionError("credential read")

    def _call(self, *args, **kwargs):
        HITS.append("api.call")
        raise AssertionError("sdk call")

    status = set_value = set_multiple_values = heartbeat = _call
    _send_receive = send = receive = detect_available_dps = _call


class Wrapper(SimpleNamespace):
    @property
    def local_key(self):
        HITS.append("wrapper.local_key")
        raise AssertionError("credential read")

    def _call(self, *args, **kwargs):
        HITS.append("wrapper.call")
        raise AssertionError("wrapper call")

    async_refresh = async_set_property = async_set_properties = _call
    async_possible_types = receive_loop = _call


class Leaf(SimpleNamespace):
    @property
    def config_entries(self):
        HITS.append("config_entries")
        raise AssertionError("deprecated property read")


def make_fixture(*, data=None, options=None, lock=None):
    api = Api(parent=None, id=DEVICE_ID)
    lock = Lock() if lock is None else lock
    wrapper = Wrapper(_api=api, _api_lock=lock, dev_cid=None,
                      dev_id=DEVICE_ID, unique_id=DEVICE_ID)
    record = {"device": wrapper, "tuyadevice": api, "tuyadevicelock": lock}
    entry = SimpleNamespace(
        domain="tuya_local", state=SimpleNamespace(value="loaded"),
        entry_id="synthetic-entry",
        data=MappingProxyType({}) if data is None else data,
        options=(MappingProxyType({"type": "petsafe_freshfeed_raw",
                                   "device_id": DEVICE_ID})
                 if options is None else options),
    )
    registry_device = Leaf(
        id=LEAF_ID, config_entry_id=entry.entry_id, config_subentry_id=None,
        is_composite_device=False,
        identifiers={("tuya_local", DEVICE_ID)},
    )
    domain_data = {DEVICE_ID: record}
    return SimpleNamespace(api=api, lock=lock, wrapper=wrapper, record=record,
                           entry=entry, registry_device=registry_device,
                           domain_data=domain_data)


def kwargs(f):
    return {"entry": f.entry, "registry_device": f.registry_device,
            "domain_data": f.domain_data}


class FixtureMixin:
    def setUp(self):
        HITS.clear()

    def assert_fault(self, f=None, **overrides):
        args = kwargs(f) if f is not None else {}
        args.update(overrides)
        with self.assertRaises(SourceBindingError) as caught:
            bind_source(**args)
        error = caught.exception
        self.assertIs(type(error), SourceBindingError)
        self.assertEqual(error.args, ("source_binding_fault",))
        self.assertEqual(str(error), "source_binding_fault")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertEqual(HITS, [])

    def assert_binds(self, f):
        binding = bind_source(**kwargs(f))
        self.assertIs(type(binding), SourceBinding)
        self.assertIs(binding.is_current(**kwargs(f)), True)
        self.assertEqual(HITS, [])
        return binding


class BindAcceptTests(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def test_plan_fixture_binds_and_is_current(self):
        f = make_fixture()
        binding = bind_source(entry=f.entry, registry_device=f.registry_device,
                              domain_data=f.domain_data)
        self.assertIs(binding.is_current(entry=f.entry,
                                         registry_device=f.registry_device,
                                         domain_data=f.domain_data), True)
        self.assertEqual(HITS, [])

    async def test_exact_dict_data_and_options_supported(self):
        f = make_fixture(data={}, options={"type": "petsafe_freshfeed_raw",
                                           "device_id": DEVICE_ID})
        self.assert_binds(f)

    async def test_selected_fields_from_data_when_options_empty(self):
        f = make_fixture(
            data=MappingProxyType({"type": "petsafe_freshfeed_raw",
                                   "device_id": DEVICE_ID, "device_cid": ""}),
            options=MappingProxyType({}))
        self.assert_binds(f)

    async def test_options_override_data_for_selected_fields(self):
        f = make_fixture(
            data=MappingProxyType({"type": "other_profile", "device_id": "other",
                                   "device_cid": "child"}),
            options=MappingProxyType({"type": "petsafe_freshfeed_raw",
                                      "device_id": DEVICE_ID, "device_cid": None}))
        self.assert_binds(f)

    async def test_options_precedence_rejects_wrong_option_over_right_data(self):
        f = make_fixture(
            data=MappingProxyType({"type": "petsafe_freshfeed_raw",
                                   "device_id": DEVICE_ID}),
            options=MappingProxyType({"type": "other_profile"}))
        self.assert_fault(f)

    async def test_fields_merge_per_key_across_data_and_options(self):
        f = make_fixture(
            data=MappingProxyType({"device_id": DEVICE_ID}),
            options=MappingProxyType({"type": "petsafe_freshfeed_raw",
                                      "device_cid": ""}))
        self.assert_binds(f)

    async def test_unrelated_credentials_never_read_or_disclosed(self):
        canary = Canary()
        f = make_fixture(
            data=MappingProxyType({"local_key": canary, "host": canary,
                                   "device_id": DEVICE_ID}),
            options=MappingProxyType({"type": "petsafe_freshfeed_raw",
                                      "local_key": canary}))
        f.domain_data["other-device"] = canary
        f.record["extra"] = canary
        binding = self.assert_binds(f)
        text = repr(binding)
        for private in (DEVICE_ID, "synthetic-entry", LEAF_ID, "Wrapper", "Api",
                        "namespace", "petsafe_freshfeed_raw"):
            self.assertNotIn(private, text)
        self.assertNotIn(DEVICE_ID, str(binding))
        self.assertEqual(HITS, [])

    async def test_deprecated_config_entries_never_read(self):
        f = make_fixture()
        binding = self.assert_binds(f)
        binding.is_current(**kwargs(f))
        self.assertNotIn("config_entries", HITS)

    async def test_bind_does_not_mutate_or_bind_sources(self):
        f = make_fixture()
        before = [dict(vars(o)) for o in (f.api, f.wrapper, f.entry,
                                          f.registry_device)]
        record_before, domain_before = dict(f.record), dict(f.domain_data)
        self.assert_binds(f)
        after = [dict(vars(o)) for o in (f.api, f.wrapper, f.entry,
                                         f.registry_device)]
        for old, new in zip(before, after):
            self.assertEqual(old.keys(), new.keys())
            for key in old:
                self.assertIs(old[key], new[key])
        self.assertEqual(record_before, f.record)
        self.assertEqual(domain_before, f.domain_data)
        self.assertFalse(f.lock.locked())
        self.assertIsNone(f.lock._loop)

    async def test_binding_does_not_retain_lock_after_release(self):
        f = make_fixture()
        lock_ref = weakref.ref(f.lock)
        binding = self.assert_binds(f)
        del binding, f
        gc.collect()
        self.assertIsNone(lock_ref())


class BindRejectEntryTests(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def test_wrong_domain(self):
        f = make_fixture()
        f.entry.domain = "localtuya"
        self.assert_fault(f)

    async def test_domain_subclass(self):
        f = make_fixture()
        f.entry.domain = Tripwire("tuya_local")
        self.assert_fault(f)

    async def test_wrong_state(self):
        for value in ("setup_error", "not_loaded", Tripwire("loaded"), None):
            with self.subTest(value=value):
                f = make_fixture()
                f.entry.state = SimpleNamespace(value=value)
                self.assert_fault(f)

    async def test_bad_entry_id(self):
        for value in ("", "x" * 129, Tripwire("synthetic-entry"), 7, None):
            with self.subTest(value=value):
                f = make_fixture()
                f.entry.entry_id = value
                if type(value) is str:
                    f.registry_device.config_entry_id = value
                self.assert_fault(f)

    async def test_wrong_or_missing_profile(self):
        for options in ({"device_id": DEVICE_ID},
                        {"type": "petsafe_freshfeed", "device_id": DEVICE_ID},
                        {"type": Tripwire("petsafe_freshfeed_raw"),
                         "device_id": DEVICE_ID}):
            with self.subTest(options=options):
                HITS.clear()
                f = make_fixture(options=MappingProxyType(options))
                self.assert_fault(f)

    async def test_bad_device_id(self):
        for value in ("", "x" * 129, "synthetic\x00device", 12, None):
            with self.subTest(value=value):
                f = make_fixture(options=MappingProxyType(
                    {"type": "petsafe_freshfeed_raw", "device_id": value}))
                if type(value) is str:
                    for o in (f.api,):
                        o.id = value
                    f.wrapper.dev_id = f.wrapper.unique_id = value
                    f.registry_device.identifiers = {("tuya_local", value)}
                    f.domain_data = {value: f.record}
                self.assert_fault(f)

    async def test_device_id_subclass_never_compared(self):
        value = Tripwire(DEVICE_ID)
        f = make_fixture(options=MappingProxyType(
            {"type": "petsafe_freshfeed_raw", "device_id": value}))
        HITS.clear()
        self.assert_fault(f)

    async def test_device_id_not_matching_source_record(self):
        f = make_fixture(options=MappingProxyType(
            {"type": "petsafe_freshfeed_raw", "device_id": "other-device"}))
        self.assert_fault(f)

    async def test_bad_device_cid(self):
        for value in ("child", Tripwire(""), 0, False):
            with self.subTest(value=value):
                f = make_fixture(options=MappingProxyType(
                    {"type": "petsafe_freshfeed_raw", "device_id": DEVICE_ID,
                     "device_cid": value}))
                HITS.clear()
                self.assert_fault(f)

    async def test_unsupported_mapping_containers(self):
        good = {"type": "petsafe_freshfeed_raw", "device_id": DEVICE_ID}

        class DictSub(dict):
            pass

        for container in (OrderedDict(good), DictSub(good), list(good.items()),
                          None, frozenset(good)):
            for field in ("data", "options"):
                with self.subTest(container=type(container), field=field):
                    f = make_fixture()
                    setattr(f.entry, field, container)
                    self.assert_fault(f)

    async def test_non_str_key_in_options(self):
        f = make_fixture(options=MappingProxyType(
            {"type": "petsafe_freshfeed_raw", "device_id": DEVICE_ID, 1: "x"}))
        self.assert_fault(f)

    async def test_str_subclass_key_rejected_before_hash_or_eq(self):
        for field in ("data", "options"):
            with self.subTest(field=field):
                key = Tripwire("device_id")
                f = make_fixture()
                setattr(f.entry, field, MappingProxyType(
                    {"type": "petsafe_freshfeed_raw", key: DEVICE_ID}))
                HITS.clear()
                self.assert_fault(f)


class BindRejectSourceTests(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def test_domain_data_not_exact_dict(self):
        f = make_fixture()

        class DictSub(dict):
            pass

        for value in (DictSub(f.domain_data), MappingProxyType(f.domain_data),
                      None, [(DEVICE_ID, f.record)]):
            with self.subTest(value=type(value)):
                self.assert_fault(f, domain_data=value)

    async def test_domain_data_key_subclass(self):
        f = make_fixture()
        f.domain_data[Tripwire("unrelated")] = {}
        HITS.clear()
        self.assert_fault(f)

    async def test_domain_data_non_str_key(self):
        f = make_fixture()
        f.domain_data[3] = {}
        self.assert_fault(f)

    async def test_missing_record(self):
        f = make_fixture()
        self.assert_fault(f, domain_data={})

    async def test_malformed_record(self):
        class DictSub(dict):
            pass

        for mutate in (
                lambda f: f.domain_data.__setitem__(DEVICE_ID, DictSub(f.record)),
                lambda f: f.domain_data.__setitem__(
                    DEVICE_ID, MappingProxyType(f.record)),
                lambda f: f.record.pop("device"),
                lambda f: f.record.pop("tuyadevice"),
                lambda f: f.record.pop("tuyadevicelock"),
                lambda f: f.record.__setitem__(5, None),
                lambda f: f.record.__setitem__(Tripwire("extra"), None)):
            with self.subTest(mutate=mutate):
                f = make_fixture()
                mutate(f)
                HITS.clear()
                self.assert_fault(f)

    async def test_api_parent_or_id_mismatch(self):
        for name, value in (("parent", object()), ("id", "other-device"),
                            ("id", Tripwire(DEVICE_ID)), ("id", None)):
            with self.subTest(name=name, value=value):
                f = make_fixture()
                setattr(f.api, name, value)
                HITS.clear()
                self.assert_fault(f)

    async def test_wrapper_field_mismatch(self):
        for name, value in (("dev_id", "other-device"),
                            ("dev_id", Tripwire(DEVICE_ID)),
                            ("unique_id", "other-device"),
                            ("unique_id", Tripwire(DEVICE_ID)),
                            ("dev_cid", "child"), ("dev_cid", "")):
            with self.subTest(name=name, value=value):
                f = make_fixture()
                setattr(f.wrapper, name, value)
                HITS.clear()
                self.assert_fault(f)

    async def test_missing_attributes(self):
        for obj, name in (("api", "parent"), ("api", "id"), ("wrapper", "_api"),
                          ("wrapper", "_api_lock"), ("wrapper", "dev_cid"),
                          ("wrapper", "dev_id"), ("wrapper", "unique_id")):
            with self.subTest(obj=obj, name=name):
                f = make_fixture()
                delattr(getattr(f, obj), name)
                self.assert_fault(f)

    async def test_api_alias_split(self):
        f = make_fixture()
        f.record["tuyadevice"] = Api(parent=None, id=DEVICE_ID)
        self.assert_fault(f)
        f = make_fixture()
        f.wrapper._api = Api(parent=None, id=DEVICE_ID)
        self.assert_fault(f)

    async def test_lock_alias_split(self):
        f = make_fixture()
        f.record["tuyadevicelock"] = Lock()
        self.assert_fault(f)
        f = make_fixture()
        f.wrapper._api_lock = Lock()
        self.assert_fault(f)

    async def test_lock_type_exact_asyncio_lock(self):
        class LockSub(Lock):
            pass

        for lock in (threading.Lock(), LockSub(), asyncio.Semaphore(), None):
            with self.subTest(lock=type(lock)):
                f = make_fixture(lock=lock) if lock is not None else make_fixture()
                if lock is None:
                    f.wrapper._api_lock = f.record["tuyadevicelock"] = None
                self.assert_fault(f)

    async def test_record_wrapper_with_foreign_api(self):
        f = make_fixture()
        f.record["device"] = Wrapper(_api=Api(parent=None, id=DEVICE_ID),
                                     _api_lock=f.lock, dev_cid=None,
                                     dev_id=DEVICE_ID, unique_id=DEVICE_ID)
        self.assert_fault(f)


class BindRejectLeafTests(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def test_bad_leaf_id(self):
        for value in ("A" * 32, "a" * 31, "a" * 33, "g" * 32, Tripwire("a" * 32),
                      None, 1):
            with self.subTest(value=value):
                f = make_fixture()
                f.registry_device.id = value
                HITS.clear()
                self.assert_fault(f)

    async def test_cross_entry_leaf(self):
        for value in ("other-entry", Tripwire("synthetic-entry"), None):
            with self.subTest(value=value):
                f = make_fixture()
                f.registry_device.config_entry_id = value
                HITS.clear()
                self.assert_fault(f)

    async def test_subentry_leaf(self):
        for value in ("sub", ""):
            with self.subTest(value=value):
                f = make_fixture()
                f.registry_device.config_subentry_id = value
                self.assert_fault(f)

    async def test_composite_flag(self):
        for value in (True, 0, None, "False"):
            with self.subTest(value=value):
                f = make_fixture()
                f.registry_device.is_composite_device = value
                self.assert_fault(f)

    async def test_identifiers_container_and_members(self):
        good = ("tuya_local", DEVICE_ID)
        for value in (frozenset({good}), [good], {good: None}, None,
                      {good, ("tuya_local",)},
                      {good, ("tuya_local", DEVICE_ID, "x")},
                      {good, ("tuya_local", 5)},
                      {good, ("tuya_local", Tripwire("other"))},
                      {("tuya_local", "other-device")},
                      {("localtuya", DEVICE_ID)},
                      set()):
            with self.subTest(value=value):
                f = make_fixture()
                f.registry_device.identifiers = value
                HITS.clear()
                self.assert_fault(f)

    async def test_identifier_list_member_rejected(self):
        class ListLike(tuple):
            pass

        f = make_fixture()
        f.registry_device.identifiers = {ListLike(("tuya_local", DEVICE_ID))}
        self.assert_fault(f)

    async def test_missing_leaf_attribute(self):
        for name in ("id", "config_entry_id", "config_subentry_id",
                     "is_composite_device", "identifiers"):
            with self.subTest(name=name):
                f = make_fixture()
                delattr(f.registry_device, name)
                self.assert_fault(f)


class IsCurrentDriftTests(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def bound(self):
        f = make_fixture()
        return f, bind_source(**kwargs(f))

    async def assert_stale(self, mutate):
        f, binding = await self.bound()
        mutate(f)
        HITS.clear()
        self.assertIs(binding.is_current(**kwargs(f)), False)
        self.assertEqual(HITS, [])

    async def test_leaf_id_change(self):
        await self.assert_stale(lambda f: setattr(f.registry_device, "id", "b" * 32))

    async def test_leaf_config_entry_change(self):
        def mutate(f):
            f.registry_device.config_entry_id = "other-entry"
        await self.assert_stale(mutate)

    async def test_leaf_identifiers_change(self):
        await self.assert_stale(lambda f: setattr(
            f.registry_device, "identifiers", {("tuya_local", "other")}))

    async def test_leaf_identifiers_mutated_in_place(self):
        def mutate(f):
            f.registry_device.identifiers.clear()
        await self.assert_stale(mutate)

    async def test_leaf_subentry_or_composite_change(self):
        await self.assert_stale(lambda f: setattr(
            f.registry_device, "config_subentry_id", "sub"))
        await self.assert_stale(lambda f: setattr(
            f.registry_device, "is_composite_device", True))

    async def test_options_profile_change(self):
        await self.assert_stale(lambda f: setattr(f.entry, "options", MappingProxyType(
            {"type": "other_profile", "device_id": DEVICE_ID})))

    async def test_options_device_id_change(self):
        def mutate(f):
            f.entry.options = MappingProxyType(
                {"type": "petsafe_freshfeed_raw", "device_id": "other"})
            f.domain_data["other"] = f.record
        await self.assert_stale(mutate)

    async def test_options_cid_change(self):
        await self.assert_stale(lambda f: setattr(f.entry, "options", MappingProxyType(
            {"type": "petsafe_freshfeed_raw", "device_id": DEVICE_ID,
             "device_cid": "child"})))

    async def test_options_cid_none_to_empty_change(self):
        await self.assert_stale(lambda f: setattr(f.entry, "options", MappingProxyType(
            {"type": "petsafe_freshfeed_raw", "device_id": DEVICE_ID,
             "device_cid": ""})))

    async def test_underlying_options_dict_mutation(self):
        backing = {"type": "petsafe_freshfeed_raw", "device_id": DEVICE_ID}
        f = make_fixture(options=MappingProxyType(backing))
        binding = bind_source(**kwargs(f))
        backing["device_cid"] = "child"
        self.assertIs(binding.is_current(**kwargs(f)), False)

    async def test_data_override_appears(self):
        f = make_fixture(data={}, options={"type": "petsafe_freshfeed_raw",
                                           "device_id": DEVICE_ID})
        binding = bind_source(**kwargs(f))
        f.entry.data["device_cid"] = "child"
        self.assertIs(binding.is_current(**kwargs(f)), False)

    async def test_entry_replaced(self):
        f, binding = await self.bound()
        clone = SimpleNamespace(**vars(f.entry))
        self.assertIs(binding.is_current(entry=clone,
                                         registry_device=f.registry_device,
                                         domain_data=f.domain_data), False)

    async def test_registry_leaf_replaced(self):
        f, binding = await self.bound()
        clone = Leaf(**vars(f.registry_device))
        self.assertIs(binding.is_current(entry=f.entry, registry_device=clone,
                                         domain_data=f.domain_data), False)

    async def test_wrapper_replaced(self):
        def mutate(f):
            f.record["device"] = Wrapper(**vars(f.wrapper))
        await self.assert_stale(mutate)

    async def test_api_replaced(self):
        def mutate(f):
            api = Api(parent=None, id=DEVICE_ID)
            f.wrapper._api = api
            f.record["tuyadevice"] = api
        await self.assert_stale(mutate)

    async def test_lock_replaced(self):
        def mutate(f):
            lock = Lock()
            f.wrapper._api_lock = lock
            f.record["tuyadevicelock"] = lock
        await self.assert_stale(mutate)

    async def test_record_replaced_with_same_references(self):
        await self.assert_stale(lambda f: f.domain_data.__setitem__(
            DEVICE_ID, dict(f.record)))

    async def test_api_alias_split_after_bind(self):
        await self.assert_stale(lambda f: setattr(
            f.wrapper, "_api", Api(parent=None, id=DEVICE_ID)))

    async def test_wrapper_ids_drift(self):
        await self.assert_stale(lambda f: setattr(f.wrapper, "unique_id", "x"))
        await self.assert_stale(lambda f: setattr(f.wrapper, "dev_cid", "child"))
        await self.assert_stale(lambda f: setattr(f.api, "parent", object()))

    async def test_source_removed(self):
        await self.assert_stale(lambda f: f.domain_data.pop(DEVICE_ID))

    async def test_state_changed(self):
        await self.assert_stale(lambda f: setattr(
            f.entry, "state", SimpleNamespace(value="not_loaded")))

    async def test_subclass_scalar_drift_without_comparison(self):
        await self.assert_stale(lambda f: setattr(
            f.entry, "domain", Tripwire("tuya_local")))

    async def test_garbage_arguments_return_false(self):
        f, binding = await self.bound()
        self.assertIs(binding.is_current(entry=None, registry_device=None,
                                         domain_data=None), False)

    async def test_raising_attribute_returns_false(self):
        f, binding = await self.bound()

        class Boom:
            @property
            def value(self):
                raise RuntimeError("private detail")

        f.entry.state = Boom()
        self.assertIs(binding.is_current(**kwargs(f)), False)

    async def test_unbound_holder_is_never_current(self):
        f = make_fixture()
        self.assertIs(SourceBinding().is_current(**kwargs(f)), False)


class LoopOwnershipTests(FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.loops = [asyncio.new_event_loop(), asyncio.new_event_loop()]

    def tearDown(self):
        for loop in self.loops:
            loop.close()

    def run_on(self, index, coro):
        return self.loops[index].run_until_complete(coro)

    def test_bind_requires_running_loop(self):
        f = self.run_on(0, self._make())
        self.assert_fault(f)

    async def _make(self):
        return make_fixture()

    async def _bind(self, f):
        return bind_source(**kwargs(f))

    async def _current(self, binding, f):
        return binding.is_current(**kwargs(f))

    async def _expect_fault(self, f):
        self.assert_fault(f)

    def test_rebind_same_loop_allowed(self):
        f = self.run_on(0, self._make())
        self.run_on(0, self._bind(f))
        second = self.run_on(0, self._bind(f))
        self.assertIs(self.run_on(0, self._current(second, f)), True)

    def test_bind_same_lock_from_other_loop_rejected(self):
        f = self.run_on(0, self._make())
        self.run_on(0, self._bind(f))
        self.run_on(1, self._expect_fault(f))

    def test_new_source_reusing_bound_lock_on_other_loop_rejected(self):
        f = self.run_on(0, self._make())
        self.run_on(0, self._bind(f))

        async def other():
            g = make_fixture(lock=f.lock)
            self.assert_fault(g)

        self.run_on(1, other())

    def test_lock_loop_already_bound_elsewhere_rejected(self):
        f = self.run_on(0, self._make())

        async def contend():
            await f.lock.acquire()
            waiter = asyncio.ensure_future(f.lock.acquire())
            await asyncio.sleep(0)
            f.lock.release()
            await waiter
            f.lock.release()

        self.run_on(0, contend())
        self.assertIs(f.lock._loop, self.loops[0])
        self.run_on(1, self._expect_fault(f))

    def test_is_current_on_other_loop_false(self):
        f = self.run_on(0, self._make())
        binding = self.run_on(0, self._bind(f))
        self.assertIs(self.run_on(1, self._current(binding, f)), False)
        self.assertIs(self.run_on(0, self._current(binding, f)), True)

    def test_is_current_without_running_loop_false(self):
        f = self.run_on(0, self._make())
        binding = self.run_on(0, self._bind(f))
        self.assertIs(binding.is_current(**kwargs(f)), False)

    def test_first_admitted_loop_released_with_lock(self):
        f = self.run_on(0, self._make())
        binding = self.run_on(0, self._bind(f))
        lock_ref = weakref.ref(f.lock)
        del binding, f
        gc.collect()
        self.assertIsNone(lock_ref())
        g = self.run_on(1, self._make())
        self.run_on(1, self._bind(g))


class ModuleSurfaceTests(unittest.TestCase):
    def test_error_is_runtime_error_with_fixed_message(self):
        error = SourceBindingError()
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), "source_binding_fault")

    def test_no_ha_or_sdk_imports(self):
        import sys
        self.assertIsNotNone(sb)
        self.assertNotIn("homeassistant", sys.modules)
        self.assertNotIn("tinytuya", sys.modules)


if __name__ == "__main__":
    unittest.main()
