"""Vendor skip-trace tag registry.

Central naming for the 8 tags that track which vendors touched which records
and which vendor sourced each individual phone number.

Two layers:

  Record-level tags (evergreen, filter-friendly): applied to a record when a
  vendor cascade step completes, regardless of whether the vendor returned
  data. Lets DataSift filter presets ask "show me records SmartSkip has NOT
  been tried on yet" via `NOT traced_smartskip`.

    traced_tracerfy    · traced_datasift · traced_enformion · traced_smartskip

  Phone-source tags: applied to individual phone numbers to attribute origin.
  Stacks with existing Trestle tier tags (Dial First / Second / Third / Fourth
  / Drop). Useful when a phone answers and you want to know which vendor
  surfaced it — e.g. Enformion phones tend to be older mailing-address hits.

    src:tracerfy · src:datasift · src:enformion · src:smartskip

All tags are created via the existing optimistic-create pattern in
datasift_api.py — first call to a helper creates the tag if absent, then
caches per-process.

Also defines the extra state tags the SmartSkip lane uses:

    smartskip_no_match       (SmartSkip ran, returned zero owner+relative data)
    smartskip_deferred       (SmartSkip couldn't complete this pass — retry)
    smartskip_submitted      (record is currently in a SmartSkip batch — dedup guard)
    needs_4th_trace          (all standard vendors done, still weak phones)
"""
from __future__ import annotations

from typing import Iterable

import datasift_api as ds


# ─────────────────────────────────────────────────────────────────────
# Canonical tag names
# ─────────────────────────────────────────────────────────────────────

# Record-level "was traced by vendor X" — evergreen, one apply per record
RECORD_TAG_TRACED_TRACERFY  = "traced_tracerfy"
RECORD_TAG_TRACED_DATASIFT  = "traced_datasift"
RECORD_TAG_TRACED_ENFORMION = "traced_enformion"
RECORD_TAG_TRACED_SMARTSKIP = "traced_smartskip"

# Phone-level source attribution — stacks with Trestle tier tags
PHONE_TAG_SRC_TRACERFY  = "src:tracerfy"
PHONE_TAG_SRC_DATASIFT  = "src:datasift"
PHONE_TAG_SRC_ENFORMION = "src:enformion"
PHONE_TAG_SRC_SMARTSKIP = "src:smartskip"

# SmartSkip lifecycle / state tags
RECORD_TAG_SMARTSKIP_NO_MATCH  = "smartskip_no_match"
RECORD_TAG_SMARTSKIP_DEFERRED  = "smartskip_deferred"
RECORD_TAG_SMARTSKIP_SUBMITTED = "smartskip_submitted"

# Rehash flag — set when standard cascade finishes but phones are weak
RECORD_TAG_NEEDS_4TH_TRACE = "needs_4th_trace"

# Manual cascade queue — operator applies via DataSift UI to prioritize
# specific records for the next cascade run. Auto-removed after cascade
# succeeds on the record. Filter preset "Pending Cascade" = tag:queue_cascade.
RECORD_TAG_QUEUE_CASCADE = "queue_cascade"

# Tax-distress lifecycle tags (applied by tax_distress_pipeline + detect_tax_caught_up).
# tax_delinquent   — set when property is on the current county delinquent roll
# tax_caught_up    — set when property was previously delinquent but has now
#                    paid off (parcel disappeared from the current roll).
#                    Auto-removes tax_delinquent to stop tax-distress marketing.
# Operator filter presets:
#   "Currently Tax Delinquent" = tag:tax_delinquent AND NOT tag:tax_caught_up
#   "Tax Caught Up (stop tax mail)" = tag:tax_caught_up
RECORD_TAG_TAX_DELINQUENT = "tax_delinquent"
RECORD_TAG_TAX_CAUGHT_UP = "tax_caught_up"


# Group aliases for iteration
ALL_TRACED_RECORD_TAGS = (
    RECORD_TAG_TRACED_TRACERFY,
    RECORD_TAG_TRACED_DATASIFT,
    RECORD_TAG_TRACED_ENFORMION,
    RECORD_TAG_TRACED_SMARTSKIP,
)

ALL_SRC_PHONE_TAGS = (
    PHONE_TAG_SRC_TRACERFY,
    PHONE_TAG_SRC_DATASIFT,
    PHONE_TAG_SRC_ENFORMION,
    PHONE_TAG_SRC_SMARTSKIP,
)

# Map from vendor short-name to its record + phone-source tag names.
# Keys are the short-names cascades use internally (tracerfy / datasift /
# enformion / smartskip).
VENDOR_TAGS: dict[str, dict[str, str]] = {
    "tracerfy":  {"record": RECORD_TAG_TRACED_TRACERFY,  "phone": PHONE_TAG_SRC_TRACERFY},
    "datasift":  {"record": RECORD_TAG_TRACED_DATASIFT,  "phone": PHONE_TAG_SRC_DATASIFT},
    "enformion": {"record": RECORD_TAG_TRACED_ENFORMION, "phone": PHONE_TAG_SRC_ENFORMION},
    "smartskip": {"record": RECORD_TAG_TRACED_SMARTSKIP, "phone": PHONE_TAG_SRC_SMARTSKIP},
}


# ─────────────────────────────────────────────────────────────────────
# UUID helpers (thin wrappers over datasift_api's optimistic-create)
# ─────────────────────────────────────────────────────────────────────


def traced_record_tag_uuid(vendor: str) -> str:
    """Return UUID of the `traced_<vendor>` record tag, creating if absent."""
    if vendor not in VENDOR_TAGS:
        raise ValueError(f"Unknown vendor {vendor!r} — expected one of {list(VENDOR_TAGS)}")
    return ds.tag_uuid(VENDOR_TAGS[vendor]["record"], create_if_missing=True)


def src_phone_tag_uuid(vendor: str) -> str:
    """Return UUID of the `src:<vendor>` phone tag, creating if absent."""
    if vendor not in VENDOR_TAGS:
        raise ValueError(f"Unknown vendor {vendor!r} — expected one of {list(VENDOR_TAGS)}")
    return ds.phone_tag_uuid(VENDOR_TAGS[vendor]["phone"], create_if_missing=True)


def state_tag_uuid(name: str) -> str:
    """Return UUID of a SmartSkip lifecycle / rehash-state / tax-distress tag."""
    valid = {
        RECORD_TAG_SMARTSKIP_NO_MATCH,
        RECORD_TAG_SMARTSKIP_DEFERRED,
        RECORD_TAG_SMARTSKIP_SUBMITTED,
        RECORD_TAG_NEEDS_4TH_TRACE,
        RECORD_TAG_QUEUE_CASCADE,
        RECORD_TAG_TAX_DELINQUENT,
        RECORD_TAG_TAX_CAUGHT_UP,
    }
    if name not in valid:
        raise ValueError(f"Unknown state tag {name!r} — expected one of {valid}")
    return ds.tag_uuid(name, create_if_missing=True)


def ensure_all_tags_exist() -> dict[str, str]:
    """Create all 12 vendor + state tags in DataSift.

    Idempotent — safe to call at startup of any cascade script to warm the
    cache. Returns {tag_name: uuid} for all created/looked-up tags.
    """
    resolved = {}
    for vendor in VENDOR_TAGS:
        rec_name = VENDOR_TAGS[vendor]["record"]
        phone_name = VENDOR_TAGS[vendor]["phone"]
        resolved[rec_name] = traced_record_tag_uuid(vendor)
        resolved[phone_name] = src_phone_tag_uuid(vendor)
    for state in (
        RECORD_TAG_SMARTSKIP_NO_MATCH,
        RECORD_TAG_SMARTSKIP_DEFERRED,
        RECORD_TAG_SMARTSKIP_SUBMITTED,
        RECORD_TAG_NEEDS_4TH_TRACE,
        RECORD_TAG_QUEUE_CASCADE,
        RECORD_TAG_TAX_DELINQUENT,
        RECORD_TAG_TAX_CAUGHT_UP,
    ):
        resolved[state] = state_tag_uuid(state)
    return resolved


def clear_queue_cascade(property_uuid: str) -> None:
    """Remove the queue_cascade tag from a record — call after cascade finishes.

    Idempotent — silent no-op if the tag isn't on the record.
    """
    tag_uuid = state_tag_uuid(RECORD_TAG_QUEUE_CASCADE)
    try:
        ds.remove_tags(property_uuid, [tag_uuid])
    except AttributeError:
        # Older datasift_api without remove_tags — try low-level endpoint
        try:
            ds._post(f"/property/{property_uuid}/remove-tags/",
                     {"tags": [RECORD_TAG_QUEUE_CASCADE]})
        except Exception:
            pass  # Not fatal — tag will still show but record is otherwise done


# ─────────────────────────────────────────────────────────────────────
# Application helpers — apply tags to a record after cascade step
# ─────────────────────────────────────────────────────────────────────


def mark_vendor_traced(property_uuid: str, vendor: str) -> None:
    """Apply the `traced_<vendor>` tag to a record — call at end of each
    cascade step regardless of whether the vendor returned data.

    Bypasses UUID→title reverse lookup (which was failing silently on
    freshly-created tags) by POSTing the TITLE directly. DataSift's
    /property/{uuid}/add-tags/ endpoint accepts title strings and creates
    the association if the tag exists.
    """
    if vendor not in VENDOR_TAGS:
        raise ValueError(f"Unknown vendor {vendor!r}")
    title = VENDOR_TAGS[vendor]["record"]
    # Ensure tag exists in DataSift account (idempotent — cached after first call)
    ds.tag_uuid(title, create_if_missing=True)
    # POST title directly — no UUID→title round-trip
    ds._post(f"/property/{property_uuid}/add-tags/", {"tags": [title]})


def mark_state(property_uuid: str, state: str) -> None:
    """Apply one of the state tags (smartskip_no_match, needs_4th_trace, etc.)."""
    valid = {
        RECORD_TAG_SMARTSKIP_NO_MATCH,
        RECORD_TAG_SMARTSKIP_DEFERRED,
        RECORD_TAG_SMARTSKIP_SUBMITTED,
        RECORD_TAG_NEEDS_4TH_TRACE,
        RECORD_TAG_QUEUE_CASCADE,
    }
    if state not in valid:
        raise ValueError(f"Unknown state tag {state!r}")
    ds.tag_uuid(state, create_if_missing=True)
    ds._post(f"/property/{property_uuid}/add-tags/", {"tags": [state]})


def apply_phone_source(
    property_uuid: str,
    phone_uuid: str,
    vendor: str,
) -> None:
    """Apply the `src:<vendor>` tag to a specific phone number on a record."""
    tag = src_phone_tag_uuid(vendor)
    ds.add_phone_tag(property_uuid, phone_uuid, tag)


__all__ = [
    # Names
    "RECORD_TAG_TRACED_TRACERFY", "RECORD_TAG_TRACED_DATASIFT",
    "RECORD_TAG_TRACED_ENFORMION", "RECORD_TAG_TRACED_SMARTSKIP",
    "PHONE_TAG_SRC_TRACERFY", "PHONE_TAG_SRC_DATASIFT",
    "PHONE_TAG_SRC_ENFORMION", "PHONE_TAG_SRC_SMARTSKIP",
    "RECORD_TAG_SMARTSKIP_NO_MATCH", "RECORD_TAG_SMARTSKIP_DEFERRED",
    "RECORD_TAG_SMARTSKIP_SUBMITTED", "RECORD_TAG_NEEDS_4TH_TRACE",
    "ALL_TRACED_RECORD_TAGS", "ALL_SRC_PHONE_TAGS", "VENDOR_TAGS",
    # UUIDs
    "traced_record_tag_uuid", "src_phone_tag_uuid", "state_tag_uuid",
    "ensure_all_tags_exist",
    # Application
    "mark_vendor_traced", "mark_state", "apply_phone_source",
]
