"""Demand-driven, separately held cloud evidence; never a LAN report."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import time

from homeassistant.util import dt as dt_util


def create_entry_reader(hass, entry):
    """Construct without network work; restore and fetch only on explicit demand."""
    from .account import (DPRead, restore_session, read_dps, load_app_profile,
                          stored_session_shape_valid)
    from .repairs import async_cloud_auth_issue

    stored = entry.data.get("cloud_session")
    stored_route = entry.data.get("cloud_route")
    # Route validation loads the contract file, so full restoration runs in the
    # executor on first need; construction performs only the structural check.
    shape_valid = stored_session_shape_valid(stored, stored_route)
    session = None
    restore_pending = shape_valid

    async def fetch():
        nonlocal session, restore_pending
        generation = helper._generation
        if not entry.options.get("cloud_read_enabled", False):
            return DPRead("unavailable")
        if restore_pending:
            restore_pending = False
            session = await hass.async_add_executor_job(restore_session, stored, stored_route)
            if helper._closed or generation != helper._generation:
                return DPRead("unavailable")
        if session is None:
            result = DPRead("session_rejected")
        else:
            def read():
                return read_dps(session, entry.data.get("device_id"),
                                profile=load_app_profile(), deadline_s=10.0)
            result = await hass.async_add_executor_job(read)
        if helper._closed or generation != helper._generation:
            return DPRead("unavailable")
        if result.outcome == "session_rejected":
            had_session = session is not None
            session = None
            if had_session or "cloud_session" in entry.data or "cloud_route" in entry.data:
                # Suppress listener reload: rejection must leave LAN running.
                from .const import DOMAIN, TRANSITIONS_KEY
                transitions = hass.data.setdefault(DOMAIN, {}).setdefault(TRANSITIONS_KEY, set())
                transitions.add(entry.entry_id)
                try:
                    data = dict(entry.data)
                    data.pop("cloud_session", None)
                    data.pop("cloud_route", None)
                    hass.config_entries.async_update_entry(entry, data=data)
                    # Update listeners are scheduled tasks; let them see the fence.
                    await asyncio.sleep(0)
                finally:
                    transitions.discard(entry.entry_id)
                if (helper._closed or generation != helper._generation
                        or not entry.options.get("cloud_read_enabled", False)):
                    return DPRead("unavailable")
            # Every eligible rejected demand (already rate limited to one per minute)
            # asks again, so a dismissed flow can be restarted; Home Assistant
            # deduplicates an active reauth flow for the entry.
            async_cloud_auth_issue(hass, entry, True)
            entry.async_start_reauth(hass)
        return result

    from .const import DOMAIN
    budgets = hass.data.setdefault(DOMAIN, {}).setdefault("cloud_read_budgets", {})
    budget = budgets.setdefault(entry.entry_id, {})
    helper = CloudRead(fetch, budget=budget)
    helper.auth_state = "signed_in" if shape_valid else "signed_out"
    return helper


@dataclass(frozen=True)
class CloudEvidence:
    value: object
    received_at: float
    epoch: int
    generation: int
    # None means never reported locally in this epoch; zero is a real sequence.
    local_sequence: int | None
    received_wall_at: datetime


class CloudRead:
    """Coalesce one read per minute and fence results after cancellation or close.

    The fetch coroutine must itself drain its executor work on cancellation. This
    helper shields that work from callers and explicitly drains it on close.
    """

    def __init__(self, fetch, *, clock=time.monotonic, deadline=10.0, budget=None):
        self._fetch = fetch
        self._clock = clock
        self._deadline = deadline
        self._budget = budget if budget is not None else {}
        self._task = None
        self._waiters = 0
        self._task_generation = None
        self._generation = 0
        self._closed = False
        self._suspended = False
        self._epoch = None
        self.auth_state = "unknown"
        self._evidence = {}
        self._dp140_veto = False
        self.last_outcome = "unknown"
        self._listeners = []

    @property
    def in_flight(self):
        """True until the actual fetch has drained, including after caller timeout."""
        return self._task is not None

    @property
    def blocks_dispatch(self):
        """Only a still-valid read blocks writes; obsolete I/O drains separately."""
        return self._task is not None and self._task_generation == self._generation

    def subscribe(self, listener):
        """Notify consumers synchronously after valid evidence is published.

        Listeners receive the epoch and must recheck local precedence and their
        current lifecycle/write-window gates; this is not a LAN report hook.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def invalidate(self):
        """Fence old epoch/results without resetting the per-entry attempt budget."""
        self._generation += 1
        self._evidence.clear()
        self._dp140_veto = False

    def dp140_veto(self, epoch):
        """Return the cloud safety latch only for its current epoch."""
        return epoch == self._epoch and self._dp140_veto

    def evidence(self, key, *, sequences=None):
        """Return cloud evidence only when no local report supersedes it.

        Pass current epoch-scoped per-key sequences to exclude reports received
        during or after the request. Maps must contain only actually reported
        keys, never placeholders for unobserved DPs. Pre-existing local reports
        are excluded even when the caller omits the current map.
        """
        evidence = self._evidence.get(key)
        if (evidence is None or evidence.local_sequence is not None
                or (sequences is not None and key in sequences)):
            return None
        return evidence

    async def demand(self, *, epoch, sequences, allowed=True):
        """Read once using actual epoch-scoped per-key LAN report sequences.

        Omit never-reported keys. Consumers must pass their no-dispatch/no-ack
        window gate and revalidate evidence against current sequences on use.
        """
        if self._closed or self._suspended or not allowed:
            return
        if epoch != self._epoch:
            self.invalidate()
            self._epoch = epoch
        task = self._task
        if task is not None and not self.blocks_dispatch:
            return
        if task is None:
            now = self._clock()
            last_attempt = self._budget.get("last_attempt")
            if last_attempt is not None and now - last_attempt < 60:
                return
            self._budget["last_attempt"] = now
            generation = self._generation
            self._task_generation = generation
            task = asyncio.create_task(self._run(epoch, dict(sequences), generation))
            self._task = task
        self._waiters += 1
        try:
            await asyncio.wait_for(asyncio.shield(task), self._deadline)
        except TimeoutError:
            # A caller deadline fences the shared result even when another
            # coalesced caller is still waiting for the underlying I/O to drain.
            if self._task_generation == self._generation:
                self._generation += 1
        except asyncio.CancelledError:
            if self._waiters == 1 and self._task_generation == self._generation:
                self._generation += 1
            raise
        finally:
            self._waiters -= 1

    async def _run(self, epoch, sequences, generation):
        try:
            # Validate the demand's generation before entering fetch: fetch may
            # immediately delete rejected credentials or request reauth. There
            # is no yield between this fence and fetch capturing its generation.
            if self._closed or generation != self._generation:
                return
            try:
                result = await self._fetch()
            except Exception:
                if not self._closed and generation == self._generation:
                    self.last_outcome = "unavailable"
                return
            if self._closed or generation != self._generation:
                return
            self.last_outcome = (result.outcome if result.outcome in
                                 {"ok", "unavailable", "malformed", "session_rejected"}
                                 else "unavailable")
            if result.outcome == "session_rejected":
                self.auth_state = "signed_out"
            if (self._closed or generation != self._generation
                    or result.outcome not in {"ok", "malformed"}):
                return
            # A malformed DP1 must not discard independently decoded DP140
            # safety evidence. Failed envelopes carry no presence flag.
            receipt = self._clock()
            wall_receipt = dt_util.utcnow()
            if result.outcome == "ok" and result.dp1 is not None:
                self._evidence["1"] = CloudEvidence(result.dp1, receipt, epoch, generation, sequences.get("1"), wall_receipt)
            if result.dp140_present:
                self._evidence["140"] = CloudEvidence(result.dp140, receipt, epoch, generation, sequences.get("140"), wall_receipt)
                if "140" not in sequences:
                    from .quick_feed import QuickFeed, decode_quick_feed
                    value = result.dp140
                    status = value.status if type(value) is QuickFeed else decode_quick_feed(value).status
                    if status not in ("idle", "ready", "failed"):
                        self._dp140_veto = True
            for listener in tuple(self._listeners):
                listener(epoch)
        finally:
            self._task = None

    async def async_suspend(self):
        """Refuse demands and drain actual I/O, keeping fetch/listeners/auth.

        A shutdown that is later refused calls ``resume``; a successful one
        finishes with ``close``. Suspension invalidates any evidence.
        """
        self._suspended = True
        self.invalidate()
        if self._task is not None:
            await asyncio.shield(self._task)

    def resume(self):
        """Undo ``async_suspend``; a final ``close`` is never revived."""
        self._suspended = False

    async def close(self):
        self._closed = True
        self.invalidate()
        self._listeners.clear()
        if self._task is not None:
            await asyncio.shield(self._task)
        self._fetch = None
        self.auth_state = "signed_out"
