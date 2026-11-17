"""Neutral local-source contract shared by legacy and owned sources (synthetic)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from custom_components.petsafe_freshfeed import local_source
from custom_components.petsafe_freshfeed.local_source import (
    ACK_DPS, OBSERVED_DPS, LocalSource, split_frame,
)
from custom_components.petsafe_freshfeed.observed import RAW_DPS


class DpsSetTests(unittest.TestCase):
    def test_observed_is_raw_plus_schedule_and_quick_feed(self):
        self.assertEqual(OBSERVED_DPS, frozenset(str(dp) for dp in (*RAW_DPS, 1, 140)))
        self.assertTrue(all(type(dp) is str for dp in OBSERVED_DPS))

    def test_ack_dps_exact(self):
        self.assertEqual(ACK_DPS, frozenset({"1", "4", "14", "110", "111", "125", "140",
                                             "142", "145", "150"}))
        self.assertTrue(ACK_DPS <= OBSERVED_DPS)


class SplitFrameTests(unittest.TestCase):
    def test_monitor_frame_split_without_double_delivery(self):
        parts = split_frame({"1": "x", "110": True, "4": 2, "999": 1, "131": [1], "130": "s"},
                            exchange=False)
        self.assertEqual(parts, [({"1": "x", "110": True, "4": 2}, True), ({"130": "s"}, False)])
        keys = [key for dps, _ in parts for key in dps]
        self.assertEqual(len(keys), len(set(keys)))

    def test_exchange_frame_always_ineligible(self):
        self.assertEqual(split_frame({"1": "x", "130": "v"}, exchange=True),
                         [({"1": "x"}, False), ({"130": "v"}, False)])

    def test_empty_parts_omitted_and_types_filtered(self):
        self.assertEqual(split_frame({"110": 1.5, "999": True}, exchange=False), [])
        self.assertEqual(split_frame({"145": 3}, exchange=False), [({"145": 3}, True)])
        self.assertEqual(split_frame(None, exchange=False), [])
        self.assertEqual(split_frame({1: True}, exchange=False), [])


class SettleTests(unittest.IsolatedAsyncioTestCase):
    async def test_settle_drains_actual_future_through_cancellation(self):
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = asyncio.get_running_loop().run_in_executor(executor, release.wait, 2)
            task = asyncio.ensure_future(local_source._settle(future))
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(future.done())
        finally:
            release.set()
            executor.shutdown(wait=True)

    async def test_settle_returns_result(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(5)
        self.assertEqual(await local_source._settle(future), 5)


class ContractTests(unittest.TestCase):
    def test_local_source_protocol_members(self):
        for name in ("epoch", "last_sequence", "api", "is_current", "subscribe", "flush",
                     "code_ok", "async_claim_receive", "async_enable_transport",
                     "async_wait_idle", "async_detach"):
            self.assertTrue(hasattr(LocalSource, name) or name in LocalSource.__annotations__,
                            name)


if __name__ == "__main__":
    unittest.main()
