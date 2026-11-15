import base64
import unittest

from custom_components.petsafe_freshfeed.quick_feed import QuickFeed, decode_quick_feed


class QuickFeedTests(unittest.TestCase):
    def test_all_commands_statuses_and_minute_bounds(self):
        for command, name in ((0, 'none'), (16, 'start'), (32, 'cancel'), (48, 'now')):
            for status, label in enumerate(('idle', 'pending', 'feeding', 'ready', 'failed')):
                for minutes in (0, 255):
                    value = base64.b64encode(bytes((command, status, minutes))).decode()
                    self.assertEqual(decode_quick_feed(value), QuickFeed(name, label, minutes))

    def test_live_four_byte_reports(self):
        for value, status, minutes in (
            ('EAAAAA==', 'idle', 0), ('EAEFAA==', 'pending', 5),
            ('EAEEAA==', 'pending', 4), ('EAEDAA==', 'pending', 3),
            ('EAECAA==', 'pending', 2), ('EAEBAA==', 'pending', 1),
            ('EAEAAA==', 'pending', 0), ('EAIAAA==', 'feeding', 0),
            ('EAMAAA==', 'ready', 0),
        ):
            with self.subTest(value=value):
                self.assertEqual(decode_quick_feed(value), QuickFeed('start', status, minutes))

    def test_invalid_inputs_are_unknown(self):
        for value in (None, '', 'EA==', 'EAAAAQ==', 'EAAAAAAA', 'EAAAAA=',
                      'EAAAAA===', 'EAAAA B==', 'EBAAAA==', 'EQUAAA==',
                      '!!!!', 'ABUA', 'QQAA', 'AAAA\n'):
            with self.subTest(value=value):
                self.assertEqual(decode_quick_feed(value), QuickFeed(None, None, None))
