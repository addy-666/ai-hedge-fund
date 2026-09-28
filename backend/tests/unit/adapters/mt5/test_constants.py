"""Cross-check our MQL5 constant tables against the real MetaTrader5 package (Windows CI only)."""

from __future__ import annotations

import pytest

from aifund.adapters.mt5 import constants as c

mt5 = pytest.importorskip("MetaTrader5", reason="MetaTrader5 package only exists on Windows")


@pytest.mark.parametrize(("name", "value"), c.PACKAGE_CROSSCHECK)
def test_constant_matches_package(name: str, value: int) -> None:
    assert getattr(mt5, name) == value


def test_every_retcode_the_package_defines_is_named() -> None:
    package_codes = {getattr(mt5, n) for n in dir(mt5) if n.startswith("TRADE_RETCODE_")}
    assert package_codes <= set(c.RETCODES), sorted(package_codes - set(c.RETCODES))
