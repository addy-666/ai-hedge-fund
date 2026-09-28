"""Filling-mode selection (docs/03 §12.1). The prototype hard-coded IOC, which some symbols reject."""

from __future__ import annotations

from aifund.domain.market import FillingMode, SymbolSpec

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


def choose_filling(spec: SymbolSpec) -> FillingMode:
    """IOC if the symbol allows it, else FOK, else RETURN (exchange-execution symbols)."""
    if spec.filling_mode_flags & SYMBOL_FILLING_IOC:
        return FillingMode.IOC
    if spec.filling_mode_flags & SYMBOL_FILLING_FOK:
        return FillingMode.FOK
    return FillingMode.RETURN
