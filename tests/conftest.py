"""Shared pytest fixtures/setup for the maine-government-feeds test suite.

Adds scripts/ to sys.path so tests can import the build/validate/classify
modules directly, mirroring how they run in CI (`python scripts/<name>.py`).
None of these tests touch the network.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
