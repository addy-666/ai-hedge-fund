"""Identifier generation. ULIDs sort by creation time, which keeps logs and cursors ordered."""

from __future__ import annotations

from ulid import ULID


def new_id() -> str:
    return str(ULID())
