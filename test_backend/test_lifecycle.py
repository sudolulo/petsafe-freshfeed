import asyncio
import contextlib
import io
import math
import sys
import unittest
from unittest.mock import patch

from custom_components.petsafe_freshfeed.lifecycle import LifecycleFence


class Idle:
    def __init__(self, name, calls, result=True, action=None):
        self.name, self.calls, self.result, self.action = name, calls, result, action

    async def async_wait_idle(self, timeout):
        self.calls.append((self.name, timeout))
        if self.action:
            return await self.action()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def __getattr__(self, name):
        raise AssertionError('unexpected collaborator access: ' + name)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make(self, first=True, second=True, action=None):
        calls = []
        return LifecycleFence(Idle('transport', calls, first, action), Idle('witness', calls, second)), calls

    async def test_construction_validation_and_properties(self):
        fence, calls = self.make()
        self.assertEqual(repr(fence), '<LifecycleFence>')
        self.assertFalse(fence.is_stopping)
        self.assertFalse(fence.is_unloaded)
        with self.assertRaises(AttributeError):
            fence.is_stopping = True
        class HostileInt(int):
            def __float__(self):
                raise AssertionError('conversion')
        class HostileFloat(float):
            def __float__(self):
                raise AssertionError('conversion')
        for value in (True, False, 0, -1, math.nan, math.inf, -math.inf, 10**400, HostileInt(1), HostileFloat(1), '1'):
            with self.subTest(value=type(value).__name__), self.assertRaisesRegex(ValueError, '^invalid_lifecycle_timeout$'):
                await fence.async_stop(value)
            self.assertFalse(fence.is_stopping)
        self.assertEqual(calls, [])

    async def test_finite_timeout_absolute_deadline_overflow(self):
        fence, calls = self.make()
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'time', return_value=sys.float_info.max):
            with self.assertRaisesRegex(ValueError, '^invalid_lifecycle_timeout$'):
                await fence.async_stop(sys.float_info.max)
        self.assertFalse(fence.is_stopping)
        self.assertFalse(fence.is_unloaded)
        self.assertEqual(calls, [])

    async def test_collaborator_wrong_loop_error_is_suppressed(self):
        other_loop = asyncio.new_event_loop()
        try:
            future = other_loop.create_future()
            async def wrong_loop():
                return await future
            fence, calls = self.make(action=wrong_loop)
            output = io.StringIO()
            with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                self.assertFalse(await fence.async_stop())
            self.assertEqual(output.getvalue(), '')
            self.assertTrue(fence.is_stopping)
            self.assertFalse(fence.is_unloaded)
            self.assertEqual([call[0] for call in calls], ['transport'])
        finally:
            other_loop.close()

    async def test_foreign_loop(self):
        fence, calls = self.make()
        def foreign():
            return asyncio.run(fence.async_stop())
        with self.assertRaisesRegex(RuntimeError, '^invalid_lifecycle_loop$'):
            await asyncio.to_thread(foreign)
        self.assertFalse(fence.is_stopping)
        self.assertEqual(calls, [])

    async def test_success_order_idempotence_and_validation(self):
        fence, calls = self.make()
        self.assertTrue(await fence.async_stop())
        self.assertTrue(fence.is_stopping)
        self.assertTrue(fence.is_unloaded)
        self.assertEqual([x[0] for x in calls], ['transport', 'witness'])
        self.assertGreater(calls[0][1], 0)
        self.assertLessEqual(calls[1][1], calls[0][1])
        self.assertTrue(await fence.async_stop())
        with self.assertRaises(ValueError):
            await fence.async_stop(False)
        self.assertEqual(len(calls), 2)

    async def test_failure_retry_and_strict_results(self):
        for value in (False, 1, 'yes', RuntimeError('secret')):
            fence, calls = self.make(value)
            self.assertFalse(await fence.async_stop())
            self.assertTrue(fence.is_stopping)
            self.assertFalse(fence.is_unloaded)
            self.assertEqual(len(calls), 1)
        fence, calls = self.make(second=RuntimeError('secret'))
        self.assertFalse(await fence.async_stop())
        self.assertFalse(fence.is_unloaded)
        self.assertEqual(len(calls), 2)
        attempts = 0
        async def retry():
            nonlocal attempts
            attempts += 1
            return attempts > 1
        fence, calls = self.make(action=retry)
        self.assertFalse(await fence.async_stop())
        self.assertTrue(await fence.async_stop())
        self.assertEqual([x[0] for x in calls], ['transport', 'transport', 'witness'])

    async def test_concurrent_and_latch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
            return True
        fence, calls = self.make(action=wait)
        task = asyncio.create_task(fence.async_stop())
        await entered.wait()
        self.assertTrue(fence.is_stopping)
        self.assertFalse(await fence.async_stop())
        self.assertEqual(len(calls), 1)
        release.set()
        self.assertTrue(await task)

    async def test_timeout_and_swallowed_timeout(self):
        for swallow in (False, True):
            async def wait():
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    if not swallow:
                        raise
                return True
            fence, calls = self.make(action=wait)
            self.assertFalse(await fence.async_stop(.01))
            self.assertFalse(fence.is_unloaded)
            self.assertEqual(len(calls), 1)

    async def test_external_cancellation(self):
        for mode in ('direct', 'swallow', 'exception', 'repeated'):
            entered = asyncio.Event()
            async def wait():
                entered.set()
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    if mode == 'direct':
                        raise
                    if mode == 'exception':
                        raise RuntimeError('secret')
                    return True
            fence, calls = self.make(action=wait)
            task = asyncio.create_task(fence.async_stop())
            await entered.wait()
            task.cancel()
            if mode == 'repeated':
                task.cancel()
            with self.assertRaises(asyncio.CancelledError) as raised:
                await task
            self.assertIsNone(raised.exception.__context__)
            self.assertFalse(fence.is_unloaded)
            self.assertEqual(len(calls), 1)

    async def test_shared_budget_and_retry_after_timeout(self):
        calls = []
        attempts = 0
        async def transport():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.sleep(.03)
            return True
        async def witness():
            if attempts == 1:
                await asyncio.sleep(.1)
            return True
        fence = LifecycleFence(Idle('transport', calls, action=transport), Idle('witness', calls, action=witness))
        self.assertFalse(await fence.async_stop(.06))
        self.assertLess(calls[1][1], calls[0][1] - .015)
        self.assertTrue(await fence.async_stop())

    async def test_timeout_external_cancellation_race_and_retry(self):
        entered = asyncio.Event()
        task_ref = None
        attempts = 0
        async def wait():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    task_ref.cancel()
                    return True
            return True
        fence, calls = self.make(action=wait)
        task_ref = asyncio.create_task(fence.async_stop(.01))
        await entered.wait()
        with self.assertRaises(asyncio.CancelledError):
            await task_ref
        self.assertFalse(fence.is_unloaded)
        self.assertTrue(await fence.async_stop())

    async def test_pending_cancellation_before_admission(self):
        fence, calls = self.make()
        async def run():
            asyncio.current_task().cancel()
            return await fence.async_stop()
        task = asyncio.create_task(run())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(fence.is_stopping)
        self.assertEqual(calls, [])
