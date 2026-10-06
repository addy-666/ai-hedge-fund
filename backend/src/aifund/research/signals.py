"""Signal study (docs/09 §2): what every detector signal would have done, with the production maths.

For each closed trigger bar in the window the study applies the pipeline's DATA-ONLY pre-flight gates
(session calendar, no entries just before a long close, spread in points and against ATR), builds the same
feature snapshot, runs the detectors, and turns every candidate into a counterfactual trade:

- stops and target from ``risk.stops.plan_stops`` with the quote at the bar close (the last closed fine bar:
  bid = its close, ask = bid + its spread), the candidate's invalidation/target and the stops-timeframe ATR;
- the path from ``reconcile.virtual.simulate`` (entry at the next bar's open at the ask for a BUY, SL first,
  gaps at the open) over M1 bars where the export has them, else M5 (pessimistic: more bars touch both);
- expiry from ``reconcile.virtual.virtual_expiry`` (time stop, or the pre-close flatten);
- costs in R: commission (converted with the symbol's tick value) and slippage points, adverse at entry and
  at market exits (stop, expiry), never at a target (a limit fills at its price).

It ignores portfolio state (limits, guards, other symbols), which is the point: it measures the SIGNAL. With
``non_overlapping`` (default) a symbol's next signal is only taken once the previous one has exited, like the
one-position-per-symbol rule, which also keeps the samples from overlapping in time.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from aifund.config.trading_config import (
    PositionManagementConfig,
    ProfileConfig,
    StopsConfig,
    SymbolConfig,
    TradingConfig,
)
from aifund.domain.decision import FeatureSnapshot, FeatureValue, SetupCandidate
from aifund.domain.enums import CloseReason, Direction, Timeframe, VirtualStatus
from aifund.domain.market import SymbolSpec, Tick
from aifund.domain.values import to_decimal
from aifund.market.cross_asset import CacheKey, CrossAssetInput
from aifund.market.feature_registry import tf_prefix
from aifund.market.features import SnapshotError, build_snapshot
from aifund.market.sessions import SessionCalendar, calendars
from aifund.reconcile.pnl import R_PLACES
from aifund.reconcile.virtual import simulate, virtual_expiry
from aifund.research.history import History, Series
from aifund.risk.stops import StopError, plan_stops
from aifund.strategies.base import SetupDetector, TfRoles

FINE_TFS = (Timeframe.M1, Timeframe.M5)  # path resolution, finest first


class Skip(StrEnum):
    SESSION_CLOSED = "SESSION_CLOSED"
    NEAR_CLOSE = "NEAR_CLOSE"
    SPREAD_POINTS = "SPREAD_POINTS"
    SPREAD_ATR = "SPREAD_ATR"
    SNAPSHOT = "SNAPSHOT"  # suffixed with the snapshot's reason code
    NO_ATR = "NO_ATR"
    NO_STOP = "NO_STOP"
    FLATTEN_AT_ONCE = "FLATTEN_AT_ONCE"
    OVERLAP = "OVERLAP"
    NO_FINE_DATA = "NO_FINE_DATA"
    NO_ENTRY = "NO_ENTRY"
    OPEN_AT_END = "OPEN_AT_END"


@dataclass(frozen=True)
class CostModel:
    commission_per_lot: Decimal = Decimal(0)  # round turn, account currency
    slippage_points: int = 0  # adverse, at entry and at market exits

    def cost_price(self, spec: SymbolSpec, exit_reason: CloseReason) -> Decimal:
        """Costs of one trade as a price distance (independent of size, so it converts to R directly)."""
        commission = self.commission_per_lot * spec.tick_size / spec.tick_value
        slip = spec.point * self.slippage_points
        market_exit = slip if exit_reason is not CloseReason.TP else Decimal(0)
        return commission + slip + market_exit


@dataclass(frozen=True)
class CrossAssetSpec:
    """The other instruments a study reads for the cross-asset features: as the engine does (roadmap 10.2)."""

    timeframe: Timeframe
    bars: int
    max_age: timedelta
    others: tuple[tuple[str, str], ...]  # (slug, broker symbol) of every instrument but the studied one

    @classmethod
    def from_config(cls, cfg: TradingConfig, symbol: SymbolConfig) -> CrossAssetSpec | None:
        others = tuple((i.slug, i.broker) for i in cfg.instruments() if i.broker != symbol.broker)
        if not others:
            return None
        ca = cfg.cross_asset
        return cls(ca.timeframe, ca.bars, timedelta(minutes=ca.max_age_minutes), others)

    def input(self, history: History, subject: Series | None, close: datetime) -> CrossAssetInput:
        """The bars closed at ``close``; an instrument without history gets none (null features, as live)."""
        others = {}
        for slug, broker in self.others:
            series = history.get(broker, self.timeframe)
            others[slug] = series.closed_at(close, self.bars) if series is not None else []
        return CrossAssetInput(
            timeframe=self.timeframe, max_age=self.max_age, others=others,
            subject=subject.closed_at(close, self.bars) if subject is not None else None,
        )  # fmt: skip


@dataclass(frozen=True)
class StudySpec:
    symbol: SymbolConfig
    roles: TfRoles
    bars_per_tf: int
    stops: StopsConfig
    position_management: PositionManagementConfig
    session: SessionCalendar | None
    costs: CostModel = CostModel()
    non_overlapping: bool = True
    cross: CrossAssetSpec | None = None  # the cross-asset features (roadmap 10.2); None = none

    @classmethod
    def from_config(
        cls,
        cfg: TradingConfig,
        symbol: SymbolConfig,
        *,
        profile: ProfileConfig | None = None,
        costs: CostModel | None = None,
        non_overlapping: bool = True,
    ) -> StudySpec:
        p = profile or cfg.profiles[symbol.profile]
        return cls(
            symbol=symbol,
            roles=TfRoles(trigger=p.trigger_tf, setup=p.setup_tf, context=tuple(p.context_tfs)),
            bars_per_tf=p.bars_per_tf,
            stops=cfg.risk.stops,
            position_management=cfg.position_management,
            session=calendars(cfg.sessions).get(symbol.session) if symbol.session else None,
            costs=costs or CostModel(),
            non_overlapping=non_overlapping,
            cross=CrossAssetSpec.from_config(cfg, symbol),
        )

    @property
    def timeframes(self) -> list[Timeframe]:
        return [self.roles.trigger, self.roles.setup, *self.roles.context]


@dataclass(frozen=True)
class SignalOutcome:
    symbol: str
    bar_time: datetime  # the trigger bar the signal fired on (its OPEN time, as in the decisions table)
    entry_time: datetime
    direction: Direction
    setup_tag: str
    detector: str  # playbook_id@version
    status: VirtualStatus  # CLOSED or EXPIRED
    exit_reason: CloseReason
    exit_time: datetime
    entry_price: Decimal
    sl_distance: Decimal
    tp_distance: Decimal
    r_gross: Decimal  # spread included (bid/ask fills), before commission and slippage
    r_net: Decimal
    mae_r: Decimal | None
    mfe_r: Decimal | None
    resolution: Timeframe
    features: Mapping[str, FeatureValue] = field(repr=False)


@dataclass
class StudyResult:
    symbol: str
    start: datetime
    end: datetime
    outcomes: list[SignalOutcome] = field(default_factory=list)
    trigger_bars: int = 0
    candidates: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    first_evaluated: datetime | None = None  # first bar with a valid snapshot (after indicator warm-up)

    @property
    def r_net(self) -> list[Decimal]:
        return [o.r_net for o in self.outcomes]


def _stop_atr(snapshot: FeatureSnapshot, roles: TfRoles, stops: StopsConfig) -> Decimal | None:
    """ATR on the configured stops timeframe, falling back to the trigger's — as the pipeline does."""
    tf = roles.trigger if stops.atr_tf == "trigger" else roles.setup
    value = snapshot.features.get(f"{tf_prefix(tf)}.atr14")
    if not isinstance(value, float):
        value = snapshot.features.get(f"{tf_prefix(roles.trigger)}.atr14")
    return to_decimal(value) if isinstance(value, float) and value > 0 else None


def _quote(
    symbol: str, moment: datetime, quote_series: Series, fallback_close: Decimal, point: Decimal
) -> Tick:
    last = quote_series.last_closed(moment)
    bid = last.close if last is not None else fallback_close
    spread = last.spread_points if last is not None else 0
    return Tick(symbol=symbol, time=moment, bid=bid, ask=bid + point * spread)


def run_study(
    history: History,
    spec: StudySpec,
    detectors: Sequence[SetupDetector],
    start: datetime,
    end: datetime,
) -> StudyResult:
    sym, roles, pm = spec.symbol.broker, spec.roles, spec.position_management
    symspec = history.specs[sym]
    series = {tf: history.series(sym, tf) for tf in spec.timeframes}
    fine = [s for tf in FINE_TFS if (s := history.get(sym, tf)) is not None]
    trigger = series[roles.trigger]
    quote_series = fine[0] if fine else trigger
    span = timedelta(minutes=roles.trigger.minutes)
    gated = not spec.symbol.trade_weekends and spec.session is not None
    cache: MutableMapping[tuple[Timeframe, datetime, int], dict[str, FeatureValue]] = {}
    xa_cache: MutableMapping[CacheKey, dict[str, FeatureValue]] = {}
    cross_subject = history.get(sym, spec.cross.timeframe) if spec.cross is not None else None
    result = StudyResult(symbol=sym, start=start, end=end)
    busy_until: datetime | None = None

    for bar in trigger.opening_between(start, end - span):
        close = bar.time + span
        result.trigger_bars += 1
        if gated:
            assert spec.session is not None
            if not spec.session.is_open(close):
                result.skipped[Skip.SESSION_CLOSED] += 1
                continue
            shut = spec.session.long_close_ahead(close, timedelta(hours=pm.long_close_hours))
            if shut is not None and close >= shut - timedelta(minutes=pm.no_entries_before_close_minutes):
                result.skipped[Skip.NEAR_CLOSE] += 1
                continue
        tick = _quote(sym, close, quote_series, bar.close, symspec.point)
        spread_points = int(tick.spread / symspec.point)
        if spec.symbol.max_spread_points is not None and spread_points > spec.symbol.max_spread_points:
            result.skipped[Skip.SPREAD_POINTS] += 1
            continue
        try:
            snapshot = build_snapshot(
                symbol=sym,
                trigger_tf=roles.trigger,
                setup_tf=roles.setup,
                context_tfs=list(roles.context),
                bars={tf: s.closed_at(close, spec.bars_per_tf) for tf, s in series.items()},
                as_of=close,
                tick=tick,
                min_bars=spec.bars_per_tf,
                tf_cache=cache,
                cross_asset=None if spec.cross is None else spec.cross.input(history, cross_subject, close),
                xa_cache=xa_cache,
            )
        except SnapshotError as exc:
            result.skipped[f"{Skip.SNAPSHOT}_{exc.reason}"] += 1
            continue
        if result.first_evaluated is None:
            result.first_evaluated = close
        spread_to_atr = snapshot.features.get("ctx.spread_to_atr")
        if isinstance(spread_to_atr, float) and spread_to_atr > float(spec.symbol.max_spread_to_atr):
            result.skipped[Skip.SPREAD_ATR] += 1
            continue

        found: list[tuple[SetupDetector, SetupCandidate]] = [
            (d, c) for d in detectors for c in d.detect(snapshot)
        ]
        for detector, candidate in sorted(found, key=lambda dc: -dc[1].strength):
            result.candidates += 1
            outcome = _trade(spec, symspec, fine, snapshot, tick, detector, candidate, close, busy_until)
            if isinstance(outcome, Skip):
                result.skipped[outcome] += 1
                continue
            result.outcomes.append(outcome)
            busy_until = outcome.exit_time if spec.non_overlapping else None
    return result


def _trade(
    spec: StudySpec,
    symspec: SymbolSpec,
    fine: list[Series],
    snapshot: FeatureSnapshot,
    tick: Tick,
    detector: SetupDetector,
    candidate: SetupCandidate,
    entry_time: datetime,
    busy_until: datetime | None,
) -> SignalOutcome | Skip:
    if busy_until is not None and entry_time < busy_until:
        return Skip.OVERLAP
    atr = _stop_atr(snapshot, spec.roles, spec.stops)
    if atr is None:
        return Skip.NO_ATR
    side = candidate.direction_hint.to_side()
    try:
        stops = plan_stops(
            side=side,
            entry_ref=tick.entry_price(side),
            spread=tick.spread,
            atr=atr,
            spec=symspec,
            cfg=spec.stops,
            invalidation=candidate.key_levels.get("invalidation"),
            target=candidate.key_levels.get("target"),
        )
    except StopError:
        return Skip.NO_STOP
    expiry = virtual_expiry(
        entry_time,
        spec.roles.trigger,
        spec.position_management,
        spec.session,
        trade_weekends=spec.symbol.trade_weekends,
    )
    if expiry is None:
        return Skip.FLATTEN_AT_ONCE
    expires_at, expire_reason = expiry
    path_series = next((s for s in fine if s.first_open is not None and s.first_open <= entry_time), None)
    if path_series is None or path_series.last_close is None:
        return Skip.NO_FINE_DATA
    path = simulate(
        side=side,
        entry_time=entry_time,
        sl_distance=stops.sl_distance,
        tp_distance=stops.tp_distance,
        expires_at=expires_at,
        expire_reason=expire_reason,
        bars=path_series.opening_between(entry_time, expires_at),
        point=symspec.point,
        now=min(expires_at + timedelta(minutes=1), path_series.last_close),
    )
    if path.status in (VirtualStatus.PENDING, VirtualStatus.NO_ENTRY):
        return Skip.NO_ENTRY
    if path.status is VirtualStatus.OPEN:
        return Skip.OPEN_AT_END
    assert path.r_multiple is not None and path.exit_reason is not None  # noqa: PT018 - finished path
    assert path.exit_time is not None and path.entry_price is not None  # noqa: PT018
    cost = spec.costs.cost_price(symspec, path.exit_reason)
    r_net = (path.r_multiple - cost / stops.sl_distance).quantize(R_PLACES, rounding=ROUND_HALF_EVEN)
    return SignalOutcome(
        symbol=symspec.symbol,
        bar_time=snapshot.bar_time,
        entry_time=entry_time,
        direction=candidate.direction_hint,
        setup_tag=candidate.setup_tag,
        detector=f"{detector.playbook_id}@{detector.version}",
        status=path.status,
        exit_reason=path.exit_reason,
        exit_time=path.exit_time,
        entry_price=path.entry_price,
        sl_distance=stops.sl_distance,
        tp_distance=stops.tp_distance,
        r_gross=path.r_multiple,
        r_net=r_net,
        mae_r=path.mae_r,
        mfe_r=path.mfe_r,
        resolution=path_series.timeframe,
        features=snapshot.features,
    )
