"""Durable migration journal and recovery reducer; synthetic, no HA, device or credentials.

The fault matrix covers this Linux test environment's POSIX filesystem only; it
is not a claim about durability on other filesystems or storage stacks.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from custom_components.petsafe_freshfeed import migration_journal as mj
from custom_components.petsafe_freshfeed import migration_state as ms
from custom_components.petsafe_freshfeed.migration_journal import MigrationJournal
from custom_components.petsafe_freshfeed.migration_state import (
    LeafOwner, MigrationAction, MigrationFacts, MigrationFault, MigrationPhase,
    MigrationPreview, MigrationRecord, MigrationStatus, PreviewReason,
    cancel_allowed, next_action, removal_allowed, status_for)

ENTRY = "01JPETSAFEENTRY0000000000A"
SOURCE = "01JTUYALOCALSRC000000000B"
LEAF = "c" * 32
DIGEST = "d" * 64
OTHER_DIGEST = "e" * 64
P1 = str(uuid.UUID(int=1, version=4))
P2 = str(uuid.UUID(int=2, version=4))
P3 = str(uuid.UUID(int=3, version=4))
SECRET = "SYNTHKEY0123abcd"  # synthetic local-key sentinel; must never be stored
P = MigrationPhase


def record(phase=P.PREPARED, revision=1, marker=P1, **overrides):
    values = dict(entry_id=ENTRY, source_entry_id=SOURCE, device_id=LEAF,
                  source_config_digest=DIGEST, source_disabled_by=None,
                  device_disabled_by=None,
                  entity_disabled_by=(("sensor.feeder_food", None), ("switch.feeder_x", "user")),
                  retiring=("sensor.tuya_raw_food", "switch.tuya_raw_feed"),
                  origin_marker=marker, phase=phase, revision=revision)
    if phase is P.VERIFIED:
        values["verified_marker"] = P2
    values.update(overrides)
    return MigrationRecord(**values)


def facts(**overrides):
    values = dict(process_marker=P2, source_disabled=True, source_loaded=False,
                  leaf_owner=LeafOwner.SOURCE, source_config_digest=DIGEST, backend="legacy",
                  owned_config_valid=False, witness_valid=True)
    values.update(overrides)
    return MigrationFacts(**values)


def assert_fixed(test, error):
    test.assertIs(type(error), MigrationFault)
    test.assertEqual(str(error), "migration_fault")
    test.assertEqual(error.args, ("migration_fault",))
    test.assertIsNone(error.__cause__)
    test.assertIsNone(error.__context__)


def journal_threads():
    return [t for t in threading.enumerate() if t.name.startswith("petsafe-migration")]


class Root:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.mkdir(os.path.join(self.root, ".storage"), 0o755)
        name = hashlib.sha256(ENTRY.encode("utf-8")).hexdigest() + ".json"
        self.dir = Path(self.root, ".storage", "petsafe_freshfeed_migrations")
        self.path = self.dir / name

    def close(self):
        self.tmp.cleanup()


def run(coro):
    return asyncio.run(coro)


async def expect_fault(test, awaitable):
    try:
        await awaitable
    except MigrationFault as error:
        assert_fixed(test, error)
        return error
    test.fail("no MigrationFault")


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.r = Root()
        self.addCleanup(self.r.close)
        self.journal = MigrationJournal(self.r.root, ENTRY)

    def test_path_is_sha256_private_and_roundtrips(self):
        async def body():
            self.assertIsNone(await self.journal.async_load())
            await self.journal.async_create(record())
            self.assertEqual(await self.journal.async_load(), record())
            await self.journal.async_advance(P.PREPARED, record(P.OLD_DRAINED, 2))
            return await MigrationJournal(self.r.root, ENTRY).async_load()
        self.assertEqual(run(body()), record(P.OLD_DRAINED, 2))
        self.assertEqual(sorted(os.listdir(self.r.dir)), [self.r.path.name])
        self.assertEqual(stat.S_IMODE(os.lstat(self.r.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.r.path).st_mode), 0o600)
        self.assertEqual(json.loads(self.r.path.read_text())["entry_id"], ENTRY)
        self.assertEqual(journal_threads(), [])

    def test_setting_targets_roundtrip_validated_and_immutable(self):
        retiring = ("number.feeder_brightness", "sensor.feeder_quick_feed_state",
                    "sensor.tuya_raw_food", "switch.feeder_meals", "switch.feeder_steel")
        targets = (("brightness_setting", 0), ("meal_counter", 3),
                   ("quick_feed_state", 1), ("stainless_inserts", 4))
        good = record(retiring=retiring, setting_targets=targets)

        async def body():
            await self.journal.async_create(good)
            loaded = await self.journal.async_load()
            await self.journal.async_advance(P.PREPARED, dataclasses.replace(
                good, phase=P.OLD_DRAINED, revision=2))
            return loaded
        self.assertEqual(run(body()), good)
        document = json.loads(self.r.path.read_bytes())
        self.assertEqual(document["setting_targets"], [list(item) for item in targets])
        # Immutable once journaled.
        run(expect_fault(self, self.journal.async_advance(P.OLD_DRAINED, dataclasses.replace(
            good, phase=P.DEVICE_MOVED, revision=3, setting_targets=targets[:1]))))
        for bad in ((("unknown_role", 0),),             # not a fixed role
                    (("brightness_setting", 5),),       # out of range
                    (("brightness_setting", True),),    # bool index
                    (("brightness_setting", -1),),
                    (("stainless_inserts", 0),),        # domain mismatch
                    (("meal_counter", 3), ("meal_counter", 4)),
                    (("meal_counter", 3), ("stainless_inserts", 3)),
                    [("meal_counter", 3)], (("meal_counter", 3, 1),)):
            with self.subTest(bad=bad), self.assertRaises(MigrationFault):
                record(retiring=retiring, setting_targets=bad)

    def test_untrusted_entry_ids(self):
        for bad in ("", "x" * 129, "a\x00b", 7, None, b"abc"):
            with self.assertRaises(MigrationFault) as caught:
                MigrationJournal(self.r.root, bad)
            assert_fixed(self, caught.exception)
        for bad_root in ("relative", self.r.root + "/../x", "/a\x00", 3):
            with self.assertRaises(MigrationFault):
                MigrationJournal(bad_root, ENTRY)
        traversal = "../../../../etc/passwd"
        journal = MigrationJournal(self.r.root, traversal)

        async def body():
            await journal.async_create(record(entry_id=traversal))
        run(body())
        expected = hashlib.sha256(traversal.encode("utf-8")).hexdigest() + ".json"
        self.assertEqual(os.listdir(self.r.dir), [expected])
        long_id = "\u00e9" * 128
        MigrationJournal(self.r.root, long_id)

    def test_phase_write_fault_matrix(self):
        """Every step of create/advance faults closed; advance only after both fsyncs."""
        steps = ("open_tmp", "write", "short_write", "fsync_file", "replace", "fsync_dir")

        for operation in ("create", "advance"):
            for step in steps:
                with self.subTest(operation=operation, step=step):
                    r = Root()
                    self.addCleanup(r.close)
                    journal = MigrationJournal(r.root, ENTRY)
                    if operation == "advance":
                        run(journal.async_create(record()))
                    before = r.path.read_bytes() if r.path.exists() else None
                    events = []
                    real = dict(open=mj._open_file, write=mj._write, fsync=mj._fsync,
                                replace=mj._replace)

                    def fake_open(name, flags, mode, dir_fd, step=step):
                        if step == "open_tmp" and name.endswith(".tmp"):
                            raise OSError(5, "boom " + SECRET)
                        return real["open"](name, flags, mode, dir_fd)

                    def fake_write(fd, data, step=step):
                        if step == "write":
                            raise OSError(28, "nospace " + SECRET)
                        if step == "short_write":
                            events.append("short")
                            return 0
                        events.append("write")
                        return real["write"](fd, data)

                    def fake_fsync(fd, step=step):
                        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                        due = "replace" in events if kind == "dir" else "write" in events
                        if step == "fsync_" + kind and due:
                            events.append("fail_" + kind)
                            raise OSError(5, "eio " + SECRET)
                        events.append("fsync_" + kind)
                        return real["fsync"](fd)

                    def fake_replace(src, dst, dir_fd, step=step):
                        if step == "replace":
                            raise OSError(5, "rename " + SECRET)
                        events.append("replace")
                        return real["replace"](src, dst, dir_fd)

                    target = record(P.OLD_DRAINED, 2) if operation == "advance" else record()
                    with patch.object(mj, "_open_file", fake_open), \
                            patch.object(mj, "_write", fake_write), \
                            patch.object(mj, "_fsync", fake_fsync), \
                            patch.object(mj, "_replace", fake_replace):
                        if operation == "advance":
                            call = journal.async_advance(P.PREPARED, target)
                        else:
                            call = journal.async_create(target)
                        error = run(expect_fault(self, call))
                    self.assertNotIn(SECRET, repr(error) + str(error))
                    after = r.path.read_bytes() if r.path.exists() else None
                    tmp_left = [n for n in os.listdir(r.dir) if n.endswith(".tmp")]
                    if step == "fsync_dir":
                        # Replace happened but durability is uncertain: reported as a
                        # fault, never as a completed advance.
                        self.assertEqual(events[-3:], ["fsync_file", "replace", "fail_dir"])
                    else:
                        # No replacement on uncertain write: prior content intact.
                        self.assertEqual(after, before)
                        self.assertNotIn("replace", events)
                        self.assertEqual(tmp_left, [])
                    self.assertEqual(journal_threads(), [])
                    # A later clean attempt reloads deterministically.
                    loaded = run(MigrationJournal(r.root, ENTRY).async_load())
                    if step == "fsync_dir":
                        self.assertEqual(loaded, target)
                    else:
                        self.assertEqual(loaded, None if operation == "create" else record())

    def test_success_orders_file_fsync_replace_dir_fsync(self):
        run(self.journal.async_create(record()))
        order = []
        real_fsync, real_replace = mj._fsync, mj._replace
        returned = []

        def fsync(fd):
            order.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_fsync(fd)

        def replace(src, dst, dir_fd):
            order.append("replace")
            real_replace(src, dst, dir_fd)

        async def body():
            await self.journal.async_advance(P.PREPARED, record(P.OLD_DRAINED, 2))
            returned.append(list(order))
        with patch.object(mj, "_fsync", fsync), patch.object(mj, "_replace", replace):
            run(body())
        self.assertEqual(returned[0], ["file", "replace", "dir"])

    def test_advance_conflicts_fault(self):
        run(self.journal.async_create(record()))
        bad = [
            (P.OLD_DRAINED, record(P.DEVICE_MOVED, 2)),          # wrong expected phase
            (P.PREPARED, record(P.OLD_DRAINED, 3)),              # revision skip
            (P.PREPARED, record(P.OLD_DRAINED, 1)),              # revision replay
            (P.PREPARED, record(P.DEVICE_MOVED, 2)),             # illegal transition
            (P.PREPARED, record(P.OLD_DRAINED, 2, device_id="f" * 32)),
            (P.PREPARED, record(P.OLD_DRAINED, 2, retiring=())),
            (P.PREPARED, record(P.OLD_DRAINED, 2, source_config_digest=OTHER_DIGEST)),
            (P.PREPARED, record(P.OLD_DRAINED, 2, entry_id="other")),
        ]
        for expected, target in bad:
            with self.subTest(target=target.phase):
                run(expect_fault(self, self.journal.async_advance(expected, target)))
        run(expect_fault(self, self.journal.async_create(record())))  # exists
        run(expect_fault(self, self.journal.async_create(record(P.OLD_DRAINED, 2))))
        self.assertEqual(run(self.journal.async_load()), record())
        missing = MigrationJournal(self.r.root, "missing-entry")
        run(expect_fault(self, missing.async_advance(
            P.PREPARED, record(P.OLD_DRAINED, 2, entry_id="missing-entry"))))

    def test_origin_marker_changes_only_at_checkpoint(self):
        async def body():
            await self.journal.async_create(record())
            await self.journal.async_advance(P.PREPARED, record(P.OLD_DRAINED, 2))
            await self.journal.async_advance(P.OLD_DRAINED, record(P.OLD_DRAINED, 3, marker=P2))
            await expect_fault(self, self.journal.async_advance(
                P.OLD_DRAINED, record(P.DEVICE_MOVED, 4, marker=P3)))
            await self.journal.async_advance(P.OLD_DRAINED, record(P.DEVICE_MOVED, 4, marker=P2))
            await expect_fault(self, self.journal.async_advance(
                P.DEVICE_MOVED, record(P.CANCEL_PREPARED, 5, marker=P2)))
            return await self.journal.async_load()
        self.assertEqual(run(body()), record(P.DEVICE_MOVED, 4, marker=P2))

    def test_journal_cancellation_drains_io(self):
        run(self.journal.async_create(record()))
        entered, release = threading.Event(), threading.Event()
        real_fsync = mj._fsync

        def slow_fsync(fd):
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                entered.set()
                release.wait(5)
            real_fsync(fd)

        async def body():
            task = asyncio.ensure_future(
                self.journal.async_advance(P.PREPARED, record(P.OLD_DRAINED, 2)))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            for _ in range(20):
                await asyncio.sleep(0.005)
            self.assertFalse(task.done())  # actual I/O still running: not settled
            self.assertEqual(len(journal_threads()), 1)
            # A second operation on the same journal waits behind the live I/O.
            second = asyncio.ensure_future(self.journal.async_load())
            await asyncio.sleep(0.02)
            self.assertFalse(second.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return await second
        with patch.object(mj, "_fsync", slow_fsync):
            loaded = run(body())
        # The write completed in full before cancellation was reported.
        self.assertEqual(loaded, record(P.OLD_DRAINED, 2))
        self.assertEqual(journal_threads(), [])
        self.assertEqual([n for n in os.listdir(self.r.dir) if n.endswith(".tmp")], [])

    def test_cancel_before_io_touches_nothing(self):
        async def body():
            task = asyncio.ensure_future(self.journal.async_create(record()))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        run(body())
        self.assertFalse(self.r.dir.exists())

    def test_corrupt_or_wrong_identity_journal_fences(self):
        run(self.journal.async_create(record()))
        good = self.r.path.read_bytes()
        document = json.loads(good)
        cases = {
            "garbage": b"{not json",
            "empty": b"",
            "nan": good.replace(b'"revision":1', b'"revision":NaN'),
            "duplicate": good[:-1] + b',"revision":1}',
            "oversize": b" " * (16 * 1024 + 1),
            "wrong_entry": json.dumps({**document, "entry_id": "someone-else"},
                                      sort_keys=True, separators=(",", ":")).encode(),
            "schema2": json.dumps({**document, "schema": 2}, sort_keys=True,
                                  separators=(",", ":")).encode(),
            "bool_revision": json.dumps({**document, "revision": True}, sort_keys=True,
                                        separators=(",", ":")).encode(),
            "unknown_phase": json.dumps({**document, "phase": "rolled_back"}, sort_keys=True,
                                        separators=(",", ":")).encode(),
            "extra_key": json.dumps({**document, "local_key": SECRET}, sort_keys=True,
                                    separators=(",", ":")).encode(),
            "missing_key": json.dumps({k: v for k, v in document.items() if k != "retiring"},
                                      sort_keys=True, separators=(",", ":")).encode(),
            "noncanonical": json.dumps(document, indent=2).encode(),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.r.path.write_bytes(data)
                os.chmod(self.r.path, 0o600)
                error = run(expect_fault(self, self.journal.async_load()))
                self.assertNotIn(SECRET, str(error) + repr(error.args))
                run(expect_fault(self, self.journal.async_advance(
                    P.PREPARED, record(P.OLD_DRAINED, 2))))
                self.assertEqual(self.r.path.read_bytes(), data)  # never repaired
        self.r.path.write_bytes(good)
        for mode in (0o644, 0o400):
            os.chmod(self.r.path, mode)
            run(expect_fault(self, self.journal.async_load()))
        os.chmod(self.r.path, 0o600)
        os.chmod(self.r.dir, 0o755)
        run(expect_fault(self, self.journal.async_load()))
        os.chmod(self.r.dir, 0o700)
        os.link(self.r.path, Path(self.r.root, "hardlink"))
        run(expect_fault(self, self.journal.async_load()))
        os.unlink(Path(self.r.root, "hardlink"))
        os.unlink(self.r.path)
        os.symlink(Path(self.r.root, "elsewhere"), self.r.path)
        Path(self.r.root, "elsewhere").write_bytes(good)
        run(expect_fault(self, self.journal.async_load()))
        self.assertEqual(journal_threads(), [])

    def test_oversized_inventory_refused_not_truncated(self):
        many = tuple(f"sensor.raw_{i}" for i in range(ms.MAX_INVENTORY + 1))
        with self.assertRaises(MigrationFault) as caught:
            record(retiring=many)
        assert_fixed(self, caught.exception)
        with self.assertRaises(MigrationFault):
            MigrationPreview(True, PreviewReason.ELIGIBLE, many)
        huge = tuple(f"sensor.{'y' * 200}_{i:03d}" for i in range(40))
        with self.assertRaises(MigrationFault):
            record(retiring=huge)
        with self.assertRaises(MigrationFault):
            MigrationPreview(True, PreviewReason.ELIGIBLE, huge)
        # Within the count bound but past the shared character bound: refused at
        # construction, so neither a preview nor a record can exist for it.
        wide = tuple(f"sensor.{'x' * 45}_{i:03d}" for i in range(ms.MAX_INVENTORY))
        self.assertGreater(sum(map(len, wide)), ms.MAX_INVENTORY_CHARS)
        with self.assertRaises(MigrationFault):
            MigrationPreview(True, PreviewReason.ELIGIBLE, wide)
        with self.assertRaises(MigrationFault):
            record(entity_disabled_by=tuple((e, "user") for e in wide))
        with self.assertRaises(MigrationFault):
            record(revision=ms.MAX_REVISION + 1)
        self.assertFalse(self.r.path.exists())

    def test_any_valid_record_fits_journal_bound(self):
        """The worst-case valid record encodes within 16 KiB and round-trips."""
        each = ms.MAX_INVENTORY_CHARS // ms.MAX_INVENTORY

        def inventory(prefix):
            # Exactly MAX_INVENTORY IDs totalling exactly MAX_INVENTORY_CHARS.
            base = f"{prefix}."
            return tuple(base + f"{i:03d}".rjust(each - len(base), "z")
                         for i in range(ms.MAX_INVENTORY))
        retiring = inventory("r")
        # Four retiring rows named for the setting roles, at the same length.
        domains = ("number", "sensor", "switch", "switch")
        retiring = tuple(f"{d}." + f"{i:03d}".rjust(each - len(d) - 1, "z")
                         for i, d in enumerate(domains)) + retiring[4:]
        targets = (("brightness_setting", 0), ("meal_counter", 3),
                   ("quick_feed_state", 1), ("stainless_inserts", 2))
        labelled = inventory("e")
        self.assertEqual(sum(map(len, retiring)), ms.MAX_INVENTORY_CHARS)
        preview = MigrationPreview(True, PreviewReason.ELIGIBLE, retiring)
        astral = "\U0001f600" * ms.MAX_ID_CHARS  # 12 escaped bytes per char
        worst = MigrationRecord(
            entry_id=astral, source_entry_id="\U0001f601" * ms.MAX_ID_CHARS,
            device_id="\U0001f602" * ms.MAX_ID_CHARS, source_config_digest=DIGEST,
            source_disabled_by="config_entry", device_disabled_by="config_entry",
            entity_disabled_by=tuple((e, "config_entry") for e in labelled),
            retiring=preview.retiring_entity_ids, origin_marker=P1,
            phase=P.VERIFIED, revision=ms.MAX_REVISION, verified_marker=P2,
            setting_targets=targets)
        self.assertLessEqual(len(mj._encode(worst)), mj.MAX_BYTES)
        self.assertGreater(len(mj._encode(worst)), 15 * 1024)  # genuinely near the bound
        journal = MigrationJournal(self.r.root, astral)

        async def body():
            await journal.async_create(dataclasses.replace(
                worst, phase=P.PREPARED, revision=1, verified_marker=None))
            return await journal.async_load()
        loaded = run(body())
        self.assertEqual(loaded.retiring, retiring)
        self.assertEqual(loaded.entity_disabled_by, worst.entity_disabled_by)
        self.assertEqual(loaded.setting_targets, targets)

    def test_journal_excludes_credentials(self):
        self.assertEqual(set(ms.RECORD_FIELDS), {
            "schema", "entry_id", "source_entry_id", "device_id", "source_config_digest",
            "source_disabled_by", "device_disabled_by", "entity_disabled_by", "retiring",
            "origin_marker", "phase", "revision", "verified_marker", "setting_targets"})
        for name in ("local_key", "host", "password", "key"):
            with self.assertRaises(TypeError):
                record(**{name: SECRET})
        for field, value in (("source_config_digest", SECRET), ("retiring", (SECRET,)),
                             ("source_disabled_by", SECRET), ("origin_marker", SECRET),
                             ("entity_disabled_by", ((SECRET, None),))):
            with self.subTest(field), self.assertRaises(MigrationFault) as caught:
                record(**{field: value})
            self.assertNotIn(SECRET, str(caught.exception) + repr(caught.exception.args))
        run(self.journal.async_create(record()))
        # An empty setting-target map is omitted: pre-existing journals stay canonical.
        self.assertEqual(set(json.loads(self.r.path.read_bytes())),
                         set(ms.RECORD_FIELDS) - {"setting_targets"})
        preview_fields = {f.name for f in dataclasses.fields(MigrationPreview)}
        self.assertEqual(preview_fields, {"eligible", "reason", "retiring_entity_ids"})
        with self.assertRaises(MigrationFault):
            MigrationPreview(False, PreviewReason.ELIGIBLE, ())
        with self.assertRaises(MigrationFault):
            MigrationPreview(True, PreviewReason.AMBIGUOUS_SOURCE, ())
        preview = MigrationPreview(False, PreviewReason.AMBIGUOUS_SOURCE, ())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            preview.eligible = True
        frozen = record()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            frozen.phase = P.VERIFIED

    def test_missing_journal_does_not_reset_intent(self):
        intent_dir = Path(self.r.root, "petsafe_freshfeed", "b" * 32)
        intent_dir.mkdir(parents=True)
        db = intent_dir / "intent.sqlite"
        db.write_bytes(b"retained-unknown-intent")
        snapshot = {p: p.read_bytes() for p in Path(self.r.root).rglob("*") if p.is_file()}
        self.assertIsNone(run(self.journal.async_load()))
        # Load of a missing journal creates nothing and touches no intent store.
        self.assertFalse(self.r.dir.exists())
        self.assertEqual({p: p.read_bytes() for p in Path(self.r.root).rglob("*")
                          if p.is_file()}, snapshot)
        # The reducer refuses to drive a migration it has no record of; it never
        # infers a virgin witness or a completed migration.
        for owner in LeafOwner:
            self.assertIs(next_action(None, facts(leaf_owner=owner)), MigrationAction.REFUSE)
        self.assertIs(status_for(None, MigrationAction.COMPLETE), MigrationStatus.REFUSED)
        self.assertFalse(cancel_allowed(None, facts()))
        # The journal lives outside the intent-store namespace.
        run(self.journal.async_create(record()))
        self.assertEqual(os.listdir(intent_dir), ["intent.sqlite"])
        self.assertEqual(db.read_bytes(), b"retained-unknown-intent")
        self.assertNotIn("petsafe_freshfeed/", str(self.r.path.relative_to(self.r.root)))


class ReducerCase(unittest.TestCase):
    def setUp(self):
        self.r = Root()
        self.addCleanup(self.r.close)
        self.journal = MigrationJournal(self.r.root, ENTRY)

    def test_process_marker_is_fresh_uuid4(self):
        parsed = uuid.UUID(ms.PROCESS_MARKER)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), ms.PROCESS_MARKER)
        self.assertNotEqual(ms.PROCESS_MARKER, str(os.getpid()))
        self.assertIs(ms.PROCESS_MARKER, ms.PROCESS_MARKER)

    def test_missing_facts_refuse(self):
        rec = record(P.OLD_DRAINED, 2)
        self.assertIs(next_action(rec, facts()), MigrationAction.RESUME)
        for field in dataclasses.fields(MigrationFacts):
            with self.subTest(field.name):
                self.assertIs(next_action(rec, facts(**{field.name: None})),
                              MigrationAction.REFUSE)
        self.assertIs(next_action(rec, facts(source_disabled=1)), MigrationAction.REFUSE)
        self.assertIs(next_action(rec, facts(backend="tuya")), MigrationAction.REFUSE)
        self.assertIs(next_action(rec, facts(process_marker="1234")), MigrationAction.REFUSE)
        self.assertIs(next_action(rec, object()), MigrationAction.REFUSE)
        self.assertIs(next_action(rec, facts(witness_valid=False)), MigrationAction.REFUSE)
        self.assertIs(next_action(rec, facts(source_config_digest=OTHER_DIGEST)),
                      MigrationAction.REFUSE)

    def test_same_process_checkpoint_refused(self):
        drained = record(P.OLD_DRAINED, 2, marker=P1)
        # The process that disabled the source may never resume, even if the
        # source looks disabled/unloaded.
        self.assertIs(next_action(drained, facts(process_marker=P1)), MigrationAction.CHECKPOINT)
        self.assertIs(status_for(drained, MigrationAction.CHECKPOINT),
                      MigrationStatus.RESTART_REQUIRED)
        # A new process where the disabled flag did not persist or the old source ran.
        self.assertIs(next_action(drained, facts(source_disabled=False)),
                      MigrationAction.CHECKPOINT)
        self.assertIs(next_action(drained, facts(source_loaded=True)), MigrationAction.CHECKPOINT)
        # PREPARED never implies the old owner stopped, in any process.
        for marker in (P1, P2):
            self.assertIs(next_action(record(), facts(process_marker=marker)),
                          MigrationAction.CHECKPOINT)
        self.assertIs(next_action(drained, facts(process_marker=P2)), MigrationAction.RESUME)
        self.assertIs(status_for(drained, MigrationAction.RESUME),
                      MigrationStatus.READY_TO_RESUME)

    def test_verified_marker_set_once_and_immutable(self):
        async def body():
            await self.journal.async_create(record())
            await self.journal.async_advance(P.PREPARED, record(P.OLD_DRAINED, 2))
            await self.journal.async_advance(P.OLD_DRAINED, record(P.DEVICE_MOVED, 3))
            await self.journal.async_advance(P.DEVICE_MOVED, record(P.OWNED_CONFIGURED, 4))
            await self.journal.async_advance(
                P.OWNED_CONFIGURED, record(P.VERIFIED, 5, verified_marker=P2))
            # A later VERIFIED advance may never rewrite the verifying process.
            await expect_fault(self, self.journal.async_advance(
                P.VERIFIED, record(P.VERIFIED, 6, verified_marker=P3)))
            await self.journal.async_advance(P.VERIFIED, record(P.VERIFIED, 6, verified_marker=P2))
            return await self.journal.async_load()
        self.assertEqual(run(body()), record(P.VERIFIED, 6, verified_marker=P2))
        # VERIFIED requires the marker; no other phase may carry one.
        with self.assertRaises(MigrationFault):
            record(P.VERIFIED, 5, verified_marker=None)
        for phase in P:
            if phase is not P.VERIFIED:
                with self.subTest(phase), self.assertRaises(MigrationFault):
                    record(phase, 2, verified_marker=P2)
        owned = facts(leaf_owner=LeafOwner.PETSAFE, backend="owned", owned_config_valid=True,
                      process_marker=P3)
        self.assertTrue(removal_allowed(record(P.VERIFIED, 6, verified_marker=P2), owned))
        self.assertFalse(removal_allowed(record(P.VERIFIED, 6, verified_marker=P3), owned))

    def test_status_for_every_reducer_output(self):
        S, A = MigrationStatus, MigrationAction
        expected = [
            (record(), A.CHECKPOINT, S.RESTART_REQUIRED),
            (record(P.OLD_DRAINED, 2), A.CHECKPOINT, S.RESTART_REQUIRED),
            (record(P.OLD_DRAINED, 2), A.RESUME, S.READY_TO_RESUME),
            (record(P.DEVICE_MOVED, 3), A.RESUME, S.FORWARD_REPAIR_REQUIRED),
            (record(P.OWNED_CONFIGURED, 4), A.RESUME, S.FORWARD_REPAIR_REQUIRED),
            (record(P.VERIFIED, 5), A.RESUME, S.FORWARD_REPAIR_REQUIRED),
            (record(P.OWNED_CONFIGURED, 4), A.VERIFY, S.READY_TO_RESUME),
            (record(P.CANCEL_PREPARED, 3), A.CANCEL, S.READY_TO_RESUME),
            (record(P.VERIFIED, 5), A.COMPLETE, S.COMPLETE),
            (record(P.LEGACY_RESTORED, 4), A.COMPLETE, S.CANCELLED),
        ]
        for rec, action, status in expected:
            with self.subTest(phase=rec.phase, action=action):
                self.assertIs(status_for(rec, action), status)
        for phase in P:
            self.assertIs(status_for(record(phase, 2), A.REFUSE), S.REFUSED)
        # Pairs the reducer never produces refuse rather than guess a status.
        for rec, action in ((record(), A.RESUME), (record(P.DEVICE_MOVED, 3), A.VERIFY),
                            (record(P.OLD_DRAINED, 2), A.CANCEL),
                            (record(P.DEVICE_MOVED, 3), A.CHECKPOINT),
                            (record(P.OWNED_CONFIGURED, 4), A.COMPLETE),
                            (record(P.CANCEL_PREPARED, 3), A.COMPLETE)):
            with self.subTest(phase=rec.phase, action=action):
                self.assertIs(status_for(rec, action), S.REFUSED)

    def test_move_landed_before_journal_resumes_forward(self):
        """Crash window: registry move persisted, DEVICE_MOVED journal write did not."""
        drained = record(P.OLD_DRAINED, 3, marker=P2)  # checkpointed by process P2
        moved = dict(leaf_owner=LeafOwner.PETSAFE)
        after_crash = facts(**moved, process_marker=P3)
        self.assertIs(next_action(drained, after_crash), MigrationAction.RESUME)
        self.assertFalse(cancel_allowed(drained, after_crash))
        # Same process as the checkpoint, or old source observed running: fence.
        self.assertIs(next_action(drained, facts(**moved, process_marker=P2)),
                      MigrationAction.REFUSE)
        self.assertIs(next_action(drained, facts(**moved, process_marker=P3, source_loaded=True)),
                      MigrationAction.REFUSE)
        self.assertIs(next_action(drained, facts(**moved, process_marker=P3,
                                                 source_disabled=False)),
                      MigrationAction.REFUSE)
        # The journal then advances forward to DEVICE_MOVED; never back to cancel.
        async def body():
            await self.journal.async_create(record())
            await self.journal.async_advance(P.PREPARED, record(P.OLD_DRAINED, 2))
            await self.journal.async_advance(P.OLD_DRAINED, drained)
            await self.journal.async_advance(P.OLD_DRAINED,
                                             record(P.DEVICE_MOVED, 4, marker=P2))
            return await self.journal.async_load()
        self.assertEqual(run(body()), record(P.DEVICE_MOVED, 4, marker=P2))

    def test_phase_never_overrides_actual_owner(self):
        moved = dict(leaf_owner=LeafOwner.PETSAFE)
        owned = dict(leaf_owner=LeafOwner.PETSAFE, backend="owned", owned_config_valid=True)
        # Post-move phases with a still-legacy leaf: the journal got ahead of a
        # lost (delayed) registry save. Never COMPLETE/VERIFY/cancel: forward
        # RESUME redoes the move, only in a different process with the old
        # source stopped, whatever backend the config entry persisted.
        for phase, revision in ((P.DEVICE_MOVED, 3), (P.OWNED_CONFIGURED, 4), (P.VERIFIED, 5)):
            for backend in (dict(), dict(backend="owned", owned_config_valid=True)):
                self.assertIs(next_action(record(phase, revision), facts(**backend)),
                              MigrationAction.RESUME)
                self.assertFalse(cancel_allowed(record(phase, revision), facts(**backend)))
                self.assertIs(next_action(record(phase, revision),
                                          facts(**backend, source_loaded=True)),
                              MigrationAction.REFUSE)
                self.assertIs(next_action(record(phase, revision),
                                          facts(**backend, source_disabled=False)),
                              MigrationAction.REFUSE)
                self.assertIs(next_action(record(phase, revision),
                                          facts(**backend, process_marker=P1)),
                              MigrationAction.REFUSE)
            self.assertIs(status_for(record(phase, revision), MigrationAction.RESUME),
                          MigrationStatus.FORWARD_REPAIR_REQUIRED)
        # Pre-move phases with a moved leaf: forward-only, never cancel/checkpoint.
        for phase in (P.PREPARED, P.CANCEL_PREPARED, P.LEGACY_RESTORED):
            self.assertIs(next_action(record(phase, 2), facts(**moved)), MigrationAction.REFUSE)
            self.assertFalse(cancel_allowed(record(phase, 2), facts(**moved)))
        # OLD_DRAINED with a moved leaf is the move-before-journal crash window:
        # forward RESUME in a different process with the old source stopped only.
        self.assertIs(next_action(record(P.OLD_DRAINED, 2), facts(**moved)),
                      MigrationAction.RESUME)
        self.assertIs(next_action(record(P.OLD_DRAINED, 2), facts(**moved, source_loaded=True)),
                      MigrationAction.REFUSE)
        self.assertIs(next_action(record(P.OLD_DRAINED, 2), facts(**moved, process_marker=P1)),
                      MigrationAction.REFUSE)
        self.assertFalse(cancel_allowed(record(P.OLD_DRAINED, 2), facts(**moved)))
        self.assertTrue(cancel_allowed(record(P.OLD_DRAINED, 2), facts()))
        self.assertTrue(cancel_allowed(record(), facts(source_disabled=False, source_loaded=True)))
        self.assertFalse(cancel_allowed(record(P.DEVICE_MOVED, 3), facts()))
        # Foreign/shared leaf refuses in every non-terminal phase; a finished
        # cancellation is terminal (the legacy hub validates its own source).
        for phase in P:
            if phase is P.LEGACY_RESTORED:
                continue
            self.assertIs(next_action(record(phase, 2), facts(leaf_owner=LeafOwner.OTHER)),
                          MigrationAction.REFUSE)
        # Moved leaf while the old source runs again: refuse, never two owners.
        self.assertIs(next_action(record(P.DEVICE_MOVED, 3), facts(**moved, source_loaded=True)),
                      MigrationAction.REFUSE)
        self.assertIs(next_action(record(P.VERIFIED, 5), facts(**owned, source_disabled=False)),
                      MigrationAction.REFUSE)
        # Forward progress after the move.
        self.assertIs(next_action(record(P.DEVICE_MOVED, 3), facts(**moved)),
                      MigrationAction.RESUME)
        self.assertIs(status_for(record(P.DEVICE_MOVED, 3), MigrationAction.RESUME),
                      MigrationStatus.FORWARD_REPAIR_REQUIRED)
        self.assertIs(next_action(record(P.OWNED_CONFIGURED, 4), facts(**moved)),
                      MigrationAction.RESUME)  # owned config did not persist
        self.assertIs(next_action(record(P.OWNED_CONFIGURED, 4), facts(**owned)),
                      MigrationAction.VERIFY)
        self.assertIs(next_action(record(P.VERIFIED, 5), facts(**moved)), MigrationAction.RESUME)
        verified = record(P.VERIFIED, 5)
        self.assertIs(next_action(verified, facts(**owned)), MigrationAction.COMPLETE)
        self.assertIs(status_for(verified, MigrationAction.COMPLETE), MigrationStatus.COMPLETE)
        # Removal only in a later process than the verifying one.
        self.assertFalse(removal_allowed(verified, facts(**owned, process_marker=P2)))
        self.assertTrue(removal_allowed(verified, facts(**owned, process_marker=P3)))
        self.assertFalse(removal_allowed(record(P.VERIFIED, 5), facts(**owned, process_marker=P2)))
        # A move in the checkpoint process itself is a violation.
        self.assertIs(next_action(record(P.DEVICE_MOVED, 3), facts(**moved, process_marker=P1)),
                      MigrationAction.REFUSE)
        # Cancellation path while legacy-owned.
        self.assertIs(next_action(record(P.CANCEL_PREPARED, 3), facts()), MigrationAction.CANCEL)
        restored = record(P.LEGACY_RESTORED, 4)
        self.assertIs(next_action(restored, facts()), MigrationAction.COMPLETE)
        self.assertIs(status_for(restored, MigrationAction.COMPLETE), MigrationStatus.CANCELLED)
        # Owned backend persisted before the move is inconsistent.
        self.assertIs(next_action(record(P.OLD_DRAINED, 2), facts(backend="owned")),
                      MigrationAction.REFUSE)
        with self.assertRaises(MigrationFault):
            record(P.OWNED_CONFIGURED, 4, verified_marker=P2)

    def test_terminal_phases_ignore_source_digest_and_witness(self):
        owned = dict(leaf_owner=LeafOwner.PETSAFE, backend="owned", owned_config_valid=True)
        absent = dict(source_config_digest=None, source_disabled=True, source_loaded=False)
        verified, restored = record(P.VERIFIED, 5), record(P.LEGACY_RESTORED, 4)
        for drift in (dict(source_config_digest=OTHER_DIGEST), absent, dict()):
            for witness in (True, False, None):
                with self.subTest(drift=drift, witness=witness):
                    self.assertIs(next_action(verified, facts(**owned, **drift,
                                                              witness_valid=witness)),
                                  MigrationAction.COMPLETE)
                    self.assertIs(next_action(restored, facts(**drift, witness_valid=witness)),
                                  MigrationAction.COMPLETE)
                    # A cancelled migration whose leaf was replaced stays terminal.
                    self.assertIs(next_action(restored, facts(**drift, witness_valid=witness,
                                                              leaf_owner=LeafOwner.OTHER)),
                                  MigrationAction.COMPLETE)
        # A re-enabled old source still fences the owned entry.
        self.assertIs(next_action(verified, facts(**owned, source_disabled=False)),
                      MigrationAction.REFUSE)
        # A restored entry never runs over a leaf PetSafe owns, nor as owned.
        self.assertIs(next_action(restored, facts(leaf_owner=LeafOwner.PETSAFE)),
                      MigrationAction.REFUSE)
        self.assertIs(next_action(restored, facts(backend="owned")), MigrationAction.REFUSE)
        # An absent source must be consistently stopped.
        self.assertIs(next_action(verified, facts(**owned, source_config_digest=None,
                                                  source_loaded=True)),
                      MigrationAction.REFUSE)

    def test_non_terminal_phases_require_witness(self):
        moved = dict(leaf_owner=LeafOwner.PETSAFE)
        for rec in (record(P.OLD_DRAINED, 2), record(P.DEVICE_MOVED, 3)):
            for witness in (False, None):
                self.assertIs(next_action(rec, facts(**moved, witness_valid=witness)),
                              MigrationAction.REFUSE)
        # A VERIFIED journal needing forward repair is not terminal.
        self.assertIs(next_action(record(P.VERIFIED, 5), facts(**moved, witness_valid=False)),
                      MigrationAction.REFUSE)

    def test_post_move_source_absent_or_changed_repairs_forward(self):
        moved = dict(leaf_owner=LeafOwner.PETSAFE)
        owned = dict(moved, backend="owned", owned_config_valid=True)
        for drift in (dict(source_config_digest=None), dict(source_config_digest=OTHER_DIGEST)):
            with self.subTest(drift=drift):
                for rec in (record(P.OLD_DRAINED, 2), record(P.DEVICE_MOVED, 3),
                            record(P.OWNED_CONFIGURED, 4), record(P.VERIFIED, 5)):
                    self.assertIs(next_action(rec, facts(**moved, **drift)),
                                  MigrationAction.RESUME)
                    self.assertFalse(cancel_allowed(rec, facts(**moved, **drift)))
                self.assertIs(next_action(record(P.OWNED_CONFIGURED, 4), facts(**owned, **drift)),
                              MigrationAction.VERIFY)
                # Before the move a changed or missing source never resumes.
                self.assertIs(next_action(record(P.OLD_DRAINED, 2), facts(**drift)),
                              MigrationAction.REFUSE)
                self.assertIs(next_action(record(P.DEVICE_MOVED, 3), facts(**drift)),
                              MigrationAction.REFUSE)

    def test_renew_replaces_only_a_cancelled_journal(self):
        async def body():
            await expect_fault(self, self.journal.async_renew(record(revision=2)))
            await self.journal.async_create(record())
            await expect_fault(self, self.journal.async_renew(record(revision=2)))
            await self.journal.async_advance(P.PREPARED, record(P.CANCEL_PREPARED, 2))
            await self.journal.async_advance(P.CANCEL_PREPARED, record(P.LEGACY_RESTORED, 3))
            fresh = record(revision=4, marker=P3, source_config_digest=OTHER_DIGEST)
            for bad in (record(P.OLD_DRAINED, 4), record(revision=3), record(revision=1),
                        record(revision=4, entry_id="another")):
                await expect_fault(self, self.journal.async_renew(bad))
            await self.journal.async_renew(fresh)
            return await self.journal.async_load()
        self.assertEqual(run(body()),
                         record(revision=4, marker=P3, source_config_digest=OTHER_DIGEST))
        self.assertEqual(journal_threads(), [])

    def test_remove_deletes_only_this_entry_journal(self):
        other = MigrationJournal(self.r.root, SOURCE)

        async def body():
            await self.journal.async_remove()  # absent: nothing to do
            await self.journal.async_create(record())
            await other.async_create(record(entry_id=SOURCE, source_entry_id=ENTRY))
            await self.journal.async_remove()
            await self.journal.async_remove()
            return await self.journal.async_load(), await other.async_load()
        mine, theirs = run(body())
        self.assertIsNone(mine)
        self.assertEqual(theirs, record(entry_id=SOURCE, source_entry_id=ENTRY))
        self.assertFalse(self.r.path.exists())
        self.assertEqual(len(os.listdir(self.r.dir)), 1)
        # No journal directory at all is not an error either.
        fresh = Root()
        self.addCleanup(fresh.close)
        run(MigrationJournal(fresh.root, ENTRY).async_remove())
        self.assertFalse(fresh.dir.exists())


if __name__ == "__main__":
    unittest.main()
