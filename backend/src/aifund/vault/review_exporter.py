"""Weekly review export for the TRADING BRAIN vault (roadmap 7.10, docs/04 §10, docs/00 §4).

Writes ``exports/vault/ai-fund-review-YYYY-Www.md`` — a ``type: review`` note with the vault's full
frontmatter (SCHEMA.md §4) — for each finished ISO week (Monday 00:00 UTC to Monday 00:00 UTC):

- the week in R by setup and by symbol (real trades; blocked signals' virtual trades shown apart);
- the rules that changed status in the week, and the ACTIVE rulebook with its evidence;
- the auditor's lessons and a few reviewer lessons from the week's worst trades;
- wikilinks only to vault notes the engine knows exist: the strategy notes its playbook cards cite.

The engine host cannot see the Mac vault: the file waits in ``exports/vault/``, the API serves it, and
``scripts/pull_vault_reviews.py`` on the Mac copies new ones into ``wiki/reviews/`` (never ``raw/``).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import RuleStatus, TradeStatus, VirtualStatus
from aifund.persistence.db import unit_of_work
from aifund.persistence.tables import AuditRunRow, RuleRow, TradeReviewRow, TradeRow, VirtualTradeRow
from aifund.ports.system import ClockPort
from aifund.rules import dsl

NAME = re.compile(r"^ai-fund-review-(\d{4})-W(\d{2})\.md$")
WIKILINK = re.compile(r"^\[\[[^\[\]|#]+\]\]$")
CLOSED = (TradeStatus.CLOSED, TradeStatus.ORPHAN_CLOSED)
FINISHED = (VirtualStatus.CLOSED, VirtualStatus.EXPIRED)
MAX_LESSONS = 5


def file_name(year: int, week: int) -> str:
    return f"ai-fund-review-{year}-W{week:02d}.md"


def week_start(year: int, week: int) -> datetime:
    return datetime.combine(date.fromisocalendar(year, week, 1), datetime.min.time(), tzinfo=UTC)


def last_finished_week(now: datetime) -> tuple[int, int]:
    year, week, _ = (now - timedelta(days=7)).isocalendar()
    return year, week


def playbook_links(directory: Path) -> dict[str, str]:
    """setup_tag -> the wikilink of the vault strategy note its playbook card cites."""
    links = {}
    for path in sorted(directory.glob("*.yaml")):
        card = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        source = str(card.get("source", "")).strip()
        if card.get("setup_tag") and WIKILINK.match(source):
            links[str(card["setup_tag"])] = source
    return links


@dataclass
class Week:
    year: int
    week: int
    start: datetime
    end: datetime
    real: list[tuple[str, str, float]] = field(default_factory=list)  # (symbol, setup, R)
    virtual: list[tuple[str, str, float]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    active: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)


def _table(rows: Iterable[tuple[str, str, float]], key: int) -> list[str]:
    groups: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        groups[str(row[key]) or "unknown"].append(row[2])
    out = ["| | trades | win | mean R | total R |", "|---|---:|---:|---:|---:|"]
    for k, rs in sorted(groups.items()):
        win = sum(r > 0 for r in rs) / len(rs)
        out.append(f"| {k} | {len(rs)} | {win:.0%} | {sum(rs) / len(rs):+.2f} | {sum(rs):+.2f} |")
    return out


def _evidence(e: dict[str, object] | None) -> str:
    if not e or "n_matched" not in e:
        return "no evidence recorded"
    return f"n={e['n_matched']}, {float(e['mean_matched']):+.2f}R vs {float(e['mean_unmatched']):+.2f}R"  # type: ignore[arg-type]


def render(w: Week, *, links: dict[str, str], created: date) -> str:
    setups = sorted({s for _, s, _ in [*w.real, *w.virtual] if s})
    sources = sorted({links[s] for s in setups if s in links} or set(links.values()))
    front = {
        "title": f"AI Fund Weekly Review {w.year}-W{w.week:02d}",
        "type": "review",
        "validation_status": "forward_tested",
        "automation_potential": "high",
        "created": created.isoformat(),
        "updated": created.isoformat(),
        "tags": ["review/ai-fund", "system/learning-loop"],
        "aliases": [f"ai-fund-review-{w.year}-W{w.week:02d}"],
        "sources": sources,
    }
    total = sum(r for _, _, r in w.real)
    lines = [
        "---",
        yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip(),
        "---",
        "",
        f"# AI Fund Weekly Review {w.year}-W{w.week:02d}",
        "",
        f"Week {w.start:%Y-%m-%d} to {(w.end - timedelta(days=1)):%Y-%m-%d} (UTC): {len(w.real)} trades, "
        f"{total:+.2f}R. Exported by the engine; numbers come from its database.",
        "",
        "## Results by setup",
        *(_table(w.real, 1) if w.real else ["No closed trades."]),
        "",
        "## Results by symbol",
        *(_table(w.real, 0) if w.real else ["No closed trades."]),
        "",
        "## Blocked signals (virtual trades)",
        *(_table(w.virtual, 1) if w.virtual else ["None finished this week."]),
        "",
        "## Rule changes this week",
        *([f"- {c}" for c in w.changes] or ["- none"]),
        "",
        "## Active rulebook",
        *([f"- {a}" for a in w.active] or ["- no active rules"]),
        "",
        "## Lessons",
        *([f"- {x}" for x in w.lessons + w.reviews] or ["- none this week"]),
        "",
        "## Related strategy notes",
        *[f"- {s}: {links[s]}" for s in setups if s in links],
    ]
    if not any(s in links for s in setups):
        lines += [f"- {link}" for link in sorted(set(links.values()))] or ["- none"]
    return "\n".join(lines).rstrip() + "\n"


def collect(s: Session, year: int, week: int) -> Week:
    start = week_start(year, week)
    end = start + timedelta(days=7)
    w = Week(year, week, start, end)
    for t in s.scalars(
        select(TradeRow).where(
            TradeRow.status.in_(CLOSED), TradeRow.close_time >= start, TradeRow.close_time < end
        )
    ).all():
        if t.r_multiple is not None:
            w.real.append((t.symbol, t.setup_tag or "", float(t.r_multiple)))
    for v in s.scalars(
        select(VirtualTradeRow).where(
            VirtualTradeRow.status.in_(FINISHED),
            VirtualTradeRow.exit_time >= start,
            VirtualTradeRow.exit_time < end,
        )
    ).all():
        if v.r_multiple is not None:
            w.virtual.append((v.symbol, v.setup_tag or "", float(v.r_multiple)))
    for r in s.scalars(select(RuleRow).order_by(RuleRow.rule_id, RuleRow.version)).all():
        text = _describe(r)
        for when, what in (
            (r.activated_at, "activated"),
            (r.retired_at, "retired"),
            (r.shadow_started_at, "entered shadow"),
        ):
            if when is not None and start <= when < end:
                why = f" ({r.retire_reason})" if what == "retired" and r.retire_reason else ""
                w.changes.append(f"{r.rule_id} v{r.version} {what}: {text}{why}")
        if r.status is RuleStatus.ACTIVE:
            w.active.append(f"{r.rule_id} v{r.version}: {text} ({_evidence(r.evidence)})")
    for run in s.scalars(
        select(AuditRunRow).where(AuditRunRow.created_at >= start, AuditRunRow.created_at < end)
    ).all():
        findings = (run.validation or {}).get("findings", [])
        w.lessons += [f"Audit finding: {f}" for f in findings]
    worst = s.execute(
        select(TradeReviewRow.lesson, TradeRow.r_multiple)
        .join(TradeRow, TradeRow.id == TradeReviewRow.trade_id)
        .where(TradeRow.close_time >= start, TradeRow.close_time < end)
        .order_by(TradeRow.r_multiple)
        .limit(MAX_LESSONS)
    ).all()
    w.reviews = [f"Review ({float(r):+.2f}R): {lesson}" for lesson, r in worst if r is not None]
    return w


def _describe(row: RuleRow) -> str:
    try:
        rule = dsl.parse({**row.dsl, "rule_id": row.rule_id, "version": row.version})
    except dsl.RuleError:
        return "(unreadable rule)"
    return f"{rule.describe()} -> {rule.action.describe()}"


class ReviewExporter:
    def __init__(
        self, factory: sessionmaker[Session], clock: ClockPort, *, out_dir: Path, playbooks: Path
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._out = out_dir
        self._playbooks = playbooks

    def export(self, year: int, week: int) -> Path:
        with unit_of_work(self._factory) as s:
            w = collect(s, year, week)
        text = render(w, links=playbook_links(self._playbooks), created=self._clock.now().date())
        self._out.mkdir(parents=True, exist_ok=True)
        path = self._out / file_name(year, week)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return path

    def run_once(self) -> Path | None:
        """Export the last finished week once (the engine calls this hourly)."""
        year, week = last_finished_week(self._clock.now())
        if (self._out / file_name(year, week)).exists():
            return None
        return self.export(year, week)


def listing(directory: Path) -> list[str]:
    return sorted((p.name for p in directory.glob("*.md") if NAME.match(p.name)), reverse=True)
