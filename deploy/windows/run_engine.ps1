# aifund engine supervisor (docs/06 §2): waits for the MT5 terminal, runs the engine, restarts it with backoff.
# Started at logon by the "aifund-engine" scheduled task. Logs to <repo>\logs\run_engine.log.
$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Backend = Join-Path $Repo "backend"
$Log = Join-Path $Repo "logs\run_engine.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

function Write-Log([string]$Message) {
    "$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')) $Message" | Add-Content -Path $Log
}

$Delay = 5
while ($true) {
    while (-not (Get-Process -Name "terminal64" -ErrorAction SilentlyContinue)) {
        Write-Log "waiting for the MT5 terminal (terminal64.exe)"
        Start-Sleep -Seconds 15
    }
    Write-Log "starting the engine"
    $Started = Get-Date
    Push-Location $Backend
    & uv run python -m aifund.engine
    $Code = $LASTEXITCODE
    Pop-Location
    Write-Log "engine exited with code $Code"
    if (((Get-Date) - $Started).TotalMinutes -gt 10) { $Delay = 5 }   # it ran a while: start the backoff again
    Write-Log "restarting in $Delay s"
    Start-Sleep -Seconds $Delay
    $Delay = [Math]::Min($Delay * 2, 300)                              # 5 s -> 5 min
}
