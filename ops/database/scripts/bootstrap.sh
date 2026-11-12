#!/usr/bin/env bash
# Applies ops/database/sql/bootstrap.sql. Uses standard libpq variables:
# PGHOST, PGPORT, PGUSER, PGDATABASE and PGPASSWORD (or ~/.pgpass).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${PGDATABASE:?PGDATABASE is required}"
psql -v ON_ERROR_STOP=1 -f "$here/../sql/bootstrap.sql"