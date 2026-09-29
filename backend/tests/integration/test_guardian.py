"""Engine side of the Guardian EA (roadmap 5.7a): the heartbeat file and the halt flag."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.null import NullNotifier
from aifund.domain.enums import EngineState, Mode
from aifund.engine.guardian import HALT_FLAG, HEARTBEAT, GuardianFiles, GuardianWatch
from aifund.engine.state import StateMachine, Trigger


async def running(factory: sessionmaker[Session], clock: FakeClock) -> StateMachine:
    sm = StateMachine("acc", Mode.DEMO, factory, clock, NullNotifier())
    await sm.fire(Trigger.START)
    await sm.fire(Trigger.STARTED)
    return sm


async def test_the_heartbeat_is_written_and_the_flag_halts(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    sm = await running(factory, clock)
    files = GuardianFiles(tmp_path / "aifund")
    watch = GuardianWatch(files, sm, clock)
    assert await watch.run_once() is None
    beat = (tmp_path / "aifund" / HEARTBEAT).read_text()
    assert beat == f"{int(clock.now().timestamp())} {clock.now().isoformat()} RUNNING\n"
    assert not (tmp_path / "aifund" / (HEARTBEAT + ".tmp")).exists()

    (tmp_path / "aifund" / HALT_FLAG).write_text("HARD_DAILY_LOSS: equity 9590.00 <= 9600.00\n")
    assert await watch.run_once() == "HARD_DAILY_LOSS: equity 9590.00 <= 9600.00"
    assert (sm.state, sm.halt_reason) == (
        EngineState.HALTED,
        "Guardian EA: HARD_DAILY_LOSS: equity 9590.00 <= 9600.00",
    )
    await watch.run_once()  # still set: no second transition
    assert sm.state is EngineState.HALTED
    assert "HALTED" in (tmp_path / "aifund" / HEARTBEAT).read_text()


async def test_an_empty_or_unreadable_flag_still_halts(tmp_path: Path) -> None:
    files = GuardianFiles(tmp_path)
    assert files.halt_flag() is None
    (tmp_path / HALT_FLAG).write_text("")
    assert files.halt_flag() == "halt flag set (no reason given)"
    (tmp_path / HALT_FLAG).unlink()
    (tmp_path / HALT_FLAG).mkdir()  # reading a directory fails
    assert (files.halt_flag() or "").startswith("unreadable halt flag")


async def test_a_stopped_engine_is_not_halted_by_the_flag(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    sm = StateMachine("acc", Mode.DEMO, factory, clock, NullNotifier())
    (tmp_path / HALT_FLAG).write_text("x")
    await GuardianWatch(GuardianFiles(tmp_path), sm, clock).run_once()
    assert sm.state is EngineState.STOPPED  # STOPPED sends nothing anyway; START re-checks the flag
