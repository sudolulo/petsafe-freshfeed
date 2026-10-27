"""Owned synthetic store bootstrap boundaries; no HA config root, device or credentials."""

import asyncio
import contextlib
import os
from pathlib import Path
import stat
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

from custom_components.petsafe_freshfeed import sqlite_witness as sw
from custom_components.petsafe_freshfeed import store_bootstrap as bs
from custom_components.petsafe_freshfeed.store_bootstrap import (
    StoreBootstrapError, StoreDescriptor, async_provision_store, open_store)

STORE, BINDING = "a" * 32, "b" * 32
OLD = {"operation_id": "3" * 32, "operation": "feed", "state": "pending"}
NS = "petsafe_freshfeed"
REPAIR = ("chmod", "chown", "lchown", "fchmod", "fchown", "rmdir", "unlink",
          "remove", "rename", "replace", "removedirs", "truncate", "ftruncate")
REAL_PREPARE = bs._prepare
REAL_POSTCHECK = bs._postcheck
REAL_PROVISION = sw.SQLiteIntentWitness.async_provision.__func__
REAL_MKDIR = bs._mkdir
REAL_FSYNC = bs._fsync
REAL_STAT_AT = bs._stat_at


def open_fds():
    return len(os.listdir("/proc/self/fd"))


def owned_threads():
    return [t for t in threading.enumerate() if t.name.startswith("petsafe-")]


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


async def wait_event(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


class IoRecorder:
    """Records (and refuses) any filesystem access on the bootstrap/witness paths."""

    def __init__(self):
        self.calls = []

    def _hit(self, name):
        def refuse(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(name)
        return refuse

    @contextlib.contextmanager
    def active(self):
        with contextlib.ExitStack() as stack:
            # Global stat calls are left alone (asyncio debug traceback capture
            # uses os.stat); the module performs every stat through its seams.
            for name in ("open", "mkdir", "fsync", "listdir"):
                stack.enter_context(patch.object(bs.os, name, side_effect=self._hit(name)))
            for name in ("_submit", "_mkdir", "_fsync", "_stat_at", "_fstat"):
                stack.enter_context(patch.object(bs, name, side_effect=self._hit(name)))
            for name in ("_submit", "_open_sqlite", "_stat_at", "_stat_parent"):
                stack.enter_context(patch.object(sw, name, side_effect=self._hit(name)))
            yield


class RepairRecorder:
    def __init__(self):
        self.calls = []

    @contextlib.contextmanager
    def active(self):
        with contextlib.ExitStack() as stack:
            for name in REPAIR:
                if hasattr(os, name):
                    def refuse(*args, _name=name, **kwargs):
                        self.calls.append(_name)
                        raise AssertionError(_name)
                    stack.enter_context(patch.object(os, name, side_effect=refuse))
            yield


class BootstrapBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="store-bootstrap-", dir=Path(__file__).resolve().parent)
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        self.root = self.base / "config"
        self.root.mkdir(mode=0o700)
        self.tasks, self.releases = [], []
        self.provision_calls = []
        self.fd_baseline = open_fds()

    def new_root(self, label, root_mode=0o700):
        root = self.base / label
        root.mkdir(mode=0o700)
        os.chmod(root, root_mode)
        return root

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    @contextlib.contextmanager
    def spy_provision(self):
        calls = self.provision_calls

        async def spy(cls, path, *, store_id, binding_id):
            calls.append(path)
            return await REAL_PROVISION(cls, path, store_id=store_id, binding_id=binding_id)
        with patch.object(sw.SQLiteIntentWitness, "async_provision", classmethod(spy)):
            yield

    async def fault(self, awaitable, private=()):
        with self.assertRaises(StoreBootstrapError) as caught:
            await awaitable
        error = caught.exception
        self.assertEqual(str(error), "store_bootstrap_fault")
        self.assertEqual(error.args, ("store_bootstrap_fault",))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = "".join(traceback.format_exception(error)) + repr(error)
        for marker in (str(self.base), *private):
            self.assertNotIn(marker, rendered)
        return error

    def assert_settled(self):
        self.assertEqual(owned_threads(), [])
        self.assertEqual(open_fds(), self.fd_baseline)

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 8)
        # Fixture removal only after every owned worker settled; partial trees
        # left by failure tests are never "repaired", only removed with the
        # synthetic directory once nothing can still touch them.
        if not all(task.done() for task in self.tasks) or owned_threads():
            raise AssertionError("owned work did not settle; refuse fixture cleanup")
        self.assert_settled()
        self.tmp.cleanup()


class ProvisionAndReopenTests(BootstrapBase):
    async def test_explicit_provision_then_existing_only_reopen(self):
        descriptor = await async_provision_store(
            str(self.root), binding_id=BINDING, store_id=STORE)
        self.assertIsInstance(descriptor, StoreDescriptor)
        witness = open_store(str(self.root), binding_id=descriptor.binding_id,
                             store_id=descriptor.store_id)
        self.assertIsInstance(witness, sw.SQLiteIntentWitness)
        self.assertIsNone(await witness.async_load_last_intent())
        self.assertTrue(await witness.async_wait_idle(3))
        self.assert_settled()

    async def test_owned_root_any_mode_provisions_with_private_app_dirs(self):
        # The spec requires only an existing euid-owned real directory; its mode
        # is not constrained (0775 is the default under umask 0002).
        root_modes = (0o700, 0o755, 0o775, 0o777)
        for root_mode in root_modes:
            with self.subTest(root_mode=oct(root_mode)):
                root = self.new_root(f"root-{root_mode:o}", root_mode)
                with self.spy_provision():
                    descriptor = await async_provision_store(
                        str(root), binding_id=BINDING, store_id=STORE)
                expected = str(root / NS / BINDING / "intent.sqlite")
                self.assertEqual(descriptor.path, expected)
                self.assertEqual(descriptor.store_id, STORE)
                self.assertEqual(descriptor.binding_id, BINDING)
                self.assertEqual(mode(root), root_mode)
                self.assertEqual(mode(root / NS), 0o700)
                self.assertEqual(mode(root / NS / BINDING), 0o700)
                self.assertEqual(mode(expected), 0o600)
                self.assertEqual(sorted(os.listdir(root / NS / BINDING)), ["intent.sqlite"])
                self.assertNotIn(str(root), repr(descriptor))
                self.assertNotIn(str(self.base), str(descriptor))
                witness = open_store(str(root), binding_id=BINDING, store_id=STORE)
                self.assertIsNone(await witness.async_load_last_intent())
        self.assertEqual(len(self.provision_calls), len(root_modes))
        self.assert_settled()

    async def test_descriptor_is_read_only(self):
        descriptor = await async_provision_store(
            str(self.root), binding_id=BINDING, store_id=STORE)
        for name in ("path", "store_id", "binding_id", "extra"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                setattr(descriptor, name, "x")

    async def test_existing_namespace_is_accepted_when_owned_private(self):
        (self.root / NS).mkdir(mode=0o700)
        os.chmod(self.root / NS, 0o700)
        descriptor = await async_provision_store(
            str(self.root), binding_id=BINDING, store_id=STORE)
        other = await async_provision_store(
            str(self.root), binding_id="c" * 32, store_id=STORE)
        self.assertNotEqual(descriptor.path, other.path)

    async def test_prepared_old_is_read_by_fresh_handle(self):
        descriptor = await async_provision_store(
            str(self.root), binding_id=BINDING, store_id=STORE)
        first = open_store(str(self.root), binding_id=BINDING, store_id=STORE)
        await first.async_prepare_intent(OLD)
        self.assertTrue(await first.async_wait_idle(3))
        fresh = open_store(str(self.root), binding_id=descriptor.binding_id,
                           store_id=descriptor.store_id)
        self.assertIsNot(fresh, first)
        self.assertEqual(await fresh.async_load_last_intent(), OLD)


class OpenStoreTests(BootstrapBase):
    async def load_fault(self, witness):
        with self.assertRaises(sw.WitnessStorageError):
            await witness.async_load_last_intent()

    async def test_constructor_has_zero_io_and_missing_is_never_created(self):
        recorder = IoRecorder()
        with recorder.active():
            witness = open_store(str(self.root), binding_id=BINDING, store_id=STORE)
            root_witness = open_store("/", binding_id=BINDING, store_id=STORE)
        self.assertEqual(recorder.calls, [])
        self.assertIsInstance(root_witness, sw.SQLiteIntentWitness)
        await self.load_fault(witness)
        self.assertFalse((self.root / NS).exists())

    async def test_zero_corrupt_prior_failed_and_wrong_id_never_provision(self):
        binding_dir = self.root / NS / BINDING
        (self.root / NS).mkdir(mode=0o700)
        binding_dir.mkdir(mode=0o700)
        db = binding_dir / "intent.sqlite"
        with self.spy_provision():
            # Prior failed provisioning: binding directory without a database.
            await self.load_fault(open_store(str(self.root), binding_id=BINDING, store_id=STORE))
            self.assertEqual(os.listdir(binding_dir), [])
            await self.fault(async_provision_store(
                str(self.root), binding_id=BINDING, store_id=STORE))
            self.assertEqual(os.listdir(binding_dir), [])
            for label, content in (("zero", b""), ("corrupt", b"not a database" * 64)):
                with self.subTest(label=label):
                    fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.write(fd, content)
                    os.close(fd)
                    await self.load_fault(open_store(
                        str(self.root), binding_id=BINDING, store_id=STORE))
                    self.assertEqual(db.read_bytes(), content)
                    self.assertEqual(sorted(os.listdir(binding_dir)), ["intent.sqlite"])
                    os.unlink(db)  # synthetic fixture swap, outside any bootstrap call
        self.assertEqual(self.provision_calls, [])
        other = self.new_root("wrong-id")
        await async_provision_store(str(other), binding_id=BINDING, store_id=STORE)
        await self.load_fault(open_store(str(other), binding_id=BINDING, store_id="c" * 32))
        await self.load_fault(open_store(str(other), binding_id="c" * 32, store_id=STORE))
        self.assertFalse((other / NS / ("c" * 32)).exists())
        self.assertIsNone(await open_store(
            str(other), binding_id=BINDING, store_id=STORE).async_load_last_intent())


class ArgumentTests(BootstrapBase):
    async def test_invalid_arguments_raise_fixed_value_error_before_io(self):
        class StrSub(str):
            pass
        root = str(self.root)
        bad_roots = [Path(root), StrSub(root), root.encode(), None, 1, True, "",
                     "relative/config", root + "/", root + "/./x", root + "/../x",
                     "//config", "/x//y", root + "\x00", root + "/\ud800", "/."]
        bad_ids = [None, 1, True, "B" * 32, "b" * 31, "b" * 33, "g" * 32,
                   StrSub("b" * 32), ("b" * 32).encode(), " " + "b" * 31,
                   "b" * 31 + "\n"]
        cases = [dict(config_root=value, binding_id=BINDING, store_id=STORE)
                 for value in bad_roots]
        for value in bad_ids:
            cases.append(dict(config_root=root, binding_id=value, store_id=STORE))
            cases.append(dict(config_root=root, binding_id=BINDING, store_id=value))
        recorder = IoRecorder()
        messages = set()
        with recorder.active():
            for case in cases:
                args = dict(case)
                config_root = args.pop("config_root")
                for call in ("open", "provision"):
                    with self.subTest(call=call, case=repr(case)[:80]):
                        with self.assertRaises(ValueError) as caught:
                            if call == "open":
                                open_store(config_root, **args)
                            else:
                                await async_provision_store(config_root, **args)
                        messages.add(str(caught.exception))
                        self.assertIsNone(caught.exception.__cause__)
                        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(recorder.calls, [])
        self.assertEqual(len(messages), 1)
        self.assertNotIn(root, messages.pop())
        self.assertFalse((self.root / NS).exists())


class FilesystemBoundaryTests(BootstrapBase):
    async def rejected(self, root, private=()):
        repair = RepairRecorder()
        with self.spy_provision(), repair.active():
            await self.fault(async_provision_store(
                str(root), binding_id=BINDING, store_id=STORE), private)
        self.assertEqual(repair.calls, [])
        self.assertEqual(self.provision_calls, [])
        self.assert_settled()

    async def test_bad_config_roots_fail_without_creation(self):
        missing = self.base / "missing"
        await self.rejected(missing)
        self.assertFalse(missing.exists())
        not_dir = self.base / "file"
        not_dir.write_bytes(b"")
        await self.rejected(not_dir)
        real = self.new_root("real")
        link = self.base / "link"
        link.symlink_to(real)
        await self.rejected(link)
        nested = real / "cfg"
        nested.mkdir(mode=0o700)
        await self.rejected(link / "cfg")
        self.assertFalse((real / NS).exists())
        self.assertFalse((nested / NS).exists())
        with patch.object(bs, "_euid", return_value=os.geteuid() + 1):
            await self.rejected(self.root)
        self.assertFalse((self.root / NS).exists())

    async def test_bad_namespace_rejected_unchanged(self):
        ns = self.root / NS
        ns.mkdir(mode=0o700)
        os.chmod(ns, 0o755)
        await self.rejected(self.root)
        self.assertEqual(mode(ns), 0o755)
        self.assertEqual(os.listdir(ns), [])
        os.chmod(ns, 0o700)
        os.rmdir(ns)  # synthetic fixture swap between calls
        ns.write_bytes(b"")
        await self.rejected(self.root)
        self.assertTrue(ns.is_file())
        os.unlink(ns)
        target = self.new_root("ns-target")
        ns.symlink_to(target)
        await self.rejected(self.root)
        self.assertTrue(ns.is_symlink())
        self.assertEqual(os.listdir(target), [])
        os.unlink(ns)
        os.mkfifo(ns, 0o600)
        await self.rejected(self.root)
        self.assertTrue(stat.S_ISFIFO(os.lstat(ns).st_mode))
        os.unlink(ns)
        ns.mkdir(mode=0o700)

        def foreign(fd, name):
            info = REAL_STAT_AT(fd, name)
            if name == NS:
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info
        with patch.object(bs, "_stat_at", side_effect=foreign):
            await self.rejected(self.root)
        self.assertEqual(os.listdir(ns), [])

    async def test_any_existing_binding_path_rejected_before_sqlite(self):
        ns = self.root / NS
        ns.mkdir(mode=0o700)
        binding = ns / BINDING
        target = self.new_root("binding-target")
        fixtures = [
            ("private-dir", lambda: binding.mkdir(mode=0o700)),
            ("open-dir", lambda: (binding.mkdir(mode=0o700), os.chmod(binding, 0o755))),
            ("file", lambda: binding.write_bytes(b"")),
            ("symlink", lambda: binding.symlink_to(target)),
            ("dangling", lambda: binding.symlink_to(self.base / "nowhere")),
        ]
        for label, make in fixtures:
            with self.subTest(label=label):
                make()
                before = os.lstat(binding)
                await self.rejected(self.root)
                after = os.lstat(binding)
                self.assertEqual((before.st_ino, before.st_mode), (after.st_ino, after.st_mode))
                if binding.is_dir() and not binding.is_symlink():
                    self.assertEqual(os.listdir(binding), [])
                    os.chmod(binding, 0o700)
                    os.rmdir(binding)
                else:
                    os.unlink(binding)
        self.assertEqual(os.listdir(target), [])
        self.assertFalse((self.base / "nowhere").exists())

    async def test_repeated_provision_rejected_without_overwrite(self):
        descriptor = await async_provision_store(
            str(self.root), binding_id=BINDING, store_id=STORE)
        witness = open_store(str(self.root), binding_id=BINDING, store_id=STORE)
        await witness.async_prepare_intent(OLD)
        before = os.lstat(descriptor.path)
        await self.rejected(self.root)
        after = os.lstat(descriptor.path)
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertEqual(await open_store(
            str(self.root), binding_id=BINDING, store_id=STORE).async_load_last_intent(), OLD)

    async def test_binding_created_wrong_mode_or_owner_is_not_repaired(self):
        def loose(name, fd):
            os.mkdir(name, 0o750, dir_fd=fd)
        with patch.object(bs, "_mkdir", side_effect=lambda name, fd: (
                loose(name, fd) if name == BINDING else REAL_MKDIR(name, fd))):
            await self.rejected(self.root)
        self.assertEqual(mode(self.root / NS / BINDING), 0o750)
        self.assertEqual(os.listdir(self.root / NS / BINDING), [])
        other = self.new_root("foreign-binding")

        def foreign(fd, name):
            info = REAL_STAT_AT(fd, name)
            if name == BINDING:
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info
        with patch.object(bs, "_stat_at", side_effect=foreign):
            await self.rejected(other)
        self.assertTrue((other / NS / BINDING).is_dir())
        self.assertEqual(os.listdir(other / NS / BINDING), [])

    async def test_namespace_eexist_race_is_revalidated(self):
        def racer(kind):
            def mkdir(name, fd):
                if name != NS:
                    return REAL_MKDIR(name, fd)
                if kind == "private":
                    os.mkdir(name, 0o700, dir_fd=fd)
                elif kind == "open":
                    os.mkdir(name, 0o755, dir_fd=fd)
                else:
                    os.symlink(str(self.base / "race-target"), name, dir_fd=fd)
                raise FileExistsError(17, "private-race-marker")
            return mkdir
        with patch.object(bs, "_mkdir", side_effect=racer("private")):
            descriptor = await async_provision_store(
                str(self.root), binding_id=BINDING, store_id=STORE)
        self.assertTrue(os.path.isfile(descriptor.path))
        open_root = self.new_root("race-open")
        with patch.object(bs, "_mkdir", side_effect=racer("open")):
            await self.rejected(open_root, ("private-race-marker",))
        self.assertEqual(mode(open_root / NS), 0o755)
        self.assertEqual(os.listdir(open_root / NS), [])
        (self.base / "race-target").mkdir(mode=0o700)
        link_root = self.new_root("race-link")
        with patch.object(bs, "_mkdir", side_effect=racer("link")):
            await self.rejected(link_root, ("private-race-marker",))
        self.assertTrue((link_root / NS).is_symlink())
        self.assertEqual(os.listdir(self.base / "race-target"), [])

    async def test_mkdir_and_fsync_faults_leave_partial_tree_untouched(self):
        def failing_mkdir(target):
            def mkdir(name, fd):
                if name == target:
                    raise PermissionError(13, "private-mkdir-marker")
                return REAL_MKDIR(name, fd)
            return mkdir

        def failing_fsync(nth):
            count = [0]

            def fsync(fd):
                count[0] += 1
                if count[0] == nth:
                    raise OSError(5, "private-fsync-marker")
                return REAL_FSYNC(fd)
            return fsync

        root = self.new_root("ns-mkdir")
        with patch.object(bs, "_mkdir", side_effect=failing_mkdir(NS)):
            await self.rejected(root, ("private-mkdir-marker",))
        self.assertEqual(os.listdir(root), [])

        root = self.new_root("ns-fsync")
        with patch.object(bs, "_fsync", side_effect=failing_fsync(1)):
            await self.rejected(root, ("private-fsync-marker",))
        self.assertEqual(mode(root / NS), 0o700)
        self.assertEqual(os.listdir(root / NS), [])

        root = self.new_root("binding-mkdir")
        with patch.object(bs, "_mkdir", side_effect=failing_mkdir(BINDING)):
            await self.rejected(root, ("private-mkdir-marker",))
        self.assertEqual(os.listdir(root / NS), [])

        root = self.new_root("binding-fsync")
        with patch.object(bs, "_fsync", side_effect=failing_fsync(2)):
            await self.rejected(root, ("private-fsync-marker",))
        self.assertEqual(mode(root / NS / BINDING), 0o700)
        self.assertEqual(os.listdir(root / NS / BINDING), [])

    async def test_final_postcheck_fault_is_not_acknowledged(self):
        async def tamper_mode(cls, path, *, store_id, binding_id):
            witness = await REAL_PROVISION(cls, path, store_id=store_id, binding_id=binding_id)
            os.chmod(os.path.dirname(path), 0o750)
            return witness

        async def swap_dir(cls, path, *, store_id, binding_id):
            witness = await REAL_PROVISION(cls, path, store_id=store_id, binding_id=binding_id)
            parent = os.path.dirname(path)
            os.rename(parent, parent + "-moved")
            os.mkdir(parent, 0o700)
            os.rename(parent + "-moved/intent.sqlite", path)
            return witness

        async def swap_db(cls, path, *, store_id, binding_id):
            witness = await REAL_PROVISION(cls, path, store_id=store_id, binding_id=binding_id)
            os.chmod(path, 0o644)
            return witness

        for label, hook in (("mode", tamper_mode), ("swap", swap_dir), ("db", swap_db)):
            with self.subTest(label=label):
                root = self.new_root(f"post-{label}")
                with patch.object(sw.SQLiteIntentWitness, "async_provision", classmethod(hook)):
                    await self.fault(async_provision_store(
                        str(root), binding_id=BINDING, store_id=STORE))
                db = root / NS / BINDING / "intent.sqlite"
                self.assertTrue(db.is_file())
                if label == "mode":
                    self.assertEqual(mode(root / NS / BINDING), 0o750)
                if label == "db":
                    self.assertEqual(mode(db), 0o644)
                self.assert_settled()


class ConcurrencyTests(BootstrapBase):
    async def test_two_concurrent_provisions_have_one_reservation_winner(self):
        barrier = threading.Barrier(2, timeout=3)

        def gated(name, fd):
            if name == BINDING:
                barrier.wait()
            return REAL_MKDIR(name, fd)
        (self.root / NS).mkdir(mode=0o700)
        with self.spy_provision(), patch.object(bs, "_mkdir", side_effect=gated):
            results = await asyncio.gather(
                async_provision_store(str(self.root), binding_id=BINDING, store_id=STORE),
                async_provision_store(str(self.root), binding_id=BINDING, store_id=STORE),
                return_exceptions=True)
        winners = [r for r in results if isinstance(r, StoreDescriptor)]
        losers = [r for r in results if isinstance(r, StoreBootstrapError)]
        self.assertEqual((len(winners), len(losers)), (1, 1), results)
        self.assertEqual(str(losers[0]), "store_bootstrap_fault")
        self.assertEqual(len(self.provision_calls), 1)
        self.assertIsNone(await open_store(
            str(self.root), binding_id=BINDING, store_id=STORE).async_load_last_intent())
        self.assert_settled()

    async def test_pending_cancellation_before_submit_does_zero_io(self):
        recorder = IoRecorder()

        async def body():
            asyncio.current_task().cancel()
            await async_provision_store(str(self.root), binding_id=BINDING, store_id=STORE)
        with recorder.active():
            task = asyncio.create_task(body())
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(recorder.calls, [])
        self.assertFalse((self.root / NS).exists())

    async def cancel_repeatedly(self, task, entered, release):
        await wait_event(entered)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_repeated_cancellation_drains_preparation_worker(self):
        for outcome in ("success", "error"):
            with self.subTest(outcome=outcome):
                root = self.new_root(f"prep-{outcome}")
                entered, release, finished = threading.Event(), threading.Event(), []
                self.releases.append(release)

                def blocked(*args):
                    entered.set()
                    release.wait(5)
                    try:
                        if outcome == "error":
                            raise RuntimeError("private-worker-marker")
                        return REAL_PREPARE(*args)
                    finally:
                        finished.append(threading.current_thread().name)
                with self.spy_provision(), patch.object(bs, "_prepare", side_effect=blocked):
                    task = self.start(async_provision_store(
                        str(root), binding_id=BINDING, store_id=STORE))
                    await self.cancel_repeatedly(task, entered, release)
                self.assertEqual(len(finished), 1)
                self.assertTrue(finished[0].startswith("petsafe-bootstrap"))
                self.assertEqual(self.provision_calls, [])
                self.assert_settled()
                if outcome == "success":
                    self.assertEqual(os.listdir(root / NS / BINDING), [])

    async def test_cancellation_during_sqlite_provision_drains_witness(self):
        entered, release, finished = threading.Event(), threading.Event(), []
        self.releases.append(release)
        real_open = sw._open_sqlite

        def blocked(path):
            entered.set()
            release.wait(5)
            finished.append(True)
            return real_open(path)
        with patch.object(sw, "_open_sqlite", side_effect=blocked), \
             patch.object(bs, "_postcheck", side_effect=AssertionError("postcheck")) as post:
            task = self.start(async_provision_store(
                str(self.root), binding_id=BINDING, store_id=STORE))
            await self.cancel_repeatedly(task, entered, release)
        post.assert_not_called()
        self.assertEqual(finished, [True])
        self.assert_settled()
        db = self.root / NS / BINDING / "intent.sqlite"
        self.assertTrue(db.is_file())
        await self.fault(async_provision_store(
            str(self.root), binding_id=BINDING, store_id=STORE))

    async def test_final_postcheck_barrier_drains_before_outcome(self):
        for outcome in ("cancel-success", "cancel-error", "error", "success"):
            with self.subTest(outcome=outcome):
                root = self.new_root(f"post-{outcome}")
                entered, release, finished = threading.Event(), threading.Event(), []
                self.releases.append(release)

                def blocked(*args):
                    entered.set()
                    release.wait(5)
                    try:
                        if outcome.endswith("error"):
                            raise RuntimeError("private-postcheck-marker")
                        return REAL_POSTCHECK(*args)
                    finally:
                        finished.append(open_fds())
                with patch.object(bs, "_postcheck", side_effect=blocked):
                    task = self.start(async_provision_store(
                        str(root), binding_id=BINDING, store_id=STORE))
                    if outcome.startswith("cancel"):
                        await self.cancel_repeatedly(task, entered, release)
                    else:
                        await wait_event(entered)
                        release.set()
                        if outcome == "error":
                            await self.fault(task, ("private-postcheck-marker",))
                        else:
                            self.assertIsInstance(await task, StoreDescriptor)
                self.assertEqual(len(finished), 1)
                self.assert_settled()
                self.assertTrue((root / NS / BINDING / "intent.sqlite").is_file())


class DisclosureTests(BootstrapBase):
    async def test_error_and_descriptor_reprs_have_no_private_detail(self):
        marker = self.base / "private-root-marker"
        error = await self.fault(async_provision_store(
            str(marker), binding_id=BINDING, store_id=STORE), ("private-root-marker",))
        self.assertEqual(repr(error), "StoreBootstrapError('store_bootstrap_fault')")
        descriptor = await async_provision_store(
            str(self.root), binding_id=BINDING, store_id=STORE)
        for rendered in (repr(descriptor), str(descriptor)):
            self.assertNotIn(str(self.root), rendered)
            self.assertNotIn("/", rendered)


if __name__ == "__main__":
    unittest.main()
