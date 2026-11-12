"""人工事件标记：events.jsonl。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import append_jsonl, as_utc, iter_jsonl, iso_utc, parse_timestamp, utc_now


@dataclass(slots=True)
class Event:
    ts: datetime
    event: str
    note: str = ""
    elapsed_seconds: float | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "timestamp": iso_utc(self.ts),
            "elapsed_seconds": None
            if self.elapsed_seconds is None
            else round(self.elapsed_seconds, 3),
            "event": self.event,
            "note": self.note,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any], started_at: datetime | None) -> "Event | None":
        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            return None
        event = str(record.get("event") or "").strip()
        if not event:
            return None
        elapsed = record.get("elapsed_seconds")
        if elapsed is None and started_at is not None:
            elapsed = (ts - started_at).total_seconds()
        return cls(
            ts=ts,
            event=event,
            note=str(record.get("note") or ""),
            elapsed_seconds=None if elapsed is None else float(elapsed),
        )


def append_event(
    path: Path,
    event: str,
    note: str = "",
    started_at: datetime | None = None,
    ts: datetime | None = None,
) -> Event:
    ts = as_utc(ts or utc_now())
    elapsed = (ts - started_at).total_seconds() if started_at else None
    record = Event(ts=ts, event=event, note=note, elapsed_seconds=elapsed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        append_jsonl(fh, record.to_record())
    return record


def load_events(path: Path, started_at: datetime | None = None) -> list[Event]:
    events = [Event.from_record(rec, started_at) for rec in iter_jsonl(path)]
    out = [e for e in events if e is not None]
    out.sort(key=lambda e: e.ts)
    return out
