# aifund nightly backup (docs/06 §8): online SQLite backup, gzip, retention, optional rclone upload.
# Set AIFUND_BACKUP_REMOTE (e.g. "secret:aifund", an rclone crypt remote) as a user environment variable to upload.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location (Join-Path $Repo "backend")
$UvArgs = @("run", "python", "scripts/backup_db.py", "--alert")
if ($env:AIFUND_BACKUP_REMOTE) { $UvArgs += @("--remote", $env:AIFUND_BACKUP_REMOTE) }
& uv @UvArgs
exit $LASTEXITCODE
