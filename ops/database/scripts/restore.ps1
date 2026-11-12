# Restores a dump made by backup.ps1. Usage: .\restore.ps1 -File <file.dump>
# Variables: PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD. Existing objects are dropped first
# (--clean --if-exists), so run it against the intended database only.
param([Parameter(Mandatory = $true)][string]$File)
$ErrorActionPreference = 'Stop'
if (-not $env:PGDATABASE) { throw 'PGDATABASE is required' }
pg_restore --clean --if-exists --no-owner --dbname="$env:PGDATABASE" $File
if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE" }