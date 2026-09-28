"""Deterministic synthetic market data: a seeded M1 random walk aggregated into higher timeframes."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from decimal import Decimal

from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar


def random_walk_m1(symbol: str, start: datetime, minutes: int, *, seed: int = 7, price: float = 4000.0,
                   vol: float = 0.6, spread_points: int = 28) -> list[Bar]:  # fmt: skip
    rng = random.Random(seed)
    bars, close = [], price
    for i in range(minutes):
        o = close
        path = [o + rng.gauss(0, vol) for _ in range(4)]
        close = path[-1]
        hi, lo = max(o, *path), min(o, *path)
        q = Decimal("0.01")
        bars.append(Bar(symbol=symbol, timeframe=Timeframe.M1, time=start + timedelta(minutes=i),
                        open=Decimal(str(o)).quantize(q), high=Decimal(str(hi)).quantize(q),
                        low=Decimal(str(lo)).quantize(q), close=Decimal(str(close)).quantize(q),
                        tick_volume=rng.randint(5, 50), spread_points=spread_points))  # fmt: skip
    return bars


def aggregate(m1: list[Bar], tf: Timeframe) -> list[Bar]:
    """Aggregate M1 bars into ``tf`` bars aligned to multiples of the timeframe from the epoch."""
    buckets: dict[datetime, list[Bar]] = {}
    span = tf.minutes * 60
    for b in m1:
        key = datetime.fromtimestamp(int(b.time.timestamp()) // span * span, b.time.tzinfo)
        buckets.setdefault(key, []).append(b)
    return [
        Bar(symbol=grp[0].symbol, timeframe=tf, time=key, open=grp[0].open, high=max(x.high for x in grp),
            low=min(x.low for x in grp), close=grp[-1].close, tick_volume=sum(x.tick_volume for x in grp),
            spread_points=grp[-1].spread_points)
        for key, grp in sorted(buckets.items())
    ]  # fmt: skip
