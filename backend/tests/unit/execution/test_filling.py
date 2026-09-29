"""Filling-mode choice (docs/03 §12.1): IOC when allowed, else FOK, else RETURN."""

from __future__ import annotations

import pytest

from aifund.domain.market import FillingMode
from aifund.execution.filling import choose_filling
from tests.unit.risk.test_stops import XAU


@pytest.mark.parametrize(
    ("flags", "mode"),
    [(3, FillingMode.IOC), (2, FillingMode.IOC), (1, FillingMode.FOK), (0, FillingMode.RETURN)],
)
def test_filling_preference(flags: int, mode: FillingMode) -> None:
    assert choose_filling(XAU.model_copy(update={"filling_mode_flags": flags})) is mode
