"""Pure codec for FreshFeed meal plans.

Weekday bits run from Sunday (bit 0) through Monday (bit 6).
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
import re


class ScheduleCodecError(ValueError):
    """An invalid schedule or schedule representation."""


_ERROR = 'Invalid meal schedule'


def _validate_fields(fields):
    for value, (minimum, maximum) in zip(fields[:3], ((1, 127), (0, 23), (0, 59))):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ScheduleCodecError(_ERROR)
    if any(type(value) is not bool for value in fields[3:]):
        raise ScheduleCodecError(_ERROR)


@dataclass(frozen=True)
class MealEntry:
    weekmask: int
    hour: int
    minute: int
    enabled: bool
    audible: bool

    def __post_init__(self):
        _validate_fields((self.weekmask, self.hour, self.minute, self.enabled, self.audible))


def decode(raw: bytes) -> tuple[MealEntry, ...]:
    if not isinstance(raw, bytes):
        raise ScheduleCodecError(_ERROR)
    if raw == b'\xff':
        return ()
    if not raw or len(raw) > 50 or len(raw) % 5:
        raise ScheduleCodecError(_ERROR)
    entries = []
    for index in range(0, len(raw), 5):
        weekmask, hour, minute, enabled, audible = raw[index:index + 5]
        if enabled not in (0, 1) or audible not in (0, 1):
            raise ScheduleCodecError(_ERROR)
        entries.append(MealEntry(weekmask, hour, minute, bool(enabled), bool(audible)))
    return tuple(entries)


def encode(entries) -> bytes:
    if isinstance(entries, (str, bytes, bytearray)):
        raise ScheduleCodecError(_ERROR)
    try:
        iterator = iter(entries)
    except TypeError:
        raise ScheduleCodecError(_ERROR) from None
    result = bytearray()
    for entry in iterator:
        if not isinstance(entry, MealEntry) or len(result) >= 50:
            raise ScheduleCodecError(_ERROR)
        fields = (entry.weekmask, entry.hour, entry.minute, entry.enabled, entry.audible)
        _validate_fields(fields)
        result.extend(fields)
    return bytes(result) if result else b'\xff'


def decode_text(value: str) -> tuple[MealEntry, ...]:
    if not isinstance(value, str):
        raise ScheduleCodecError(_ERROR)
    try:
        if len(value) % 2 == 0 and re.fullmatch(r'[0-9a-fA-F]*', value):
            raw = bytes.fromhex(value)
        else:
            raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ScheduleCodecError(_ERROR) from None
    return decode(raw)


def decode_base64(value: str) -> tuple[MealEntry, ...]:
    if not isinstance(value, str):
        raise ScheduleCodecError(_ERROR)
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ScheduleCodecError(_ERROR) from None
    if base64.b64encode(raw).decode('ascii') != value:
        raise ScheduleCodecError(_ERROR)
    return decode(raw)


def weekdays(weekmask: int) -> tuple[int, ...]:
    return tuple(day for bit, day in enumerate((7, 6, 5, 4, 3, 2, 1))
                 if weekmask & (1 << bit))


def next_occurrence(entries: tuple[MealEntry, ...], *, now: datetime, tz: tzinfo) -> datetime | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('naive_now')
    now_utc = now.astimezone(timezone.utc)
    today = now.astimezone(tz).date()
    candidates = set()
    # Include the following week when the first weekly occurrence is a DST gap.
    for offset in range(15):
        date = today + timedelta(days=offset)
        for entry in entries:
            if not entry.enabled or date.isoweekday() not in weekdays(entry.weekmask):
                continue
            for fold in (0, 1):
                local = datetime.combine(date, time(entry.hour, entry.minute), tzinfo=tz).replace(fold=fold)
                instant = local.astimezone(timezone.utc)
                if instant.astimezone(tz).replace(tzinfo=None) == local.replace(tzinfo=None) and instant > now_utc:
                    candidates.add(instant)
    return min(candidates, default=None)


def encode_base64(entries) -> str:
    return base64.b64encode(encode(entries)).decode('ascii')


def is_every_day(weekmask) -> bool:
    return weekmask == 0x7f
