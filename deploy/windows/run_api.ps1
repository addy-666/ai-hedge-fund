# aifund API + dashboard (Phase 6, docs/05 §2, docs/06 §2). uvicorn listens on 127.0.0.1 only; `tailscale serve`
# publishes it to the tailnet as https://<machine>.<tailnet>.ts.net with a real certificate, so the session
# cookie can stay Secure. Nothing is ever bound to a public interface.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $Repo "frontend\dist\index.html"))) {
    Write-Warning "frontend\dist is missing: the API runs without the dashboard (cd frontend; npm ci; npm run build)"
}
& tailscale serve --bg 8000
if ($LASTEXITCODE -ne 0) { throw "tailscale serve failed: is Tailscale up and HTTPS enabled for the tailnet?" }
Set-Location (Join-Path $Repo "backend")
& uv run uvicorn aifund.api.app:app --host 127.0.0.1 --port 8000
exit $LASTEXITCODE
