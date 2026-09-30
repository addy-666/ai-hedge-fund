# aifund weekly research (roadmap 8.7, docs/09): new bars from MT5 merged into data/history, then the scheduled
# research run (it decides itself whether it is due: new history and research.schedule_days since the last).
# Read-only against MT5; research never sends an order. Output goes to logs\research_weekly.log.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location (Join-Path $Repo "backend")
$Log = Join-Path $Repo "logs\research_weekly.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
"=== $(Get-Date -Format o)" | Out-File -Append -Encoding utf8 $Log
& uv run python scripts/export_history.py --update *>> $Log
if ($LASTEXITCODE -ne 0) { "export failed ($LASTEXITCODE): research skipped" | Out-File -Append -Encoding utf8 $Log; exit $LASTEXITCODE }
& uv run python scripts/research.py scheduled *>> $Log
exit $LASTEXITCODE
