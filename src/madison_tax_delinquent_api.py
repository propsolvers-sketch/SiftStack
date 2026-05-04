"""Madison County, AL bulk tax-delinquent list via AssuranceWeb.

Pulls the full list of currently-delinquent parcels in one GET request to:
    https://madisonproperty.countygovservices.com/Property/Property/DelinquentParcels

The page returns ~600 delinquent parcels per year inlined as a single JSON
array (Kendo Grid `"Data":[...]`) — no auth, no pagination, no AJAX. Roughly
one HTTP round-trip per refresh.

Each record exposes the parcel number, PIN, current owner, situs address,
balance due, assessed value, tax breakdown (gross / interest / fees / paid),
and a `TaxSaleParcel` boolean indicating whether the parcel is scheduled
for the upcoming May tax-sale auction.

Used by the daily pipeline to seed the `tax_delinquent` (and downstream
`tax_sale`) lists feeding DataSift.

CLI:
    python src/madison_tax_delinquent_api.py
    python src/madison_tax_delinquent_api.py --tax-sale-only
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)

DELINQUENT_URL = (
    "https://madisonproperty.countygovservices.com/Property/Property/DelinquentParcels"
)

# The Kendo Grid initialization on the page inlines the entire result set as
# {"Data":[ {...}, {...} ]}. We extract the Data array as a JSON literal,
# then json.loads it. The array is followed by a comma + Total/Errors keys,
# so we walk balanced brackets to find its end.
_DATA_PREFIX_RE = re.compile(r'"Data"\s*:\s*\[\s*(?=\{)')


# ── Data model ───────────────────────────────────────────────────────


# Default threshold for "high exposure" — owner owes enough that the property
# is genuinely at risk. Caller can override via fetch_delinquent_parcels(min_balance=...).
HIGH_EXPOSURE_THRESHOLD = 5000.0


@dataclass(frozen=True)
class MadisonDelinquentRecord:
    """One delinquent parcel from Madison's AssuranceWeb feed."""
    parcel_id: str               # ParcelNumberFormatted: "14-06-23-4-000-043.000"
    parcel_number_raw: str       # ParcelNumber: "1406234000043000"
    pin: str                     # pclPIN
    account: str                 # panNUMBER
    owner_name: str              # currentOwners
    previous_owner: str          # previousOwners
    situs_address: str           # parcel_address (street + house number)
    legal_description: str       # legal_desc
    tax_year: int
    balance_due: float           # pcliBALANCE — current real-time balance
    tax_sale_balance: float      # taxSaleBalance — amount owed at tax sale
    assessed_value: float        # pcliVALUE
    gross_tax: float
    interest: float
    other_fees: float
    exempt: float
    paid: float
    is_tax_sale_parcel: bool     # TaxSaleParcel — flagged for next May auction
    pcli_id: int                 # internal delinquent-list ID
    # Derived classification fields. Phase 1 focuses on dollar exposure only —
    # Madison's feed is current-year-only by design (older years pruned after
    # the May auction), so timeline-based flags aren't actionable here.
    is_individual_owner: bool    # False when owner_name matches BUSINESS_RE (LLC/Inc/Corp/etc.)
    is_high_exposure: bool       # balance_due >= HIGH_EXPOSURE_THRESHOLD ($5k by default)

    @classmethod
    def from_record(cls, raw: dict) -> "MadisonDelinquentRecord":
        # Lazy import to keep this module importable without pulling all of config
        from config import BUSINESS_RE

        owner = raw.get("currentOwners") or ""
        balance = float(raw.get("pcliBALANCE") or 0)
        tax_year = int(raw.get("tyYEAR") or 0)

        is_individual = bool(owner) and not BUSINESS_RE.search(owner)
        is_high = balance >= HIGH_EXPOSURE_THRESHOLD

        return cls(
            parcel_id=raw.get("ParcelNumberFormatted") or "",
            parcel_number_raw=raw.get("ParcelNumber") or "",
            pin=str(raw.get("pclPIN") or ""),
            account=str(raw.get("panNUMBER") or ""),
            owner_name=owner,
            previous_owner=raw.get("previousOwners") or "",
            situs_address=raw.get("parcel_address") or "",
            legal_description=raw.get("legal_desc") or "",
            tax_year=tax_year,
            balance_due=balance,
            tax_sale_balance=float(raw.get("taxSaleBalance") or 0),
            assessed_value=float(raw.get("pcliVALUE") or 0),
            gross_tax=float(raw.get("gross") or 0),
            interest=float(raw.get("interest") or 0),
            other_fees=float(raw.get("otherFees") or 0),
            exempt=float(raw.get("exempt") or 0),
            paid=float(raw.get("paid") or 0),
            is_tax_sale_parcel=bool(raw.get("TaxSaleParcel")),
            pcli_id=int(raw.get("pcliID") or 0),
            is_individual_owner=is_individual,
            is_high_exposure=is_high,
        )


# ── HTTP layer ───────────────────────────────────────────────────────


def _new_client(timeout: float = 60.0) -> httpx.Client:
    """Browser-style httpx client. Long timeout because the page is ~500 KB."""
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


def _extract_data_array(html: str) -> list[dict]:
    """Pull the {"Data":[...]} array out of the inline Kendo Grid initializer.

    Walks balanced square brackets so embedded arrays in legal descriptions
    don't break the parse.
    """
    m = _DATA_PREFIX_RE.search(html)
    if not m:
        return []
    start = m.end() - 1  # include the opening `[`
    depth = 0
    end = start
    in_string = False
    escape = False
    for i in range(start, len(html)):
        c = html[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if depth != 0:
        logger.warning("Unbalanced brackets in delinquent feed; truncating parse")
        return []
    arr_text = html[start:end]
    try:
        return json.loads(arr_text)
    except json.JSONDecodeError as exc:
        logger.error("JSON decode failed on delinquent feed: %s", exc)
        return []


# ── Public API ───────────────────────────────────────────────────────


def to_notice_data(rec: "MadisonDelinquentRecord") -> "NoticeData":
    """Convert a delinquent record into a NoticeData for the enrichment pipeline.

    Sets notice_type to "tax_sale" when the parcel is flagged for the upcoming
    May auction (TaxSaleParcel=true), otherwise "tax_delinquent". Populates the
    existing tax-delinquency fields on NoticeData so downstream stages (Smarty
    address standardization, Zillow enrichment, DataSift formatter) work
    unchanged.
    """
    # Local import — avoid a circular dependency at module load time and keep
    # this adapter independently importable for ad-hoc pulls.
    from notice_parser import NoticeData

    notice_type = "tax_sale" if rec.is_tax_sale_parcel else "tax_delinquent"
    today = datetime.now().strftime("%Y-%m-%d")
    return NoticeData(
        county="Madison",
        state="AL",
        notice_type=notice_type,
        # Tax-roll record IS the source — no separate publication date
        date_added=today,
        received_date=today,
        # Property identity
        owner_name=rec.owner_name,
        tax_owner_name=rec.owner_name,
        address=rec.situs_address,
        parcel_id=rec.parcel_id,
        # Tax delinquency fields (existing NoticeData slots)
        # tax_delinquent_years stores how many full years past delinquency,
        # NOT the assessment year. Use rec.tax_year for the year context.
        tax_delinquent_amount=f"{rec.balance_due:.2f}",
        # Madison's feed is current-year-only — leave tax_delinquent_years empty
        # rather than misrepresenting it. Reserved for future Jefferson adapter.
        tax_delinquent_years="",
        # Property valuation (assessor's last-assessed)
        assessed_value=f"{rec.assessed_value:.0f}" if rec.assessed_value > 0 else "",
        # Synthesized source URL — points back to the parcel summary page
        source_url=(
            f"https://madisonproperty.countygovservices.com/Property/Property/Summary"
            f"?pcliID={rec.pcli_id}&pan={rec.account}"
        ),
        raw_text=(
            f"{notice_type.upper().replace('_', ' ')} — Parcel {rec.parcel_id} "
            f"(tax year {rec.tax_year}, ${rec.balance_due:,.2f} owed) — "
            f"{rec.legal_description}"
        ),
    )


def fetch_delinquent_parcels(
    *,
    tax_sale_only: bool = False,
    individuals_only: bool = False,
    min_balance: float = 0.0,
) -> list[MadisonDelinquentRecord]:
    """Pull the full Madison County delinquent-parcels list.

    Phase 1 focuses on **dollar exposure** as the primary distress signal.
    Madison's feed is current-year-only by design (older years are pruned
    after the May auction or redemption), so ``balance_due`` is the actionable
    filter — properties owing $5,000+ are at meaningful risk regardless of
    how recently they became delinquent.

    Args:
        tax_sale_only: When True, only return parcels flagged
            ``TaxSaleParcel=true`` (scheduled for the upcoming May auction).
        individuals_only: When True, drop entity-owned parcels (LLC, Inc,
            Corp, Partnership, etc. matched via ``BUSINESS_RE``). Trusts and
            "Estate of" / "Heirs of" records are kept — they're personal,
            not commercial entities.
        min_balance: Drop records with ``balance_due`` below this threshold.
            Recommended: ``min_balance=5000`` for high-exposure focus.

    Returns:
        List of MadisonDelinquentRecord (typical: ~600 records). Empty on
        network/parse failure.

    Raises:
        httpx.HTTPError on transport failure (no retry — caller decides).
    """
    with _new_client() as client:
        r = client.get(DELINQUENT_URL)
        r.raise_for_status()
        html = r.text

    raw_records = _extract_data_array(html)
    logger.info("Madison delinquent feed: %d raw records", len(raw_records))

    records = [MadisonDelinquentRecord.from_record(r) for r in raw_records]
    if tax_sale_only:
        records = [r for r in records if r.is_tax_sale_parcel]
    if individuals_only:
        records = [r for r in records if r.is_individual_owner]
    if min_balance > 0:
        records = [r for r in records if r.balance_due >= min_balance]
    return records


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    tax_sale_only = "--tax-sale-only" in argv
    individuals_only = "--individuals-only" in argv
    min_balance = 0.0
    for i, a in enumerate(argv):
        if a == "--min-balance" and i + 1 < len(argv):
            try:
                min_balance = float(argv[i + 1])
            except ValueError:
                pass

    records = fetch_delinquent_parcels(
        tax_sale_only=tax_sale_only,
        individuals_only=individuals_only,
        min_balance=min_balance,
    )
    print(f"\nMadison delinquent parcels: {len(records)}")
    if tax_sale_only:
        print("  (filter: TaxSaleParcel=true)")
    if individuals_only:
        print("  (filter: individuals only — entities dropped)")
    if min_balance > 0:
        print(f"  (filter: balance_due >= ${min_balance:,.2f})")

    if not records:
        return 0

    total_balance = sum(r.balance_due for r in records)
    total_value = sum(r.assessed_value for r in records)
    sale_flagged = sum(1 for r in records if r.is_tax_sale_parcel)
    print(f"  Total balance owed: ${total_balance:,.2f}")
    print(f"  Total assessed value: ${total_value:,.2f}")
    print(f"  Flagged for tax sale: {sale_flagged}")

    print("\nTop 10 by balance owed:")
    top = sorted(records, key=lambda r: r.balance_due, reverse=True)[:10]
    for r in top:
        flag = " [TAX SALE]" if r.is_tax_sale_parcel else ""
        print(
            f"  {r.parcel_id}  {r.owner_name:35s}  "
            f"{r.situs_address:30s}  ${r.balance_due:>10,.2f}{flag}"
        )

    print("\nFirst record (full):")
    print(json.dumps(asdict(records[0]), indent=2))

    # Demonstrate the NoticeData converter on the first record
    notice = to_notice_data(records[0])
    print("\nFirst record as NoticeData (key fields):")
    for f in ("county", "notice_type", "owner_name", "address", "parcel_id",
              "tax_delinquent_amount", "tax_delinquent_years", "assessed_value",
              "date_added", "source_url"):
        print(f"  {f:24s} = {getattr(notice, f)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
