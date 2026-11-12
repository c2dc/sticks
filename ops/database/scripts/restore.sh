#!/usr/bin/env bash
# Restores a dump made by backup.sh. Usage: restore.sh <file.dump>
# Variables: PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD. Existing objects are dropped first
# (--clean --if-exists), so run it against the intended database only.
set -euo pipefail
file="${1:?usage: restore.sh <file.dump>}"
: "${PGDATABASE:?PGDATABASE is required}"
pg_restore --clean --if-exists --no-owner --dbname="$PGDATABASE" "$file"