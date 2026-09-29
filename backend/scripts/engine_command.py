"""Send the running engine an operator command and wait for its result (until the Phase 6 dashboard exists).

    cd backend
    uv run python scripts/engine_command.py RESUME
    uv run python scripts/engine_command.py PAUSE
    uv run python scripts/engine_command.py FLATTEN_ALL                       # the kill switch
    uv run python scripts/engine_command.py CLOSE_POSITION --payload '{"position_id": 123456}'
    uv run python scripts/engine_command.py REARM

The command goes into the ``commands`` table like the API's would; the engine executes it within a second.
Exit code 0 = DONE, 1 = FAILED or no answer within --wait seconds.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from aifund.adapters.clock import SystemClock
from aifund.config.settings import Settings
from aifund.domain.enums import CommandStatus, CommandType
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.repositories.system import AuditLogRepository, CommandRepository
from aifund.persistence.tables import CommandRow


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("type", choices=[t.value for t in CommandType])
    p.add_argument("--payload", default=None, help="JSON object")
    p.add_argument("--by", default="operator")
    p.add_argument("--wait", type=float, default=30.0)
    args = p.parse_args(argv)
    payload = json.loads(args.payload) if args.payload else None
    factory = make_session_factory(make_engine(Settings().DATABASE_URL))
    clock = SystemClock()
    with unit_of_work(factory) as s:
        command_id = (
            CommandRepository(s, clock).enqueue(CommandType(args.type), payload, requested_by=args.by).id
        )
        AuditLogRepository(s, clock).record(actor=args.by, action=f"COMMAND_{args.type}", after=payload)
    print(f"sent {args.type} ({command_id}); waiting for the engine ...", flush=True)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        with factory() as s:
            row = s.get(CommandRow, command_id)
            if row is not None and row.status in (CommandStatus.DONE, CommandStatus.FAILED):
                print(f"{row.status.value}: {json.dumps(row.result)}")
                return 0 if row.status is CommandStatus.DONE else 1
        time.sleep(0.5)
    print("no answer: is the engine running? (the command stays queued and runs when it starts)")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
