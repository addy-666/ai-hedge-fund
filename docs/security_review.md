# Security review (roadmap 9.4)

Reviewed 2026-09-30 against the checklist in `06_OPERATIONS_AND_SECURITY.md` §6. Every item is either proven
by an automated check that runs in CI (named below) or is an operator step on the Windows host, with the exact
command. Re-run this review before L3 (live-micro) and after any change to `api/`, `config/settings.py`, the
deployment scripts or the dependency lock.

## Checklist

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Dashboard/API reachable only via Tailscale | ✅ code / ☐ operator | `run_api.ps1` binds uvicorn to `127.0.0.1:8000` and publishes it only through `tailscale serve` (HTTPS on the tailnet). Operator: block 8000 on every interface anyway (`New-NetFirewallRule -DisplayName "aifund api deny" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Block`) and check from a phone off the tailnet that `http://<public-ip>:8000` does not answer. |
| 2 | RDP restricted (Tailscale only / allowlist), strong password + NLA | ☐ operator | `Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name UserAuthentication -Value 1` (NLA); firewall rule allowing 3389 only from `100.64.0.0/10` (the tailnet); a 16+ character password for `aifund`. |
| 3 | Login rate-limited; cookies Secure/HttpOnly/SameSite=Strict; CSRF on mutations | ✅ | `tests/integration/test_api_security.py`: `test_logins_are_rate_limited_per_socket_not_per_header` (5 a minute per socket; a spoofed `X-Forwarded-For` does not reset it), `test_the_session_cookie_and_its_lifetime` (flags; server-side expiry after 12 h; logout kills the session even for a copied cookie), `test_every_route_needs_a_session` and `test_every_mutation_needs_the_csrf_token` (walk EVERY route in the OpenAPI schema: 401 without a session, 403 without or with a wrong CSRF token). |
| 4 | Re-auth for LIVE, risk increases, drawdown re-arm, block-rule approval | ✅ | `test_gated_actions_need_a_fresh_password` (SET_MODE LIVE, calibration approval) plus the existing API tests for REARM after a drawdown halt, block-rule approval / force-activation and config risk increases (`test_api.py`). Calibration approval was added to the list in 8.5 (it can raise confidence over the threshold). |
| 5 | No endpoint can place an arbitrary order | ✅ | `test_no_route_can_enqueue_anything_but_operator_commands` (the API source uses only operator command types and never names an order, an intent, the executor or the Risk Manager) and the import-linter contract "The API never touches execution, risk sizing or a broker". The only trading mutations are PAUSE / FLATTEN_ALL / CLOSE_POSITION (engine magic only). |
| 6 | Prompt-injection surface | ✅ (with a note) | Prompts contain numeric market data, curated playbook cards (committed, operator-reviewed), the engine's own rules and, for the risk critic, the specialist's thesis. No external free text (news, web) exists in v1. Note: the critic reads another model's output — a compromised provider could steer the critic, but the critic can only LOWER confidence (penalty points ≥ 0; it never changes direction, levels or size), and every LLM output is schema-validated with extra keys forbidden. |
| 7 | Dependency audit in CI | ✅ | CI job `audit`: `pip-audit` over the locked requirements (every extra, incl. MetaTrader5) and `npm audit --audit-level=high`. Result 2026-09-30: no known vulnerabilities in either. |
| 8 | Backups encrypted at rest in the remote store | ✅ code / ☐ operator | `backup_db.py --remote` refuses any rclone remote that is not of type `crypt` before writing anything (`test_backups_are_only_uploaded_to_an_encrypted_remote`). Operator: `rclone config` → a `crypt` remote (e.g. `secret:`) wrapping the cloud remote, and set `AIFUND_BACKUP_REMOTE=secret:aifund`. Keep the crypt password in the password manager, not on the VPS only. |

## Findings fixed in this review

- **WebSocket Origin check.** `/api/ws` authenticated by the session cookie but did not check the `Origin`
  header. `SameSite=Strict` already keeps the cookie off cross-site handshakes in current browsers; the
  stream is read-only. Defence in depth: a browser's `Origin` must now match the host (or `X-Forwarded-Host`,
  or an entry of `API_ALLOWED_ORIGINS`); a refusal is logged as `ws.origin_refused` with the hosts involved.
  If the tailnet proxy ever rewrites the Host header, add `https://<machine>.<tailnet>.ts.net` to
  `API_ALLOWED_ORIGINS` in `.env`.
- **Backups could be uploaded unencrypted** (item 8): now refused.

## Checked and fine

- Passwords: argon2 hashes only (`ADMIN_PASSWORD_HASH`); constant-time CSRF comparison; session tokens are
  random 256-bit values stored as SHA-256.
- The SPA fallback never serves a file outside `frontend/dist` (`test_the_spa_never_serves_outside_the_dashboard`,
  encoded and plain `..` paths).
- Secrets: `.env` only; logs redact by key and by registered secret value (0.8); the repository is public and
  carries no login, key or account number (fixtures use placeholders).
- Every mutation is written to `audit_log` with the client address (6.3).
