"""Pure LocalConnection validation: no TinyTuya, Home Assistant or network."""

import copy
import pickle
import sys
import unittest

from custom_components.petsafe_freshfeed.owned_source import (
    INVALID_CONFIGURATION, UNSUPPORTED_CONFIGURATION, LocalConnection, LocalConnectionError,
)

DEVICE = "bf0123456789abcdefgh"
HOST = "192.168.210.21"
KEY = "".join(chr(0x41 + i) for i in range(16))  # synthetic ABCD...P
SECRET_PARTS = (KEY, HOST, DEVICE)


def make(**overrides):
    values = {"device_id": DEVICE, "host": HOST, "local_key": KEY, "version": "3.5"}
    values.update(overrides)
    return LocalConnection(**values)


class LocalConnectionTests(unittest.TestCase):
    def assert_fixed(self, raised, code):
        error = raised.exception
        self.assertIsInstance(error, LocalConnectionError)
        self.assertEqual(error.args, (code,))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for part in SECRET_PARTS:
            self.assertNotIn(part, str(error))
            self.assertNotIn(part, repr(error))

    def test_valid_protocol_35_holder(self):
        connection = make()
        self.assertEqual((connection.device_id, connection.host, connection.local_key,
                          connection.version), (DEVICE, HOST, KEY, "3.5"))
        self.assertEqual(float(connection.version), 3.5)
        for host in ("10.0.0.1", "172.16.0.9", "172.31.255.254", "192.168.0.2"):
            self.assertEqual(make(host=host).host, host)

    def test_only_protocol_35_admitted_no_downgrade(self):
        for version in ("3.1", "3.2", "3.3", "3.4", "Auto", "3.6", "", None, 3.5, 3.3, "3.50", " 3.5"):
            with self.subTest(version=version), self.assertRaises(LocalConnectionError) as raised:
                make(version=version)
            self.assert_fixed(raised, UNSUPPORTED_CONFIGURATION)

    def test_host_must_be_rfc1918_ipv4_literal(self):
        for host in ("Auto", "0.0.0.0", "feeder.local", "8.8.8.8", "127.0.0.1", "169.254.1.1",
                     "172.32.0.1", "fd00::1", " 192.168.1.2", "192.168.1.2 ", "192.168.001.002",
                     "", None, 3232235777):
            with self.subTest(host=host), self.assertRaises(LocalConnectionError) as raised:
                make(host=host)
            self.assert_fixed(raised, UNSUPPORTED_CONFIGURATION)

    def test_key_exactly_16_printable_ascii(self):
        for key in ("x" * 15, "x" * 17, "", None, b"0123456789abcdef", "0123456789abcde\n",
                    "0123456789abcde\x00", "0123456789abcde\u00e9", "0123456789abcde\x7f"):
            with self.subTest(key=repr(key)), self.assertRaises(LocalConnectionError) as raised:
                make(local_key=key)
            self.assert_fixed(raised, INVALID_CONFIGURATION)
        self.assertEqual(make(local_key="!~ {}|<>^`'\"\\;:,").local_key, "!~ {}|<>^`'\"\\;:,")

    def test_device_id_shape(self):
        for device in ("", "x" * 65, "bad id", "bad/id", None, 12, "id\n"):
            with self.subTest(device=repr(device)), self.assertRaises(LocalConnectionError) as raised:
                make(device_id=device)
            self.assert_fixed(raised, INVALID_CONFIGURATION)

    def test_redacted_representations(self):
        connection = make()
        for text in (repr(connection), str(connection), f"{connection}", "%s" % (connection,),
                     "%r" % (connection,)):
            for part in SECRET_PARTS:
                self.assertNotIn(part, text)
        self.assertFalse(hasattr(connection, "__dict__"))

    def test_not_picklable_or_copyable_and_immutable(self):
        connection = make()
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.assertRaises(TypeError) as raised:
                pickle.dumps(connection, protocol)
            self.assertNotIn(KEY, str(raised.exception))
        with self.assertRaises(TypeError):
            copy.copy(connection)
        with self.assertRaises(TypeError):
            copy.deepcopy(connection)
        with self.assertRaises(AttributeError):
            connection.host = "192.168.1.9"
        with self.assertRaises(AttributeError):
            connection.extra = 1
        with self.assertRaises(AttributeError):
            del connection.version
        self.assertEqual(connection.host, HOST)

    def test_pure_import_has_no_tinytuya_or_home_assistant(self):
        self.assertNotIn("tinytuya", sys.modules)
        self.assertNotIn("homeassistant", sys.modules)
        self.assertFalse(any(name == "custom_components.tuya_local"
                             or name.startswith("custom_components.tuya_local.")
                             for name in sys.modules))


if __name__ == "__main__":
    unittest.main()
