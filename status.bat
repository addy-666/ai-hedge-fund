@echo off
title AI Fund - System Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\status.ps1"
pause
