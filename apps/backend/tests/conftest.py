"""Pytest configuration for the backend test suite.

Its mere presence makes pytest insert this ``tests`` directory onto
``sys.path`` at the start of collection, so sibling test-support modules (such
as ``_stix_seed``) are importable from the test modules regardless of the
invocation's working directory or import mode.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# No test may trigger network access or background ingestion through the app
# ``lifespan``; tests that need the orchestrator build it explicitly.
os.environ["STICKS_INGEST_ON_START"] = "0"
