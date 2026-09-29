"""Dashboard authentication (roadmap 6.1, docs/05 §2).

One operator. The password's argon2 hash lives in ``.env`` (``ADMIN_PASSWORD_HASH``). A login creates a
session: the browser gets a random token in an HttpOnly, SameSite=Strict (and Secure) cookie; the database
keeps only the token's SHA-256, a CSRF token, the 12-hour expiry and the time of the last password entry.

- Every mutation needs the ``X-CSRF-Token`` header matching the session's token (``require_csrf``).
- Re-auth-gated actions (LIVE, raising risk, REARM after a drawdown halt, …) need a password entry within the
  last 5 minutes (``require_reauth``); ``POST /api/auth/reauth`` refreshes it. PAUSE and FLATTEN_ALL never do.
- Logins are rate limited per client address (5 a minute), successful or not.
"""

from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.api import ApiSessionRepository
from aifund.persistence.repositories.system import AuditLogRepository
from aifund.persistence.tables import ApiSessionRow

COOKIE = "aifund_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL = timedelta(hours=12)
REAUTH_WINDOW = timedelta(minutes=5)
LOGIN_LIMIT = 5  # per client address per minute
_hasher = PasswordHasher()


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class SessionInfo(BaseModel):
    csrf_token: str
    expires_at: datetime
    reauth_until: datetime


class Me(BaseModel):
    user: str = "operator"
    csrf_token: str  # a reloaded dashboard gets it back here (same-origin, cookie-authenticated)
    expires_at: datetime
    reauth_fresh: bool
    reauth_until: datetime


class LoginLimiter:
    def __init__(self, limit: int = LOGIN_LIMIT, window: timedelta = timedelta(minutes=1)) -> None:
        self._limit = limit
        self._window = window
        self._hits: defaultdict[str, deque[datetime]] = defaultdict(deque)

    def allow(self, client: str, now: datetime) -> bool:
        hits = self._hits[client]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_password(stored_hash: str | None, password: str) -> bool:
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _state(request: Request) -> Any:
    return request.app.state


def current_session(request: Request) -> ApiSessionRow:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    st = _state(request)
    now = st.clock.now()
    with unit_of_work(st.factory) as s:
        row = ApiSessionRepository(s).get(token_hash(token), now)
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
        row.last_seen_at = now
        s.flush()
        s.expunge(row)
    return row


Authenticated = Annotated[ApiSessionRow, Depends(current_session)]


def require_csrf(request: Request, session: Authenticated) -> ApiSessionRow:
    sent = request.headers.get(CSRF_HEADER, "")
    if not secrets.compare_digest(sent, session.csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or wrong CSRF token")
    return session


Mutating = Annotated[ApiSessionRow, Depends(require_csrf)]


def reauth_fresh(session: ApiSessionRow, now: datetime) -> bool:
    return now - session.reauth_at <= REAUTH_WINDOW


def require_reauth(request: Request, session: ApiSessionRow) -> None:
    """Call inside a handler when the action needs a recent password entry (it depends on the payload)."""
    if not reauth_fresh(session, _state(request).clock.now()):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "reauth_required: enter the password again (5 minutes)"
        )


router = APIRouter(prefix="/api", tags=["auth"])


def _check_password(request: Request, password: str) -> None:
    st = _state(request)
    if not st.limiter.allow(client_ip(request), st.clock.now()):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts: wait a minute")
    stored = st.settings.ADMIN_PASSWORD_HASH.get_secret_value() if st.settings.ADMIN_PASSWORD_HASH else None
    if not verify_password(stored, password):
        with unit_of_work(st.factory) as s:
            AuditLogRepository(s, st.clock).record(
                actor="unknown", action="LOGIN_FAILED", ip=client_ip(request)
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong password")


@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> SessionInfo:
    _check_password(request, body.password)
    st = _state(request)
    now = st.clock.now()
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    row = ApiSessionRow(
        token_sha256=token_hash(token),
        csrf_token=csrf,
        created_at=now,
        expires_at=now + SESSION_TTL,
        reauth_at=now,
        last_seen_at=now,
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:256],
    )
    with unit_of_work(st.factory) as s:
        repo = ApiSessionRepository(s)
        repo.purge_expired(now)
        repo.add(row)
        AuditLogRepository(s, st.clock).record(actor="operator", action="LOGIN", ip=client_ip(request))
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=st.settings.API_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return SessionInfo(csrf_token=csrf, expires_at=row.expires_at, reauth_until=now + REAUTH_WINDOW)


@router.post("/auth/reauth")
def reauth(body: LoginRequest, request: Request, session: Mutating) -> SessionInfo:
    _check_password(request, body.password)
    st = _state(request)
    now = st.clock.now()
    with unit_of_work(st.factory) as s:
        row = s.get(ApiSessionRow, session.token_sha256)
        assert row is not None
        row.reauth_at = now
        AuditLogRepository(s, st.clock).record(actor="operator", action="REAUTH", ip=client_ip(request))
    return SessionInfo(
        csrf_token=session.csrf_token, expires_at=session.expires_at, reauth_until=now + REAUTH_WINDOW
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, session: Mutating) -> None:
    st = _state(request)
    with unit_of_work(st.factory) as s:
        ApiSessionRepository(s).remove(session.token_sha256)
    response.delete_cookie(COOKIE, path="/")


@router.get("/me")
def me(request: Request, session: Authenticated) -> Me:
    now = _state(request).clock.now()
    return Me(
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
        reauth_fresh=reauth_fresh(session, now),
        reauth_until=session.reauth_at + REAUTH_WINDOW,
    )
