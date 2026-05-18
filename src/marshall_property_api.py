"""Marshall County, AL property search via AssuranceWeb (countygovservices.com).

Wraps the public ASP.NET MVC search form at:
  https://marshall.countygovservices.com/property/Property/Search

The form is a Kendo Grid backed by an inline-data response: the POST body returns
HTML containing the full result set as JSON-encoded record objects embedded in
the Kendo Grid initialization script. No Playwright, no CAPTCHA, no login.

Used by the probate enrichment pipeline to map a decedent (or PR) name to one
or more parcels in Marshall County. Caller searches by owner name, gets back
parcel ID, situs address, owner-of-record, total tax, and delinquency status.

CLI usage:
    python src/marshall_property_api.py SMITH
    python src/marshall_property_api.py FULENWIDER ORVELENE
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, asdict
from typing import Iterator

import httpx

from al_tax_year import current_al_tax_year

logger = logging.getLogger(__name__)

BASE_URL = "https://marshall.countygovservices.com/property"
SEARCH_FORM_URL = f"{BASE_URL}/Property/Search"

# ── Internal regexes ──────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
)

# Each property record in the response is an inline JSON object starting with
# {"Selected":false,"ParcelInfoID":N,...} and ending with "PropertyType":"Real"}
# (or "Personal"). We extract them as raw JSON strings to parse individually.
_RECORD_RE = re.compile(
    r'\{"Selected":false,"ParcelInfoID":\d+[^\n]*?"PropertyType":"(?:Real|Personal)"\}'
)


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarshallPropertyRecord:
    """One row from the Marshall County property search."""
    parcel_number: str       # Formatted: "16-08-34-0-000-016.007"
    parcel_id: str           # Internal ParcelInfoID (use for cross-refs)
    pin: str                 # Short numeric PIN
    account: str             # Tax account number
    owner_name: str          # "SMITH, ELBERT D & KIM M" (LAST, FIRST format)
    situs_address: str       # Property street address (no city/zip — Marshall only fills street)
    tax_year: int
    total_tax: float
    balance_due: float       # > 0 means delinquent
    is_delinquent: bool      # convenience flag
    property_type: str       # "Real" or "Personal"
    property_use: str        # Friendlier label for cross-county consistency ("Real Property" | "Personal")
    is_buildable: bool       # situs has a non-zero house # (filters out "0 STREET" vacant lots)
    municipality: str        # Always "" for Marshall — AssuranceWeb search response doesn't expose city/municipality (would require a per-parcel detail fetch). Kept for cross-county field parity with Jefferson.

    @classmethod
    def from_kendo_record(cls, raw: dict) -> "MarshallPropertyRecord":
        balance = float(raw.get("BalanceDue") or 0)
        situs = (raw.get("PhysAddress") or "").strip()
        # Marshall records vacant lots with a leading "0 " in the address; anything
        # else has a real house number → likely buildable / has a structure.
        buildable = bool(situs) and not situs.startswith("0 ")
        ptype = (raw.get("PropertyType") or "").strip()
        # Marshall's search response only returns Real vs. Personal; map to a
        # friendly cross-county label. Real-property records will all read
        # "Real Property" — the residential/commercial distinction is on a
        # follow-up parcel-detail page we don't fetch in bulk.
        prop_use = "Real Property" if ptype == "Real" else (ptype or "")
        return cls(
            parcel_number=raw.get("ParcelNumberFormatted") or "",
            parcel_id=str(raw.get("ParcelInfoID") or ""),
            pin=str(raw.get("PIN") or ""),
            account=raw.get("Account") or "",
            owner_name=raw.get("FullName") or "",
            situs_address=situs,
            tax_year=int(raw.get("tyYEAR") or 0),
            total_tax=float(raw.get("TotalTax") or 0),
            balance_due=balance,
            is_delinquent=balance > 0,
            property_type=ptype,
            property_use=prop_use,
            is_buildable=buildable,
            municipality="",  # Not in AssuranceWeb search response; see field comment above
        )


# ── HTTP layer ───────────────────────────────────────────────────────


def _new_client(timeout: float = 30.0) -> httpx.Client:
    """Create an httpx client with browser-ish headers."""
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _get_form_token(client: httpx.Client) -> str:
    """GET the search form, populate session cookies, return the CSRF token."""
    r = client.get(SEARCH_FORM_URL)
    r.raise_for_status()
    m = _TOKEN_RE.search(r.text)
    if not m:
        raise RuntimeError("AssuranceWeb form changed: no __RequestVerificationToken")
    return m.group(1)


def _post_search(
    client: httpx.Client,
    token: str,
    *,
    search_type: str,           # "name" | "address" | "parcel" | etc.
    criteria1: str,
    criteria2: str = "",
    year: int = 2025,
    use_contains: bool = False,
) -> str:
    """POST the search form. Returns the response body (HTML with embedded data).

    Generic over search modes:
      name    — Criteria1=LAST, Criteria2=FIRST (optional)
      address — Criteria1=street number, Criteria2=street name root
      parcel  — Criteria1=parcel number, Criteria2 unused
      etc.
    """
    r = client.post(
        SEARCH_FORM_URL,
        data={
            "PropertySearchYear": str(year),
            "PropertySearchType": search_type,
            "UseContains": "True" if use_contains else "False",
            "SearchCriteria.Criteria1": criteria1,
            "SearchCriteria.Criteria2": criteria2,
            "SelectedParcels": "",
            "__RequestVerificationToken": token,
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SEARCH_FORM_URL,
            "Origin": BASE_URL,
        },
    )
    r.raise_for_status()
    return r.text


def _iter_records(body: str) -> Iterator[dict]:
    """Yield each property record decoded from the response body."""
    for raw in _RECORD_RE.findall(body):
        try:
            yield json.loads(raw)
        except json.JSONDecodeError as e:
            logger.debug("skip malformed Kendo record: %s", e)


# ── Public API ───────────────────────────────────────────────────────


def search_by_owner_name(
    last_name: str,
    first_name: str | None = None,
    *,
    year: int | None = None,
    use_contains: bool = False,
    real_property_only: bool = True,
) -> list[MarshallPropertyRecord]:
    """Search Marshall County properties by owner name.

    Args:
        last_name: Required. Exact match by default; set ``use_contains`` for substring.
        first_name: Optional. Helps narrow when surname is common (1,800+ for SMITH).
        year: Tax year. Defaults to ``current_al_tax_year()`` — last year's
            roll before the May auction, current year after.
        use_contains: True → "last name contains"; False → "starts with".
        real_property_only: Drop personal-property records (typically what you want).

    Returns:
        List of MarshallPropertyRecord. Empty list if no matches.

    Raises:
        httpx.HTTPError on network/server failures (after retries exhausted).
        RuntimeError if the form's CSRF token can't be located.

    Retries up to 2 times with backoff on transient HTTP/server flake.
    Matches the Madison adapter pattern — see madison_property_api for
    the full rationale (live runs showed same name returning records on
    one call then nothing on the next due to silently-swallowed errors).
    """
    if not last_name or not last_name.strip():
        raise ValueError("last_name is required")
    if year is None:
        year = current_al_tax_year()

    import time as _time

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with _new_client() as client:
                token = _get_form_token(client)
                body = _post_search(
                    client, token,
                    search_type="name",
                    criteria1=last_name.strip().upper(),
                    criteria2=(first_name or "").upper(),
                    year=year,
                    use_contains=use_contains,
                )
            if len(body) < 500:
                raise RuntimeError(
                    f"Marshall search returned suspiciously short body "
                    f"({len(body)} chars) — treating as transient failure",
                )
            break
        except (httpx.HTTPError, RuntimeError) as e:
            last_exc = e
            if attempt < 2:
                sleep_s = 1.0 if attempt == 0 else 3.0
                logger.warning(
                    "Marshall search attempt %d/3 failed for %r %r: %s — "
                    "retrying in %.1fs",
                    attempt + 1, last_name, first_name or "", e, sleep_s,
                )
                _time.sleep(sleep_s)
            else:
                logger.error(
                    "Marshall search exhausted retries for %r %r: %s",
                    last_name, first_name or "", e,
                )
                raise

    records = [
        MarshallPropertyRecord.from_kendo_record(r)
        for r in _iter_records(body)
    ]
    if real_property_only:
        records = [r for r in records if r.property_type == "Real"]
    return records


# Pattern: drop common street suffixes + directionals from the end of a
# street string so the search uses just the street-name root. AssuranceWeb's
# address mode prefers a short name root over the full street designation.
_STREET_TRAILER_RE = re.compile(
    r"\s+(?:"
    r"NE|NW|SE|SW|N|S|E|W|"
    r"AVE?|AVENUE|BLVD|CIR|CIRCLE|CT|COURT|DR|DRIVE|HWY|HIGHWAY|"
    r"LN|LANE|LOOP|PARK|PKWY|PARKWAY|PL|PLACE|RD|ROAD|"
    r"ST|STREET|TRL|TRAIL|WAY|TER|TERRACE|CV|COVE|CREEK|RUN"
    r")\.?$",
    re.IGNORECASE,
)

# Marshall's assessor stores numbered streets in digit-ordinal form ("10TH AVE",
# "9TH AVE") but the unsafe-building PDF (and human-typed addresses) often use
# spelled-out forms ("Tenth", "Ninth"). Normalize before searching.
_ORDINAL_TO_DIGIT = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th", "eleventh": "11th", "twelfth": "12th",
    "thirteenth": "13th", "fourteenth": "14th", "fifteenth": "15th",
    "sixteenth": "16th", "seventeenth": "17th", "eighteenth": "18th",
    "nineteenth": "19th", "twentieth": "20th",
}


def search_by_situs_address(
    street_number: str,
    street_name: str,
    *,
    year: int | None = None,
    real_property_only: bool = True,
) -> list[MarshallPropertyRecord]:
    """Search Marshall County properties by situs (property) address.

    AssuranceWeb's address mode takes the street number and street name as
    two separate criteria fields. The street name should be the *root* word
    (e.g. ``"Boswell"`` for ``"Boswell Dr Nw"``) — passing the full street
    designation often returns zero results because the assessor stores street
    names without the suffix in the searchable index.

    Args:
        street_number: House/street number ("3042"). Required.
        street_name: Street name root ("Boswell"). Suffixes/directionals are
            stripped automatically if you pass the full string.
        year: Tax year. Defaults to ``current_al_tax_year()``.
        real_property_only: Drop personal-property records.

    Returns:
        List of MarshallPropertyRecord. Empty if no matches.

    Raises:
        ValueError if street_number or street_name is empty.
    """
    if not street_number or not str(street_number).strip():
        raise ValueError("street_number is required")
    if not street_name or not street_name.strip():
        raise ValueError("street_name is required")
    if year is None:
        year = current_al_tax_year()

    cleaned = street_name.strip()
    # Strip unit / apt / parenthetical notation that confuses the assessor index:
    #   "Cerro Vista St Sw Unit D #Unit D" → "Cerro Vista St Sw"
    #   "Boxwood Dr Nw (unit A-D) #unit A-D" → "Boxwood Dr Nw"
    cleaned = re.sub(r"\s*\([^)]*\).*$", "", cleaned)            # drop "(...)..." tail
    cleaned = re.sub(r"\s*#.*$", "", cleaned)                    # drop "#..." tail
    cleaned = re.sub(r"\s+(?:unit|apt|suite|ste|#)\s+\S+.*$",
                     "", cleaned, flags=re.IGNORECASE)           # drop "Unit X..." tail

    # Reduce "Boswell Dr Nw" → "Boswell" by repeatedly stripping known trailers
    while True:
        new = _STREET_TRAILER_RE.sub("", cleaned).strip()
        if new == cleaned:
            break
        cleaned = new

    # Spelled-out ordinal → digit form ("Tenth" → "10th") so it matches the
    # assessor's index. Only applies when the entire root is one ordinal word.
    if cleaned.lower() in _ORDINAL_TO_DIGIT:
        cleaned = _ORDINAL_TO_DIGIT[cleaned.lower()]

    with _new_client() as client:
        token = _get_form_token(client)
        body = _post_search(
            client, token,
            search_type="address",
            criteria1=str(street_number).strip(),
            criteria2=cleaned,
            year=year,
            use_contains=False,
        )

    records = [
        MarshallPropertyRecord.from_kendo_record(r)
        for r in _iter_records(body)
    ]
    if real_property_only:
        records = [r for r in records if r.property_type == "Real"]
    return records


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if not argv:
        print("Usage: python marshall_property_api.py LASTNAME [FIRSTNAME]", file=sys.stderr)
        return 2

    last = argv[0]
    first = argv[1] if len(argv) > 1 else None

    print(f"Searching Marshall County for owner: {last}{f', {first}' if first else ''}")
    records = search_by_owner_name(last, first)
    print(f"Found {len(records)} real-property records.\n")

    for r in records[:20]:
        flag = " [DELINQUENT $%.2f]" % r.balance_due if r.is_delinquent else ""
        print(f"  {r.parcel_number}  {r.owner_name:40s}  {r.situs_address}{flag}")

    if len(records) > 20:
        print(f"\n  ... {len(records) - 20} more records suppressed")

    if records:
        print("\nFirst record (full):")
        print(json.dumps(asdict(records[0]), indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
