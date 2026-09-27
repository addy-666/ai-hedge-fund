@echo off
echo [SYSTEM] Initiating shutdown of AI Hedge Fund...
cd /d "%~dp0"
PowerShell.exe -ExecutionPolicy Bypass -File "%~dp0stop_background.ps1"
pause
