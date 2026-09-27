Write-Host "[SYSTEM] Locating AI Hedge Fund processes..."

# Find python processes running main.py
$processes = Get-WmiObject Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%main.py%'"
if ($processes) {
    foreach ($p in $processes) {
        Write-Host "[SYSTEM] Terminating Python process (PID: $($p.ProcessId))..."
        Stop-Process -Id $p.ProcessId -Force
    }
} else {
    Write-Host "[SYSTEM] No running AI Hedge Fund python processes found."
}

# Find processes listening on port 8000 (FastAPI default)
$netstat = netstat -ano | Select-String "LISTENING" | Select-String ":8000"
if ($netstat) {
    $pidToKill = ($netstat.Line -split '\s+')[-1]
    if ($pidToKill -ne "0" -and $pidToKill -ne "") {
        Write-Host "[SYSTEM] Terminating process listening on port 8000 (PID: $pidToKill)..."
        Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "[SYSTEM] Shutdown complete."
