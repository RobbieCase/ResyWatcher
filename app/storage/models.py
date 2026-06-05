"""Domain models for watches and seen-alerts tracking."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import date as Date, datetime, timezone
from typing import Any


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Watch:
    """A user's request to be alerted when a venue has matching slots."""
    id: str
    user_id: int             # Discord user ID who created it
    channel_id: int          # Discord channel to alert in
    venue_id: int
    venue_name: str          # cached for display
    venue_slug: str | None   # for booking deep link
    party_size: int
    date_start: str          # YYYY-MM-DD inclusive
    date_end: str            # YYYY-MM-DD inclusive
    time_start: str          # HH:MM inclusive (e.g. "19:00")
    time_end: str            # HH:MM inclusive (e.g. "21:30")
    days_of_week: list[int]  # 0=Mon, 6=Sun. Empty list = all days.
    poll_seconds: int
    paused: bool = False
    created_at: str = field(default_factory=_now_iso)
    last_polled_at: str | None = None
    last_alerted_at: str | None = None

    @staticmethod
    def make(**kwargs: Any) -> "Watch":
        return Watch(id=_new_id(), **kwargs)

    def matches_day(self, day: Date) -> bool:
        if self.days_of_week and day.weekday() not in self.days_of_week:
            return False
        return self.date_start <= day.isoformat() <= self.date_end

    def matches_time(self, hhmm: str) -> bool:
        if not hhmm:
            return False
        return self.time_start <= hhmm <= self.time_end

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Watch":
        return cls(**d)


@dataclass
class SeenSlot:
    """Tracks a slot we've already alerted on, to avoid spam."""
    watch_id: str
    slot_token: str
    alerted_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SeenSlot":
        return cls(**d)
