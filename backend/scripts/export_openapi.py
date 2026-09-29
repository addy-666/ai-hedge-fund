"""Write the API's OpenAPI schema (the source of the dashboard's generated types; roadmap 6.5).

    cd backend && uv run python scripts/export_openapi.py ../frontend/src/api/openapi.json

A backend test fails when the committed schema is stale: run ``npm run gen:api`` in frontend/ after changing
the API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from aifund.api.app import create_app
from aifund.config.settings import Settings


def schema() -> dict[str, Any]:
    app = create_app(Settings(DATABASE_URL="sqlite://"), dist=Path("/nonexistent"))
    data: dict[str, Any] = app.openapi()
    return data


def render() -> str:
    return json.dumps(schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else None
    text = render()
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
