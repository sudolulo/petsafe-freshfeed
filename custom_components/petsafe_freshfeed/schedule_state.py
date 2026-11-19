"""Immutable cache of device-reported schedules."""
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import base64

from .schedule_codec import MealEntry, ScheduleCodecError, decode_base64, encode


@dataclass(frozen=True)
class ScheduleState:
    entries: tuple[MealEntry, ...] | None = None
    observed_at: datetime | None = None
    source_state: str = "unknown"
    error: str | None = None
    provenance: str | None = None

    @property
    def revision(self) -> str | None:
        return sha256(encode(self.entries)).hexdigest() if self.entries is not None else None

    def with_report(self, value: str | None, at: datetime) -> "ScheduleState":
        if value is None:
            return replace(self, error="invalid_schedule")
        try:
            entries = decode_base64(value)
        except ScheduleCodecError:
            return replace(self, error="invalid_schedule")
        return ScheduleState(entries, at, "current", provenance="lan")

    def with_cloud_report(self, value: str | None, at: datetime) -> "ScheduleState":
        try:
            entries = decode_base64(value)
        except (ScheduleCodecError, TypeError):
            return replace(self, error="invalid_schedule")
        return ScheduleState(entries, at, "last_known", provenance="cloud")

    def disconnected(self) -> "ScheduleState":
        return replace(self, source_state="last_known" if self.entries is not None else "unknown")

    def to_dict(self) -> dict:
        return {
            "value": base64.b64encode(encode(self.entries)).decode("ascii") if self.entries is not None else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "error": self.error,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ScheduleState":
        if not isinstance(value, dict):
            return cls()
        try:
            entries = decode_base64(value.get("value"))
            at = datetime.fromisoformat(value["observed_at"])
            if at.tzinfo is None or at.utcoffset() is None:
                return cls()
        except (ScheduleCodecError, ValueError, TypeError, KeyError):
            return cls()
        error = value.get("error")
        provenance = value.get("provenance", "lan")
        if provenance not in ("lan", "cloud"):
            return cls()
        return cls(entries, at, "last_known", error if error == "invalid_schedule" else None, provenance)
