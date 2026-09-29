"""Make the dashboard password hash for ``.env`` (``ADMIN_PASSWORD_HASH``). The password is never stored.

cd backend
uv run python scripts/hash_password.py        # asks twice, prints ADMIN_PASSWORD_HASH=...
"""

from __future__ import annotations

import getpass
import sys

from argon2 import PasswordHasher

MIN_LENGTH = 12


def main() -> int:
    first = getpass.getpass("Dashboard password: ")
    if len(first) < MIN_LENGTH:
        print(f"use at least {MIN_LENGTH} characters", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != first:
        print("the two entries differ", file=sys.stderr)
        return 1
    print(f"ADMIN_PASSWORD_HASH={PasswordHasher().hash(first)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
