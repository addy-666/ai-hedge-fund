$ErrorActionPreference = "Continue"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Stopping AI Fund Services" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Stop Scheduled Tasks
Write-Host "Stopping scheduled tasks..." -ForegroundColor Yellow
Stop-ScheduledTask -TaskName aifund-engine, aifund-api -ErrorAction SilentlyContinue

# Stop supervisor powershell scripts
Write-Host "Stopping supervisor processes..." -ForegroundColor Yellow
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match "run_engine\.ps1" -or $_.CommandLine -match "run_api\.ps1"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Stop engine & uvicorn python processes
Write-Host "Stopping engine & API Python processes..." -ForegroundColor Yellow
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "python.exe" -and ($_.CommandLine -match "aifund\.engine" -or $_.CommandLine -match "uvicorn")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "[OK] All AI Fund background services stopped." -ForegroundColor Green
Start-Sleep -Seconds 3
