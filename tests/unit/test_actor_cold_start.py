"""Regression net for BUGFIX-02 — Apify Actor cold-start ``AttributeError``.

Bug class: a credential refactor (the AL migration) removed
``config.TNPN_EMAIL`` / ``config.TNPN_PASSWORD`` from ``src/config.py`` but
left a validator inside ``actor_main()`` that still referenced them. Every
scheduled Apify run crashed at attribute access (``config.TNPN_EMAIL``)
*before* the scraper ever started — silent on the CLI path (which never
exercises ``actor_main()``), fatal on the Actor path.

Why static source scans, not a live ``actor_main()`` invocation:
The original failure mode was a dead attribute reference surviving a config
refactor. To catch it at the unit-test layer without spinning up the Apify
Actor SDK + an event loop, the right test shape is a regex source scan
against ``src/main.py`` — it's deterministic, offline, cheap, and pinpoints
the exact regression (someone copy-pasting the dead validator back, or
adding ``tn_username`` / ``tn_password`` to ``_cred_map`` "for backward
compat"). Combined with an ``import main`` smoke test, this covers the bug
class fully without paying for an Apify run.

See ``.planning/codebase/CONCERNS.md`` (BUGFIX-02) and
``.planning/phases/01-stabilize-production/01-bugfix-02-PLAN.md`` for the
full background and the four guard shapes documented below.
"""

import importlib
import re
from pathlib import Path


# ── Shared source-text reader ──────────────────────────────────────────
# Read src/main.py once and strip comment-only lines so a historical
# breadcrumb like `# TNPN_EMAIL used to be here` (or the existing comment
# block at lines 183-187 explaining the bug) doesn't self-trip the regex.
# Inline-comment hygiene: a line whose first non-whitespace character is
# `#` is a comment-only line; partial comments after code remain in scope
# because forbidden tokens inside live code are exactly what we want to
# catch.

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MAIN_PATH = _REPO_ROOT / "src" / "main.py"


def _read_main_source_stripped() -> str:
    """Return src/main.py source with comment-only lines removed."""
    raw_lines = _MAIN_PATH.read_text(encoding="utf-8").splitlines()
    code_lines = [ln for ln in raw_lines if not ln.lstrip().startswith("#")]
    return "\n".join(code_lines)


# ── Guard 1: forbidden config.TNPN_* references ────────────────────────


def test_no_config_tnpn_references_in_main():
    """src/main.py must not reference config.TNPN_EMAIL or config.TNPN_PASSWORD.

    This catches the exact BUGFIX-02 regression: a future maintainer
    copy-pastes the dead Tennessee-era validator back into ``actor_main()``
    after a credential refactor.

    The scan strips comment-only lines first so the existing comment block
    at ``src/main.py:183-187`` (which explains the historical bug and
    mentions ``TNPN_EMAIL`` / ``TNPN_PASSWORD`` by name) doesn't self-trip
    the regex.
    """
    source = _read_main_source_stripped()
    matches = re.findall(r"config\.TNPN_(?:EMAIL|PASSWORD)", source)
    assert not matches, (
        "src/main.py references config.TNPN_EMAIL / TNPN_PASSWORD which "
        "were deleted from config.py during the AL migration. This is the "
        "BUGFIX-02 cold-start crash — every Apify scheduled run will die at "
        "attribute access before scraping. Found: %r" % (matches,)
    )


# ── Guard 2: _cred_map must not advertise tn_username / tn_password ────


def test_cred_map_does_not_accept_legacy_tn_keys():
    """_cred_map in actor_main() must not advertise tn_username / tn_password.

    Symmetric regression: someone updates ``_cred_map`` to "accept" the old
    TN keys for backward compat. Those keys would be silently dropped by
    the loop that writes ``os.environ`` (since ``config`` has no matching
    attribute to set), giving a false sense that credentials flowed through.

    Implementation: find every ``actor_input.get("KEY", ...)`` call in
    src/main.py and assert the forbidden keys are absent.
    """
    source = _read_main_source_stripped()
    keys = re.findall(r"""actor_input\.get\(\s*["']([^"']+)["']""", source)
    assert "tn_username" not in keys, (
        "src/main.py advertises 'tn_username' as an Actor-input key. "
        "config.py no longer defines TNPN_* attributes, so this key would "
        "be silently dropped — confusing the operator who set it. Use the "
        "AL-era credentials only."
    )
    assert "tn_password" not in keys, (
        "src/main.py advertises 'tn_password' as an Actor-input key. "
        "config.py no longer defines TNPN_* attributes, so this key would "
        "be silently dropped — confusing the operator who set it. Use the "
        "AL-era credentials only."
    )


# ── Guard 3: config module must not define TNPN_* attributes ───────────


def test_config_module_has_no_tnpn_attributes():
    """src/config.py must not define TNPN_EMAIL or TNPN_PASSWORD.

    Symmetric regression in the OTHER direction: someone re-adds the
    constants to ``config.py`` "so old code paths still work". The intent
    of the AL migration is that those constants are gone, and any new
    reference to them is a sign that pre-migration TN code is being
    revived (the user is on Alabama-only since 2026-04).
    """
    config = importlib.import_module("config")
    assert not hasattr(config, "TNPN_EMAIL"), (
        "config.TNPN_EMAIL was re-added after the AL migration removed it. "
        "If this is intentional (e.g. resurrecting TN coverage), remove "
        "this guard along with the matching guards in main.py."
    )
    assert not hasattr(config, "TNPN_PASSWORD"), (
        "config.TNPN_PASSWORD was re-added after the AL migration removed "
        "it. If this is intentional, remove this guard."
    )


# ── Guard 4: src/main.py must import without AttributeError ────────────


def test_main_module_imports_without_attribute_error():
    """``import main`` must succeed cleanly.

    The original BUGFIX-02 bug had ``config.TNPN_EMAIL`` referenced inside
    the body of ``actor_main()`` (a function), so the AttributeError fired
    only when the Actor SDK actually called the function — not at import
    time. This test pins down a STRONGER property: even if a future
    refactor hoists a credential reference to module level (e.g. as a
    default argument: ``def foo(x=config.TNPN_EMAIL): ...``), the import
    itself fails.

    This is the unit-level proxy for "the Apify worker can boot the file
    at all", which was the entire failure mode the original bug produced.
    Actually invoking ``actor_main()`` would require the Apify SDK +
    asyncio + a mocked KVS — that's integration-level and out of scope
    here.
    """
    # importlib.reload guards against an earlier test (or pytest's
    # collection phase) having already cached main — we want this to
    # exercise the import path freshly.
    import main  # noqa: F401  (import is the assertion)
    importlib.reload(main)
