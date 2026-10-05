"""Cross-asset features (roadmap 10.2, docs/02 §3): the other markets, relative to the symbol being decided.

For a decision on symbol S at ``as_of``, every other instrument X of the universe (``cross_asset``
config: the references plus the traded symbols) contributes ``xa.<x>.<base>`` features computed from bars
on one timeframe (H1 by default) that had CLOSED at ``as_of`` — the same rule as every other feature
(invariant 5). The engine and the research study call the same function on the same bars, so a hypothesis
researched here behaves identically live.

Markets keep different hours (BTC trades weekends, EURUSD and NAS100 do not), so:

- an instrument whose last bar closed more than ``max_age`` before ``as_of`` is CLOSED or missing: all of
  its features are null for this decision, never a stale guess;
- the pair features (correlation, log-ratio) use bars with the same open time on both sides only;
- ``gap_ret_z`` is X's move while S was shut (S's last gap of 6 h or more within its last 24 bars), the
  weekend BTC move a Monday NAS100 decision can use.

A bar that had not closed at ``as_of`` raises ``SnapshotError(FORMING_BAR)``: that is a lookahead bug
upstream, not a market condition.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from aifund.domain.decision import FeatureValue
from aifund.domain.enums import EmaStack, ReasonCode, Timeframe
from aifund.domain.market import Bar
from aifund.market import indicators as ind
from aifund.market.feature_registry import XA_FEATURES, xa_name

VOL_WINDOW = 100  # 1-bar log returns for the volatility that scales every return z
PAIR_WINDOW = 100  # matched bars for the correlation and the log-ratio z-score
MIN_PAIRS = 60  # fewer matched returns than this: the pair features are null
GAP = timedelta(hours=6)  # a pause in S's bars at least this long is a market closure
GAP_LOOKBACK = 24  # bars of S searched for that closure
FLAT = 1e-9  # a log-ratio std below this is price rounding, not a spread to score
BASES = tuple(base for base, *_ in XA_FEATURES)


class CrossAssetError(Exception):
    """Raised as SnapshotError by the caller; kept separate so this module does not import features.py."""

    def __init__(self, reason: ReasonCode, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _closes(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([float(b.close) for b in bars])


def _vol(log_close: np.ndarray) -> float | None:
    """Std of the last VOL_WINDOW 1-bar log returns, or None with too little history or no movement."""
    if len(log_close) < VOL_WINDOW + 1:
        return None
    std = float(np.std(np.diff(log_close[-(VOL_WINDOW + 1) :]), ddof=1))
    return std if std > 0 and math.isfinite(std) else None


def _ret_z(log_close: np.ndarray, n: int, vol: float | None) -> float | None:
    if vol is None or len(log_close) < n + 1:
        return None
    return _finite((log_close[-1] - log_close[-1 - n]) / (vol * math.sqrt(n)))


def _finite(x: float) -> float | None:
    v = float(x)
    return v if math.isfinite(v) else None


def _stack_and_dist(bars: Sequence[Bar]) -> tuple[str | None, float | None]:
    c = _closes(bars)
    h = np.array([float(b.high) for b in bars])
    lo = np.array([float(b.low) for b in bars])
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    atr = float(ind.atr(h, lo, c, 14)[-1])
    stack = None
    if not any(math.isnan(float(v[-1])) for v in (e20, e50, e200)):
        if e20[-1] > e50[-1] > e200[-1]:
            stack = EmaStack.BULL.value
        elif e20[-1] < e50[-1] < e200[-1]:
            stack = EmaStack.BEAR.value
        else:
            stack = EmaStack.MIXED.value
    dist = None
    if math.isfinite(atr) and atr > 0 and not math.isnan(float(e50[-1])):
        dist = _finite((c[-1] - e50[-1]) / atr)
    return stack, dist


def _matched(subject: Sequence[Bar], other: Sequence[Bar]) -> tuple[np.ndarray, np.ndarray]:
    """Log closes of both on their common open times, the last PAIR_WINDOW + 1 of them."""
    theirs = {b.time: float(b.close) for b in other}
    pairs = [(math.log(float(b.close)), math.log(theirs[b.time])) for b in subject if b.time in theirs]
    pairs = pairs[-(PAIR_WINDOW + 1) :]
    if not pairs:
        return np.array([]), np.array([])
    s, x = zip(*pairs, strict=True)
    return np.array(s), np.array(x)


def _pair(subject: Sequence[Bar], other: Sequence[Bar]) -> tuple[float | None, float | None]:
    """(correlation of matched 1-bar log returns, z-score of the last log ratio), None with too few pairs."""
    s, x = _matched(subject, other)
    if len(s) < MIN_PAIRS + 1:
        return None, None
    rs, rx = np.diff(s), np.diff(x)
    corr = None
    if np.std(rs) > 0 and np.std(rx) > 0:
        corr = _finite(np.corrcoef(rs, rx)[0, 1])
    ratio = s - x
    std = float(np.std(ratio, ddof=1))
    z = _finite((ratio[-1] - float(np.mean(ratio))) / std) if std > FLAT else None
    return corr, z


def _close_at(bars: Sequence[Bar], span: timedelta, moment: datetime) -> float | None:
    """Close of the last bar that had closed at ``moment``."""
    found = [b for b in bars if b.time + span <= moment]
    return float(found[-1].close) if found else None


def _gap_ret_z(
    subject: Sequence[Bar], other: Sequence[Bar], span: timedelta, vol: float | None
) -> float | None:
    if vol is None:
        return None
    recent = subject[-(GAP_LOOKBACK + 1) :]
    for prev, cur in zip(reversed(recent[:-1]), reversed(recent[1:]), strict=True):
        shut, reopen = prev.time + span, cur.time
        if reopen - shut >= GAP:
            before, after = _close_at(other, span, shut), _close_at(other, span, reopen)
            if before is None or after is None:
                return None
            bars_shut = (reopen - shut) / span
            return _finite(math.log(after / before) / (vol * math.sqrt(bars_shut)))
    return None


def _check_closed(name: str, bars: Sequence[Bar], span: timedelta, as_of: datetime) -> None:
    if bars and bars[-1].time + span > as_of:
        raise CrossAssetError(ReasonCode.FORMING_BAR, f"{name}: bar {bars[-1].time} closes after {as_of}")


@dataclass(frozen=True)
class CrossAssetInput:
    """What the snapshot builder needs for the cross-asset features of one decision."""

    timeframe: Timeframe
    max_age: timedelta
    others: Mapping[str, Sequence[Bar]]  # instrument slug -> its closed bars (empty: could not be read)
    subject: Sequence[Bar] | None = None  # this symbol on ``timeframe``; None: taken from the snapshot's bars


CacheKey = tuple[str, datetime, int, datetime | None, int]


def _instrument(subject: list[Bar], bars: list[Bar], span: timedelta) -> dict[str, FeatureValue]:
    values: dict[str, FeatureValue] = dict.fromkeys(BASES)
    x_log = np.log(_closes(bars))
    vol = _vol(x_log)
    values["ret4_z"] = _ret_z(x_log, 4, vol)
    values["ret24_z"] = x24 = _ret_z(x_log, 24, vol)
    values["ema_stack"], values["dist_ema50_atr"] = _stack_and_dist(bars)
    if subject:
        values["corr100"], values["ratio_z100"] = _pair(subject, bars)
        s_log = np.log(_closes(subject))
        s24 = _ret_z(s_log, 24, _vol(s_log))
        if s24 is not None and x24 is not None:
            values["rel_ret24_z"] = s24 - x24
        values["gap_ret_z"] = _gap_ret_z(subject, bars, span, vol)
    return values


def cross_features(
    subject: Sequence[Bar] | None,
    others: Mapping[str, Sequence[Bar]],
    *,
    timeframe: Timeframe,
    as_of: datetime,
    max_age: timedelta,
    cache: MutableMapping[CacheKey, dict[str, FeatureValue]] | None = None,
) -> dict[str, FeatureValue]:
    """``xa.<slug>.*`` for every instrument in ``others`` (slug -> its closed bars on ``timeframe``, oldest
    first; empty when it could not be read). ``subject``: this symbol's closed bars on the same timeframe
    (None or empty: the pair features are null). Every slug gets every key, null when unknown.

    ``cache`` (optional, ONE per subject symbol; research uses it): an instrument's values depend only on
    its bars and the subject's, so they are reused until either gets a new bar — identical to a fresh build.
    Freshness is checked on every call."""
    span = timedelta(minutes=timeframe.minutes)
    subject = list(subject or [])
    _check_closed("subject", subject, span, as_of)
    out: dict[str, FeatureValue] = {}
    for slug, seq in others.items():
        bars = list(seq)
        _check_closed(slug, bars, span, as_of)
        values: dict[str, FeatureValue] = dict.fromkeys(BASES)
        if bars and as_of - (bars[-1].time + span) <= max_age:
            key: CacheKey = (
                slug, bars[-1].time, len(bars), subject[-1].time if subject else None, len(subject),
            )  # fmt: skip
            cached = cache.get(key) if cache is not None else None
            values = cached if cached is not None else _instrument(subject, bars, span)
            if cache is not None:
                cache[key] = values
        out.update({xa_name(slug, base): v for base, v in values.items()})
    return out
