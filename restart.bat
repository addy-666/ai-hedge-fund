@echo off
title AI Fund - Restart Services
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\restart.ps1"
