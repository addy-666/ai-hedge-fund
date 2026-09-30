# aifund nightly ledger check (roadmap 9.3, docs/06 §10 L2 gate): the trades table vs MT5's deal history for the
# last 3 days. READ-ONLY. Any difference or failure is recorded (heartbeat job.verify_ledger) and alerted.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location (Join-Path $Repo "backend")
$Log = Join-Path $Repo "logs\verify_ledger.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
"=== $(Get-Date -Format o)" | Out-File -Append -Encoding utf8 $Log
& uv run python scripts/verify_ledger.py --days 3 --alert *>> $Log
exit $LASTEXITCODE
