"""Pure interpretation of the raw FreshFeed tuya_local entity states.

No Home Assistant import: every rule here is tested with stdlib unittest.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from .const import EVENT_KINDS

RAW_DPS: tuple[int, ...] = (4, 14, 110, 111, 125, 130, 131, 142, 145, 150)
REQUIRED_DPS = (4, 145)
COMPARTMENTS = 6

_MISSING = ("unknown", "unavailable")
_MEAL_KINDS = frozenset({"dispensed", "skipped_empty", "skipped_fault"})
_GROUP_MINUTES = 3
_WINDOW = timedelta(days=7)


def _present(state: str | None) -> bool:
    return state is not None and state not in _MISSING


def parse_int(state: str | None) -> int | None:
    """Parse an HA state string ("6", "6.0") as an int; None otherwise."""
    if not _present(state):
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")) or not value.is_integer():
        return None
    return int(value)


def _text(state: str | None) -> str | None:
    return state if _present(state) else None


@dataclass(frozen=True)
class Snapshot:
    available: bool
    status: str | None
    compartment: int | None
    meals_left: int | None
    food_level: str | None
    fault: bool | None
    feed_code: int | None
    defrost_minutes: int | None
    defrosting: bool | None
    brightness: int | None
    firmware_a: str | None
    firmware_b: str | None


def _compartment(state: str | None) -> int | None:
    value = parse_int(state)
    return value if value is not None and 1 <= value <= COMPARTMENTS else None


def snapshot(raw: Mapping[int, str | None]) -> Snapshot:
    available = all(_present(raw.get(dp)) for dp in REQUIRED_DPS)
    compartment = _compartment(raw.get(145))
    meals_left = None if compartment is None else COMPARTMENTS - compartment
    if meals_left is None:
        food_level = None
    elif meals_left >= 3:
        food_level = "high"
    elif meals_left >= 1:
        food_level = "low"
    else:
        food_level = "empty"
    if not available:
        status = "offline"
    elif meals_left == 0:
        status = "no_food"
    else:
        status = _text(raw.get(4))
    fault_code = parse_int(raw.get(150))
    defrost = parse_int(raw.get(142))
    return Snapshot(
        available=available,
        status=status,
        compartment=compartment,
        meals_left=meals_left,
        food_level=food_level,
        fault=None if fault_code is None else fault_code == 1,
        feed_code=parse_int(raw.get(14)),
        defrost_minutes=defrost,
        defrosting=None if defrost is None else defrost > 0,
        brightness=parse_int(raw.get(111)),
        firmware_a=_text(raw.get(130)),
        firmware_b=_text(raw.get(131)),
    )


@dataclass(frozen=True)
class FeedEvent:
    kind: str
    compartment: int | None
    at: datetime
    source_dp: int | None = None
    report_value: int | None = None

    def to_dict(self) -> dict:
        result = {"kind": self.kind, "compartment": self.compartment, "at": self.at.isoformat()}
        if self.source_dp is not None or self.report_value is not None:
            result.update(source_dp=self.source_dp, report_value=self.report_value)
        return result

    @classmethod
    def from_dict(cls, d) -> FeedEvent | None:
        if not isinstance(d, Mapping):
            return None
        kind = d.get("kind")
        compartment = d.get("compartment")
        at = d.get("at")
        if kind not in EVENT_KINDS or "compartment" not in d or not isinstance(at, str):
            return None
        if compartment is not None and (type(compartment) is not int):
            return None
        try:
            parsed = datetime.fromisoformat(at)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        source_dp, report_value = d.get("source_dp"), d.get("report_value")
        if any(value is not None and type(value) is not int for value in (source_dp, report_value)):
            return None
        return cls(kind, compartment, parsed, source_dp, report_value)


def classify_report(dp: int, old: int | None, new: int | None, *, at: datetime) -> FeedEvent | None:
    if type(old) is not int or type(new) is not int or old == new:
        return None
    if dp == 14 and 0 <= new <= 5:
        return FeedEvent("all_meals_report" if new == 5 else "meal_report", None, at, dp, new)
    if dp == 150 and new == 1:
        return FeedEvent("interrupted", None, at, dp, new)
    return None


def classify(dp: int, old: str | None, new: str | None, *, at: datetime) -> FeedEvent | None:
    """Classify one raw DP state change; unknown/unavailable on either side is never an event."""
    if dp == 145:
        before, after = _compartment(old), _compartment(new)
        if before is None or after is None:
            return None
        if after == before + 1:
            return FeedEvent("dispensed", after, at)
        if after == 1 and before > 1:
            return FeedEvent("refilled", 1, at)
        return None
    if dp == 14:
        before, after = parse_int(old), parse_int(new)
        if before is None or after is None or before == after:
            return None
        if after == 9:
            return FeedEvent("skipped_empty", None, at)
        if after == 10:
            return FeedEvent("skipped_fault", None, at)
    return None


def infer_meal_times(events: Iterable[FeedEvent], *, now: datetime, tz: tzinfo) -> list[time]:
    """Local meal times seen on at least two distinct local dates in the last 7 days."""
    since = now - _WINDOW
    samples: list[tuple[int, date]] = []
    for event in events:
        if event.kind not in _MEAL_KINDS or not since <= event.at <= now:
            continue
        local = event.at.astimezone(tz)
        samples.append((local.hour * 60 + local.minute, local.date()))
    samples.sort()
    groups: list[tuple[int, set[date]]] = []
    for minute, day in samples:
        if groups and minute - groups[-1][0] <= _GROUP_MINUTES:
            groups[-1][1].add(day)
        else:
            groups.append((minute, {day}))
    return [time(first // 60, first % 60) for first, days in groups if len(days) >= 2]


def meals_today(events: Iterable[FeedEvent], *, now: datetime, tz: tzinfo) -> int:
    local_now = now.astimezone(tz)
    midnight = datetime.combine(local_now.date(), time(0), tzinfo=tz)
    return sum(1 for e in events if e.kind == "dispensed" and midnight <= e.at <= now)


def last_of(events: Iterable[FeedEvent], kinds: Collection[str]) -> FeedEvent | None:
    matching = [e for e in events if e.kind in kinds]
    return max(matching, key=lambda e: e.at) if matching else None


def prune(events: Iterable[FeedEvent], *, now: datetime, days: int = 7, limit: int = 500) -> list[FeedEvent]:
    """Keep events from the last `days` days, at most `limit` newest, oldest first."""
    since = now - timedelta(days=days)
    kept = sorted((e for e in events if e.at >= since), key=lambda e: e.at)
    return kept[-limit:] if limit > 0 else []
