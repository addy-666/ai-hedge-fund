"""Per-package branch-coverage gate (roadmap R.0).

    cd backend
    uv run pytest -q --cov --cov-branch --cov-report=json
    uv run python scripts/coverage_gate.py            # reads coverage.json

Floors live in pyproject.toml under ``[tool.aifund.coverage_floors]`` (package -> percent). The money path
(``aifund.risk``, ``aifund.execution``, ``aifund.reconcile``) is held at 100; every other floor may only go
up. Coverage counts statements and branches together, as ``coverage report`` does.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


@dataclass
class Tally:
    covered: int = 0
    total: int = 0

    @property
    def percent(self) -> float:
        return 100.0 if self.total == 0 else 100.0 * self.covered / self.total


def module_of(path: str) -> str:
    """``src/aifund/risk/stops.py`` -> ``aifund.risk.stops``."""
    parts = Path(path).with_suffix("").parts
    return ".".join(parts[parts.index("aifund") :])


def tally(report: dict[str, object], package: str) -> Tally:
    out = Tally()
    files = report["files"]
    assert isinstance(files, dict)
    for path, data in files.items():
        module = module_of(path)
        if module != package and not module.startswith(package + "."):
            continue
        s = data["summary"]
        out.covered += s["covered_lines"] + s["covered_branches"]
        out.total += s["num_statements"] + s["num_branches"]
    return out


def check(report: dict[str, object], floors: dict[str, float]) -> list[str]:
    failures = []
    for package, floor in sorted(floors.items()):
        t = tally(report, package)
        status = "ok " if t.percent + 1e-9 >= floor else "LOW"
        print(f"{status} {package:<22} {t.percent:6.2f}%  (floor {floor:g}%, {t.covered}/{t.total})")
        if t.total == 0:
            failures.append(f"{package}: no measured code (renamed package or missing --cov?)")
        elif status == "LOW":
            failures.append(f"{package}: {t.percent:.2f}% < {floor:g}%")
    return failures


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", default=str(BACKEND / "coverage.json"))
    p.add_argument("--pyproject", default=str(BACKEND / "pyproject.toml"))
    args = p.parse_args(argv)
    floors = tomllib.loads(Path(args.pyproject).read_text())["tool"]["aifund"]["coverage_floors"]
    report = json.loads(Path(args.report).read_text())
    failures = check(report, {k: float(v) for k, v in floors.items()})
    for f in failures:
        print(f"coverage gate: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
