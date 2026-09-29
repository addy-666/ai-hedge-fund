"""Weekly vault review (roadmap 7.10, docs/04 §10): the note follows the vault's SCHEMA.md (frontmatter,
wikilinks to notes that exist), is exported once per finished week, is served by the API, and the Mac script
copies only new notes into wiki/reviews (never raw/, never over an edited note)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.config.settings import PROJECT_ROOT
from aifund.domain.enums import RuleStatus
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import AuditRunRepository, RuleRepository, TradeReviewRepository
from aifund.rules import dsl
from aifund.vault import review_exporter as rx
from tests.fakes.learning import seed_outcome

PLAYBOOKS = PROJECT_ROOT / "config" / "playbooks"
VAULT = PROJECT_ROOT.parent / "TRADING BRAIN"
SCRIPT = PROJECT_ROOT / "backend" / "scripts" / "pull_vault_reviews.py"
MONDAY = datetime(2026, 9, 21, tzinfo=UTC)  # ISO 2026-W39
REQUIRED = (
    "title",
    "type",
    "validation_status",
    "automation_potential",
    "created",
    "updated",
    "tags",
    "aliases",
    "sources",
)


def frontmatter(text: str) -> dict[str, Any]:
    assert text.startswith("---\n")
    head = text.split("---\n")[1]
    data: dict[str, Any] = yaml.safe_load(head)
    return data


def seed_week(factory: sessionmaker[Session], clock: FakeClock) -> None:
    with unit_of_work(factory) as s:
        _, t1 = seed_outcome(s, when=MONDAY + timedelta(days=1), r=-1, features={"m15.rsi14": 72.0})
        seed_outcome(s, when=MONDAY + timedelta(days=2), r=2, features={"m15.rsi14": 40.0})
        seed_outcome(s, when=MONDAY + timedelta(days=2, hours=3), r=-1, features={}, virtual=True)
        seed_outcome(s, when=MONDAY - timedelta(days=3), r=5, features={})  # the week before: not in it
        TradeReviewRepository(s, clock).add(
            t1,
            tags=["CHASED_EXTENSION"],
            thesis_verdict="WRONG",
            execution_quality=2,
            lesson="Wait for the pullback.",
        )
        rule = dsl.parse(
            {
                "rule_id": "R-0001",
                "conditions": {"all": [{"feature": "h1.rsi14", "op": ">", "value": 70}]},
                "action": {"type": "penalty", "points": 15},
            }
        )
        RuleRepository(s, clock).add(
            rule_id="R-0001",
            version=1,
            status=RuleStatus.ACTIVE,
            dsl=dsl.dump(rule),
            dsl_sha256=dsl.dsl_sha256(rule),
            origin="AUDITOR",
            evidence={"n_matched": 30, "mean_matched": -0.5, "mean_unmatched": 0.1},
            activated_at=MONDAY + timedelta(days=4),
            shadow_started_at=MONDAY - timedelta(days=20),
        )
        run = AuditRunRepository(s, clock).start(
            trigger="nightly", window_from=MONDAY, window_to=MONDAY, n_trades=3, n_virtual=1
        )
        run.created_at = MONDAY + timedelta(days=3)
        AuditRunRepository(s, clock).finish(
            run.id, "DONE", validation={"results": [], "findings": ["setup X loses"]}
        )


def test_the_weekly_note_follows_the_vault_schema(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    seed_week(factory, clock)
    exporter = rx.ReviewExporter(factory, clock, out_dir=tmp_path, playbooks=PLAYBOOKS)
    path = exporter.export(2026, 39)
    text = path.read_text(encoding="utf-8")
    front = frontmatter(text)
    assert path.name == "ai-fund-review-2026-W39.md"
    assert all(k in front for k in REQUIRED)
    assert (front["type"], front["title"]) == ("review", "AI Fund Weekly Review 2026-W39")
    assert date.fromisoformat(front["created"]) == clock.now().date() == date.fromisoformat(front["updated"])
    assert front["tags"] == ["review/ai-fund", "system/learning-loop"]
    assert front["sources"] and all(rx.WIKILINK.match(s) for s in front["sources"])  # noqa: PT018
    assert "| mtf_trend_pullback | 2 | 50% | +0.50 | +1.00 |" in text  # the week's trades only
    blocked = text.split("## Blocked signals (virtual trades)")[1]
    assert "| mtf_trend_pullback | 1 | 0% | -1.00 | -1.00 |" in blocked
    assert (
        "R-0001 v1 activated: LONG/SHORT any setup on any symbol when h1.rsi14 > 70 -> -15 confidence" in text
    )
    assert "Audit finding: setup X loses" in text and "Review (-1.00R): Wait for the pullback." in text  # noqa: PT018
    assert "[[high_probability_multi_timeframe_trend_pullback]]" in text


@pytest.mark.skipif(
    not (VAULT / "SCHEMA.md").is_file(), reason="the TRADING BRAIN vault is not next to the repo"
)
def test_every_wikilink_resolves_in_the_real_vault(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    seed_week(factory, clock)
    text = (
        rx.ReviewExporter(factory, clock, out_dir=tmp_path, playbooks=PLAYBOOKS).export(2026, 39).read_text()
    )
    notes = {p.stem for p in (VAULT / "wiki").rglob("*.md")}
    links = {m[2:-2] for m in __import__("re").findall(r"\[\[[^\]]+\]\]", text)}
    assert links and links <= notes, links - notes  # noqa: PT018


def test_each_finished_week_is_exported_once(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    exporter = rx.ReviewExporter(factory, clock, out_dir=tmp_path, playbooks=PLAYBOOKS)
    assert rx.last_finished_week(clock.now()) == (2026, 39)  # 2026-09-28 is the Monday of W40
    first = exporter.run_once()
    assert first is not None and "No closed trades." in first.read_text()  # noqa: PT018
    assert exporter.run_once() is None
    assert rx.listing(tmp_path) == ["ai-fund-review-2026-W39.md"]
    assert rx.week_start(2026, 1) == datetime(2025, 12, 29, tzinfo=UTC)


def test_playbook_links_need_a_wikilink(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text('setup_tag: a\nsource: "[[note_a]]"\n', encoding="utf-8")
    (tmp_path / "b.yaml").write_text("setup_tag: b\nsource: a book\n", encoding="utf-8")
    assert rx.playbook_links(tmp_path) == {"a": "[[note_a]]"}


# ---------------------------------------------------------------- the Mac pull script


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("pull_vault_reviews", SCRIPT)
    assert spec is not None and spec.loader is not None  # noqa: PT018
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_api(files: dict[str, str], password: str = "pw") -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            ok = json.loads(request.content)["password"] == password
            return httpx.Response(200 if ok else 401, json={"csrf_token": "c"} if ok else {"detail": "no"})
        if path == "/api/auth/logout":
            return httpx.Response(204)
        if path == "/api/exports/vault":
            return httpx.Response(200, json=[*sorted(files), "../escape.md"])
        name = path.rsplit("/", 1)[-1]
        return httpx.Response(200, text=files[name]) if name in files else httpx.Response(404)

    return httpx.MockTransport(handle)


def vault(tmp_path: Path) -> Path:
    root = tmp_path / "TRADING BRAIN"
    (root / "wiki" / "reviews").mkdir(parents=True)
    (root / "SCHEMA.md").write_text("# schema\n")
    return root


def test_the_pull_script_copies_only_new_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    script = load_script()
    root = vault(tmp_path)
    (root / "wiki" / "reviews" / "ai-fund-review-2026-W38.md").write_text("edited in Obsidian")
    files = {"ai-fund-review-2026-W38.md": "server copy", "ai-fund-review-2026-W39.md": "new note"}
    monkeypatch.setenv("AIFUND_PASSWORD", "pw")
    assert script.main(["--url", "https://engine", "--vault", str(root)], transport=fake_api(files)) == 0
    assert (root / "wiki" / "reviews" / "ai-fund-review-2026-W39.md").read_text() == "new note"
    assert (root / "wiki" / "reviews" / "ai-fund-review-2026-W38.md").read_text() == "edited in Obsidian"
    assert "new: ai-fund-review-2026-W39.md" in capsys.readouterr().out
    assert not (root / "escape.md").exists()
    assert script.main(["--url", "https://engine", "--vault", str(root)], transport=fake_api(files)) == 0
    assert "nothing new" in capsys.readouterr().out


def test_the_pull_script_refuses_what_is_not_the_vault_or_a_bad_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = load_script()
    monkeypatch.setenv("AIFUND_PASSWORD", "wrong")
    assert script.main(["--url", "https://engine", "--vault", str(tmp_path)], transport=fake_api({})) == 1
    assert (
        script.main(["--url", "https://engine", "--vault", str(vault(tmp_path))], transport=fake_api({})) == 1
    )
    target = script.reviews_dir(vault(tmp_path / "ok"))
    assert target.parts[-2:] == ("wiki", "reviews")
