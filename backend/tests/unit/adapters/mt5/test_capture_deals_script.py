"""The Windows deal-capture script, run end-to-end against the fake terminal: read-only, demo-only, and the
fixture it writes rebuilds the balance and carries no account identity."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS

import pytest

from aifund.adapters.mt5.deal_history import DealFixture
from tests.fakes.fake_mt5 import FakeMT5, make_account, make_symbol
from tests.unit.adapters.mt5.test_smoke_script import EXAMPLE_CONFIG, _gateway_with

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "capture_deals.py"
EXAMPLE = Path(__file__).resolve().parents[3] / "fixtures" / "mt5_deals" / "example_synthetic.json"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_deals", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def terminal(balance: float = 10006.2, trade_mode: int = 0) -> FakeMT5:
    """A demo terminal holding the example's deals (plus a field the fixture must drop)."""
    now = datetime.now(UTC).timestamp()
    offset = timedelta(hours=3).total_seconds()
    fake = FakeMT5(
        tick_advance_ms=700,
        account=make_account(balance=balance, trade_mode=trade_mode),
        symbols={n: make_symbol(n) for n in ("XAUUSD", "BTCUSD", "NAS100.r")},
        deals=[NS(**d, external_id="LP-99") for d in DealFixture.load(EXAMPLE).deals],
    )
    for i, name in enumerate(fake.symbols):
        msc = int((now + offset - 2 - i) * 1000)
        fake.ticks[name] = NS(time=msc // 1000, time_msc=msc, bid=100.0 + i, ask=100.2 + i)
    return fake


@pytest.fixture(autouse=True)
def demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_LOGIN", "12345678")
    monkeypatch.setenv("MT5_PASSWORD", "demo-password")
    monkeypatch.setenv("MT5_SERVER", "Broker-Demo")
    monkeypatch.setenv("CONFIG_PATH", str(EXAMPLE_CONFIG))


async def run(fake: FakeMT5, out: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    script = load_script()
    monkeypatch.setattr(script, "MT5Gateway", _gateway_with(fake))
    code: int = await script.main(["--out", str(out), "--note", "test"])
    return code


async def test_capture_writes_a_balanced_anonymous_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = terminal()
    out = tmp_path / "capture.json"
    assert await run(fake, out, monkeypatch) == 0, capsys.readouterr().out
    text = out.read_text()
    assert "12345678" not in text  # the login
    assert "demo-password" not in text
    assert "LP-99" not in text  # external_id dropped
    data = json.loads(text)
    assert (data["server"], data["trade_mode"], data["server_offset_minutes"]) == ("Broker-Demo", "DEMO", 180)
    assert len(data["deals"]) == 12
    assert "OK: the deals rebuild the MT5 balance exactly." in capsys.readouterr().out
    assert not {"order_send", "order_check"} & set(fake.names())  # read-only


async def test_a_mismatch_is_reported_but_still_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "capture.json"
    assert await run(terminal(balance=10000.0), out, monkeypatch) == 1
    assert "mismatch -6.20" in capsys.readouterr().out  # balance 10000.00 - deals 10006.20
    assert out.exists()


async def test_real_accounts_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "capture.json"
    assert await run(terminal(trade_mode=2), out, monkeypatch) == 2  # ACCOUNT_TRADE_MODE_REAL
    assert not out.exists()
