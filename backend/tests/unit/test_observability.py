from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aifund.config.settings import Settings
from aifund.observability import (
    REDACTED,
    clear_registered_secrets,
    configure_logging,
    correlation,
    get_logger,
    register_secret,
    register_settings_secrets,
)

DEEPSEEK_KEY = "sk-live-4f9a8b7c6d5e"
MT5_PASSWORD = "Tr4d3r!Pass"


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("test", stream=buf)
    clear_registered_secrets()
    yield buf
    clear_registered_secrets()
    logging.getLogger().handlers.clear()


def lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_output_is_json_with_utc_timestamp_level_and_component(stream: io.StringIO) -> None:
    get_logger("engine").info("engine.started", mode="DEMO")
    (rec,) = lines(stream)
    assert rec["event"] == "engine.started"
    assert rec["level"] == "info"
    assert rec["component"] == "test"
    assert rec["mode"] == "DEMO"
    assert rec["timestamp"].endswith("Z")


def test_sensitive_keys_are_redacted_at_any_depth(stream: io.StringIO) -> None:
    get_logger("x").info(
        "llm.request",
        api_key=DEEPSEEK_KEY,
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        creds=[{"password": MT5_PASSWORD}],
    )
    raw = stream.getvalue()
    assert DEEPSEEK_KEY not in raw
    assert MT5_PASSWORD not in raw
    (rec,) = lines(stream)
    assert rec["api_key"] == REDACTED
    assert rec["headers"] == {"Authorization": REDACTED, "Content-Type": "application/json"}
    assert rec["creds"] == [{"password": REDACTED}]


def test_registered_secret_values_are_scrubbed_from_messages_and_exceptions(stream: io.StringIO) -> None:
    settings = Settings(_env_file=None, DEEPSEEK_API_KEY=DEEPSEEK_KEY, MT5_PASSWORD=MT5_PASSWORD)  # type: ignore[call-arg, arg-type]
    register_settings_secrets(settings)
    log = get_logger("x")
    log.warning("login failed for password %s" % MT5_PASSWORD)  # noqa: UP031 - simulate a careless message
    try:
        raise RuntimeError(f"401 Unauthorized: invalid key {DEEPSEEK_KEY}")
    except RuntimeError:
        log.exception("llm.error", detail={"body": f"key={DEEPSEEK_KEY}"})
    logging.getLogger("third.party").error("stdlib logger leaked %s", DEEPSEEK_KEY)
    raw = stream.getvalue()
    assert DEEPSEEK_KEY not in raw
    assert MT5_PASSWORD not in raw
    assert raw.count(REDACTED) >= 4
    assert len(lines(stream)) == 3


def test_short_values_are_not_registered_to_avoid_scrubbing_common_text(stream: io.StringIO) -> None:
    register_secret("abc")
    get_logger("x").info("abc is fine")
    assert lines(stream)[0]["event"] == "abc is fine"


def test_correlation_ids_are_bound_and_released(stream: io.StringIO) -> None:
    log = get_logger("pipeline")
    with correlation(decision_id="01DEC", symbol="XAUUSDm", intent_id=None):
        log.info("stage.done", stage="RISK")
    log.info("after")
    first, second = lines(stream)
    assert first["decision_id"] == "01DEC"
    assert first["symbol"] == "XAUUSDm"
    assert "intent_id" not in first
    assert "decision_id" not in second


def test_file_handler_writes_jsonl(tmp_path: Path) -> None:
    configure_logging("engine", log_dir=tmp_path / "logs", stream=io.StringIO())
    get_logger("x").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    content = (tmp_path / "logs" / "engine.jsonl").read_text()
    assert json.loads(content.splitlines()[0])["event"] == "hello"
    logging.getLogger().handlers.clear()
