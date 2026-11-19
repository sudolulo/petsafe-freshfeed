"""Neutral local-source contract for the owned source.

No Home Assistant or Tuya Local import. A source owns exactly one
authenticated client per physical feeder and delivers decoded observations,
numbered on the owning event loop, to its subscribers. Report types, the
drain-through-cancel helper and the DP sets live here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from .observed import RAW_DPS

if TYPE_CHECKING:
    from .critical_transport import CriticalTransport, IntentWitness

# Every datapoint a source may observe: the raw entity DPS plus the schedule
# (1) and quick-feed (140) datapoints, as string IDs.
OBSERVED_DPS: frozenset[str] = frozenset(str(dp) for dp in (*RAW_DPS, 1, 140))
# Datapoints an acknowledgement predicate may judge, including the writable
# setting DPs (110, 111, 125) acknowledged by a later report of the same DP.
ACK_DPS: frozenset[str] = frozenset({"1", "4", "14", "110", "111", "125", "140", "142",
                                     "145", "150"})


@dataclass(frozen=True)
class DeviceReport:
    epoch: int
    sequence: int
    at: datetime
    dps: dict[str, bool | int | str | None]
    ack_eligible: bool = True
    received_monotonic: float = field(default_factory=monotonic)


@dataclass(frozen=True)
class DispatchMark:
    """Watermark captured at the actual send boundary, on the owning loop.

    Already decoded reports are numbered before the mark. A later sequence
    alone does not prove the device sent a frame after dispatch: even ordinary
    asynchronous pushes may have been buffered before the write.
    """

    operation_id: str
    epoch: int
    sequence: int


async def _settle(future):
    """Await an executor future/task to its ACTUAL completion.

    Cancellation is remembered, never forwarded to the underlying work, and
    re-raised only once that work has settled.
    """
    cancelled = False
    while not future.done():
        try:
            await asyncio.wait({future})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not future.cancelled():
            future.exception()  # retrieved: never an unhandled private error
        raise asyncio.CancelledError()
    return future.result()


def split_frame(dps, *, exchange: bool) -> list[tuple[dict[str, bool | int | str | None], bool]]:
    """Split one decoded frame into (ACK_DPS part, observation-only remainder).

    Observed malformed values become None in the ineligible remainder, so they
    invalidate prior observations without ever acknowledging a write. Each DP
    is delivered exactly once. Exchange frames are always ineligible.
    """
    if not isinstance(dps, dict):
        return []
    ack, rest = {}, {}
    for key, value in dps.items():
        if type(key) is not str or key not in OBSERVED_DPS:
            continue
        if type(value) not in (bool, int, str):
            rest[key] = None
        else:
            (ack if key in ACK_DPS else rest)[key] = value
    parts = []
    # Invalidate stale observations before any eligible part of the same frame.
    if rest and any(value is None for value in rest.values()):
        parts.append((rest, False))
        rest = {}
    if ack:
        parts.append((ack, not exchange))
    if rest:
        parts.append((rest, False))
    return parts


@runtime_checkable
class ClientOwner(Protocol):
    """The one owner of an authenticated client and its shared asyncio lock."""

    api: object
    lock: asyncio.Lock

    def is_current(self) -> bool: ...


class LocalSource(Protocol):
    """Observation and guarded-dispatch contract of one feeder's source."""

    epoch: int
    last_sequence: int
    api: object

    def is_current(self) -> bool: ...

    def subscribe(self, report_cb: Callable[[DeviceReport], None],
                  connection_cb: Callable[[int, bool], None]) -> Callable[[], None]: ...

    def flush(self) -> None: ...

    def code_ok(self) -> bool: ...

    async def async_claim_receive(self) -> None: ...

    async def async_enable_transport(self, witness: IntentWitness, *,
                                     on_dispatch: Callable[[DispatchMark], None]
                                     ) -> CriticalTransport: ...

    async def async_wait_idle(self, timeout: float) -> bool: ...

    async def async_detach(self) -> bool: ...
