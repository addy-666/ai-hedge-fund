# Install on a Windows VPS (roadmap 5.7, docs/06 §1–§3)

A fresh Windows Server 2022 (or Windows 10/11) host to the engine running unattended, and back after a reboot.
Every step is manual once; after it the host restarts itself into a working state.

## 1. Host and user

1. Create a local user **`aifund`** (standard user). Enable **auto-logon** for it (Sysinternals *Autologon*):
   MT5 is a GUI program and runs in this interactive session, never as a service.
2. Windows Update: active hours around your trading day; restarts only in the weekend window.
3. Time: `w32tm /resync` (Windows Time service on, NTP). The engine alerts when the clock drifts.
4. Install: **Git**, **uv** (`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`), **Tailscale**
   (log in; the dashboard is only ever served on the Tailscale address), and optionally **rclone** for off-site
   backups.
5. Leaving RDP: *disconnect*, never *sign out* (signing out stops MT5 and the engine).

## 2. MetaTrader 5

1. Install the broker's MT5 build **in portable mode** into `C:\aifund\mt5\` (run the installer, then start it
   once with `terminal64.exe /portable`).
2. Log in to the **DEMO** account and tick *Save password*.
3. Tools → Options → Expert Advisors: **Allow algorithmic trading**.
4. Tools → Options → Charts: **Max bars in chart = Unlimited** (the history export and research need it).
5. Market Watch: only the configured symbols (the engine selects its own).
6. Guardian EA: copy `mql5\GuardianEA.mq5` to `C:\aifund\mt5\MQL5\Experts\`, compile it in MetaEditor (F7), attach
   it to **one** chart. Inputs: `InpMagic` = `engine.magic`; `InpHardDailyLossPct` = the engine's daily limit + 1;
   `InpCalendarCurrencies` = the `news_currencies` of your symbols. Tools → Options → Notifications: your MetaQuotes
   ID, so the EA can push to the MT5 mobile app. Check the Experts tab: "Guardian EA started".

## 3. The code

```powershell
cd C:\aifund
git clone https://github.com/addy-666/ai-hedge-fund.git
cd ai-hedge-fund\backend
uv sync --python 3.12 --extra mt5
```

Create `C:\aifund\ai-hedge-fund\.env` (never committed; restrict it to `aifund` and Administrators:
right-click → Properties → Security):

```ini
BROKER=mt5
MT5_LOGIN=...            # the DEMO account
MT5_PASSWORD=...
MT5_SERVER=...
MT5_PATH=C:\aifund\mt5\terminal64.exe
MT5_PORTABLE=true
DEEPSEEK_API_KEY=...     # only if strategy.analyst_enabled
TELEGRAM_BOT_TOKEN=...   # with alerts.telegram: true
TELEGRAM_CHAT_ID=...
HEALTHCHECKS_URL=https://hc-ping.com/<uuid>   # period 1 min, grace 3 min, alerts to Telegram/e-mail
```

Copy `config\trading.example.yaml` to `config\trading.yaml` and edit it: `engine.mode: DEMO`,
`engine.account_label`, symbols and sessions (holiday closures from the broker's notices), `llm.pricing` if the
analyst runs, `alerts.telegram: true`. Outside SIM every detector needs E1 evidence in `config\evidence\`
(`docs/09`); without it the engine refuses to start — or set `strategy.dry_run: true` for a no-orders run.

```powershell
uv run alembic upgrade head                       # creates <repo>\data\aifund.db
uv run python scripts/mt5_smoke.py                # read-only checks against the terminal
```

## 4. Scheduled tasks

In an **elevated** PowerShell, logged in as `aifund`:

```powershell
powershell -ExecutionPolicy Bypass -File C:\aifund\ai-hedge-fund\deploy\windows\install_tasks.ps1
```

| Task | Runs | When |
|---|---|---|
| `aifund-mt5` | `C:\aifund\mt5\terminal64.exe /portable` | at logon; restarted on failure every minute |
| `aifund-engine` | `deploy\windows\run_engine.ps1`: waits for the terminal, runs `python -m aifund.engine`, restarts it with backoff 5 s → 5 min | at logon |
| `aifund-api` | `deploy\windows\run_api.ps1`: `tailscale serve --bg 8000` (tailnet-only HTTPS) + uvicorn on `127.0.0.1:8000` serving the API and `frontend\dist` | at logon; restarted on failure every minute |
| `aifund-backup` | `deploy\windows\backup.ps1` → `scripts\backup_db.py` (online backup, gzip, 30 daily + 12 monthly; set user variable `AIFUND_BACKUP_REMOTE=secret:aifund` to upload with rclone — it must be an rclone `crypt` remote wrapping the cloud one, or the upload is refused) | daily 21:30 UTC |
| `aifund-ledger` | `deploy\windows\verify_ledger.ps1` → `scripts\verify_ledger.py --days 3 --alert`: trades vs MT5 deals; a difference or a failure is a CRITICAL alert and the `job.verify_ledger` heartbeat (System page) | daily 22:15 UTC |
| `aifund-research` | `deploy\windows\research_weekly.ps1`: `export_history.py --update` (new bars merged in), then `research.py scheduled` (runs only when due; log `logs\research_weekly.log`) | Tuesdays 03:00 UTC |

Before the API task can serve the dashboard (once, and after every frontend change):

```powershell
cd C:\aifund\ai-hedge-fund\backend; uv run python scripts/hash_password.py   # paste the printed ADMIN_PASSWORD_HASH=... line into .env
cd ..\frontend; npm ci; npm run build
```

Enable HTTPS certificates for the tailnet once (Tailscale admin console → DNS → HTTPS Certificates). The
dashboard is then `https://<machine>.<tailnet>.ts.net` from any device on the tailnet, and nowhere else.

## 5. First start

```powershell
Start-ScheduledTask aifund-mt5; Start-ScheduledTask aifund-engine; Start-ScheduledTask aifund-api
Get-Content C:\aifund\ai-hedge-fund\logs\run_engine.log -Wait
```

The engine always starts **PAUSED** (Telegram: "Engine restarted"). Check `logs\engine.jsonl` for
`engine.booted`, then resume it from the dashboard (System → Resume) or with the script:

```powershell
cd C:\aifund\ai-hedge-fund\backend
uv run python scripts/engine_command.py RESUME
```

Other commands: `PAUSE`, `STOP`, `START`, `REARM`, `FLATTEN_ALL` (the kill switch),
`CLOSE_POSITION --payload '{"position_id": 123}'`, `RELOAD_CONFIG`.

## 6. Acceptance: reboot test (5.7 DoD)

1. `Restart-Computer`. After auto-logon: the terminal and the engine start by themselves; the engine lands in
   **PAUSED** with a "Engine restarted" alert, no order is sent, healthchecks.io stays green.
2. Kill the engine (`Stop-Process -Name python`): `run_engine.ps1` restarts it within seconds, again PAUSED.
3. The next day at 21:30 UTC a file appears in `backups\`.
4. `uv run python scripts/verify_ledger.py --days 7` prints `ledger OK`.
5. Guardian: in the Strategy Tester (or on the demo with a tiny `InpHardDailyLossPct`), a forced equity drop closes
   the engine positions, writes `halt.flag`, and the engine goes **HALTED** within 5 s; REARM is refused until the
   flag is deleted.

## 7. Backup and restore drill (9.5, monthly)

The nightly `aifund-backup` task writes `backups\aifund-YYYYMMDD-HHMM.db.gz` (integrity-checked), uploads it to
the encrypted remote, and only then thins old data (events > 30 days, LLM call text > 180 days). The result is
the `job.backup` heartbeat on the System page; a failure is a CRITICAL alert.

On the Mac, once a month:

1. Fetch the newest backup from the remote: `rclone copy secret:aifund/<file>.db.gz ~/aifund-restore/`.
2. `cd backend && uv run python scripts/verify_db.py ~/aifund-restore/<file>.db.gz` → exit 0 and `OK`
   (integrity, foreign keys, schema at the code's head, ledger consistency, row counts).
3. Restore it for real: `gunzip -k` it to `data/restore.db`, start the API on it
   (`DATABASE_URL=sqlite:///data/restore.db uv run uvicorn aifund.api.app:app`) and check that the Journal shows
   the last closed trades of the Windows host.
4. Record the drill: date, file, verify output, in `docs/PROGRESS.md` (operational measurements).

Restoring on the Windows host after a disk failure: stop `aifund-engine` and `aifund-api`, verify the file as
above, decompress it to the `DATABASE_URL` path, start both tasks; the engine lands in PAUSED and reconciles
against MT5 before anything else (`verify_ledger.py --days 30` then shows what happened meanwhile).

## Rotating secrets

New key in `.env` → PAUSE → restart the engine task (`Stop-ScheduledTask aifund-engine; Start-ScheduledTask
aifund-engine`) in a quiet period. `RELOAD_CONFIG` does not reload secrets.
