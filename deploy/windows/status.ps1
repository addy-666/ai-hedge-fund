$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " AI Fund System Status" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Check MT5
$mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue
Write-Host "MetaTrader 5 (MT5): " -NoNewline
if ($mt5) {
    Write-Host "RUNNING (PID: $($mt5.Id))" -ForegroundColor Green
} else {
    Write-Host "NOT RUNNING (Please start MT5)" -ForegroundColor Red
}

# Check Engine
$engine = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "python.exe" -and $_.CommandLine -match "aifund\.engine"
}
Write-Host "Trading Engine:     " -NoNewline
if ($engine) {
    Write-Host "RUNNING (PID: $($engine.ProcessId))" -ForegroundColor Green
} else {
    Write-Host "STOPPED" -ForegroundColor Red
}

# Check API / Dashboard
$api = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "python.exe" -and $_.CommandLine -match "uvicorn"
}
Write-Host "API & Dashboard:    " -NoNewline
if ($api) {
    Write-Host "RUNNING (PID: $($api.ProcessId))" -ForegroundColor Green
} else {
    Write-Host "STOPPED" -ForegroundColor Red
}

Write-Host ""
$Log = Join-Path $Repo "logs\run_engine.log"
if (Test-Path $Log) {
    Write-Host "--- Latest Engine Logs (Last 10 lines) ---" -ForegroundColor Cyan
    Get-Content $Log -Tail 10
}
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
