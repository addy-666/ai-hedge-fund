"""Synthetic learning-loop datasets: outcomes in R with a planted losing condition, or pure noise."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from aifund.domain.enums import Direction
from aifund.rules.miner import Sample

START = datetime(2026, 1, 5, tzinfo=UTC)
SESSIONS = ("ASIA", "LONDON", "OVERLAP", "NY")


def dataset(
    n: int = 600,
    *,
    seed: int = 0,
    planted: bool = True,
    loss_rate: float = 0.9,
    until: float = 1.0,
    virtual_share: float = 0.0,
) -> list[Sample]:
    """``n`` outcomes, one every 6 hours. Baseline: +1.5R with p=0.45, else -1R (expectancy ~+0.13R).
    Planted: longs with m15.rsi14 > 40 in the NY session (~12% of samples) lose with ``loss_rate``, in the
    first ``until`` fraction of the timeline only (after that the pattern stops losing, as markets change)."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        rsi = float(rng.uniform(5, 95))
        session = SESSIONS[int(rng.integers(0, 4))]
        direction = Direction.LONG if rng.random() < 0.7 else Direction.SHORT
        features = {
            "m15.rsi14": round(rsi, 2),
            "h1.adx14": round(float(rng.uniform(10, 50)), 2),
            "m15.nr7": bool(rng.random() < 0.2),
            "ctx.session": session,
            "prop.llm_confidence": 70,
        }
        hit = planted and i < n * until and direction is Direction.LONG and rsi > 40 and session == "NY"
        win = rng.random() > loss_rate if hit else rng.random() < 0.45
        out.append(
            Sample(
                key=f"S{i:04d}",
                time=START + timedelta(hours=6 * i),
                symbol="XAUUSD",
                direction=direction,
                setup_tag="mtf_trend_pullback",
                trigger_tf="M15",
                r=1.5 if win else -1.0,
                virtual=bool(rng.random() < virtual_share),
                features=features,
                tags=("GOOD_TRADE_BAD_OUTCOME",) if not win and not hit else (),
            )
        )
    return out
