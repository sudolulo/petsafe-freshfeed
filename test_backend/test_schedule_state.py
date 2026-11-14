import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone, timedelta

from custom_components.petsafe_freshfeed.schedule_state import ScheduleState


class ScheduleStateTests(unittest.TestCase):
    def test_reports_disconnect_and_revision(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        initial = ScheduleState()
        self.assertIsNone(initial.entries)
        self.assertIsNone(initial.revision)
        current = initial.with_report('/w==', at)
        self.assertEqual(current.entries, ())
        self.assertEqual(current.source_state, 'current')
        repeated = current.with_report('/w==', at + timedelta(seconds=1))
        self.assertEqual(repeated.revision, current.revision)
        self.assertNotEqual(repeated.observed_at, current.observed_at)
        disconnected = repeated.disconnected()
        self.assertEqual(disconnected.source_state, 'last_known')
        self.assertEqual(disconnected.revision, current.revision)
        self.assertEqual(disconnected.observed_at, repeated.observed_at)
        with self.assertRaises(FrozenInstanceError):
            disconnected.error = 'x'

    def test_nonempty_report_round_trip_and_content_revision(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        current = ScheduleState().with_report('fwgAAQE=', at)
        self.assertIsNotNone(current.entries)
        self.assertEqual(len(current.entries), 1)
        self.assertEqual(current.source_state, 'current')
        self.assertEqual(current.observed_at, at)
        repeated = current.with_report('fwgAAQE=', at + timedelta(seconds=1))
        self.assertEqual(repeated.revision, current.revision)
        restored = ScheduleState.from_dict(repeated.to_dict())
        self.assertEqual(restored.entries, current.entries)
        self.assertEqual(restored.observed_at, repeated.observed_at)
        self.assertEqual(restored.source_state, 'last_known')
        self.assertEqual(restored.revision, current.revision)
        changed = repeated.with_report('fwgBAQE=', at + timedelta(seconds=2))
        self.assertNotEqual(changed.entries, current.entries)
        self.assertNotEqual(changed.revision, current.revision)

    def test_invalid_report_preserves_cache(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        unknown = ScheduleState().with_report('bad', at)
        self.assertIsNone(unknown.entries)
        self.assertEqual(unknown.source_state, 'unknown')
        self.assertEqual(unknown.error, 'invalid_schedule')
        previous = ScheduleState().with_report('/w==', at).disconnected()
        invalid = previous.with_report('bad', at + timedelta(seconds=1))
        self.assertEqual(invalid.entries, ())
        self.assertEqual(invalid.observed_at, at)
        self.assertEqual(invalid.source_state, 'last_known')
        self.assertEqual(invalid.error, 'invalid_schedule')

    def test_restore_and_invalid_cache(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        current = ScheduleState().with_report('/w==', at)
        restored = ScheduleState.from_dict(current.to_dict())
        self.assertEqual(restored.entries, ())
        self.assertEqual(restored.source_state, 'last_known')
        self.assertEqual(restored.revision, current.revision)
        for invalid in (None, {}, {'value': 'bad', 'observed_at': 'invalid'}):
            self.assertIsNone(ScheduleState.from_dict(invalid).entries)
