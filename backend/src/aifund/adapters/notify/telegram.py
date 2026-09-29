"""Telegram alerts and the healthchecks.io dead-man switch (roadmap 5.5, docs/06 §7).

Both are best effort (``NotifierPort``): a failure is logged, never raised into trading code. The bot token
lives in the URL, so errors are logged without the URL (the log redaction also scrubs registered secrets).
Identical alerts within ``dedupe_s`` are sent once (a flapping check must not flood the phone).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import structlog

from aifund.ports.system import ClockPort, Severity

log = structlog.get_logger(__name__)
PREFIX = {Severity.CRITICAL: "🔴 CRITICAL", Severity.WARN: "🟠 WARN", Severity.INFO: "🔵 INFO"}
MAX_TEXT = 4000  # Telegram's limit is 4096 characters


def format_alert(severity: Severity, title: str, body: str = "", *, account: str = "") -> str:
    head = f"{PREFIX[severity]} · {title}" + (f" [{account}]" if account else "")
    text = head if not body else f"{head}\n{body}"
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        client: httpx.AsyncClient,
        clock: ClockPort,
        *,
        account: str = "",
        min_severity: Severity = Severity.INFO,
        dedupe_s: float = 60.0,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        self._url = f"{base_url}/bot{token}/sendMessage"
        self._chat = chat_id
        self._client = client
        self._clock = clock
        self._account = account
        self._min = list(Severity).index(min_severity)
        self._dedupe = timedelta(seconds=dedupe_s)
        self._recent: dict[tuple[Severity, str, str], datetime] = {}

    async def notify(self, severity: Severity, title: str, body: str = "") -> None:
        if list(Severity).index(severity) < self._min:
            return
        key, now = (severity, title, body), self._clock.now()
        last = self._recent.get(key)
        if last is not None and now - last < self._dedupe:
            return
        self._recent = {k: t for k, t in self._recent.items() if now - t < self._dedupe}
        self._recent[key] = now
        text = format_alert(severity, title, body, account=self._account)
        try:
            response = await self._client.post(
                self._url,
                json={"chat_id": self._chat, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
            if response.status_code != 200:
                log.warning("telegram.rejected", status=response.status_code, title=title)
        except httpx.HTTPError as exc:
            log.warning("telegram.failed", error=type(exc).__name__, title=title)


class HealthchecksPinger:
    """External dead-man switch: the engine pings every minute; healthchecks.io alerts when it stops."""

    def __init__(self, url: str, client: httpx.AsyncClient) -> None:
        self._url = url.rstrip("/")
        self._client = client

    async def ping(self, *, failing: bool = False) -> bool:
        try:
            response = await self._client.get(self._url + ("/fail" if failing else ""), timeout=10)
        except httpx.HTTPError as exc:
            log.warning("healthchecks.failed", error=type(exc).__name__)
            return False
        return response.status_code == 200


class FanOut:
    """Sends every alert to each notifier (e.g. Telegram and the log); one failing never stops the others."""

    def __init__(self, *notifiers: object) -> None:
        self._notifiers = notifiers

    async def notify(self, severity: Severity, title: str, body: str = "") -> None:
        for n in self._notifiers:
            try:
                await n.notify(severity, title, body)  # type: ignore[attr-defined]
            except Exception as exc:
                log.warning("notifier.failed", notifier=type(n).__name__, error=str(exc))


class LogNotifier:
    async def notify(self, severity: Severity, title: str, body: str = "") -> None:
        {Severity.CRITICAL: log.error, Severity.WARN: log.warning}.get(severity, log.info)(
            "alert", severity=severity.value, title=title, body=body
        )
