# Applies ops/database/sql/bootstrap.sql. Uses standard libpq variables:
# PGHOST, PGPORT, PGUSER, PGDATABASE and PGPASSWORD.
$ErrorActionPreference = 'Stop'
if (-not $env:PGDATABASE) { throw 'PGDATABASE is required' }
$sql = Join-Path $PSScriptRoot '..\sql\bootstrap.sql'
psql -v ON_ERROR_STOP=1 -f $sql
if ($LASTEXITCODE -ne 0) { throw "psql failed with exit code $LASTEXITCODE" }