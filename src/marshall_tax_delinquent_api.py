"""Marshall County, AL bulk tax-delinquent list — STUB (source currently offline).

Marshall is on the same AssuranceWeb platform as Madison
(countygovservices.com), so the public delinquent-parcels listing lives at:
    https://marshall.countygovservices.com/property/Property/DelinquentParcels

As of 2026-05-12 that page renders:
    "Payments are currently disabled."
    "The Delinquent Parcels listing is currently disabled."

There is no inline Kendo Grid initialization while the listing is disabled —
the JS bundle (`/property/bundles/DelinquentParcels.min.js`) contains only
export-button handlers; the actual data-source endpoint is not exposed in any
bundle or `data-*` attribute, so we cannot determine the API URL or field
shape until the county re-enables the listing.

This module exists so the Marshall county column lines up with Madison /
Jefferson in the unified tax-distress pipeline. It probes the page on each
call; if the "currently disabled" string is present it logs a warning and
returns `[]`. When Marshall re-enables the listing the parser can be
back-filled (the API surface and `to_notice_data()` conversion are already
defined here so downstream code requires no changes).

CLI:
    python src/marshall_tax_delinquent_api.py
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)

DELINQUENT_URL = (
    "https://marshall.countygovservices.com/property/Property/DelinquentParcels"
)

_DISABLED_MARKER = "Delinquent Parcels listing is currently disabled"

# Mirror Madison's threshold so cross-county filters stay consistent.
HIGH_EXPOSURE_THRESHOLD = 5000.0


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarshallDelinquentRecord:
    """One delinquent parcel from Marshall's AssuranceWeb feed.

    Field set mirrors `MadisonDelinquentRecord` for cross-county parity in the
    unified pipeline. Populated by `from_record()` once the source comes back
    online — until then this dataclass is reserved for the eventual parser.
    """
    parcel_id: str
    parcel_number_raw: str
    pin: str
    account: str
    owner_name: str
    previous_owner: str
    situs_address: str
    legal_description: str
    tax_year: int
    balance_due: float
    tax_sale_balance: float
    assessed_value: float
    gross_tax: float
    interest: float
    other_fees: float
    exempt: float
    paid: float
    is_tax_sale_parcel: bool
    pcli_id: int
    is_individual_owner: bool
    is_high_exposure: bool


# ── HTTP layer ───────────────────────────────────────────────────────


def _new_client(timeout: float = 60.0) -> httpx.Client:
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


def is_source_disabled() -> bool:
    """Quick probe: True when Marshall's delinquent-parcels listing is offline.

    Cheap (one GET, ~6KB body). Used by `fetch_delinquent_parcels()` and by the
    unified pipeline orchestrator to decide whether to skip Marshall this run.
    """
    try:
        with _new_client(timeout=15) as client:
            r = client.get(DELINQUENT_URL)
            r.raise_for_status()
            return _DISABLED_MARKER in r.text
    except httpx.HTTPError as exc:
        logger.warning("Marshall delinquent probe failed: %s", exc)
        return True  # Treat transport failure as disabled — caller skips.


# ── Public API ───────────────────────────────────────────────────────


def to_notice_data(rec: MarshallDelinquentRecord) -> "NoticeData":
    """Convert a Marshall delinquent record into a NoticeData.

    Mirrors `madison_tax_delinquent_api.to_notice_data` so downstream stages
    (Smarty address standardization, Zillow enrichment, DataSift formatter)
    work identically across counties. Live data parsing is not yet wired —
    once the listing comes back online and `from_record()` is implemented,
    this converter is ready to use.
    """
    from notice_parser import NoticeData

    notice_type = "tax_sale" if rec.is_tax_sale_parcel else "tax_delinquent"
    today = datetime.now().strftime("%Y-%m-%d")
    return NoticeData(
        county="Marshall",
        state="AL",
        notice_type=notice_type,
        date_added=today,
        received_date=today,
        owner_name=rec.owner_name,
        tax_owner_name=rec.owner_name,
        address=rec.situs_address,
        parcel_id=rec.parcel_id,
        tax_delinquent_amount=f"{rec.balance_due:.2f}",
        tax_delinquent_years="",
        assessed_value=f"{rec.assessed_value:.0f}" if rec.assessed_value > 0 else "",
        source_url=(
            f"https://marshall.countygovservices.com/property/Property/Summary"
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
) -> list[MarshallDelinquentRecord]:
    """Pull the Marshall County delinquent-parcels list (or `[]` while offline).

    Signature matches `madison_tax_delinquent_api.fetch_delinquent_parcels` for
    drop-in use by the unified `tax_distress_pipeline` once the source returns.

    Today (2026-05): the source is disabled — returns `[]` with a one-time
    warning. The unified pipeline tolerates an empty list cleanly.
    """
    if is_source_disabled():
        logger.warning(
            "Marshall delinquent listing is currently disabled at %s — "
            "returning empty list. Re-check after the May tax sale.",
            DELINQUENT_URL,
        )
        return []

    # Source is live but the parser is not yet wired. Bail loudly so we don't
    # silently lose data once Marshall re-enables the listing.
    raise NotImplementedError(
        "Marshall delinquent listing is live again — back-fill the Kendo Grid "
        "parser in marshall_tax_delinquent_api.py. Inspect the page's inline "
        "JS for the data shape (Marshall uses a real REST endpoint at "
        "/property/api/PropertyAPI/* rather than Madison's inlined Data array)."
    )


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if is_source_disabled():
        print(
            f"\nMarshall delinquent parcels: SOURCE DISABLED\n"
            f"  URL: {DELINQUENT_URL}\n"
            f"  Status: 'The Delinquent Parcels listing is currently disabled.'\n"
            f"  This adapter will activate automatically once Marshall County "
            f"re-enables the public listing.\n"
        )
        return 0

    records = fetch_delinquent_parcels()
    print(f"\nMarshall delinquent parcels: {len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
