# Custom-format dump with pg_dump. Variables: PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD,
# and optionally BACKUP_DIR (default: .\backups).
$ErrorActionPreference = 'Stop'
if (-not $env:PGDATABASE) { throw 'PGDATABASE is required' }
$dir = if ($env:BACKUP_DIR) { $env:BACKUP_DIR } else { '.\backups' }
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$out = Join-Path $dir "$($env:PGDATABASE)-$stamp.dump"
pg_dump --format=custom --no-owner --file="$out"
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
Write-Host "Backup written to $out"