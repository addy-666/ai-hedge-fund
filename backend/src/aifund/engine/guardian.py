"""Engine side of the Guardian EA (roadmap 5.7a, docs/06 §4): the heartbeat it watches, the flag it raises.

The EA runs inside the MT5 terminal, independent of Python, and talks to the engine through two files in the
terminal's common data folder (``<commondata_path>\\Files\\aifund``):

- ``heartbeat.txt`` — written by the engine every cycle: ``<unix seconds> <ISO UTC> <engine state>``. When it
  goes stale the EA protects the positions itself (attaches missing stops).
- ``halt.flag`` — written by the EA when its HARD loss or drawdown limit trips (after closing every engine
  position): the reason text. While it exists the engine sends no order and moves to HALTED; REARM is refused
  until the operator deletes it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from aifund.domain.enums import EngineState
from aifund.engine.state import StateMachine, Trigger
from aifund.ports.system import ClockPort

log = structlog.get_logger(__name__)
HEARTBEAT = "heartbeat.txt"
HALT_FLAG = "halt.flag"


class GuardianFiles:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def write_heartbeat(self, clock: ClockPort, state: EngineState) -> None:
        now = clock.now()
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.directory / (HEARTBEAT + ".tmp")
        tmp.write_text(f"{int(now.timestamp())} {now.isoformat()} {state.value}\n", encoding="ascii")
        tmp.replace(self.directory / HEARTBEAT)  # atomic: the EA never reads half a line

    def halt_flag(self) -> str | None:
        """The EA's halt reason while its flag file exists (an unreadable flag still halts)."""
        path = self.directory / HALT_FLAG
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            return f"unreadable halt flag ({exc})"
        return text or "halt flag set (no reason given)"


class GuardianWatch:
    """Loop (every 5 s): beat the heartbeat file and halt the engine while the EA's flag is set."""

    def __init__(self, files: GuardianFiles, state: StateMachine, clock: ClockPort) -> None:
        self._files = files
        self._state = state
        self._clock = clock

    async def run_once(self) -> str | None:
        await asyncio.to_thread(self._files.write_heartbeat, self._clock, self._state.state)
        flag = await asyncio.to_thread(self._files.halt_flag)
        stopped = self._state.state in (EngineState.HALTED, EngineState.FLATTENING)
        if flag is not None and not stopped and self._state.can(Trigger.GUARDIAN_HALT):
            log.error("guardian.halt", reason=flag)
            await self._state.fire(Trigger.GUARDIAN_HALT, f"Guardian EA: {flag}")
        return flag
