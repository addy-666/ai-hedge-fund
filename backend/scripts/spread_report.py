"""Spread vs volatility per symbol from exported history — the evidence for the spread gates.

    cd backend
    uv run python scripts/spread_report.py                 # all symbols in data/history, trigger TF M15

For each symbol: spread (points) percentiles, spread / ATR(14) percentiles on the trigger timeframe, and
the share of bars each candidate ``max_spread_to_atr`` would block. Set the gates in config/trading.yaml
from this (spread measured on the last M1 bar of each trigger bar, as the replay sees it).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from aifund.config.settings import PROJECT_ROOT
from aifund.domain.enums import Timeframe
from aifund.market import indicators as ind

GATES = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--history", default=str(PROJECT_ROOT / "data" / "history"))
    p.add_argument("--timeframe", default="M15")
    args = p.parse_args(argv)
    root, tf = Path(args.history), Timeframe(args.timeframe)
    specs = json.loads((root / "specs.json").read_text())
    for sym, spec in sorted(specs.items()):
        point = float(spec["point"])
        bars = pq.read_table(root / sym / f"{tf.value}.parquet").to_pydict()
        m1 = pq.read_table(root / sym / "M1.parquet").to_pydict()
        spread_at = dict(zip([t.timestamp() for t in m1["time"]], m1["spread"], strict=True))
        atr = ind.atr(np.array(bars["high"]), np.array(bars["low"]), np.array(bars["close"]), 14)
        last_m1 = (tf.minutes - 1) * 60
        ratios, points = [], []
        for i, t in enumerate(bars["time"]):
            s = spread_at.get(t.timestamp() + last_m1)
            if s is None or np.isnan(atr[i]) or atr[i] <= 0:
                continue
            ratios.append(s * point / atr[i])
            points.append(s)
        if not ratios:
            print(f"{sym}: no overlapping M1/{tf.value} data")
            continue
        r, pts = np.array(ratios), np.array(points)
        print(
            f"{sym}  bars={len(r)}  spread points p50={np.percentile(pts, 50):.0f} "
            f"p99={np.percentile(pts, 99):.0f} max={pts.max()}"
        )
        print(
            f"      spread/ATR p50={np.percentile(r, 50):.3f} p90={np.percentile(r, 90):.3f} "
            f"p99={np.percentile(r, 99):.3f} max={r.max():.3f}"
        )
        print("      " + "  ".join(f"gate {g:.2f} blocks {100 * (r > g).mean():.1f}%" for g in GATES))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
