"""Copy new weekly review notes from the engine into the TRADING BRAIN vault (roadmap 7.10, docs/04 §10).

Runs on the Mac, which holds the vault; the engine host serves the notes over the tailnet:

    cd backend && uv run python scripts/pull_vault_reviews.py --url https://<machine>.<tailnet>.ts.net \\
        --vault "$HOME/Desktop/TRADING OS/TRADING BRAIN"

The dashboard password comes from $AIFUND_PASSWORD or a prompt. Only files the vault does not have yet are
written, and only into ``wiki/reviews/`` — never ``raw/`` (the vault's immutable sources) and never over an
existing note (a note you edited in Obsidian stays yours).
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

import httpx

NAME = re.compile(r"^ai-fund-review-\d{4}-W\d{2}\.md$")


class PullError(Exception):
    pass


def reviews_dir(vault: Path) -> Path:
    vault = vault.expanduser().resolve()
    if not (vault / "SCHEMA.md").is_file() or not (vault / "wiki").is_dir():
        raise PullError(f"{vault} does not look like the TRADING BRAIN vault (no SCHEMA.md / wiki/)")
    return vault / "wiki" / "reviews"  # the only place this script writes: never raw/


def pull(client: httpx.Client, vault: Path, password: str) -> list[str]:
    target = reviews_dir(vault)
    login = client.post("/api/auth/login", json={"password": password})
    if login.status_code != 200:
        raise PullError(f"login failed: HTTP {login.status_code}")
    names = client.get("/api/exports/vault")
    names.raise_for_status()
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name in names.json():
        if not isinstance(name, str) or not NAME.match(name) or (target / name).exists():
            continue
        body = client.get(f"/api/exports/vault/{name}")
        body.raise_for_status()
        (target / name).write_text(body.text, encoding="utf-8")
        written.append(name)
    client.post("/api/auth/logout", headers={"X-CSRF-Token": login.json().get("csrf_token", "")})
    return written


def main(argv: list[str], transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="the dashboard, e.g. https://engine.tailnet.ts.net")
    parser.add_argument("--vault", required=True, type=Path, help="the TRADING BRAIN folder")
    args = parser.parse_args(argv)
    password = os.environ.get("AIFUND_PASSWORD") or getpass.getpass("dashboard password: ")
    try:
        with httpx.Client(base_url=args.url, transport=transport, timeout=30) as client:
            written = pull(client, args.vault, password)
    except (PullError, httpx.HTTPError) as exc:
        print(f"not pulled: {exc}", file=sys.stderr)
        return 1
    print("\n".join(f"new: {n}" for n in written) or "nothing new")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
