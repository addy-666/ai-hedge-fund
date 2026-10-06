"""Evidence records and gate E1 enforcement (docs/09 §7, roadmap R.7).

A detector may run outside SIM only with an evidence record in ``config/evidence/*.json`` that matches it
exactly: same detector id and version, same parameter hash (``params_sha256`` of the detector), a gate of
``E1_PASSED`` or later, the symbol it would trade and the profile (trigger / setup / context timeframes) it
was researched on. The research loop writes these records; a changed parameter makes the record STALE (the
hash no longer matches) and the engine refuses to start. SIM is exempt (research and replay run there), and
so is a dry run (``strategy.dry_run``: decisions are recorded, nothing is ever sent).

The record lives in ``config`` because both the research loop (which writes it) and the engine (which
checks it) read it, and those two layers may not import each other.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aifund.domain.enums import Mode, Timeframe


class EvidenceGate(StrEnum):
    E1_FAILED = "E1_FAILED"
    E1_PASSED = "E1_PASSED"
    E2_PASSED = "E2_PASSED"


RUNNABLE = {EvidenceGate.E1_PASSED, EvidenceGate.E2_PASSED}


class EvidenceError(Exception):
    """A detector would run outside SIM without matching, passing evidence (the message lists every one)."""


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detector: str = Field(min_length=1)  # playbook id (a DSL hypothesis id for LLM/operator hypotheses)
    version: str = Field(min_length=1)
    params_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate: EvidenceGate
    symbols: list[str] = Field(min_length=1)
    profile: dict[str, str]  # trigger, setup, context ("H4" or "H4,D1")
    walk_forward: dict[str, Any]
    holdout: dict[str, Any]
    trials: list[str] = Field(default_factory=list)  # trial-ledger ids this record rests on
    hypothesis: dict[str, Any] | None = None  # the DSL document, for DSL detectors
    created_at: datetime

    @property
    def filename(self) -> str:
        return f"{self.detector}_v{self.version}_{self.params_sha256[:12]}.json"


def profile_key(trigger: Timeframe, setup: Timeframe, context: Iterable[Timeframe]) -> dict[str, str]:
    return {"trigger": trigger.value, "setup": setup.value, "context": ",".join(tf.value for tf in context)}


def load_evidence(directory: Path) -> list[EvidenceRecord]:
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(p for p in directory.glob("*.json") if not p.name.startswith("g_llm_")):
        try:
            records.append(EvidenceRecord.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise EvidenceError(f"{path.name}: invalid evidence record: {exc.errors()[0]['msg']}") from exc
    return records


def write_evidence(directory: Path, record: EvidenceRecord) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / record.filename
    path.write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


class Fingerprinted(Protocol):
    """What a detector must expose to be matched against evidence."""

    playbook_id: str
    version: str

    def params_sha256(self) -> str: ...


@dataclass(frozen=True)
class Deployment:
    """One detector as the engine would run it: on this symbol, with this profile."""

    detector: Fingerprinted
    symbol: str
    profile: dict[str, str]


def problem(d: Deployment, records: Sequence[EvidenceRecord]) -> str | None:
    """Why ``d`` may not run outside SIM, or None when a matching passing record exists."""
    det = d.detector
    name = f"{det.playbook_id} v{det.version} on {d.symbol}"
    same = [r for r in records if r.detector == det.playbook_id and r.version == det.version]
    if not same:
        return f"{name}: no evidence record (research it first: scripts/research.py)"
    sha = det.params_sha256()
    matching = [r for r in same if r.params_sha256 == sha]
    if not matching:
        return f"{name}: evidence is STALE (parameters changed since research: {sha[:12]} not in records)"
    passing = [r for r in matching if r.gate in RUNNABLE]
    if not passing:
        return f"{name}: evidence gate is {matching[0].gate.value}, not E1_PASSED"
    if not any(d.symbol in r.symbols for r in passing):
        return f"{name}: researched on {passing[0].symbols}, not on {d.symbol}"
    if not any(r.profile == d.profile for r in passing):
        return f"{name}: researched with profile {passing[0].profile}, runs with {d.profile}"
    return None


def require_evidence(
    deployments: Iterable[Deployment],
    records: Sequence[EvidenceRecord],
    *,
    mode: Mode,
    dry_run: bool,
    demo_override: bool = False,
) -> None:
    """Gate E1 at startup: raise EvidenceError listing every deployment without passing evidence.
    ``demo_override`` (``engine.demo_orders_without_evidence``, roadmap 10.11) waives it in DEMO only."""
    if mode is Mode.SIM or dry_run or (demo_override and mode is Mode.DEMO):
        return
    problems = [p for d in deployments if (p := problem(d, records)) is not None]
    if problems:
        raise EvidenceError(f"gate E1 refuses to start in {mode.value}:\n- " + "\n- ".join(problems))


# ---------------------------------------------------------------- G-LLM (the analyst must beat the baseline)


class GLlmSignoff(BaseModel):
    """Operator sign-off that the analyst beat the baseline net of its cost (docs/09 §7 G-LLM), for one
    prompt version and model. Written by ``scripts/uplift_report.py --sign-off`` only when the gate passed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: Literal["G_LLM_PASSED"] = "G_LLM_PASSED"
    prompt_version: str = Field(min_length=1)  # e.g. analyst_v1
    model: str = Field(min_length=1)
    n: int = Field(ge=1)
    mean_uplift_r: float
    ci90: tuple[float, float]
    signed_off_by: str = Field(min_length=1)
    signed_off_at: datetime

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9.-]+", "_", f"{self.prompt_version}_{self.model}").strip("_")
        return f"g_llm_{safe}.json"  # model ids may hold characters Windows forbids in file names


def load_g_llm(directory: Path) -> list[GLlmSignoff]:
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("g_llm_*.json")):
        try:
            out.append(GLlmSignoff.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise EvidenceError(f"{path.name}: invalid G-LLM sign-off: {exc.errors()[0]['msg']}") from exc
    return out


def write_g_llm(directory: Path, signoff: GLlmSignoff) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / signoff.filename
    path.write_text(
        json.dumps(signoff.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def require_g_llm(
    signoffs: Sequence[GLlmSignoff],
    *,
    prompt_version: str,
    model: str,
    mode: Mode,
    analyst_orders: bool,
    demo_override: bool = False,
) -> None:
    """Outside SIM the analyst may send orders only with a sign-off for this prompt version and model
    (``demo_override``: waived in DEMO only, roadmap 10.11)."""
    if mode is Mode.SIM or not analyst_orders or (demo_override and mode is Mode.DEMO):
        return
    if not any(s.prompt_version == prompt_version and s.model == model for s in signoffs):
        raise EvidenceError(
            f"G-LLM refuses analyst orders in {mode.value}: no sign-off for {prompt_version} on {model} "
            "(keep strategy.analyst_orders off to run the analyst in shadow; scripts/uplift_report.py)"
        )
