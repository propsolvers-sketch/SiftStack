"""Unit tests for Slack Block Kit helpers appended to ``src/slack_notifier.py``.

Covers ``build_funnel_block`` (per-pipeline funnel section) and
``build_service_rates_block`` (4-service today + 7-day rate section).

These are PURE FUNCTIONS — they construct a dict that the caller appends
to a ``blocks`` list before invoking ``_send_webhook``. There is no HTTP
call in this test file. The ``requests`` library is not imported. No
network access is attempted.

Block Kit shape (per Slack docs):
    {"type": "section", "text": {"type": "mrkdwn", "text": "..."}}
"""

from __future__ import annotations

from collections import OrderedDict

from slack_notifier import build_funnel_block, build_service_rates_block


# ── build_funnel_block ───────────────────────────────────────────────


def test_build_funnel_block_snapshot() -> None:
    """The block has the documented Block Kit shape and contains every gate."""
    gate_counts = OrderedDict(
        [("scraped", 47), ("deduped", 42), ("tier_gated", 12), ("uploaded", 3)]
    )
    block = build_funnel_block("apn_probate", gate_counts)

    assert block["type"] == "section"
    assert block["text"]["type"] == "mrkdwn"
    text = block["text"]["text"]

    # Header includes the pipeline name
    assert "apn_probate" in text
    assert "Funnel" in text

    # Every gate name + count is rendered
    assert "scraped" in text
    assert "47" in text
    assert "deduped" in text
    assert "42" in text
    assert "tier_gated" in text
    assert "12" in text
    assert "uploaded" in text
    assert "3" in text


def test_build_funnel_block_preserves_order() -> None:
    """Gate lines appear in INSERTION order, not sorted alphabetically (D-01)."""
    gate_counts = OrderedDict(
        [("scraped", 100), ("uploaded", 5), ("deduped", 80)]
    )
    block = build_funnel_block("benchmark", gate_counts)
    text = block["text"]["text"]

    # Find each gate's position in the rendered text; insertion order
    # (scraped → uploaded → deduped) must hold even though alphabetic
    # order would be (deduped → scraped → uploaded).
    pos_scraped = text.index("scraped")
    pos_uploaded = text.index("uploaded")
    pos_deduped = text.index("deduped")

    assert pos_scraped < pos_uploaded < pos_deduped


def test_build_funnel_block_returns_plain_dict() -> None:
    """Return value is a plain dict — JSON-serialisable, no slack_sdk model."""
    import json

    block = build_funnel_block("p", OrderedDict([("a", 1)]))
    # Must be JSON-dumpable without a custom encoder
    serialised = json.dumps(block)
    assert "section" in serialised
    assert "mrkdwn" in serialised


def test_build_funnel_block_accepts_plain_dict_too() -> None:
    """Python 3.7+ dict preserves insertion order; both dict and OrderedDict work."""
    block = build_funnel_block(
        "tax_distress",
        {"bulk_fetched": 600, "individual_owner": 420, "tier_gated": 38},
    )
    text = block["text"]["text"]
    assert "bulk_fetched" in text
    assert "600" in text


def test_build_funnel_block_renders_zero_counts() -> None:
    """A gate at 0 still renders — pre-seeded gates surface as zero drops."""
    block = build_funnel_block("p", OrderedDict([("scraped", 10), ("matched", 0)]))
    text = block["text"]["text"]
    assert "matched" in text
    assert ": 0" in text


# ── build_service_rates_block ────────────────────────────────────────


def test_build_service_rates_block_renders_both_today_and_rolling() -> None:
    """Every service line shows today's rate AND the 7-day rolling rate."""
    per_run = {"2captcha": 1.0, "smarty": 0.857, "tracerfy": 0.33, "llm": 0.95}
    rolling = {"2captcha": 0.99, "smarty": 0.92, "tracerfy": 0.41, "llm": 0.96}

    block = build_service_rates_block(per_run, rolling)
    assert block["type"] == "section"
    assert block["text"]["type"] == "mrkdwn"
    text = block["text"]["text"]

    # Header
    assert "Service Rates" in text

    # 2Captcha: 100% today | 99% 7-day
    assert "2Captcha" in text
    assert "100% today" in text
    assert "99% 7-day" in text

    # Smarty: 86% today | 92% 7-day  (round to nearest int)
    assert "Smarty" in text
    assert "86% today" in text
    assert "92% 7-day" in text

    # Tracerfy: 33% today | 41% 7-day
    assert "Tracerfy" in text
    assert "33% today" in text
    assert "41% 7-day" in text

    # LLM: 95% today | 96% 7-day
    assert "LLM" in text
    assert "95% today" in text
    assert "96% 7-day" in text


def test_build_service_rates_block_handles_none_values() -> None:
    """per_run==None renders 'n/a today'; rolling==None renders '— 7-day'."""
    per_run = {"2captcha": 1.0, "smarty": 0.85, "tracerfy": None, "llm": 0.95}
    rolling = {"2captcha": 0.99, "smarty": 0.92, "tracerfy": 0.41, "llm": None}

    block = build_service_rates_block(per_run, rolling)
    text = block["text"]["text"]

    # tracerfy today is None → "n/a today"
    assert "Tracerfy: n/a today" in text
    # llm rolling is None → "— 7-day" (em dash)
    assert "— 7-day" in text


def test_build_service_rates_block_display_order() -> None:
    """Service order is fixed: 2Captcha, Smarty, Tracerfy, LLM."""
    per_run = {"2captcha": 1.0, "smarty": 1.0, "tracerfy": 1.0, "llm": 1.0}
    rolling = {"2captcha": 1.0, "smarty": 1.0, "tracerfy": 1.0, "llm": 1.0}

    block = build_service_rates_block(per_run, rolling)
    text = block["text"]["text"]

    # Order must be 2Captcha < Smarty < Tracerfy < LLM (by text index)
    pos_2captcha = text.index("2Captcha")
    pos_smarty = text.index("Smarty")
    pos_tracerfy = text.index("Tracerfy")
    pos_llm = text.index("LLM")

    assert pos_2captcha < pos_smarty < pos_tracerfy < pos_llm


def test_build_service_rates_block_missing_service_treated_as_none() -> None:
    """A service absent from the input dicts renders as if its value were None."""
    # Pass empty dicts — every service should still appear (display order
    # is determined by the module-level _RATE_DISPLAY_ORDER constant,
    # NOT by the caller's dict keys).
    block = build_service_rates_block({}, {})
    text = block["text"]["text"]

    assert "2Captcha" in text
    assert "Smarty" in text
    assert "Tracerfy" in text
    assert "LLM" in text
    assert "n/a today" in text
    assert "— 7-day" in text


def test_build_service_rates_block_rounds_to_nearest_int() -> None:
    """0.857 → 86%, 0.855 → 86% (banker's rounding from round()), 0.999 → 100%."""
    per_run = {"2captcha": 0.999, "smarty": 0.857, "tracerfy": 0.001, "llm": 0.5}
    rolling = {"2captcha": 0.99, "smarty": 0.92, "tracerfy": 0.41, "llm": 0.96}

    block = build_service_rates_block(per_run, rolling)
    text = block["text"]["text"]

    assert "100% today" in text  # 2captcha 0.999 → 100
    assert "86% today" in text  # smarty 0.857 → 86
    assert "0% today" in text  # tracerfy 0.001 → 0
    assert "50% today" in text  # llm 0.5 → 50


def test_build_service_rates_block_returns_plain_dict() -> None:
    """JSON-serialisable; no slack_sdk model objects."""
    import json

    block = build_service_rates_block({}, {})
    serialised = json.dumps(block)
    assert "Service Rates" in serialised


# ── Negative — no HTTP call attempted ─────────────────────────────────


def test_no_http_call_during_block_construction(monkeypatch) -> None:
    """Sanity-net: even if the test importer brings ``requests`` in,
    constructing a block must not call ``requests.post`` (the builders
    are pure functions; ``_send_webhook`` is the only sender)."""
    import requests

    sentinel = {"called": False}

    def _boom(*args, **kwargs):
        sentinel["called"] = True
        raise AssertionError("requests.post must not be called from block builders")

    monkeypatch.setattr(requests, "post", _boom)

    # Both builders — neither should trigger requests.post
    build_funnel_block("p", OrderedDict([("scraped", 1)]))
    build_service_rates_block(
        {"smarty": 0.9}, {"smarty": 0.9}
    )

    assert sentinel["called"] is False
