"""Local-only coordination of trusted idle contracts.

A future coordinator must enforce the stopping latch; this object does not
prevent direct use of its collaborators or perform a Home Assistant unload.
"""

import asyncio
import math


class LifecycleFence:
    def __init__(self, transport, witness):
        self._loop = asyncio.get_running_loop()
        self._transport = transport
        self._witness = witness
        self._stopping = False
        self._unloaded = False
        self._busy = False

    def __repr__(self):
        return '<LifecycleFence>'

    @property
    def is_stopping(self):
        return self._stopping

    @property
    def is_unloaded(self):
        return self._unloaded

    async def async_stop(self, timeout=5.0):
        if type(timeout) not in (int, float):
            raise ValueError('invalid_lifecycle_timeout')
        try:
            timeout = float(timeout)
        except OverflowError:
            raise ValueError('invalid_lifecycle_timeout') from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid_lifecycle_timeout')
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            raise RuntimeError('invalid_lifecycle_loop')
        deadline = loop.time() + timeout
        if not math.isfinite(deadline):
            raise ValueError('invalid_lifecycle_timeout')
        task = asyncio.current_task()
        if task.cancelling():
            raise asyncio.CancelledError
        if self._unloaded:
            return True
        if self._busy:
            return False
        self._stopping = True
        self._busy = True
        timer = asyncio.timeout_at(deadline)

        def check_cancellation():
            if task.cancelling() > (1 if timer.expired() else 0):
                raise asyncio.CancelledError

        try:
            try:
                async with timer:
                    for collaborator in (self._transport, self._witness):
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            return False
                        try:
                            result = await collaborator.async_wait_idle(remaining)
                        except Exception:
                            result = False
                        check_cancellation()
                        if result is not True or loop.time() >= deadline:
                            return False
                    check_cancellation()
                    if loop.time() >= deadline:
                        return False
                    self._unloaded = True
                    return True
            except TimeoutError:
                check_cancellation()
                return False
        finally:
            self._busy = False
