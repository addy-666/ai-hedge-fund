@echo off
title AI Fund - Stop Services
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\stop.ps1"
