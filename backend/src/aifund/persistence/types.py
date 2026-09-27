"""Column types that keep money exact and time in UTC on every backend.

SQLite has no decimal type: SQLAlchemy's ``Numeric`` round-trips through binary float there, which is not
acceptable for prices, volumes and P&L. ``DecimalText`` stores the exact decimal string on SQLite and a
real ``NUMERIC`` on Postgres. Caveat on SQLite: SQL-side arithmetic/ordering on these columns needs
``CAST(col AS REAL)`` and is approximate — do money maths in Python.

``UtcDateTime`` stores fixed-width ISO-8601 UTC strings on SQLite (lexicographic order == time order) and
``timestamptz`` on Postgres; it rejects naive datetimes on the way in and returns aware UTC datetimes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, Numeric, String
from sqlalchemy.types import TypeDecorator, TypeEngine

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class DecimalText(TypeDecorator[Decimal]):
    impl = String(64)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(28, 10, asdecimal=True))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(f"DecimalText columns take Decimal, got {type(value).__name__}")
        if not value.is_finite():
            raise ValueError(f"non-finite decimal {value}")
        return value if dialect.name == "postgresql" else format(value, "f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(value)


class UtcDateTime(TypeDecorator[datetime]):
    impl = String(27)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(String(27))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UtcDateTime columns take datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime rejected: store timezone-aware UTC")
        value = value.astimezone(UTC)
        return value if dialect.name == "postgresql" else value.strftime(_TS_FORMAT)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.utcoffset() != timedelta(0) else value
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)
