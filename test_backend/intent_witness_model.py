"""Synthetic witness model, NOT a filesystem or production adapter.

Visible writes, durable snapshots, and explicit virgin provisioning are distinct.
Real concurrent Futures model serialization/drain, not fsync, VFS, power loss,
authoritative storage identity, or a future real adapter's implementation.
"""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
import threading


@dataclass
class PrepareStep:
    mode: str = "success"
    entered: threading.Event = field(default_factory=threading.Event)
    committed: threading.Event = field(default_factory=threading.Event)
    before_commit: threading.Event | None = None
    after_commit: threading.Event | None = None
    cancellation_seen: asyncio.Event = field(default_factory=asyncio.Event)
    future: object = None


class DurableWitnessModel:
    """Shared synthetic store: loads/writers serialize until actual I/O settles.

    ``crash_restart`` is an offline snapshot, legal only after producers settle.
    Live coordinator replacement shares this model's serialization rather than
    forking a snapshot while an older producer can still write.
    """

    def __init__(self, record=None, *, virgin=False, load_error=False):
        self.visible = deepcopy(record)
        self.durable = deepcopy(record)
        self.virgin = virgin
        self.load_error = load_error
        self.preparations = []
        self.steps = deque()
        self.history = []
        self.load_entered = asyncio.Event()
        self._serial = asyncio.Lock()
        self.active = 0
        self.max_active = 0

    def queue_step(self, mode="success", *, pause=None):
        if mode not in {"success", "error_before", "error_after", "fsync_old",
                        "fsync_new", "cancel_before", "cancel_after"}:
            raise ValueError("invalid_synthetic_mode")
        if pause not in {None, "before", "after"}:
            raise ValueError("invalid_synthetic_pause")
        step = PrepareStep(mode)
        if pause == "before":
            step.before_commit = threading.Event()
        elif pause == "after":
            step.after_commit = threading.Event()
        self.steps.append(step)
        self.history.append(step)
        return step

    def release_all(self):
        for step in self.history:
            for gate in (step.before_commit, step.after_commit):
                if gate is not None:
                    gate.set()

    async def async_load_last_intent(self):
        self.load_entered.set()
        async with self._serial:
            if self.load_error or (self.durable is None and not self.virgin):
                raise RuntimeError("synthetic_untrusted_storage")
            return deepcopy(self.durable)

    @staticmethod
    def _wait(gate):
        if gate is not None and not gate.wait(2):
            raise RuntimeError("synthetic_gate_timeout")

    def _write(self, record, step):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        step.entered.set()
        try:
            self._wait(step.before_commit)
            if step.mode == "error_before":
                raise RuntimeError("synthetic_before_commit")
            if step.mode == "cancel_before":
                raise asyncio.CancelledError()
            self.visible = dict(record)
            if step.mode != "fsync_old":
                self.durable = dict(record)
                self.virgin = False
            step.committed.set()
            self._wait(step.after_commit)
            if step.mode in {"error_after", "fsync_old", "fsync_new"}:
                raise RuntimeError("synthetic_ambiguous_commit")
            if step.mode == "cancel_after":
                raise asyncio.CancelledError()
        finally:
            self.active -= 1

    async def async_prepare_intent(self, record):
        # Lease includes actual I/O settlement, not asyncio-wrapper lifetime.
        async with self._serial:
            self.preparations.append(dict(record))
            if not self.steps:
                self.queue_step()
            step = self.steps.popleft()
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synthetic-witness")
            loop = asyncio.get_running_loop()
            completed = loop.create_future()

            def complete():
                if not completed.done():
                    completed.set_result(None)

            step.future = executor.submit(self._write, dict(record), step)
            step.future.add_done_callback(lambda future: loop.call_soon_threadsafe(complete))
            cancelled = False
            try:
                while not step.future.done():
                    try:
                        await asyncio.shield(completed)
                    except asyncio.CancelledError:
                        cancelled = True
                        step.cancellation_seen.set()
                # Consume actual completion even if cancellation takes precedence.
                try:
                    step.future.result()
                except BaseException:
                    if cancelled:
                        raise asyncio.CancelledError() from None
                    raise
                if cancelled:
                    raise asyncio.CancelledError()
            finally:
                executor.shutdown(wait=True)

    def crash_restart(self):
        if self._serial.locked() or any(step.future is not None and not step.future.done()
                                       for step in self.history):
            raise RuntimeError("synthetic_live_producer_not_a_crash_snapshot")
        return DurableWitnessModel(deepcopy(self.durable), virgin=self.virgin,
                                   load_error=self.load_error)
