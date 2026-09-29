"""Engine state machine (roadmap 5.1, docs/01 §9): transitions, persistence, alerts, restarts, modes."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.null import NullNotifier
from aifund.domain.enums import AccountTradeMode, EngineState, Mode
from aifund.engine.state import (
    ENTRIES,
    LEARNING,
    POSITION_MANAGEMENT,
    TRANSITIONS,
    IllegalTransition,
    StateMachine,
    Trigger,
    apply,
    mode_problems,
    restore_trigger,
)
from aifund.persistence.tables import EngineStateRow, EventRow
from aifund.ports.system import Severity

S, T = EngineState, Trigger

# the transitions of docs/01 §9, written out independently of the table under test
DIAGRAM = {
    (S.STOPPED, T.START): S.STARTING,
    (S.STARTING, T.STARTED): S.RUNNING,
    (S.STARTING, T.START_FAILED): S.STOPPED,
    (S.RUNNING, T.PAUSE): S.PAUSED,
    (S.RUNNING, T.ERROR_BUDGET): S.PAUSED,
    (S.RUNNING, T.DISCONNECTED): S.PAUSED,
    (S.PAUSED, T.RESUME): S.RUNNING,
    (S.RUNNING, T.LIMIT_BREACH): S.HALTED,
    (S.RUNNING, T.GUARDIAN_HALT): S.HALTED,
    (S.PAUSED, T.LIMIT_BREACH): S.HALTED,
    (S.PAUSED, T.GUARDIAN_HALT): S.HALTED,
    (S.HALTED, T.REARM): S.PAUSED,
    (S.RUNNING, T.FLATTEN_ALL): S.FLATTENING,
    (S.PAUSED, T.FLATTEN_ALL): S.FLATTENING,
    (S.HALTED, T.FLATTEN_ALL): S.FLATTENING,
    (S.FLATTENING, T.FLATTENED): S.HALTED,
    (S.RUNNING, T.STOP): S.STOPPED,
    (S.PAUSED, T.STOP): S.STOPPED,
}
RESTARTS = {
    (S.STARTING, T.RESTORED_PAUSED): S.PAUSED,
    (S.STARTING, T.RESTORED_HALTED): S.HALTED,
    (S.STARTING, T.RESTORED_FLATTENING): S.FLATTENING,
}
HARMLESS_REPEATS = {
    (S.PAUSED, T.PAUSE), (S.PAUSED, T.ERROR_BUDGET), (S.PAUSED, T.DISCONNECTED), (S.HALTED, T.LIMIT_BREACH),
    (S.HALTED, T.GUARDIAN_HALT), (S.FLATTENING, T.LIMIT_BREACH), (S.FLATTENING, T.GUARDIAN_HALT),
    (S.FLATTENING, T.FLATTEN_ALL),
}  # fmt: skip


@pytest.mark.parametrize(("state", "trigger"), list(itertools.product(S, T)))
def test_every_state_and_trigger(state: EngineState, trigger: Trigger) -> None:
    if (state, trigger) in DIAGRAM or (state, trigger) in RESTARTS:
        assert apply(state, trigger) is {**DIAGRAM, **RESTARTS}[(state, trigger)]
    elif (state, trigger) in HARMLESS_REPEATS:
        assert apply(state, trigger) is state
    else:
        with pytest.raises(IllegalTransition, match=f"{trigger.value} is not allowed in {state.value}"):
            apply(state, trigger)


def test_the_table_has_nothing_else() -> None:
    assert set(TRANSITIONS) == set(DIAGRAM) | set(RESTARTS) | HARMLESS_REPEATS


def test_what_each_state_allows() -> None:
    assert {S.RUNNING} == ENTRIES  # new entries only while RUNNING
    assert S.STOPPED not in POSITION_MANAGEMENT
    assert S.HALTED in POSITION_MANAGEMENT
    assert S.FLATTENING not in LEARNING


@pytest.mark.parametrize(
    ("persisted", "trigger"),
    [(S.RUNNING, T.RESTORED_PAUSED), (S.PAUSED, T.RESTORED_PAUSED), (S.STOPPED, T.RESTORED_PAUSED),
     (S.STARTING, T.RESTORED_PAUSED), (S.HALTED, T.RESTORED_HALTED), (S.FLATTENING, T.RESTORED_FLATTENING)],
)  # fmt: skip
def test_a_restart_never_lands_in_running(persisted: EngineState, trigger: Trigger) -> None:
    assert restore_trigger(persisted) is trigger
    assert apply(S.STARTING, trigger) is not S.RUNNING


@pytest.mark.parametrize(
    ("mode", "account", "allow", "confirmed", "problems"),
    [
        (Mode.SIM, None, False, False, []),
        (Mode.PAPER, AccountTradeMode.REAL, False, False, []),  # data only
        (Mode.DEMO, AccountTradeMode.DEMO, False, False, []),
        (Mode.DEMO, AccountTradeMode.REAL, False, False, ["mode DEMO needs a demo account"]),
        (Mode.LIVE, AccountTradeMode.REAL, True, True, []),
        (Mode.LIVE, AccountTradeMode.DEMO, True, True, ["mode LIVE needs a real account"]),
        (Mode.LIVE, AccountTradeMode.REAL, False, True, ["engine.allow_live"]),
        (Mode.LIVE, AccountTradeMode.REAL, True, False, ["operator confirmation"]),
    ],
)
def test_mode_rules(
    mode: Mode, account: AccountTradeMode | None, allow: bool, confirmed: bool, problems: list[str]
) -> None:
    found = mode_problems(mode, account, allow_live=allow, live_confirmed=confirmed)
    assert len(found) == len(problems)
    assert all(p in f for p, f in zip(problems, found, strict=True))


async def test_transitions_are_persisted_evented_and_alerted(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    notifier = NullNotifier()
    seen: list[tuple[EngineState, EngineState]] = []
    sm = StateMachine(
        "acc", Mode.DEMO, factory, clock, notifier, on_change=lambda a, b, _t: seen.append((a, b))
    )
    assert sm.state is S.STOPPED and not sm.entries_allowed  # noqa: PT018
    await sm.fire(T.START)
    await sm.fire(T.STARTED)
    assert sm.entries_allowed
    await sm.fire(T.LIMIT_BREACH, "DAILY_LOSS: 3.10% >= limit 3.0%")
    await sm.fire(T.GUARDIAN_HALT, "halt.flag")  # a second cause keeps the first reason
    assert (sm.state, sm.halt_reason) == (S.HALTED, "DAILY_LOSS: 3.10% >= limit 3.0%")
    await sm.fire(T.FLATTEN_ALL, "operator")
    assert sm.halt_reason == "DAILY_LOSS: 3.10% >= limit 3.0%"
    await sm.fire(T.FLATTENED)
    await sm.fire(T.REARM, "operator")
    assert (sm.state, sm.halt_reason, sm.can(T.RESUME), sm.can(T.STARTED)) == (S.PAUSED, None, True, False)
    with pytest.raises(IllegalTransition):
        await sm.fire(T.FLATTENED)

    with factory() as s:
        row = s.get(EngineStateRow, "acc")
        events = s.scalars(
            select(EventRow).where(EventRow.type == "engine.state").order_by(EventRow.seq)
        ).all()
    assert row is not None and row.state is S.PAUSED  # noqa: PT018
    assert [e.payload["to"] for e in events if e.payload] == [
        "STARTING",
        "RUNNING",
        "HALTED",
        "FLATTENING",
        "HALTED",
        "PAUSED",
    ]
    assert seen == [(S.STOPPED, S.STARTING), (S.STARTING, S.RUNNING), (S.RUNNING, S.HALTED),
                    (S.HALTED, S.FLATTENING), (S.FLATTENING, S.HALTED), (S.HALTED, S.PAUSED)]  # fmt: skip
    assert [(sev, title) for sev, title, _ in notifier.sent] == [
        (Severity.CRITICAL, "Engine HALTED"), (Severity.CRITICAL, "Engine FLATTENING"),
        (Severity.CRITICAL, "Engine HALTED"), (Severity.WARN, "Engine PAUSED"),
    ]  # fmt: skip

    again = StateMachine("acc", Mode.DEMO, factory, clock, NullNotifier())
    assert again.state is S.PAUSED  # read back from engine_state
