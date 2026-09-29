"""Live events (roadmap 6.4, docs/05 §3): ``/api/ws?since=<seq>`` streams ``events`` rows as ``{seq, ts, type,
severity, payload}``. A client that reconnects with the last seq it saw receives every event it missed exactly
once (``seq`` is AUTOINCREMENT: never reused). ``since=-1`` starts from now: the first message is a
``stream.start`` carrying the current seq. Authenticated by the session cookie; no mutation happens here."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from aifund.api.auth import COOKIE, token_hash
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.api import ApiSessionRepository
from aifund.persistence.repositories.system import EventRepository

router = APIRouter(prefix="/api", tags=["events"])
POLL_S = 0.5
BATCH = 500


def _authenticated(websocket: WebSocket) -> bool:
    token = websocket.cookies.get(COOKIE)
    if not token:
        return False
    st = websocket.app.state
    with unit_of_work(st.factory) as s:
        return ApiSessionRepository(s).get(token_hash(token), st.clock.now()) is not None


def _events(websocket: WebSocket, since: int) -> list[dict[str, Any]]:
    st = websocket.app.state
    with unit_of_work(st.factory) as s:
        rows = EventRepository(s, st.clock).since(since, BATCH)
        return [
            {
                "seq": r.seq,
                "ts": r.ts.isoformat(),
                "type": r.type,
                "severity": r.severity,
                "payload": r.payload,
            }
            for r in rows
        ]


def _latest(websocket: WebSocket) -> int:
    st = websocket.app.state
    with unit_of_work(st.factory) as s:
        return EventRepository(s, st.clock).latest_seq()


@router.websocket("/ws")
async def events(websocket: WebSocket, since: int = 0) -> None:
    if not await asyncio.to_thread(_authenticated, websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    last = since
    if since < 0:  # "from now": tell the client where the stream starts, so a reconnect can resume from there
        last = await asyncio.to_thread(_latest, websocket)
        await websocket.send_json(
            {"seq": last, "ts": None, "type": "stream.start", "severity": "info", "payload": None}
        )
    closed = asyncio.ensure_future(websocket.receive_text())  # completes when the client goes away
    try:
        while True:
            for event in await asyncio.to_thread(_events, websocket, last):
                await websocket.send_json(event)
                last = event["seq"]
            done, _ = await asyncio.wait({closed}, timeout=POLL_S)
            if done:
                if isinstance(closed.exception(), WebSocketDisconnect) or closed.exception() is not None:
                    return
                closed = asyncio.ensure_future(websocket.receive_text())  # a client ping: keep listening
    except WebSocketDisconnect:
        return
    finally:
        closed.cancel()
