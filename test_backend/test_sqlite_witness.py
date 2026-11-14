"""Owned synthetic SQLite boundaries, not VFS/power-loss/HA admission evidence."""

import asyncio
import gc
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import traceback
import unittest
import weakref
from unittest.mock import patch

from custom_components.petsafe_freshfeed import sqlite_witness as sw

STORE, BINDING = "1" * 32, "2" * 32
OLD = {"operation_id": "3" * 32, "operation": "feed", "state": "pending"}
NEW = {"operation_id": "4" * 32, "operation": "settings", "state": "unknown"}


async def thread_entered(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


class CursorProbe:
    def __init__(self, owner, cursor):
        self.owner, self.real = owner, cursor
        self.closed = False
        self.sql = ""

    def execute(self, sql, parameters=()):
        self.sql = sql
        self.owner.sql.append(sql)
        self.owner.before(sql)
        self.real.execute(sql, parameters)
        self.owner.after(sql)
        return self

    def fetchall(self):
        return self.owner.rows(self.sql, self.real.fetchall())

    @property
    def description(self):
        return self.real.description

    @property
    def rowcount(self):
        return self.real.rowcount

    def close(self):
        self.real.close()
        self.closed = True


class ConnectionProbe:
    def __init__(self, real, before=lambda sql: None, after=lambda sql: None,
                 rows=lambda sql, rows: rows):
        self.real, self.before, self.after, self.rows = real, before, after, rows
        self.sql, self.cursors = [], []
        self.closed = False
        self.thread = threading.get_ident()
        self.thread_object = threading.current_thread()

    def cursor(self):
        assert threading.get_ident() == self.thread
        cursor = CursorProbe(self, self.real.cursor())
        self.cursors.append(cursor)
        return cursor

    def execute(self, *args, **kwargs):
        raise AssertionError("adapter must own explicit cursors")

    def close(self):
        assert threading.get_ident() == self.thread
        self.real.close()
        self.closed = True

    def __getattr__(self, name):
        return getattr(self.real, name)


class ProbeFactory:
    def __init__(self, before=lambda sql: None, after=lambda sql: None,
                 rows=lambda sql, rows: rows):
        self.open = sw._open_sqlite
        self.before, self.after, self.rows = before, after, rows
        self.connections = []

    def __call__(self, path):
        probe = ConnectionProbe(self.open(path), self.before, self.after, self.rows)
        self.connections.append(probe)
        return probe

    def assert_closed(self, test):
        test.assertTrue(self.connections)
        for conn in self.connections:
            test.assertTrue(conn.closed)
            test.assertTrue(all(cursor.closed for cursor in conn.cursors))


def changed_stat(info, *, uid=None, dev=None, ino=None):
    values = list(info)
    for index, value in ((4, uid), (2, dev), (1, ino)):
        if value is not None:
            values[index] = value
    return os.stat_result(values)


def foreign_loop_load(path):
    async def run():
        try:
            witness = sw.SQLiteIntentWitness(path, store_id=STORE, binding_id=BINDING)
            record = await witness.async_load_last_intent()
            if not await witness.async_wait_idle(2):
                return ("rejected", "not_idle")
            return ("loaded", record)
        except (ValueError, RuntimeError) as error:
            return ("rejected", str(error))
    return asyncio.run(run())


class SQLiteWitnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="sqlite-witness-", dir=Path(__file__).resolve().parent)
        self.root = Path(self.tmp.name)
        os.chmod(self.root, 0o700)
        self.tasks, self.releases, self.handles = [], [], []

    def path(self, label):
        parent = self.root / label
        parent.mkdir(mode=0o700)
        return str(parent / "witness.sqlite")

    def handle(self, path, store=STORE, binding=BINDING):
        witness = sw.SQLiteIntentWitness(path, store_id=store, binding_id=binding)
        self.handles.append(witness)
        return witness

    async def provision(self, path):
        witness = await sw.SQLiteIntentWitness.async_provision(
            path, store_id=STORE, binding_id=BINDING)
        self.handles.append(witness)
        return witness

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def edit(self, path, statements):
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True,
                               autocommit=True, timeout=0, cached_statements=0)
        try:
            for sql, args in statements:
                cursor = conn.cursor()
                try:
                    cursor.execute(sql, args)
                finally:
                    cursor.close()
        finally:
            conn.close()

    async def fault(self, awaitable):
        with self.assertRaises(sw.WitnessStorageError) as caught:
            await awaitable
        error = caught.exception
        self.assertEqual(str(error), "sqlite_witness_storage_fault")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn("private-sql-marker", "".join(traceback.format_exception(error)))

    async def asyncTearDown(self):
        try:
            for release in self.releases:
                release.set()
            if self.tasks:
                await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 8)
            for witness in self.handles:
                if witness._gate.healthy:
                    self.assertTrue(await witness.async_wait_idle(3))
                self.assertIsNone(witness._gate.actual)
                self.assertEqual(witness._gate.pending, 0)
        finally:
            if (all(w._gate.actual is None and w._gate.pending == 0 for w in self.handles)
                    and all(t.done() for t in self.tasks)):
                self.tmp.cleanup()
            else:
                raise AssertionError("owned work did not settle; refuse directory cleanup")

    async def test_provision_is_explicit_and_retains_ack(self):
        path = self.path("basic")
        witness = self.handle(path)
        await self.fault(witness.async_load_last_intent())
        self.assertFalse(Path(path).exists())
        witness = await self.provision(path)
        self.assertIsNone(await witness.async_load_last_intent())
        self.assertIsNone(await witness.async_prepare_intent(OLD))
        self.assertEqual(await self.handle(path).async_load_last_intent(), OLD)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    async def test_constructor_is_strict_and_zero_io(self):
        class StrSubclass(str):
            pass
        path = self.path("arguments")
        bad_paths = [Path(path), StrSubclass(path), True, None, "relative.sqlite",
                     path + "/", path + "\x00", str(Path(path).parent) + "/./x",
                     str(Path(path).parent) + "/../x", "//tmp/x", path + "\ud800"]
        with patch.object(sw.os, "open", side_effect=AssertionError("open")), \
             patch.object(sw, "_stat_at", side_effect=AssertionError("stat")), \
             patch.object(sw, "_stat_parent", side_effect=AssertionError("stat")), \
             patch.object(sw, "_open_sqlite", side_effect=AssertionError("sqlite")):
            self.handle(path)
            for value in bad_paths:
                with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                    self.handle(value)
            for value in [True, "A" * 32, "1" * 31, StrSubclass(STORE), 1]:
                for key in ("store", "binding"):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        self.handle(path, **{key: value})

    async def test_invalid_argument_errors_are_fixed_and_no_running_loop(self):
        path = self.path("fixed-errors")
        for category in ("path", "store", "binding"):
            messages = []
            for marker in ("private-one", "private-two"):
                with self.assertRaises(ValueError) as caught:
                    self.handle(marker if category == "path" else path,
                                **({category: marker} if category != "path" else {}))
                messages.append(str(caught.exception))
                self.assertNotIn(marker, messages[-1])
            self.assertEqual(*messages)
        def no_loop():
            with patch("os.open", side_effect=AssertionError("open")):
                try:
                    sw.SQLiteIntentWitness(path, store_id=STORE, binding_id=BINDING)
                except RuntimeError:
                    return True
            return False
        self.assertTrue(await asyncio.to_thread(no_loop))

    async def test_invalid_records_rejected_before_io(self):
        class DictSubclass(dict):
            pass
        class StrSubclass(str):
            pass
        witness = self.handle(self.path("invalid-records"))
        invalid = [None, [], DictSubclass(OLD), {}, {**OLD, "extra": 1},
                   {k: v for k, v in OLD.items() if k != "state"},
                   {StrSubclass(k): v for k, v in OLD.items()}]
        for key, values in {
            "operation_id": [True, 12, "A" * 32, "a" * 31, StrSubclass("a" * 32)],
            "operation": [True, "erase", StrSubclass("feed")],
            "state": [True, "done", StrSubclass("pending")],
        }.items():
            invalid.extend({**OLD, key: value} for value in values)
        with patch("os.open", side_effect=AssertionError("open")), \
             patch.object(sw, "_submit") as submitted, \
             patch.object(sw, "_stat_at") as stat_at, \
             patch.object(sw, "_stat_parent") as stat_parent, \
             patch.object(sw, "_open_sqlite") as sqlite_open:
            for record in invalid:
                with self.subTest(kind=type(record).__name__), self.assertRaises(ValueError):
                    await witness.async_prepare_intent(record)
            messages = []
            for marker in ("private-one", "private-two"):
                with self.assertRaises(ValueError) as caught:
                    await witness.async_prepare_intent({**OLD, "state": marker})
                messages.append(str(caught.exception))
                self.assertNotIn(marker, messages[-1])
            self.assertEqual(*messages)
            for spy in (submitted, stat_at, stat_parent, sqlite_open):
                spy.assert_not_called()

    async def test_hostile_record_keys_rejected_without_invoking_key_hooks(self):
        hooks = {"armed": False, "raise": False, "calls": []}
        def hook(kind):
            if hooks["armed"]:
                hooks["calls"].append(kind)
                if hooks["raise"]:
                    raise RuntimeError("private-hook-marker")
        class HookedStr(str):
            def __hash__(self):
                hook("hash")
                return str.__hash__(self)
            def __eq__(self, other):
                hook("eq")
                return str.__eq__(self, other)
            def __ne__(self, other):
                hook("ne")
                return str.__ne__(self, other)
        class Colliding:
            # Shares a valid key's hash so any set/dict membership would call __eq__.
            def __init__(self, name):
                self.name = name
            def __hash__(self):
                hook("hash")
                return hash(self.name)
            def __eq__(self, other):
                hook("eq")
                return True
            def __ne__(self, other):
                hook("ne")
                return False
        witness = self.handle(self.path("hostile-keys"))
        with patch("os.open", side_effect=AssertionError("open")), \
             patch.object(sw, "_submit") as submitted, \
             patch.object(sw, "_stat_at") as stat_at, \
             patch.object(sw, "_stat_parent") as stat_parent, \
             patch.object(sw, "_open_sqlite") as sqlite_open:
            for raising in (False, True):
                hooks["armed"], hooks["raise"] = False, raising
                # Dict insertion hashes keys before validation; arm only afterwards.
                partial = {k: v for k, v in OLD.items() if k != "state"}
                invalid = [None, [], {}, {**OLD, "extra": 1}, dict(partial),
                           {HookedStr(k): v for k, v in OLD.items()},
                           {**partial, HookedStr("state"): "pending"},
                           {**partial, Colliding("state"): "pending"},
                           {Colliding(k): v for k, v in OLD.items()}]
                self.assertTrue(all(len(record) == 3 for record in invalid[5:]))
                hooks["calls"].clear()
                hooks["armed"] = True
                try:
                    for index, record in enumerate(invalid):
                        with self.subTest(raising=raising, index=index):
                            outcome = None
                            try:
                                await witness.async_prepare_intent(record)
                            except Exception as error:  # capture any raw leak for assertion
                                outcome = error
                            self.assertIs(type(outcome), ValueError)
                            self.assertEqual(str(outcome), "invalid_witness_record")
                            self.assertIsNone(outcome.__cause__)
                            self.assertIsNone(outcome.__context__)
                            self.assertNotIn("private-hook-marker",
                                             "".join(traceback.format_exception(outcome)))
                            self.assertEqual(hooks["calls"], [])
                finally:
                    hooks["armed"] = False
            for spy in (submitted, stat_at, stat_parent, sqlite_open):
                spy.assert_not_called()
        self.assertEqual(witness._gate.pending, 0)

    async def test_connection_contract_and_explicit_cursor_cleanup(self):
        path = self.path("connection")
        witness = await self.provision(path)
        factory = ProbeFactory()
        real_connect, calls = sqlite3.connect, []
        def capture(*args, **kwargs):
            calls.append((args, kwargs))
            return real_connect(*args, **kwargs)
        with patch.object(sw, "_open_sqlite", factory), patch.object(
                sw.sqlite3, "connect", side_effect=capture):
            await witness.async_prepare_intent(OLD)
            self.assertEqual(await witness.async_load_last_intent(), OLD)
        factory.assert_closed(self)
        self.assertEqual(len(calls), 2)
        for args, kwargs in calls:
            self.assertEqual(args, (Path(path).as_uri() + "?mode=rw",))
            self.assertEqual(kwargs, dict(uri=True, autocommit=True, timeout=0, cached_statements=0))
        for conn in factory.connections:
            self.assertEqual(conn.sql.count("BEGIN IMMEDIATE"), 1)
            self.assertEqual(conn.sql.count("COMMIT"), 1)
        self.assertFalse(any(sql.startswith("UPDATE") for sql in factory.connections[-1].sql))

    async def test_missing_zero_corrupt_truncated_are_never_virgin(self):
        for label in ("missing", "zero", "corrupt", "truncated"):
            with self.subTest(label=label):
                path = self.path(label)
                if label == "zero":
                    Path(path).touch(mode=0o600)
                elif label == "corrupt":
                    Path(path).write_bytes(b"private-sql-marker:not a database")
                    os.chmod(path, 0o600)
                elif label == "truncated":
                    await (await self.provision(path)).async_prepare_intent(OLD)
                    Path(path).write_bytes(Path(path).read_bytes()[:100])
                witness = self.handle(path)
                await self.fault(witness.async_load_last_intent())
                await self.fault(witness.async_prepare_intent(NEW))
                if label == "missing":
                    self.assertFalse(Path(path).exists())

    async def test_provision_never_overwrites_existing_or_failed_reservation(self):
        for label in ("zero", "valid", "failed"):
            with self.subTest(label=label):
                path = self.path("existing-" + label)
                if label == "zero":
                    Path(path).touch(mode=0o600)
                elif label == "valid":
                    await (await self.provision(path)).async_prepare_intent(OLD)
                else:
                    with patch.object(sw, "_open_sqlite", side_effect=OSError("private-sql-marker")):
                        await self.fault(sw.SQLiteIntentWitness.async_provision(
                            path, store_id=STORE, binding_id=BINDING))
                    self.assertTrue(Path(path).exists())
                before = (os.stat(path).st_ino, Path(path).read_bytes())
                await self.fault(sw.SQLiteIntentWitness.async_provision(
                    path, store_id=STORE, binding_id=BINDING))
                self.assertEqual((os.stat(path).st_ino, Path(path).read_bytes()), before)

    async def test_uri_quoting_and_identity_mismatch(self):
        path = str(self.root / "? # % space 雪.sqlite")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        self.assertEqual(await self.handle(path).async_load_last_intent(), OLD)
        for store, binding in (("a" * 32, BINDING), (STORE, "b" * 32)):
            wrong = self.handle(path, store, binding)
            await self.fault(wrong.async_load_last_intent())
            await self.fault(wrong.async_prepare_intent(NEW))
        self.assertEqual(await witness.async_load_last_intent(), OLD)

    async def test_every_header_schema_and_row_boundary_fails_closed(self):
        cases = [
            ("application", [("PRAGMA application_id=0", ())]),
            ("user-version", [("PRAGMA user_version=2", ())]),
            ("format", [("UPDATE witness_meta SET format_version=2", ())]),
            ("store", [("UPDATE witness_meta SET store_id=?", ("a" * 32,))]),
            ("binding", [("UPDATE witness_meta SET binding_id=?", ("b" * 32,))]),
            ("meta-missing", [("DELETE FROM witness_meta", ())]),
            ("intent-missing", [("DELETE FROM witness_intent", ())]),
            ("meta-extra", [("INSERT INTO witness_meta VALUES(2,1,?,?)", (STORE, BINDING))]),
            ("intent-extra", [("INSERT INTO witness_intent VALUES(2,NULL,NULL,NULL)", ())]),
            ("partial-null", [("UPDATE witness_intent SET state=NULL", ())]),
            ("bad-id", [("UPDATE witness_intent SET operation_id='INVALID'", ())]),
            ("bad-operation", [("UPDATE witness_intent SET operation='erase'", ())]),
            ("bad-state", [("UPDATE witness_intent SET state='done'", ())]),
            ("extra-table", [("CREATE TABLE extra(x INTEGER) STRICT", ())]),
            ("extra-index", [("CREATE INDEX extra ON witness_intent(state)", ())]),
            ("extra-view", [("CREATE VIEW extra AS SELECT * FROM witness_intent", ())]),
            ("extra-trigger", [("CREATE TRIGGER extra AFTER UPDATE ON witness_intent BEGIN SELECT 1; END", ())]),
            ("missing-table", [("DROP TABLE witness_intent", ())]),
            ("nonstrict", [("PRAGMA writable_schema=ON", ()),
                ("UPDATE sqlite_schema SET sql=replace(sql,' STRICT','') WHERE name='witness_intent'", ())]),
            ("alternate-ddl", [("PRAGMA writable_schema=ON", ()),
                ("UPDATE sqlite_schema SET sql=replace(sql,'CHECK(singleton=1)',"
                 "'CHECK(1=singleton)') WHERE name='witness_intent'", ())]),
        ]
        for label, statements in cases:
            with self.subTest(label=label):
                path = self.path("invalid-" + label)
                witness = await self.provision(path)
                await witness.async_prepare_intent(OLD)
                self.edit(path, [("PRAGMA ignore_check_constraints=ON", ())] + statements)
                before = Path(path).read_bytes()
                await self.fault(witness.async_load_last_intent())
                await self.fault(witness.async_prepare_intent(NEW))
                self.assertEqual(Path(path).read_bytes(), before)

    async def test_strict_row_and_schema_metadata_matrix(self):
        meta = "SELECT singleton, format_version, store_id, binding_id FROM witness_meta"
        intent = "SELECT singleton, operation_id, operation, state FROM witness_intent"
        cases = [(meta, [(True, 1, STORE, BINDING)]), (meta, [(1, True, STORE, BINDING)]),
                 (meta, [(1, 1, 123, BINDING)]), (meta, [(1, 1, STORE.encode(), BINDING)]),
                 (meta, [(1, 1, STORE, BINDING.encode())]),
                 (intent, [(True, OLD["operation_id"], "feed", "pending")]),
                 (intent, [(1, 123, "feed", "pending")]),
                 (intent, [(1, OLD["operation_id"].encode(), "feed", "pending")]),
                 (intent, [(1, OLD["operation_id"], b"feed", "pending")]),
                 (intent, [(1, OLD["operation_id"], "feed", b"pending")]),
                 (intent, [(1, None, "feed", "pending")]),
                 ("PRAGMA table_xinfo(witness_meta)", []),
                 ("PRAGMA table_xinfo(witness_intent)", []), ("PRAGMA table_list", [])]
        path = self.path("metadata-matrix")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        for query, replacement in cases:
            with self.subTest(query=query, value=repr(replacement)):
                def rows(sql, actual):
                    return replacement if sql == query else actual
                factory = ProbeFactory(rows=rows)
                with patch.object(sw, "_open_sqlite", factory):
                    await self.fault(witness.async_load_last_intent())
                    await self.fault(witness.async_prepare_intent(NEW))
                factory.assert_closed(self)
        self.assertEqual(await witness.async_load_last_intent(), OLD)

    async def test_file_links_modes_and_sidecars_rejected_without_repair(self):
        for label in ("db-mode", "parent-mode", "db-symlink", "db-hardlink", "parent-symlink",
                      "wal", "shm", "journal-symlink", "journal-directory", "journal-mode",
                      "journal-hardlink"):
            with self.subTest(label=label):
                path = self.path("unsafe-" + label)
                witness = await self.provision(path)
                await witness.async_prepare_intent(OLD)
                target, parent = Path(path), Path(path).parent
                if label == "db-mode":
                    os.chmod(path, 0o640)
                elif label == "parent-mode":
                    os.chmod(parent, 0o750)
                elif label == "db-symlink":
                    saved = parent / "saved.sqlite"
                    target.rename(saved)
                    target.symlink_to(saved.name)
                elif label == "db-hardlink":
                    os.link(path, parent / "alias.sqlite")
                elif label == "parent-symlink":
                    alias = self.root / "parent-alias"
                    alias.symlink_to(parent.name, target_is_directory=True)
                    witness = self.handle(str(alias / target.name))
                else:
                    suffix = "-wal" if label == "wal" else "-shm" if label == "shm" else "-journal"
                    sidecar = Path(path + suffix)
                    if label == "journal-symlink":
                        sidecar.symlink_to(target.name)
                    elif label == "journal-directory":
                        sidecar.mkdir(mode=0o700)
                    else:
                        sidecar.touch(mode=0o600)
                        if label == "journal-mode":
                            os.chmod(sidecar, 0o644)
                        if label == "journal-hardlink":
                            os.link(sidecar, parent / "journal-alias")
                try:
                    with patch.object(sw, "_open_sqlite") as opened:
                        await self.fault(witness.async_load_last_intent())
                        await self.fault(witness.async_prepare_intent(NEW))
                        opened.assert_not_called()
                    if label in ("wal", "shm"):
                        self.assertTrue(Path(path + "-" + label).exists())
                    if label == "db-mode":
                        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
                finally:
                    os.chmod(parent, 0o700)

    async def test_oversize_database_and_journal_rejected_before_sqlite(self):
        for kind in ("db", "journal"):
            path = self.path("oversize-" + kind)
            witness = await self.provision(path)
            await witness.async_prepare_intent(OLD)
            target = Path(path) if kind == "db" else Path(path + "-journal")
            limit = 1_048_576 if kind == "db" else 2_097_152
            with target.open("wb") as fixture:
                fixture.truncate(limit + 1)
            os.chmod(target, 0o600)
            with patch.object(sw, "_open_sqlite") as opened:
                await self.fault(witness.async_load_last_intent())
                await self.fault(witness.async_prepare_intent(NEW))
                opened.assert_not_called()
            self.assertEqual(target.stat().st_size, limit + 1)

    async def test_special_files_missing_parent_and_sidecar_provision_refusal(self):
        for kind in ("directory", "fifo"):
            path = self.path("special-" + kind)
            if kind == "directory":
                Path(path).mkdir(mode=0o700)
            else:
                os.mkfifo(path, mode=0o600)
            with patch.object(sw, "_open_sqlite") as opened:
                await self.fault(self.handle(path).async_load_last_intent())
                await self.fault(self.handle(path).async_prepare_intent(NEW))
                opened.assert_not_called()
        missing = self.root / "not-created" / "witness.sqlite"
        await self.fault(self.handle(str(missing)).async_load_last_intent())
        await self.fault(sw.SQLiteIntentWitness.async_provision(
            str(missing), store_id=STORE, binding_id=BINDING))
        self.assertFalse(missing.parent.exists())
        for suffix in ("-journal", "-wal", "-shm"):
            path = self.path("provision-sidecar" + suffix)
            sidecar = Path(path + suffix)
            sidecar.write_bytes(b"owned-synthetic-sidecar")
            os.chmod(sidecar, 0o600)
            await self.fault(sw.SQLiteIntentWitness.async_provision(path, store_id=STORE, binding_id=BINDING))
            self.assertFalse(Path(path).exists())
            self.assertEqual(sidecar.read_bytes(), b"owned-synthetic-sidecar")

    async def test_owner_and_postcommit_identity_rechecks(self):
        for seam in ("_stat_at", "_stat_parent"):
            for field in ("uid", "dev", "ino"):
                for action in ("load", "prepare"):
                    path = self.path(seam + field + action)
                    witness = await self.provision(path)
                    await witness.async_prepare_intent(OLD)
                    committed = threading.Event()
                    def after(sql):
                        if sql == "COMMIT":
                            committed.set()
                    real = getattr(sw, seam)
                    def changed(*args, **kwargs):
                        info = real(*args, **kwargs)
                        if field == "uid":
                            return changed_stat(info, uid=os.geteuid() + 1)
                        if committed.is_set():
                            return changed_stat(info, **{field: getattr(info, "st_" + field) + 1})
                        return info
                    factory = ProbeFactory(after=after)
                    with patch.object(sw, "_open_sqlite", factory), patch.object(sw, seam, side_effect=changed):
                        await self.fault(witness.async_load_last_intent() if action == "load"
                                         else witness.async_prepare_intent(NEW))
                    if factory.connections:
                        factory.assert_closed(self)
                    self.assertTrue(await witness.async_wait_idle(1))

    async def test_real_owned_path_replacement_after_commit_is_not_ack(self):
        path = self.path("replace-after-commit")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        saved = Path(path).with_name("saved.sqlite")
        def replace(sql):
            if sql == "COMMIT":
                Path(path).rename(saved)
                Path(path).touch(mode=0o600)
        factory = ProbeFactory(after=replace)
        with patch.object(sw, "_open_sqlite", factory):
            await self.fault(witness.async_prepare_intent(NEW))
        factory.assert_closed(self)
        self.assertEqual(Path(path).stat().st_size, 0)
        await self.fault(witness.async_load_last_intent())
        self.assertTrue(saved.exists())

    async def test_wrong_owner_journal_rejected_before_sqlite(self):
        path = self.path("journal-owner")
        witness = await self.provision(path)
        Path(path + "-journal").touch(mode=0o600)
        real = sw._stat_at
        def changed(fd, name):
            info = real(fd, name)
            return changed_stat(info, uid=os.geteuid() + 1) if name.endswith("-journal") else info
        with patch.object(sw, "_stat_at", side_effect=changed), patch.object(sw, "_open_sqlite") as opened:
            await self.fault(witness.async_load_last_intent())
            opened.assert_not_called()

    async def test_required_connection_settings_and_versions(self):
        path = self.path("capabilities")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        for kind in ("defensive", "trusted", "unavailable", "set-unavailable", "sync", "journal"):
            def rows(sql, actual):
                if kind == "sync" and sql == "PRAGMA synchronous":
                    return [(0,)]
                if kind == "journal" and sql == "PRAGMA journal_mode":
                    return [("wal",)]
                return actual
            factory = ProbeFactory(rows=rows)
            def open_probe(value):
                probe = factory(value)
                real_get = probe.real.getconfig
                def getconfig(flag):
                    actual = real_get(flag)
                    if kind == "unavailable":
                        raise AttributeError("private-sql-marker")
                    if kind == "defensive" and flag == sqlite3.SQLITE_DBCONFIG_DEFENSIVE:
                        return False
                    if kind == "trusted" and flag == sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA:
                        return True
                    return actual
                probe.getconfig = getconfig
                if kind == "set-unavailable":
                    def setconfig(*args):
                        raise AttributeError("private-sql-marker")
                    probe.setconfig = setconfig
                return probe
            with patch.object(sw, "_open_sqlite", side_effect=open_probe):
                await self.fault(witness.async_load_last_intent())
                await self.fault(witness.async_prepare_intent(NEW))
            factory.assert_closed(self)
        for name, value in (("_PYTHON_VERSION", (3, 12, 9)), ("_SQLITE_VERSION", (3, 36, 0))):
            with patch.object(sw, name, value):
                await self.fault(witness.async_load_last_intent())
                await self.fault(witness.async_prepare_intent(NEW))
                new_path = self.path("version-" + name)
                await self.fault(sw.SQLiteIntentWitness.async_provision(
                    new_path, store_id=STORE, binding_id=BINDING))
                self.assertFalse(Path(new_path).exists())

    async def test_wal_is_rejected_not_converted(self):
        path = self.path("real-wal")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        self.edit(path, [("PRAGMA journal_mode=WAL", ())])
        parent = Path(path).parent
        def snapshot():
            # Owned synthetic directory only: exact DB bytes plus every sidecar.
            return {entry.name: entry.read_bytes() for entry in sorted(parent.iterdir())}
        before = snapshot()
        # Observed: SQLite removes -wal/-shm on the fixture's last close, so the
        # owned directory holds only the DB; any new sidecar would break equality.
        self.assertEqual(sorted(before), ["witness.sqlite"])
        db_before = before["witness.sqlite"]
        # SQLite header bytes 18/19 are the file-format write/read versions: 2 means WAL.
        self.assertEqual(db_before[18:20], b"\x02\x02")
        factory = ProbeFactory()
        with patch.object(sw, "_open_sqlite", factory):
            await self.fault(witness.async_load_last_intent())
            self.assertEqual(snapshot(), before)
            await self.fault(witness.async_prepare_intent(NEW))
            self.assertEqual(snapshot(), before)
        factory.assert_closed(self)
        self.assertEqual(Path(path).read_bytes()[:100], db_before[:100])
        self.assertEqual(Path(path).read_bytes(), db_before)
        for conn in factory.connections:
            self.assertFalse(any("JOURNAL_MODE=" in sql.upper() or "CHECKPOINT" in sql.upper()
                                 for sql in conn.sql))

    async def test_strict_records_and_copy_before_gate_wait(self):
        path = self.path("copy")
        witness = await self.provision(path)
        await witness._gate.lock.acquire()
        record = dict(OLD)
        task = self.start(witness.async_prepare_intent(record))
        try:
            await asyncio.sleep(0)
            record.update(NEW)
        finally:
            witness._gate.lock.release()
        await task
        loaded = await witness.async_load_last_intent()
        self.assertEqual(loaded, OLD)
        loaded.update(NEW)
        self.assertEqual(await witness.async_load_last_intent(), OLD)
        for operation in ("feed", "defrost", "refilled", "settings", "schedule"):
            for state in ("pending", "unknown"):
                value = {**OLD, "operation": operation, "state": state}
                await witness.async_prepare_intent(value)
                self.assertEqual(await witness.async_load_last_intent(), value)

    async def test_commit_error_is_never_ack_or_readback_repaired(self):
        for stage in ("before", "after"):
            path = self.path("commit-" + stage)
            witness = await self.provision(path)
            await witness.async_prepare_intent(OLD)
            def fail(sql):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("private-sql-marker")
            factory = ProbeFactory(**{stage: fail})
            with patch.object(sw, "_open_sqlite", factory):
                await self.fault(witness.async_prepare_intent(NEW))
            factory.assert_closed(self)
            self.assertEqual(factory.connections[0].sql.count("COMMIT"), 1)
            self.assertEqual(factory.connections[0].sql.count("ROLLBACK"), int(stage == "before"))
            self.assertTrue(await witness.async_wait_idle(1))
            self.assertEqual(await self.handle(path).async_load_last_intent(), OLD if stage == "before" else NEW)

    async def test_update_rowcount_and_active_commit_fail_without_retry(self):
        for kind in ("update", "count-zero", "count-two", "active"):
            path = self.path("transaction-" + kind)
            witness = await self.provision(path)
            await witness.async_prepare_intent(OLD)
            real_execute = sw._execute
            commits = []
            def intercept(conn, sql, parameters=()):
                if sql == "COMMIT":
                    commits.append(sql)
                    if kind == "active":
                        # Real transaction stays active: falsely positive COMMIT delegate.
                        return [], -1
                if sql.startswith("UPDATE") and kind == "update":
                    raise OSError("private-sql-marker")
                rows, count = real_execute(conn, sql, parameters)
                if sql.startswith("UPDATE") and kind.startswith("count"):
                    return rows, 0 if kind == "count-zero" else 2
                return rows, count
            factory = ProbeFactory()
            with patch.object(sw, "_open_sqlite", factory), patch.object(sw, "_execute", intercept):
                await self.fault(witness.async_prepare_intent(NEW))
            factory.assert_closed(self)
            self.assertEqual(len(commits), int(kind == "active"))
            self.assertEqual(factory.connections[0].sql.count("ROLLBACK"), 1)
            self.assertEqual(await self.handle(path).async_load_last_intent(), OLD)

    async def test_fsync_failures_never_ack_or_return_authoritative_load(self):
        for seam in ("_fsync_db", "_fsync_parent"):
            for when in ("before", "after"):
                path = self.path(seam + "-" + when)
                witness = await self.provision(path)
                await witness.async_prepare_intent(OLD)
                real = getattr(sw, seam)
                def fail(fd):
                    if when == "after":
                        real(fd)
                    raise OSError("private-sql-marker")
                with patch.object(sw, seam, side_effect=fail):
                    await self.fault(witness.async_prepare_intent(NEW))
                    await self.fault(witness.async_load_last_intent())
                self.assertTrue(await witness.async_wait_idle(1))
                self.assertEqual(await self.handle(path).async_load_last_intent(), NEW)

    async def test_safe_journal_and_ack_barrier_order(self):
        path = self.path("barrier-order")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        Path(path + "-journal").touch(mode=0o600)
        for action in ("load", "prepare"):
            stages = []
            def after(sql):
                if sql == "COMMIT":
                    stages.append("commit")
            factory = ProbeFactory(after=after)
            close_cursor, close_conn = sw._close_cursor, sw._close_connection
            db_sync, dir_sync, real_stat = sw._fsync_db, sw._fsync_parent, sw._stat_at
            def cursor_close(cursor):
                close_cursor(cursor)
                if "commit" in stages:
                    stages.append("cursor-close")
            def connection_close(conn):
                close_conn(conn)
                stages.append("connection-close")
            def sync_db(fd):
                factory.assert_closed(self)
                db_sync(fd)
                stages.append("db-sync")
            def sync_dir(fd):
                factory.assert_closed(self)
                dir_sync(fd)
                stages.append("dir-sync")
            def poststat(*args, **kwargs):
                result = real_stat(*args, **kwargs)
                if "dir-sync" in stages:
                    stages.append("postcheck")
                return result
            with patch.object(sw, "_open_sqlite", factory), \
                 patch.object(sw, "_close_cursor", cursor_close), \
                 patch.object(sw, "_close_connection", connection_close), \
                 patch.object(sw, "_fsync_db", sync_db), \
                 patch.object(sw, "_fsync_parent", sync_dir), \
                 patch.object(sw, "_stat_at", poststat):
                if action == "load":
                    self.assertEqual(await witness.async_load_last_intent(), OLD)
                else:
                    await witness.async_prepare_intent(NEW)
                stages.append("return")
            required = ["commit", "cursor-close", "connection-close", "db-sync", "dir-sync", "postcheck", "return"]
            self.assertEqual([stages.index(stage) for stage in required],
                             sorted(stages.index(stage) for stage in required))
            self.assertEqual(factory.connections[0].sql.count("BEGIN IMMEDIATE"), 1)

    async def test_provision_reservation_barriers_and_failed_final_barrier_no_retry(self):
        path = self.path("reservation")
        stages, flags_seen = [], []
        real_open, real_sqlite = sw.os.open, sw._open_sqlite
        real_db, real_parent = sw._fsync_db, sw._fsync_parent
        def capture_open(name, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                flags_seen.append((name, flags, args, kwargs))
                stages.append("reserve")
            return real_open(name, flags, *args, **kwargs)
        def sync_db(fd):
            real_db(fd)
            stages.append("db-sync")
        def sync_parent(fd):
            real_parent(fd)
            stages.append("dir-sync")
            if stages.count("dir-sync") == 2:
                raise OSError("private-sql-marker")
        def open_sqlite(value):
            stages.append("sqlite")
            return real_sqlite(value)
        with patch.object(sw.os, "open", capture_open), patch.object(sw, "_open_sqlite", open_sqlite), \
             patch.object(sw, "_fsync_db", sync_db), patch.object(sw, "_fsync_parent", sync_parent):
            await self.fault(sw.SQLiteIntentWitness.async_provision(path, store_id=STORE, binding_id=BINDING))
        self.assertEqual(stages, ["reserve", "db-sync", "dir-sync", "sqlite", "db-sync", "dir-sync"])
        self.assertEqual(len(flags_seen), 1)
        name, flags, args, kwargs = flags_seen[0]
        self.assertEqual(name, "witness.sqlite")
        self.assertEqual(args, (0o600,))
        self.assertIs(type(kwargs["dir_fd"]), int)
        for flag in (os.O_CREAT, os.O_EXCL, os.O_NOFOLLOW, os.O_CLOEXEC):
            self.assertTrue(flags & flag)
        before = (os.stat(path).st_ino, Path(path).read_bytes())
        await self.fault(sw.SQLiteIntentWitness.async_provision(path, store_id=STORE, binding_id=BINDING))
        self.assertEqual((os.stat(path).st_ino, Path(path).read_bytes()), before)
        self.assertIsNone(await self.handle(path).async_load_last_intent())

    async def test_uncertain_cursor_or_connection_close_latches_path(self):
        for seam in ("_close_cursor", "_close_connection"):
            path = self.path("uncertain-" + seam)
            witness = await self.provision(path)
            await witness.async_prepare_intent(OLD)
            real = getattr(sw, seam)
            def close_then_fail(resource):
                real(resource)
                raise OSError("private-sql-marker")
            factory = ProbeFactory()
            with patch.object(sw, "_open_sqlite", factory), patch.object(sw, seam, close_then_fail):
                await self.fault(witness.async_prepare_intent(NEW))
            factory.assert_closed(self)
            self.assertFalse(await witness.async_wait_idle(0))
            gate_ref = weakref.ref(witness._gate)
            self.handles.remove(witness)
            del witness
            gc.collect()
            self.assertIsNotNone(gate_ref())
            replacement = self.handle(path)
            self.assertIs(replacement._gate, gate_ref())
            self.assertFalse(await replacement.async_wait_idle(0))
            with patch.object(sw, "_submit") as submitted:
                await self.fault(replacement.async_load_last_intent())
                await self.fault(replacement.async_prepare_intent(NEW))
                await self.fault(sw.SQLiteIntentWitness.async_provision(path, store_id=STORE, binding_id=BINDING))
                submitted.assert_not_called()

    async def test_rollback_failure_still_closes_and_latches(self):
        path = self.path("rollback-failure")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        def fail(sql):
            if sql.startswith("UPDATE witness_intent") or sql == "ROLLBACK":
                raise sqlite3.OperationalError("private-sql-marker")
        factory = ProbeFactory(before=fail)
        with patch.object(sw, "_open_sqlite", factory):
            await self.fault(witness.async_prepare_intent(NEW))
        factory.assert_closed(self)
        self.assertIn("ROLLBACK", factory.connections[0].sql)
        self.assertNotIn("COMMIT", factory.connections[0].sql)
        self.assertFalse(await witness.async_wait_idle(0))
        self.assertFalse(await self.handle(path).async_wait_idle(0))
        self.assertIsNone(witness._gate.actual)

    async def test_actual_future_owns_lease_through_repeated_cancel(self):
        path = self.path("cancel-after-commit")
        first = await self.provision(path)
        second = self.handle(path)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        once = False
        def pause(sql):
            nonlocal once
            if sql == "COMMIT" and not once:
                once = True
                entered.set()
                if not release.wait(5):
                    raise AssertionError("owned gate not released")
        factory = ProbeFactory(after=pause)
        with patch.object(sw, "_open_sqlite", factory):
            producer = self.start(first.async_prepare_intent(OLD))
            await thread_entered(entered)
            actual = first._gate.actual
            producer.cancel()
            await asyncio.sleep(0)
            producer.cancel()
            load = self.start(second.async_load_last_intent())
            write = self.start(second.async_prepare_intent(NEW))
            await asyncio.sleep(0)
            self.assertFalse(producer.done())
            self.assertFalse(load.done())
            self.assertFalse(write.done())
            self.assertFalse(actual.done())
            self.assertFalse(actual.cancelled())
            self.assertEqual(first._gate.pending, 3)
            self.assertFalse(await second.async_wait_idle(0.01))
            self.assertEqual(len(factory.connections), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await producer
            self.assertEqual(await load, OLD)
            await write
        self.assertTrue(actual.done())
        self.assertFalse(actual.cancelled())
        self.assertTrue(await second.async_wait_idle(1))
        self.assertEqual(await second.async_load_last_intent(), NEW)
        factory.assert_closed(self)

    async def test_cancel_gate_waiter_has_zero_io_and_pending_is_accounted(self):
        witness = self.handle(self.path("gate-wait"))
        await witness._gate.lock.acquire()
        try:
            with patch.object(sw, "_submit") as submitted:
                task = self.start(witness.async_prepare_intent(OLD))
                await asyncio.sleep(0)
                self.assertEqual(witness._gate.pending, 1)
                self.assertFalse(await witness.async_wait_idle(0))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                submitted.assert_not_called()
            self.assertEqual(witness._gate.pending, 0)
        finally:
            witness._gate.lock.release()
        self.assertTrue(await witness.async_wait_idle(0))

    async def test_idle_timeout_values(self):
        witness = self.handle(self.path("idle-values"))
        for timeout in (True, False, -1, math.inf, -math.inf, math.nan, "1", None):
            with self.subTest(kind=type(timeout).__name__), self.assertRaises(ValueError):
                await witness.async_wait_idle(timeout)
        self.assertTrue(await witness.async_wait_idle(0))
        self.assertTrue(await witness.async_wait_idle(0.0))

    async def assert_fixed_timeout_error(self, awaitable):
        outcome = None
        try:
            await awaitable
        except Exception as error:  # capture any raw leak (e.g. OverflowError) for assertion
            outcome = error
        self.assertIs(type(outcome), ValueError)
        self.assertEqual(str(outcome), "invalid_witness_timeout")
        self.assertIsNone(outcome.__cause__)
        self.assertIsNone(outcome.__context__)

    async def test_unrepresentable_huge_timeout_is_fixed_error_when_idle(self):
        witness = await self.provision(self.path("idle-huge"))
        await self.assert_fixed_timeout_error(witness.async_wait_idle(10 ** 400))
        self.assertTrue(await witness.async_wait_idle(0))

    async def test_unrepresentable_huge_timeout_is_fixed_error_while_pending(self):
        path = self.path("pending-huge")
        witness = await self.provision(path)
        huge = 10 ** 400
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real_sync = sw._fsync_db
        once = False
        def pause(fd):
            nonlocal once
            real_sync(fd)
            if not once:
                once = True
                entered.set()
                if not release.wait(5):
                    raise AssertionError("owned fsync pause not released")
        try:
            with patch.object(sw, "_fsync_db", pause):
                producer = self.start(witness.async_prepare_intent(OLD))
                await thread_entered(entered)
                actual = witness._gate.actual
                self.assertEqual(witness._gate.pending, 1)
                await self.assert_fixed_timeout_error(witness.async_wait_idle(huge))
                self.assertFalse(producer.done())
                self.assertFalse(actual.done())
                self.assertFalse(actual.cancelled())
                self.assertTrue(witness._gate.healthy)
                self.assertEqual(witness._gate.pending, 1)
                release.set()
                await producer
        finally:
            release.set()
        self.assertTrue(await witness.async_wait_idle(1))
        self.assertEqual(await witness.async_load_last_intent(), OLD)

    async def test_representable_huge_float_timeout_is_accepted(self):
        path = self.path("idle-max-float")
        witness = await self.provision(path)
        self.assertTrue(await witness.async_wait_idle(1e308))
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real_sync = sw._fsync_db
        once = False
        def pause(fd):
            nonlocal once
            real_sync(fd)
            if not once:
                once = True
                entered.set()
                if not release.wait(5):
                    raise AssertionError("owned fsync pause not released")
        try:
            with patch.object(sw, "_fsync_db", pause):
                producer = self.start(witness.async_prepare_intent(OLD))
                await thread_entered(entered)
                idle = self.start(witness.async_wait_idle(1e308))
                await asyncio.sleep(0)
                self.assertFalse(idle.done())
                release.set()
                await producer
                async with asyncio.timeout(3):
                    self.assertTrue(await idle)
        finally:
            release.set()

    async def test_cancel_before_coroutine_runs_has_zero_io(self):
        witness = self.handle(self.path("pre-start"))
        with patch.object(sw, "_submit") as submitted:
            task = self.start(witness.async_load_last_intent())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            submitted.assert_not_called()
        self.assertTrue(await witness.async_wait_idle(0))

    async def test_cancel_after_acquire_before_actual_submit_has_zero_io(self):
        witness = self.handle(self.path("acquired-no-submit"))
        real_lock = witness._gate.lock
        class CancelAfterAcquire:
            async def acquire(self):
                result = await real_lock.acquire()
                asyncio.current_task().cancel()
                return result
            def release(self):
                real_lock.release()
        with patch.object(witness._gate, "lock", CancelAfterAcquire()), patch.object(sw, "_submit") as submitted:
            task = self.start(witness.async_prepare_intent(OLD))
            with self.assertRaises(asyncio.CancelledError):
                await task
            submitted.assert_not_called()
        self.assertFalse(real_lock.locked())
        self.assertTrue(await witness.async_wait_idle(0))

    async def test_cancel_actual_submitted_before_storage_worker_runs(self):
        path = self.path("submitted-before-io")
        witness = await self.provision(path)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real_submit = sw._submit
        def submit(executor, function, *args):
            def blocked():
                entered.set()
                if not release.wait(5):
                    raise AssertionError("owned worker not released")
                return function(*args)
            return real_submit(executor, blocked)
        with patch.object(sw, "_submit", submit), patch.object(sw, "_open_sqlite", wraps=sw._open_sqlite) as opened:
            task = self.start(witness.async_prepare_intent(OLD))
            await thread_entered(entered)
            actual = witness._gate.actual
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            opened.assert_not_called()
            self.assertFalse(actual.cancelled())
            self.assertFalse(task.done())
            self.assertFalse(await witness.async_wait_idle(0))
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(actual.done())
        self.assertFalse(actual.cancelled())
        self.assertEqual(await witness.async_load_last_intent(), OLD)

    async def test_load_prepare_and_completion_cancellation_dominate_outcomes(self):
        for action in ("load", "prepare"):
            for fail_after in (False, True):
                for at_completion in (False, True):
                    for cancel_early in (False, True) if at_completion else (True,):
                        path = self.path("race-" + action + str(fail_after) + str(at_completion) + str(cancel_early))
                        witness = await self.provision(path)
                        await witness.async_prepare_intent(OLD)
                        entered, release = threading.Event(), threading.Event()
                        self.releases.append(release)
                        once = False
                        def pause(sql):
                            nonlocal once
                            if sql == "COMMIT" and not once:
                                once = True
                                entered.set()
                                if not release.wait(5):
                                    raise AssertionError("owned COMMIT pause not released")
                                if fail_after:
                                    raise OSError("private-sql-marker")
                        factory = ProbeFactory(after=pause)
                        with patch.object(sw, "_open_sqlite", factory):
                            task = self.start(witness.async_load_last_intent() if action == "load"
                                              else witness.async_prepare_intent(NEW))
                            await thread_entered(entered)
                            actual = witness._gate.actual
                            if at_completion:
                                loop = asyncio.get_running_loop()
                                actual.add_done_callback(lambda future: loop.call_soon_threadsafe(task.cancel))
                            if cancel_early:
                                task.cancel()
                                await asyncio.sleep(0)
                                task.cancel()
                            following = self.start(self.handle(path).async_load_last_intent())
                            await asyncio.sleep(0)
                            self.assertFalse(following.done())
                            self.assertEqual(len(factory.connections), 1)
                            release.set()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                            self.assertEqual(await following, OLD if action == "load" else NEW)
                        factory.assert_closed(self)
                        self.assertTrue(actual.done())
                        self.assertFalse(actual.cancelled())

    async def test_idle_timeout_and_cancel_leave_no_waiter_or_producer_cancel(self):
        path = self.path("idle-isolation")
        witness = await self.provision(path)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def pause(sql):
            if sql == "COMMIT":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("owned idle test not released")
        factory = ProbeFactory(after=pause)
        with patch.object(sw, "_open_sqlite", factory):
            producer = self.start(witness.async_prepare_intent(OLD))
            await thread_entered(entered)
            actual = witness._gate.actual
            following = self.start(self.handle(path).async_load_last_intent())
            await asyncio.sleep(0)
            baseline = asyncio.all_tasks()
            self.assertFalse(await witness.async_wait_idle(0.01))
            idle = self.start(witness.async_wait_idle(5))
            await asyncio.sleep(0)
            idle.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await idle
            await asyncio.sleep(0)
            self.assertFalse(asyncio.all_tasks() - baseline)
            self.assertFalse(producer.done())
            self.assertFalse(actual.done())
            self.assertFalse(actual.cancelled())
            self.assertEqual(witness._gate.pending, 2)
            release.set()
            await producer
            self.assertEqual(await following, OLD)
        factory.assert_closed(self)
        self.assertTrue(await witness.async_wait_idle(0))

    async def test_healthy_gate_is_wrong_loop_fenced_then_weakly_collectible(self):
        path = self.path("weak-loop")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        self.assertTrue(await witness.async_wait_idle(1))
        result = await asyncio.to_thread(foreign_loop_load, path)
        self.assertEqual(result[0], "rejected")
        self.assertNotIn(path, result[1])
        self.assertEqual(await witness.async_load_last_intent(), OLD)
        gate_ref = weakref.ref(witness._gate)
        self.handles.remove(witness)
        del witness
        gc.collect()
        self.assertIsNone(gate_ref())
        self.assertNotIn(path, sw._gates)
        self.assertEqual(await asyncio.to_thread(foreign_loop_load, path), ("loaded", OLD))

    async def test_unhealthy_gate_survives_gc_and_rejects_foreign_loop(self):
        path = self.path("strong-loop")
        witness = await self.provision(path)
        real = sw._close_connection
        def close_then_fail(conn):
            real(conn)
            raise OSError("private-sql-marker")
        factory = ProbeFactory()
        with patch.object(sw, "_open_sqlite", factory), patch.object(sw, "_close_connection", close_then_fail):
            await self.fault(witness.async_load_last_intent())
        factory.assert_closed(self)
        gate_ref = weakref.ref(witness._gate)
        self.assertIsNone(witness._gate.actual)
        self.handles.remove(witness)
        del witness, factory
        gc.collect()
        self.assertIsNotNone(gate_ref())
        self.assertIs(sw._unhealthy_gates[path], gate_ref())
        self.assertEqual((await asyncio.to_thread(foreign_loop_load, path))[0], "rejected")
        replacement = self.handle(path)
        self.assertIs(replacement._gate, gate_ref())
        self.assertFalse(await replacement.async_wait_idle(0))

    async def test_operations_on_existing_handle_are_wrong_loop_fenced(self):
        path = self.path("wrong-loop-operations")
        witness = await self.provision(path)
        await witness.async_prepare_intent(OLD)
        def foreign():
            async def run():
                for call in (witness.async_load_last_intent, lambda: witness.async_prepare_intent(NEW),
                             witness.async_wait_idle):
                    with self.assertRaises(ValueError) as caught:
                        await call()
                    self.assertNotIn(path, str(caught.exception))
            asyncio.run(run())
        with patch.object(sw, "_submit") as submitted:
            await asyncio.to_thread(foreign)
            submitted.assert_not_called()
        self.assertEqual(await witness.async_load_last_intent(), OLD)

    async def test_provision_counts_pending_and_drains_cancellation(self):
        path = self.path("provision-cancel")
        witness = self.handle(path)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real_sync = sw._fsync_parent
        once = False
        def pause(fd):
            nonlocal once
            real_sync(fd)
            if not once:
                once = True
                entered.set()
                if not release.wait(5):
                    raise AssertionError("owned reservation not released")
        with patch.object(sw, "_fsync_parent", pause):
            task = self.start(sw.SQLiteIntentWitness.async_provision(path, store_id=STORE, binding_id=BINDING))
            await thread_entered(entered)
            self.assertEqual(witness._gate.pending, 1)
            actual = witness._gate.actual
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            self.assertFalse(await witness.async_wait_idle(0))
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(actual.done())
        self.assertFalse(actual.cancelled())
        self.assertIsNone(await witness.async_load_last_intent())
        await self.fault(sw.SQLiteIntentWitness.async_provision(path, store_id=STORE, binding_id=BINDING))

    async def test_cancelled_idle_waiter_still_propagates_when_path_turns_unhealthy(self):
        path = self.path("unhealthy-idle-cancel")
        witness = await self.provision(path)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        real_close = sw._close_connection
        def close_fail(conn):
            real_close(conn)
            entered.set()
            if not release.wait(5):
                raise AssertionError("owned close pause not released")
            raise OSError("private-sql-marker")
        factory = ProbeFactory()
        with patch.object(sw, "_open_sqlite", factory), patch.object(sw, "_close_connection", close_fail):
            producer = self.start(witness.async_load_last_intent())
            await thread_entered(entered)
            idle = self.start(witness.async_wait_idle(5))
            await asyncio.sleep(0)
            idle.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await idle
            release.set()
            await self.fault(producer)
        factory.assert_closed(self)
        baseline = asyncio.all_tasks()
        self.assertFalse(await witness.async_wait_idle(0))
        self.assertEqual(asyncio.all_tasks(), baseline)

    async def test_submit_failure_and_executor_shutdown_error_are_sanitized(self):
        from concurrent.futures import ThreadPoolExecutor
        path = self.path("submit-failure")
        witness = await self.provision(path)
        class ShutdownFailure(ThreadPoolExecutor):
            def shutdown(self, *args, **kwargs):
                super().shutdown(*args, **kwargs)
                raise OSError("private-sql-marker")
        for executor in (ThreadPoolExecutor, ShutdownFailure):
            with patch.object(sw, "ThreadPoolExecutor", executor), \
                 patch.object(sw, "_submit", side_effect=OSError("private-sql-marker")), \
                 patch.object(sw, "_open_sqlite") as opened:
                await self.fault(witness.async_load_last_intent())
                opened.assert_not_called()
            self.assertEqual(witness._gate.pending, 0)
            self.assertFalse(witness._gate.lock.locked())

    async def test_real_executor_threads_are_joined_before_return(self):
        path = self.path("threads-joined")
        witness = await self.provision(path)
        factory = ProbeFactory()
        with patch.object(sw, "_open_sqlite", factory):
            await witness.async_prepare_intent(OLD)
            self.assertEqual(await witness.async_load_last_intent(), OLD)
        factory.assert_closed(self)
        self.assertTrue(all(not conn.thread_object.is_alive() for conn in factory.connections))

    async def test_postcommit_zero_or_oversize_database_is_never_ack(self):
        for size in (0, 1_048_577):
            path = self.path("post-size-" + str(size))
            witness = await self.provision(path)
            await witness.async_prepare_intent(OLD)
            def change_size(sql):
                if sql == "COMMIT":
                    with Path(path).open("r+b") as fixture:
                        fixture.truncate(size)
            factory = ProbeFactory(after=change_size)
            with patch.object(sw, "_open_sqlite", factory):
                await self.fault(witness.async_prepare_intent(NEW))
            factory.assert_closed(self)
            self.assertEqual(Path(path).stat().st_size, size)
            await self.fault(witness.async_load_last_intent())
