"""The research holdout and schedule (roadmap 8.7): a fixed window, rolled forward only onto new data; the
weekly trigger fires only on new history and never twice within schedule_days."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aifund.research.holdout import HoldoutError, HoldoutState, ScheduleState, due, plan

START = datetime(2025, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 1, tzinfo=UTC)  # 365 days
NOW = datetime(2026, 7, 2, tzinfo=UTC)


def test_the_first_run_sets_the_holdout_and_it_then_stays_put(tmp_path: Path) -> None:
    state = HoldoutState()
    window, changed = plan(
        state, data_start=START, data_end=END, fraction=Decimal("0.25"), roll_days=90, now=NOW
    )
    assert changed
    assert (window.start, window.end, window.generation) == (END - timedelta(days=91.25), END, 1)
    # a month of new data: the holdout does NOT drift (the new bars wait, unused)
    later = END + timedelta(days=30)
    same, changed = plan(
        state, data_start=START, data_end=later, fraction=Decimal("0.25"), roll_days=90, now=NOW
    )
    assert (same, changed) == (window, False)
    path = tmp_path / "holdout.json"
    state.save(path)
    assert HoldoutState.load(path) == state
    assert HoldoutState.load(tmp_path / "missing.json") == HoldoutState()


def test_it_rolls_forward_onto_new_data_only() -> None:
    state = HoldoutState()
    first, _ = plan(state, data_start=START, data_end=END, fraction=Decimal("0.25"), roll_days=90, now=NOW)
    new_end = END + timedelta(days=90)
    second, rolled = plan(
        state, data_start=START, data_end=new_end, fraction=Decimal("0.25"), roll_days=90, now=NOW
    )
    assert rolled
    assert (second.start, second.end, second.generation) == (END, new_end, 2)  # exactly the unseen bars
    assert state.past == [first]  # the old holdout joins the walk-forward history
    assert second.start >= first.end  # never overlaps a window a hypothesis was judged on


@pytest.mark.parametrize(
    ("data_start", "data_end"),
    [(START, END - timedelta(days=1)), (START + timedelta(days=300), END)],
)
def test_an_export_that_no_longer_covers_the_holdout_is_refused(
    data_start: datetime, data_end: datetime
) -> None:
    state = HoldoutState()
    plan(state, data_start=START, data_end=END, fraction=Decimal("0.25"), roll_days=90, now=NOW)
    with pytest.raises(HoldoutError, match="does not cover the recorded holdout"):
        plan(state, data_start=data_start, data_end=data_end, fraction=Decimal("0.25"), roll_days=90, now=NOW)


def test_the_weekly_trigger(tmp_path: Path) -> None:
    assert due(ScheduleState(), fingerprint="a", now=NOW, every_days=7) == (True, "first scheduled run")
    ran = ScheduleState(NOW, "a")
    soon = due(ran, fingerprint="b", now=NOW + timedelta(days=6), every_days=7)
    assert soon == (False, "last run 2026-07-02 00:00 UTC, less than 7 days ago")
    stale = due(ran, fingerprint="a", now=NOW + timedelta(days=8), every_days=7)
    assert stale == (False, "no new history since the last run (export it first)")
    assert due(ran, fingerprint="b", now=NOW + timedelta(days=7), every_days=7) == (True, "new history")
    path = tmp_path / "schedule.json"
    ran.save(path)
    assert ScheduleState.load(path) == ran
    ScheduleState().save(path)
    assert ScheduleState.load(path) == ScheduleState()
    assert ScheduleState.load(tmp_path / "missing.json") == ScheduleState()
