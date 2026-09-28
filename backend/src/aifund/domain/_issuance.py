"""Issuance of executable order intents.

Only ``aifund.risk`` may import this module (enforced by import-linter, see pyproject.toml). Anything else
that constructs an ``OrderIntent`` gets an object the executor will refuse.
"""

from __future__ import annotations

from typing import Any

from aifund.domain.intent import OrderIntent


def issue_order_intent(**fields: Any) -> OrderIntent:
    intent = OrderIntent(**fields)
    intent._issued = True
    return intent
