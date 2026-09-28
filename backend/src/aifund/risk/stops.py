"""Stop-loss / take-profit construction from ATR (docs/03 §10). Pure Decimal arithmetic.

- The stop distance comes from the setup's invalidation level (plus a buffer and the spread) when that
  level is on the right side of entry, else ``k_sl_default`` ATR; it is clamped to
  [``k_sl_min``, ``k_sl_max``] ATR and never inside the broker's minimum stop distance.
- The target is the setup's target when its reward/risk lies inside [``rr_min``, ``rr_max``]; otherwise
  the reward/risk is clamped into that band (``rr_default`` when there is no usable target).
- Levels are rounded to the tick: the SL AWAY from entry (never a tighter stop than planned) and the TP
  TOWARD entry (never a more ambitious target) — unless that would drop the reward/risk below ``rr_min``,
  in which case the TP rounds away so the floor holds. Distances are then re-derived from the rounded
  levels, so they always equal what the order carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from aifund.config.trading_config import StopsConfig
from aifund.domain.enums import Side
from aifund.domain.market import SymbolSpec
from aifund.domain.values import Rounding, quantize_to_step


class StopError(ValueError):
    """No valid stop/target can be built from these inputs (the caller rejects the trade)."""


@dataclass(frozen=True)
class StopPlan:
    side: Side
    entry_ref: Decimal
    sl: Decimal
    tp: Decimal
    sl_distance: Decimal
    tp_distance: Decimal
    atr: Decimal
    sl_atr_multiple: Decimal
    rr: Decimal
    used_invalidation: bool
    used_target: bool
    clamped: str  # "", "min_atr", "max_atr" or "broker_min"


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(value, high))


def plan_stops(
    *,
    side: Side,
    entry_ref: Decimal,
    spread: Decimal,
    atr: Decimal,
    spec: SymbolSpec,
    cfg: StopsConfig,
    invalidation: Decimal | None = None,
    target: Decimal | None = None,
) -> StopPlan:
    if atr <= 0:
        raise StopError(f"ATR must be positive, got {atr}")
    if entry_ref <= 0 or spread < 0:
        raise StopError("invalid entry or spread")
    sign = 1 if side is Side.BUY else -1

    # --- stop distance
    used_invalidation = invalidation is not None and sign * (entry_ref - invalidation) > 0
    if used_invalidation:
        assert invalidation is not None
        raw = abs(entry_ref - invalidation) + cfg.invalidation_buffer_atr * atr + spread
    else:
        raw = cfg.k_sl_default * atr
    low, high = cfg.k_sl_min * atr, cfg.k_sl_max * atr
    sl_dist = _clamp(raw, low, high)
    clamped = "min_atr" if raw < low else "max_atr" if raw > high else ""
    broker_min = (spec.stops_level_points + 2) * spec.point + spread
    if sl_dist < broker_min:
        sl_dist, clamped = broker_min, "broker_min"

    # --- stop level, rounded away from entry
    sl_raw = entry_ref - sign * sl_dist
    sl = quantize_to_step(sl_raw, spec.tick_size, Rounding.DOWN if side is Side.BUY else Rounding.UP)
    if sl <= 0:
        raise StopError(f"stop {sl} is not a valid price")
    sl_distance = abs(entry_ref - sl)

    # --- target
    used_target = False
    rr: Decimal
    if target is not None and sign * (target - entry_ref) > 0:
        rr_raw = abs(target - entry_ref) / sl_distance
        if cfg.rr_min <= rr_raw <= cfg.rr_max:
            tp_raw, rr, used_target = target, rr_raw, True
        else:
            rr = _clamp(rr_raw, cfg.rr_min, cfg.rr_max)
            tp_raw = entry_ref + sign * rr * sl_distance
    else:
        rr = cfg.rr_default
        tp_raw = entry_ref + sign * rr * sl_distance
    toward, away = (Rounding.DOWN, Rounding.UP) if side is Side.BUY else (Rounding.UP, Rounding.DOWN)
    tp = quantize_to_step(tp_raw, spec.tick_size, toward)
    if abs(tp - entry_ref) < cfg.rr_min * sl_distance:
        tp = quantize_to_step(tp_raw, spec.tick_size, away)  # the RR floor must hold after rounding
    tp_distance = abs(tp - entry_ref)
    if tp_distance <= 0 or tp <= 0:
        raise StopError("target collapses onto entry after rounding")

    return StopPlan(
        side=side,
        entry_ref=entry_ref,
        sl=sl,
        tp=tp,
        sl_distance=sl_distance,
        tp_distance=tp_distance,
        atr=atr,
        sl_atr_multiple=sl_distance / atr,
        rr=tp_distance / sl_distance,
        used_invalidation=used_invalidation,
        used_target=used_target,
        clamped=clamped,
    )
