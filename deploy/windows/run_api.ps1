# aifund API (Phase 6, docs/06 §2): serves the dashboard on the Tailscale address only.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$TailscaleIp = (& tailscale ip -4) | Select-Object -First 1
if (-not $TailscaleIp) { throw "Tailscale is not up: the API is never exposed on a public interface" }
Set-Location (Join-Path $Repo "backend")
& uv run uvicorn aifund.api.app:app --host $TailscaleIp --port 8000
