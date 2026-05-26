"""Shared pytest setup for tests/unit/.

Bootstraps two things every offline regression test in this directory needs:

1. ``src/`` on ``sys.path`` so ``import main``, ``import config``, ``import
   notice_parser`` etc. resolve without per-file boilerplate. Mirrors the
   ``sys.path.insert(0, ...)`` idiom used in the legacy ``tests/test_*.py``
   files (see ``tests/test_parser.py:6-8``) but lifts it to a single ambient
   fixture so the unit-test files stay focused on assertions.

2. ``.env`` loaded via ``python-dotenv`` so any test that touches
   ``src/config.py`` gets a sane environment (per TESTING.md: "Tests rely on
   ``.env`` being loaded"). This is a no-op when ``.env`` doesn't exist (e.g.
   in CI without secrets) — ``config.py`` defaults every credential to ``""``.

This conftest is scoped to ``tests/unit/`` only. The legacy ``tests/test_*.py``
scripts and repo-root ``test_*.py`` integration scripts are unaffected.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── sys.path bootstrap ─────────────────────────────────────────────────
# Make src/ importable as a top-level package so `import main`,
# `import config`, etc. resolve from inside any test in tests/unit/.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# ── .env bootstrap ─────────────────────────────────────────────────────
# Load .env so config.py's `os.getenv(...)` calls find the developer's local
# credentials. Safe when .env is absent — load_dotenv() returns False silently
# and config.py falls back to the empty-string defaults.
load_dotenv(_REPO_ROOT / ".env")
