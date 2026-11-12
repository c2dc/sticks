#!/usr/bin/env bash
# Custom-format dump with pg_dump. Variables: PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD,
# and optionally BACKUP_DIR (default: ./backups).
set -euo pipefail
: "${PGDATABASE:?PGDATABASE is required}"
dir="${BACKUP_DIR:-./backups}"
mkdir -p "$dir"
out="$dir/${PGDATABASE}-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --format=custom --no-owner --file="$out"
echo "Backup written to $out"