$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Restarting AI Fund Services" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# 1. Stop existing tasks & processes
Write-Host "[1/3] Stopping existing services..." -ForegroundColor Yellow
Stop-ScheduledTask -TaskName aifund-engine, aifund-api -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "run_engine\.ps1" -or $_.CommandLine -match "run_api\.ps1"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "python.exe" -and ($_.CommandLine -match "aifund\.engine" -or $_.CommandLine -match "uvicorn")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep -Seconds 2

# 2. Check if Task Scheduler tasks exist
Write-Host "[2/3] Launching background services..." -ForegroundColor Yellow
if (Get-ScheduledTask -TaskName aifund-engine -ErrorAction SilentlyContinue) {
    Start-ScheduledTask -TaskName aifund-api -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName aifund-engine -ErrorAction SilentlyContinue
    Write-Host "  -> Started via Windows Task Scheduler." -ForegroundColor Green
} else {
    $RunEngine = Join-Path $Repo "deploy\windows\run_engine.ps1"
    $RunApi = Join-Path $Repo "deploy\windows\run_api.ps1"
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$RunApi`"" -WindowStyle Hidden
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$RunEngine`"" -WindowStyle Hidden
    Write-Host "  -> Started via hidden background processes." -ForegroundColor Green
}

# 3. Confirmation
Start-Sleep -Seconds 2
Write-Host ""
Write-Host "[3/3] Done! All services are running in background." -ForegroundColor Green
Write-Host "You can close this window now." -ForegroundColor Gray
Start-Sleep -Seconds 3
