"""Foreclosure owner-name cache — cross-source enrichment fallback.

Problem this solves (identified 2026-07-13):

    The trustee-portal foreclosure adapters (Rubin Lublin, Tiffany & Bosco
    pending, T&B Sales Results, Halliday Watkins Mann) each enrich owner
    names via a **county property API address lookup** (Jefferson E-Ring,
    Madison + Marshall AssuranceWeb). Coverage is ~26-30% across all 3
    primary adapters — the other 70-74% of records have empty owner_name,
    which cascades: no Tracerfy skip-trace → no phones → no Trestle scoring
    → the DataSift comment shows "Foreclosure | Auction: X | Case#: Y"
    with no PHONE TIERS section.

    Meanwhile, main.py daily (APN scrape) extracts owner names from the
    mortgage notice body itself ("executed by X") — the MORTGAGOR, who is
    the person actually losing the house — via LLM-augmented regex. That
    hit rate is 80-90%. Cross-source dedup at DataSift upload time means
    dual-source properties (published in both APN and a trustee portal)
    keep the APN-derived phones. But **for properties T&B/RL/HWM find
    BEFORE they publish to APN** (which happens ~10-20% of the time —
    trustee portals list ahead of the statutory publication window),
    there's no APN record to fall back on.

Fix (this module):

    Maintain a rolling 90-day address→owner_name cache populated from
    APN daily foreclosure output. When a trustee adapter's property-API
    lookup misses, look up the same address in the cache. When it hits,
    populate ``owner_name`` and continue through the standard skip-trace
    chain. Cost: negligible — the cache is a small JSON file (~1KB per 5
    records × 90 days × ~30 records/day ≈ 500KB steady state).

    The cache is INTENTIONALLY unidirectional: only APN populates it,
    only trustee adapters consume it. This prevents feedback loops where
    a poor property-API name would replace a good APN name. APN is the
    ground truth because it reads the mortgage document itself.

Cache file: ``output/observability/foreclosure_owner_cache.json``

    {
        "<normalized_key>": {
            "owner_name": "MCDANIEL, ENNIS L",
            "first_seen": "2026-05-22",
            "last_seen":  "2026-07-13",
            "source":     "apn"
        }
    }

Key normalization (see ``_normalize_key``): uppercase, punctuation stripped,
trailing directional/suffix normalized ("Rd" → "ROAD"), whitespace
collapsed, city+ZIP5 appended for disambiguation.

Usage — from a trustee adapter:

    if not rec.owner_name:
        cached = owner_cache.lookup(rec.address, rec.city, rec.zipcode)
        if cached and cached.get("owner_name"):
            rec.owner_name = cached["owner_name"]

Or bulk:

    filled = owner_cache.fill_missing_owners(
        records, address_attr="address", city_attr="city",
        zip_attr="zipcode", owner_attr="owner_name",
    )
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_CACHE_PATH = (
    Path(__file__).parent.parent / "output" / "observability" /
    "foreclosure_owner_cache.json"
)
_PRUNE_DAYS = 90

# Common street-suffix normalizations — USPS standard abbreviations mapped
# to a canonical form. Applied AFTER uppercase + punctuation strip.
_SUFFIX_MAP = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "AV": "AVE",
    "BOULEVARD": "BLVD",
    "DRIVE": "DR",
    "ROAD": "RD",
    "LANE": "LN",
    "COURT": "CT",
    "CIRCLE": "CIR",
    "PLACE": "PL",
    "PARKWAY": "PKWY",
    "HIGHWAY": "HWY",
    "TERRACE": "TER",
    "TRAIL": "TRL",
    "SQUARE": "SQ",
    "WAY": "WAY",
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}

_PUNCT_RE = re.compile(r"[.,;:()\'\"\-#]")
_WS_RE = re.compile(r"\s+")


def _normalize_key(address: str, city: str, zip5: str) -> str:
    """Canonical address key for cache lookups.

    Format: ``"<street tokens>|<CITY>|<ZIP5>"``.

    Uses USPS-CASS-style suffix abbreviations so that "4030 McClanahan Road,
    Bessemer, 35022" and "4030 McClanahan Rd, BESSEMER, 35022-0000" match.
    Directionals (NW/SW/etc.) are preserved because they're often part of
    the actual address discriminator (e.g. "5603 5th Street South" vs
    "5603 5th Street North" are distinct properties on grid streets).
    """
    if not address:
        return ""

    a = address.upper()
    a = _PUNCT_RE.sub(" ", a)
    a = _WS_RE.sub(" ", a).strip()

    # Normalize suffix tokens (last 1-2 tokens after the house number)
    tokens = a.split(" ")
    normalized = []
    for tok in tokens:
        normalized.append(_SUFFIX_MAP.get(tok, tok))

    a = " ".join(normalized)

    c = (city or "").upper().strip()
    c = _PUNCT_RE.sub("", c)
    c = _WS_RE.sub(" ", c).strip()

    z = (zip5 or "").strip()[:5]

    return f"{a}|{c}|{z}"


# ── Persistence ──────────────────────────────────────────────────────


def load() -> dict[str, dict[str, Any]]:
    """Load the on-disk cache. Returns {} if missing/corrupt."""
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Owner cache load failed (%s); starting fresh", e)
        return {}


def save(cache: dict[str, dict[str, Any]]) -> None:
    """Persist the cache to disk (creates parent dir if needed)."""
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def prune(cache: dict[str, dict[str, Any]], *, days: int = _PRUNE_DAYS,
          ) -> tuple[dict, int]:
    """Drop entries whose ``last_seen`` is older than ``days`` ago.

    Returns (pruned_cache, dropped_count).
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    kept: dict[str, dict[str, Any]] = {}
    dropped = 0
    for k, v in cache.items():
        last_seen = v.get("last_seen") or v.get("first_seen") or ""
        if isinstance(last_seen, str) and last_seen >= cutoff:
            kept[k] = v
        else:
            dropped += 1
    return kept, dropped


# ── Lookup ───────────────────────────────────────────────────────────


def lookup(address: str, city: str, zip_code: str,
           cache: dict[str, dict[str, Any]] | None = None,
           ) -> dict[str, Any] | None:
    """Look up a single address → cached owner entry.

    Returns the cached dict (with owner_name, last_seen, source) or None.
    Passing ``cache`` avoids re-reading disk on repeated calls.
    """
    if not address:
        return None
    c = cache if cache is not None else load()
    key = _normalize_key(address, city, zip_code)
    if not key:
        return None
    return c.get(key)


def fill_missing_owners(
    records: list,
    *,
    address_attr: str = "address",
    city_attr: str = "city",
    zip_attr: str = "zipcode",
    owner_attr: str = "owner_name",
) -> int:
    """Populate ``owner_attr`` on records where it's currently empty.

    Returns the count of records filled. Attribute names default to the
    convention used by Rubin Lublin, T&B pending, and HWM record classes
    (``address`` / ``city`` / ``zipcode`` / ``owner_name``). T&B Results
    and any NoticeData-based flow can override ``zip_attr="zip"``.
    """
    if not records:
        return 0

    cache = load()
    if not cache:
        return 0

    filled = 0
    for r in records:
        current_owner = (getattr(r, owner_attr, "") or "").strip()
        if current_owner:
            continue

        addr = getattr(r, address_attr, "") or ""
        city = getattr(r, city_attr, "") or ""
        zip5 = getattr(r, zip_attr, "") or ""
        cached = lookup(addr, city, zip5, cache=cache)
        if cached and cached.get("owner_name"):
            setattr(r, owner_attr, cached["owner_name"])
            filled += 1
            logger.info(
                "Owner cache HIT: %r → %r (source=%s, last_seen=%s)",
                addr, cached["owner_name"], cached.get("source", "?"),
                cached.get("last_seen", "?"),
            )
    return filled


# ── Update from APN daily foreclosure output ─────────────────────────


def _compose_owner(col_first_field: str, col_last_field: str) -> str:
    """Compose the owner_name string from the two DataSift CSV columns.

    Quirk of the current data_formatter → datasift_formatter path
    (verified 2026-07-13 against production output): the column LABELED
    "Owner First Name" (col 5) actually holds the SURNAME, and the column
    labeled "Owner Last Name" (col 6) holds the given name(s). E.g.:

        Owner First Name = "MARONEY"
        Owner Last Name  = "CHRISSY"

    We preserve that order (surname first) which happens to match the
    tax-roll "LAST, FIRST" convention that trustee adapters + Tracerfy
    already normalize against. Fixing the header vs data mismatch is
    a separate cleanup — for cache purposes we take the data as-is so
    downstream matches remain consistent.
    """
    a = (col_first_field or "").strip()
    b = (col_last_field or "").strip()
    if a and b:
        return f"{a}, {b}"
    return a or b


def update_from_datasift_csv(
    csv_path: str | Path,
    cache: dict[str, dict[str, Any]] | None = None,
    *,
    source: str = "apn",
    today_iso: str | None = None,
) -> tuple[dict, int, int]:
    """Ingest owner names from a DataSift-format foreclosure CSV into the cache.

    Uses the standard SiftStack DataSift column layout:
      col 1: Property Street Address
      col 2: Property City
      col 4: Property ZIP Code
      col 5: Owner First Name
      col 6: Owner Last Name

    Only rows with a non-empty owner name are ingested. Rows without an
    address or owner are skipped silently. Existing cache entries get
    their ``last_seen`` refreshed; new addresses land as new entries.

    Returns (updated_cache, added_count, refreshed_count).
    """
    if cache is None:
        cache = load()
    today = today_iso or date.today().isoformat()

    added = 0
    refreshed = 0
    p = Path(csv_path)
    if not p.exists():
        logger.warning("Owner cache update: CSV %s not found", p)
        return cache, 0, 0

    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return cache, 0, 0

        # Find the columns by header name — the DataSift schema is stable
        # but we key on names rather than indices in case of future shifts.
        try:
            col_addr = header.index("Property Street Address")
            col_city = header.index("Property City")
            col_zip = header.index("Property ZIP Code")
            col_first = header.index("Owner First Name")
            col_last = header.index("Owner Last Name")
        except ValueError as e:
            logger.warning("Owner cache update: CSV header mismatch: %s", e)
            return cache, 0, 0

        for row in reader:
            if len(row) <= max(col_addr, col_city, col_zip, col_first, col_last):
                continue
            addr = row[col_addr].strip()
            city = row[col_city].strip()
            zip5 = row[col_zip].strip()[:5]
            owner = _compose_owner(row[col_first], row[col_last])
            if not addr or not owner:
                continue

            key = _normalize_key(addr, city, zip5)
            if not key:
                continue

            if key in cache:
                # Refresh last_seen — preserve first_seen + owner (APN
                # authoritative; do not overwrite unless the current entry
                # is empty).
                cache[key]["last_seen"] = today
                if not cache[key].get("owner_name"):
                    cache[key]["owner_name"] = owner
                refreshed += 1
            else:
                cache[key] = {
                    "owner_name": owner,
                    "first_seen": today,
                    "last_seen": today,
                    "source": source,
                }
                added += 1

    return cache, added, refreshed
