"""The CI coverage gate: per-package tallies, floors, and the failure it must never miss."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "coverage_gate.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coverage_gate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


def summary(lines: int, covered: int, branches: int, covered_branches: int) -> dict[str, object]:
    return {
        "summary": {
            "num_statements": lines,
            "covered_lines": covered,
            "num_branches": branches,
            "covered_branches": covered_branches,
        }
    }


REPORT = {
    "files": {
        "src/aifund/risk/stops.py": summary(10, 10, 4, 4),
        "src/aifund/risk/sizing.py": summary(10, 9, 2, 2),  # one line missed
        "src/aifund/riskless/other.py": summary(5, 0, 0, 0),  # a prefix, not a subpackage
        "src/aifund/engine/pipeline.py": summary(20, 18, 0, 0),
    }
}


def test_packages_tally_statements_and_branches_and_ignore_prefix_lookalikes() -> None:
    gate = load()
    risk = gate.tally(REPORT, "aifund.risk")
    assert (risk.covered, risk.total) == (25, 26)  # 14 + 11 of 14 + 12
    assert gate.tally(REPORT, "aifund.risk.stops").percent == 100.0


def test_a_missed_money_path_line_fails_the_gate() -> None:
    gate = load()
    failures = gate.check(REPORT, {"aifund.risk": 100.0, "aifund.engine": 90.0})
    assert failures == ["aifund.risk: 96.15% < 100%"]


def test_a_package_with_no_measured_code_fails() -> None:
    gate = load()
    assert gate.check(REPORT, {"aifund.research": 100.0}) == [
        "aifund.research: no measured code (renamed package or missing --cov?)"
    ]


@pytest.mark.parametrize(("floor", "code"), [(90, 0), (100, 1)])
def test_main_reads_floors_from_pyproject(tmp_path: Path, floor: int, code: int) -> None:
    gate = load()
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(REPORT))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[tool.aifund.coverage_floors]\n"aifund.risk" = {floor}\n')
    assert gate.main(["--report", str(report), "--pyproject", str(pyproject)]) == code
