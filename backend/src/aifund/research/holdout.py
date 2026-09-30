"""The research holdout window and the weekly schedule (roadmap 8.7, docs/09 §3, §7).

The holdout is a FIXED window of history kept out of every walk-forward, recorded in
``<research>/holdout.json`` the first time research runs: the last ``holdout_fraction`` of the export at
that moment, ``[start, end)``. It does not drift as the export grows: data after ``end`` stays unused until
the holdout ROLLS FORWARD, which happens only when at least ``holdout_roll_days`` of new data exist. Then the
new holdout is exactly the new data ``[old end, export end)`` — bars no hypothesis has ever been judged on —
and the old holdout joins the walk-forward history. Each window is a generation; the ledger allows ONE
holdout look per hypothesis per window (``Ledger.record``), so a window is never reused for the same
hypothesis, and one that failed faces the next window with its old holdout now inside its walk-forward.

The schedule (``<research>/schedule.json``): a scheduled run is due when ``schedule_days`` have passed since
the last one AND the export changed since (its fingerprint), or on ``--force``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any


class HoldoutError(Exception):
    """The export no longer covers the recorded holdout (it was replaced or cut): research must not guess."""


@dataclass(frozen=True)
class HoldoutWindow:
    start: datetime
    end: datetime
    generation: int
    set_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> HoldoutWindow:
        return cls(
            start=datetime.fromisoformat(raw["start"]),
            end=datetime.fromisoformat(raw["end"]),
            generation=int(raw["generation"]),
            set_at=datetime.fromisoformat(raw["set_at"]),
        )


@dataclass
class HoldoutState:
    current: HoldoutWindow | None = None
    past: list[HoldoutWindow] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> HoldoutState:
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            current=HoldoutWindow.from_json(raw["current"]) if raw.get("current") else None,
            past=[HoldoutWindow.from_json(w) for w in raw.get("past", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "current": self.current.to_json() if self.current else None,
            "past": [w.to_json() for w in self.past],
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def plan(
    state: HoldoutState,
    *,
    data_start: datetime,
    data_end: datetime,
    fraction: Decimal,
    roll_days: int,
    now: datetime,
) -> tuple[HoldoutWindow, bool]:
    """The holdout for an export spanning ``[data_start, data_end)``: (window, rolled or newly set).
    Mutates ``state``; the caller saves it."""
    current = state.current
    if current is None:
        start = data_end - (data_end - data_start) * float(fraction)
        state.current = HoldoutWindow(start, data_end, 1, now)
        return state.current, True
    if data_start > current.start or data_end < current.end:
        raise HoldoutError(
            f"the export [{data_start:%Y-%m-%d}, {data_end:%Y-%m-%d}) does not cover the recorded holdout "
            f"[{current.start:%Y-%m-%d}, {current.end:%Y-%m-%d}): restore the export, or start a new "
            "research directory (the old holdout.json and ledger belong together)"
        )
    if data_end - current.end >= timedelta(days=roll_days):
        state.past.append(current)
        state.current = HoldoutWindow(current.end, data_end, current.generation + 1, now)
        return state.current, True
    return current, False


@dataclass(frozen=True)
class ScheduleState:
    last_run: datetime | None = None
    fingerprint: str | None = None

    @classmethod
    def load(cls, path: Path) -> ScheduleState:
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        last = raw.get("last_run")
        return cls(datetime.fromisoformat(last) if last else None, raw.get("fingerprint"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        last = self.last_run.isoformat() if self.last_run else None
        payload = {"last_run": last, "fingerprint": self.fingerprint}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def due(state: ScheduleState, *, fingerprint: str, now: datetime, every_days: int) -> tuple[bool, str]:
    """(run now?, why)."""
    if state.last_run is None:
        return True, "first scheduled run"
    if now - state.last_run < timedelta(days=every_days):
        return False, f"last run {state.last_run:%Y-%m-%d %H:%M} UTC, less than {every_days} days ago"
    if fingerprint == state.fingerprint:
        return False, "no new history since the last run (export it first)"
    return True, "new history"
