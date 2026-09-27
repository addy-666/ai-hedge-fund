$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
$batPath = Join-Path $scriptPath "run_server.bat"

Write-Host "[SYSTEM] Spawning detached AI Hedge Fund process via WMI..."
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "cmd.exe /c `"$batPath`""
}
Write-Host "[SYSTEM] Background process started successfully. Check server.log for output."
