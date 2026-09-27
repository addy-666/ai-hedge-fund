@echo off
echo [SYSTEM] Requesting detached execution...
cd /d "%~dp0"
PowerShell.exe -ExecutionPolicy Bypass -File "%~dp0start_background.ps1"
echo [SYSTEM] Done. You may close this window.
pause
