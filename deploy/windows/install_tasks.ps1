# Registers the aifund scheduled tasks for the CURRENT user (run once, in an elevated PowerShell, as "aifund").
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_tasks.ps1 [-MT5Path C:\aifund\mt5\terminal64.exe]
param([string]$MT5Path = "C:\aifund\mt5\terminal64.exe")
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$User = "$env:USERDOMAIN\$env:USERNAME"
$PS = "powershell.exe"
$Tasks = @{   # name = (command, arguments)
    "aifund-mt5"    = @($MT5Path, "/portable")
    "aifund-engine" = @($PS, "-NoProfile -ExecutionPolicy Bypass -File `"$Here\run_engine.ps1`"")
    "aifund-api"    = @($PS, "-NoProfile -ExecutionPolicy Bypass -File `"$Here\run_api.ps1`"")
    "aifund-backup" = @($PS, "-NoProfile -ExecutionPolicy Bypass -File `"$Here\backup.ps1`"")
    "aifund-ledger" = @($PS, "-NoProfile -ExecutionPolicy Bypass -File `"$Here\verify_ledger.ps1`"")
    "aifund-research" = @($PS, "-NoProfile -ExecutionPolicy Bypass -File `"$Here\research_weekly.ps1`"")
}
foreach ($Name in $Tasks.Keys) {
    $Xml = Get-Content -Raw -Path (Join-Path $Here "tasks\$Name.xml")
    $Command, $Arguments = $Tasks[$Name]
    $Xml = $Xml.Replace("__USER__", [Security.SecurityElement]::Escape($User))
    $Xml = $Xml.Replace("__COMMAND__", [Security.SecurityElement]::Escape($Command))
    $Xml = $Xml.Replace("__ARGUMENTS__", [Security.SecurityElement]::Escape($Arguments))
    Register-ScheduledTask -TaskName $Name -Xml $Xml -Force | Out-Null
    Write-Host "registered $Name"
}
Write-Host "Done. Sign out and back in (auto-logon) or run: Start-ScheduledTask aifund-mt5; Start-ScheduledTask aifund-engine; Start-ScheduledTask aifund-api"
