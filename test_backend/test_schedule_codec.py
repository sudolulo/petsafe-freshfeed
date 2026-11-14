import base64
import dataclasses
from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from custom_components.petsafe_freshfeed import schedule_codec as codec


class ScheduleCodecTests(unittest.TestCase):
    def test_observed_vector_and_text_forms(self):
        raw = bytes.fromhex('7f070001007f130001007f0c000100')
        expected = (codec.MealEntry(127, 7, 0, True, False),
                    codec.MealEntry(127, 19, 0, True, False),
                    codec.MealEntry(127, 12, 0, True, False))
        self.assertEqual(codec.decode(raw), expected)
        self.assertIs(codec.decode(raw)[0].enabled, True)
        self.assertIs(codec.decode(raw)[0].audible, False)
        self.assertEqual(codec.encode(expected), raw)
        self.assertEqual(codec.decode_text(raw.hex().upper()), expected)
        self.assertEqual(codec.decode_text(base64.b64encode(raw).decode()), expected)
        self.assertEqual(codec.encode_base64(expected), base64.b64encode(raw).decode())

    def test_round_trips_and_limits(self):
        entries = (codec.MealEntry(1, 0, 0, False, True),
                   codec.MealEntry(127, 23, 59, True, False)) * 5
        raw = codec.encode(entries)
        self.assertEqual(codec.decode(raw), entries)
        self.assertEqual(codec.encode(codec.decode(raw)), raw)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            entries[0].hour = 2

    def test_empty_schedule_wire_marker(self):
        self.assertEqual(codec.decode(b'\xff'), ())
        self.assertEqual(codec.encode(()), b'\xff')
        self.assertEqual(codec.decode_text('ff'), ())
        self.assertEqual(codec.encode_base64(()), '/w==')
        for raw in (b'', b'\xff\xff', b'\xff\x01\x00\x00\x01\x00'):
            with self.subTest(raw=raw), self.assertRaises(codec.ScheduleCodecError):
                codec.decode(raw)

    def test_invalid_fields_on_construction_and_decode(self):
        valid = [1, 0, 0, True, False]
        for index, bad in ((0, 0), (0, 128), (1, 24), (2, 60),
                           (0, -1), (1, -1), (2, -1)):
            fields = valid.copy()
            fields[index] = bad
            with self.subTest(fields=fields), self.assertRaises(codec.ScheduleCodecError):
                codec.MealEntry(*fields)
            if 0 <= bad <= 255:
                with self.subTest(wire=fields), self.assertRaises(codec.ScheduleCodecError):
                    codec.decode(bytes(fields))
        for index in range(3):
            fields = valid.copy()
            fields[index] = True
            with self.subTest(index=index), self.assertRaises(codec.ScheduleCodecError):
                codec.MealEntry(*fields)
        for index in (3, 4):
            for bad in (0, 1, 2, -1, None):
                fields = valid.copy()
                fields[index] = bad
                with self.subTest(fields=fields), self.assertRaises(codec.ScheduleCodecError):
                    codec.MealEntry(*fields)
            for bad in (2, 255):
                fields = valid.copy()
                fields[index] = bad
                with self.subTest(wire=fields), self.assertRaises(codec.ScheduleCodecError):
                    codec.decode(bytes(fields))

    def test_invalid_lengths_and_types(self):
        for raw in (b'1234567', b'\x01\x00\x00\x01\x00' * 11, '12345', bytearray(5), None, True):
            with self.subTest(raw=raw), self.assertRaises(codec.ScheduleCodecError):
                codec.decode(raw)
        for entries in ([codec.MealEntry(1, 0, 0, True, False)] * 11, [object()], None, b''):
            with self.subTest(entries=entries), self.assertRaises(codec.ScheduleCodecError):
                codec.encode(entries)

    def test_text_rejection_and_fixed_error(self):
        messages = set()
        for value in ('secret!', 'a', 'abc=', '!!!!', None, b'YWJj'):
            with self.subTest(value=value):
                with self.assertRaises(codec.ScheduleCodecError) as error:
                    codec.decode_text(value)
                messages.add(str(error.exception))
                if isinstance(value, str) and len(value) > 3:
                    self.assertNotIn(value, str(error.exception))
        self.assertEqual(len(messages), 1)

    def test_strict_base64_live_vector(self):
        value = 'fwcAAQB/EwABAH8MAAEA'
        entries = (codec.MealEntry(127, 7, 0, True, False),
                   codec.MealEntry(127, 19, 0, True, False),
                   codec.MealEntry(127, 12, 0, True, False))
        self.assertEqual(codec.decode_base64(value), entries)
        self.assertEqual(codec.encode_base64(entries), value)
        self.assertEqual(codec.decode_base64('/w=='), ())
        rows = (codec.MealEntry(64, 23, 59, False, True), entries[0], entries[0])
        self.assertEqual(codec.decode_base64(codec.encode_base64(rows)), rows)
        # Canonical base64 composed entirely of hexadecimal characters.
        self.assertEqual(codec.decode_base64('aA00AAA0AA0AAA0AAAAA'),
                         (codec.MealEntry(104, 13, 52, False, False),
                          codec.MealEntry(52, 0, 13, False, False),
                          codec.MealEntry(13, 0, 0, False, False)))
        bad_wire = [b'', b'\x01\x00\x00\x01\x00' * 11,
                    b'\x00\x00\x00\x01\x00', b'\x80\x00\x00\x01\x00',
                    b'\x01\x18\x00\x01\x00', b'\x01\x00\x3c\x01\x00',
                    b'\x01\x00\x00\x02\x00', b'\x01\x00\x00\x01\x02']
        # A five-byte row also has unused pad bits; AB== and similar
        # noncanonical encodings must not be accepted as equivalent data.
        invalid = [' /w==', '/w==\n', '/w=', '/w===', '/x==',
                   'AQAAAAB=', '!!!!', None, b'/w==']
        invalid += [base64.b64encode(raw).decode() for raw in bad_wire]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(codec.ScheduleCodecError):
                codec.decode_base64(value)

    def test_weekday_bits(self):
        for bit, day in zip((1, 2, 4, 8, 16, 32, 64), (7, 6, 5, 4, 3, 2, 1)):
            self.assertEqual(codec.weekdays(bit), (day,))

    def test_next_occurrence_dst(self):
        ny = ZoneInfo('America/New_York')
        utc = timezone.utc
        daily = (codec.MealEntry(127, 2, 30, True, True),)
        self.assertEqual(codec.next_occurrence(daily, now=datetime(2026, 3, 8, 6, tzinfo=utc), tz=ny), datetime(2026, 3, 9, 6, 30, tzinfo=utc))
        sunday = (codec.MealEntry(1, 2, 30, True, True),)
        self.assertEqual(codec.next_occurrence(sunday, now=datetime(2026, 3, 2, 6, tzinfo=utc), tz=ny),
                         datetime(2026, 3, 15, 6, 30, tzinfo=utc))
        daily = (codec.MealEntry(127, 1, 30, True, True),)
        for hour, minute, expected in [(5, 0, datetime(2026, 11, 1, 5, 30, tzinfo=utc)),
                                       (5, 45, datetime(2026, 11, 1, 6, 30, tzinfo=utc)),
                                       (6, 30, datetime(2026, 11, 2, 6, 30, tzinfo=utc))]:
            self.assertEqual(codec.next_occurrence(daily, now=datetime(2026, 11, 1, hour, minute, tzinfo=utc), tz=ny), expected)

    def test_next_occurrence_weekly_and_unavailable(self):
        utc = timezone.utc
        now = datetime(2026, 11, 2, 8, tzinfo=utc)  # Monday
        rows = (codec.MealEntry(64, 9, 0, True, True), codec.MealEntry(64, 8, 30, True, False),
                codec.MealEntry(64, 8, 1, False, True))
        self.assertEqual(codec.next_occurrence(rows, now=now, tz=utc), datetime(2026, 11, 2, 8, 30, tzinfo=utc))
        self.assertEqual(codec.next_occurrence(rows[:1], now=datetime(2026, 11, 2, 9, tzinfo=utc), tz=utc), datetime(2026, 11, 9, 9, tzinfo=utc))
        self.assertEqual(codec.next_occurrence((codec.MealEntry(127, 14, 0, True, True),), now=now, tz=ZoneInfo('Asia/Kathmandu')), datetime(2026, 11, 2, 8, 15, tzinfo=utc))
        self.assertIsNone(codec.next_occurrence((), now=now, tz=utc))
        self.assertIsNone(codec.next_occurrence(rows[2:], now=now, tz=utc))
        with self.assertRaisesRegex(ValueError, '^naive_now$'):
            codec.next_occurrence(rows, now=datetime(2026, 11, 2), tz=utc)

    def test_every_day(self):
        self.assertTrue(codec.is_every_day(127))
        self.assertFalse(codec.is_every_day(126))


if __name__ == '__main__':
    unittest.main()
