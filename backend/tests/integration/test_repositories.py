from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.config.loader import load_trading_config
from aifund.domain._issuance import issue_order_intent
from aifund.domain.enums import (
    CommandStatus,
    CommandType,
    DecisionOutcome,
    Direction,
    EngineState,
    IntentKind,
    IntentStatus,
    Mode,
    Side,
    Timeframe,
)
from aifund.domain.errors import DuplicateIntentError, InvariantViolation
from aifund.domain.ids import new_id
from aifund.domain.intent import OrderIntent, intent_comment, make_idempotency_key
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.intents import IntentRepository
from aifund.persistence.repositories.system import (
    AuditLogRepository,
    CommandRepository,
    ConfigVersionRepository,
    EngineStateRepository,
    EventRepository,
    HeartbeatRepository,
)
from aifund.persistence.tables import OrderIntentRow
from aifund.ports.system import Severity

from .conftest import T0

EXAMPLE_CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"


def _intent(
    *,
    bar_offset_min: int = 0,
    direction: Direction = Direction.LONG,
    symbol: str = "XAUUSDm",
    setup_tag: str | None = None,
) -> OrderIntent:
    intent_id = new_id()
    key = make_idempotency_key(
        account=12345678,
        symbol=symbol,
        trigger_tf=Timeframe.M15,
        bar_time=T0 + timedelta(minutes=bar_offset_min),
        direction=direction,
        strategy_version="v1",
    )
    buy = direction is Direction.LONG
    fields: dict[str, Any] = dict(
        id=intent_id,
        idempotency_key=key,
        decision_id=None,
        kind=IntentKind.OPEN,
        symbol=symbol,
        side=Side.BUY if buy else Side.SELL,
        volume=Decimal("0.10"),
        price_ref=Decimal("2350.00"),
        sl=Decimal("2335.00") if buy else Decimal("2365.00"),
        tp=Decimal("2380.00") if buy else Decimal("2320.00"),
        sl_distance=Decimal("15.00"),
        tp_distance=Decimal("30.00"),
        risk_money=Decimal("150"),
        risk_pct=Decimal("0.5"),
        magic=26092801,
        comment=intent_comment(intent_id),
        setup_tag=setup_tag,
        created_at=T0,
    )
    return issue_order_intent(**fields)


# ---------------------------------------------------------------- intents


def test_intent_is_persisted_pending_with_exact_values(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    intent = _intent()
    with unit_of_work(factory) as s:
        IntentRepository(s, clock).add(intent, account_id="acc")
    with factory() as s:
        row = s.get(OrderIntentRow, intent.id)
        assert row is not None
        assert row.status is IntentStatus.PENDING
        assert row.volume == Decimal("0.10")
        assert row.sl == Decimal("2335.00")


def test_an_open_intent_carries_its_strategy_and_today_counts_per_strategy(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    """Roadmap 10.7 / 10.9: the setup tag is the open position's strategy; the strategy cap counts FILLED
    opens of that tag on that symbol since the trading day started."""
    nr7 = [_intent(bar_offset_min=15 * i, setup_tag="nr7_breakout") for i in range(3)]
    other = _intent(bar_offset_min=60, setup_tag="failure_test_2b")
    with unit_of_work(factory) as s:
        repo = IntentRepository(s, clock)
        for i, intent in enumerate([*nr7, other]):
            repo.add(intent, account_id="acc")
            if i != 2:  # the third nr7 never filled
                repo.transition(intent.id, IntentStatus.SENT)
                repo.transition(intent.id, IntentStatus.FILLED, position_id=900 + i)
    with factory() as s:
        repo = IntentRepository(s, clock)
        assert repo.opens_today("XAUUSDm", "nr7_breakout", T0) == 2
        assert repo.opens_today("XAUUSDm", "nr7_breakout", T0 + timedelta(minutes=1)) == 0  # not today
        assert repo.opens_today("XAUUSDm", "failure_test_2b", T0) == 1
        assert repo.opens_today("BTCUSD", "nr7_breakout", T0) == 0
        assert {pid: r.setup_tag for pid, r in repo.filled_opens([900, 901, 903]).items()} == {
            900: "nr7_breakout", 901: "nr7_breakout", 903: "failure_test_2b",
        }  # fmt: skip


def test_recent_directions_can_be_one_familys(factory: sessionmaker[Session], clock: FakeClock) -> None:
    """Roadmap 10.8: with family slots the flip-flop lock reads only the family's own proposals."""
    with unit_of_work(factory) as s:
        repo = DecisionRepository(s, clock)
        for i, (direction, tag) in enumerate([("LONG", "a"), ("SHORT", "b"), ("SHORT", "a"), ("NONE", "a"),
                                               ("LONG", "b"), ("LONG", "a")]):  # fmt: skip
            repo.add(account_id="acc", symbol="X", trigger_tf="M15", bar_time=T0 + timedelta(minutes=15 * i),
                     stage_reached="DECISION", outcome=DecisionOutcome.SHADOW,
                     proposal={"direction": direction, "setup_tag": tag})  # fmt: skip
    with factory() as s:
        repo = DecisionRepository(s, clock)
        everything = repo.recent_directions("X", "M15")
        assert [d.value for d in everything] == ["LONG", "SHORT", "SHORT", "LONG", "LONG"]
        family = repo.recent_directions("X", "M15", setup_tags=["a"])
        assert [d.value for d in family] == ["LONG", "SHORT", "LONG"]
        assert [d.value for d in repo.recent_directions("X", "M15", limit=1, setup_tags=["b"])] == ["LONG"]


def test_duplicate_idempotency_key_raises_domain_error_and_keeps_transaction_usable(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    first = _intent()
    duplicate = issue_order_intent(
        **{**first.model_dump(), "id": (other := new_id()), "comment": intent_comment(other)}
    )
    with unit_of_work(factory) as s:
        repo = IntentRepository(s, clock)
        repo.add(first, account_id="acc")
        with pytest.raises(DuplicateIntentError) as exc:
            repo.add(duplicate, account_id="acc")
        assert exc.value.idempotency_key == first.idempotency_key
        repo.add(
            _intent(bar_offset_min=15), account_id="acc"
        )  # session still usable after the savepoint rollback
    with factory() as s:
        assert len(IntentRepository(s, clock).non_terminal()) == 2


def test_duplicate_is_detected_across_sessions_as_after_a_restart(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    intent = _intent()
    with unit_of_work(factory) as s:
        IntentRepository(s, clock).add(intent, account_id="acc")
    clone = issue_order_intent(**{**intent.model_dump(), "id": (i := new_id()), "comment": intent_comment(i)})
    with pytest.raises(DuplicateIntentError), unit_of_work(factory) as s:
        IntentRepository(s, clock).add(clone, account_id="acc")


def test_legal_and_illegal_transitions(factory: sessionmaker[Session], clock: FakeClock) -> None:
    intent = _intent()
    with unit_of_work(factory) as s:
        repo = IntentRepository(s, clock)
        repo.add(intent, account_id="acc")
        with pytest.raises(InvariantViolation, match="PENDING -> FILLED"):
            repo.transition(intent.id, IntentStatus.FILLED)
        repo.transition(intent.id, IntentStatus.SENT, sent_at=clock.now(), attempts=1)
        repo.transition(intent.id, IntentStatus.UNKNOWN)
        clock.advance(30)
        row = repo.transition(
            intent.id, IntentStatus.FILLED, position_id=987654, fill_price=Decimal("2350.12")
        )
        assert row.resolved_at == clock.now()
        with pytest.raises(InvariantViolation, match="FILLED -> REJECTED"):
            repo.transition(intent.id, IntentStatus.REJECTED)
        other = _intent(bar_offset_min=15)
        repo.add(other, account_id="acc")
        with pytest.raises(InvariantViolation, match="not updatable"):
            repo.transition(other.id, IntentStatus.SENT, volume=Decimal(100))


def test_non_terminal_intents_lock_only_their_symbol(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    gold, eur = _intent(), _intent(symbol="EURUSDm")
    with unit_of_work(factory) as s:
        repo = IntentRepository(s, clock)
        repo.add(gold, account_id="acc")
        repo.add(eur, account_id="acc")
        repo.transition(eur.id, IntentStatus.CHECK_FAILED)
        assert [r.id for r in repo.non_terminal("XAUUSDm")] == [gold.id]
        assert repo.non_terminal("EURUSDm") == []


# ---------------------------------------------------------------- control tables


def test_engine_state_lifecycle(factory: sessionmaker[Session], clock: FakeClock) -> None:
    with unit_of_work(factory) as s:
        repo = EngineStateRepository(s, clock)
        row = repo.get_or_create("acc", Mode.DEMO)
        assert row.state is EngineState.STOPPED
        assert repo.get_or_create("acc", Mode.SIM).mode is Mode.DEMO  # existing row wins
        clock.advance(5)
        halted = repo.set_state("acc", EngineState.HALTED, halt_reason="daily loss 3.1%")
        assert halted.updated_at == clock.now()
        with pytest.raises(InvariantViolation):
            repo.set_state("nope", EngineState.RUNNING)


def test_config_versions_dedupe_unchanged_files(factory: sessionmaker[Session], clock: FakeClock) -> None:
    loaded = load_trading_config(EXAMPLE_CONFIG)
    with unit_of_work(factory) as s:
        repo = ConfigVersionRepository(s, clock)
        first = repo.record(loaded, created_by="engine")
        assert repo.record(loaded, created_by="engine").id == first.id
        latest = repo.latest()
        assert latest is not None
        assert latest.sha256 == loaded.sha256


def test_commands_are_claimed_oldest_first_and_finished(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    with unit_of_work(factory) as s:
        repo = CommandRepository(s, clock)
        pause = repo.enqueue(CommandType.PAUSE, None, requested_by="op")
        clock.advance(1)
        flatten = repo.enqueue(CommandType.FLATTEN_ALL, None, requested_by="op")
        claimed = repo.claim_next()
        assert claimed is not None
        assert claimed.id == pause.id
        assert claimed.status is CommandStatus.RUNNING
        repo.finish(pause.id, ok=True, result={"state": "PAUSED"})
        with pytest.raises(InvariantViolation):
            repo.finish(pause.id, ok=True)
        nxt = repo.claim_next()
        assert nxt is not None
        assert nxt.id == flatten.id
        assert repo.claim_next() is None


def test_events_tail_by_sequence(factory: sessionmaker[Session], clock: FakeClock) -> None:
    with unit_of_work(factory) as s:
        repo = EventRepository(s, clock)
        seqs = [repo.append("trade.opened", Severity.INFO, {"n": n}) for n in range(5)]
        assert seqs == sorted(seqs)
        assert [e.payload for e in repo.since(seqs[2])] == [{"n": 3}, {"n": 4}]
        assert len(repo.since(0, limit=2)) == 2


def test_heartbeats_and_audit_log(factory: sessionmaker[Session], clock: FakeClock) -> None:
    with unit_of_work(factory) as s:
        hb = HeartbeatRepository(s, clock)
        hb.beat("engine")
        clock.advance(10)
        hb.beat("engine", detail={"loop": "reconciler"})
        beats = hb.all()
        assert len(beats) == 1
        assert beats[0].last_beat_at == clock.now()
        row = AuditLogRepository(s, clock).record(
            actor="operator", action="config.update", before={"risk": "0.5"}, after={"risk": "0.75"}
        )
        assert row.ts == clock.now()


def test_unit_of_work_rolls_back_on_error(factory: sessionmaker[Session], clock: FakeClock) -> None:
    with pytest.raises(RuntimeError), unit_of_work(factory) as s:  # noqa: PT012 - work then fail inside the UoW
        EventRepository(s, clock).append("x", Severity.INFO)
        raise RuntimeError("boom")
    with factory() as s:
        assert EventRepository(s, clock).since(0) == []
