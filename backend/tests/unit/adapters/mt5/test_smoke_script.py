"""The Windows smoke script, run end-to-end against the fake terminal. Proves it is read-only."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace as NS

import pytest

from aifund.adapters.mt5 import gateway as gateway_mod
from tests.fakes.fake_mt5 import FakeMT5, make_symbol

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "mt5_smoke.py"
EXAMPLE_CONFIG = Path(__file__).resolve().parents[5] / "config" / "trading.example.yaml"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mt5_smoke", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake() -> FakeMT5:
    now = datetime.now().timestamp()
    offset = timedelta(hours=3).total_seconds()
    fake = FakeMT5(
        tick_advance_ms=700,
        symbols={n: make_symbol(n) for n in ("XAUUSDm", "EURUSDm", "BTCUSDm")},
    )
    for i, name in enumerate(fake.symbols):
        msc = int((now + offset - 2 - i) * 1000)
        fake.ticks[name] = NS(time=msc // 1000, time_msc=msc, bid=100.0 + i, ask=100.2 + i)
    return fake


async def test_smoke_script_is_read_only_and_passes_on_a_healthy_demo(
    fake: FakeMT5, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load_script()
    monkeypatch.setattr(script, "MT5Gateway", _gateway_with(fake))
    monkeypatch.setenv("MT5_LOGIN", "12345678")
    monkeypatch.setenv("MT5_PASSWORD", "demo-password")
    monkeypatch.setenv("MT5_SERVER", "Broker-Demo")
    monkeypatch.setenv("CONFIG_PATH", str(EXAMPLE_CONFIG))
    code = await script.main()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "all checks passed" in out
    assert "broker server time = UTC+3.00h" in out
    assert "demo-password" not in out
    assert not {"order_send", "order_check"} & set(fake.names())


async def test_smoke_script_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    script = load_script()
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        monkeypatch.setenv(key, "")
    real_settings = script.Settings
    monkeypatch.setattr(script, "Settings", lambda: real_settings(_env_file=None))
    assert await script.main() == 2


def _gateway_with(fake: FakeMT5) -> type[gateway_mod.MT5Gateway]:
    class _Gateway(gateway_mod.MT5Gateway):
        def __init__(self, creds: gateway_mod.MT5Credentials, *, clock: object) -> None:
            super().__init__(creds, clock=_FastClock(), module_loader=lambda: fake)

    return _Gateway


class _FastClock:
    """Real time, but sampling sleeps are skipped so the test stays fast."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        return None
