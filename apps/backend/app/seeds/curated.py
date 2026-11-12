"""Command line entry: ``python -m app.seeds.curated``.

Applies the curated seed (JSON files under `app/seeds/data`) to the configured database (idempotent) and
prints a summary of counts. Nothing sensitive (URLs, credentials) is printed.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    from app.db.session import SessionLocal
    from app.seeds.apply import apply_curated_seed

    with SessionLocal() as session:
        try:
            summary = apply_curated_seed(session)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - report the class only, no details
            session.rollback()
            print(f"curated seed failed: {type(exc).__name__}", file=sys.stderr)
            return 1
    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
