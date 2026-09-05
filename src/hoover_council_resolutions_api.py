"""Hoover (Jefferson County, AL) code-enforcement via City Council resolutions.

Replaces the SeeClickFix adapter (`hoover_code_enforcement_api.py`) as the
Hoover source in `code_violation_pipeline._fetch_hoover`.

Why (researched 2026-09-05): Hoover flipped EVERY SeeClickFix request
category to ``private_visibility: true`` around 2026-05-17. The public
issues API still returns pre-flip history but nothing newer — the feed is
submit-only now, and the adapter had been returning 0 rows daily since
mid-June. Not a platform migration; a deliberate visibility change.

What this reads instead: the City Council's public "Recently Proposed
and/or Approved Resolutions" page — a plain HTML table (Number /
Description / Approved) where nuisance-abatement resolutions carry the
property address in the title, e.g.

    8944-26  A Resolution Declaring A Weed And Other Vegetation Nuisance
             And Directing The Abatement Of Said Nuisance Pursuant To
             Alabama Law For The Property Located At 2177 Kelly Lane
             8/24/2026

Signal class: these are FORMAL council orders (AL § 11-67 weed/nuisance
abatement; § 11-53B unsafe-structure demolition), i.e. the already-
escalated subset of what the private complaint feed contained. Lower
volume (council meets ~2x/month; ~2-6 nuisance resolutions per meeting)
but higher intent than raw citizen complaints.

Titles carry a street only — no city/ZIP. Hoover straddles 35216 / 35226
/ 35244 / 35022 (Jefferson) and 35242 (Shelby), so ZIP is recovered via a
single Smarty lookup anchored on "Hoover AL"; the tier gate then runs on
that ZIP. Owner comes from the Jefferson E-Ring situs search (reused from
the SeeClickFix adapter's ``enrich_with_owner``).

Cross-run dedup is NOT done here — `code_violation_pipeline` already
keys `seen_code_violations.json` on (address, case#); the resolution
number is written to ``case_number`` so that dedup is exact.

CLI:
    python src/hoover_council_resolutions_api.py                 # all nuisance rows on the page
    python src/hoover_council_resolutions_api.py --all-rows      # dump every resolution (recon)
    python src/hoover_council_resolutions_api.py --enrich-owners # + Jefferson owner lookup
"""
from __future__ import annotations

import argparse
import html as _html
import logging
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

RESOLUTIONS_URL = (
    "https://www.hooveralabama.gov/1480/Recently-Proposed-andor-Approved-Resolut"
)
_BASE = "https://www.hooveralabama.gov"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Classification ───────────────────────────────────────────────────
# Structure-level orders → tear-down framing (fires `demolish` via the
# `unsafe_building` subtype, same posture as Huntsville's list).
_STRUCTURE_RE = re.compile(
    r"unsafe\s+(?:structure|building)|demoli|condemn|"
    r"structure[^.]{0,60}public\s+nuisance|dilapidat",
    re.IGNORECASE,
)
# Lot-level orders (weeds, vegetation, debris, junk) → clean-up framing.
_WEED_RE = re.compile(
    r"weed|vegetation|overgrow|grass|debris|junk|litter|unsanitary|"
    r"abatement\s+of\s+(?:said\s+)?nuisance",
    re.IGNORECASE,
)
_NUISANCE_ANY_RE = re.compile(r"nuisance|abate", re.IGNORECASE)

# "... For The Property Located At 2177 Kelly Lane" / "... Located At 405 Cahaba River Estate."
_LOCATED_AT_RE = re.compile(
    r"\blocated\s+at\s+(?P<addr>\d+[^,;()]*?)\s*(?:,\s*hoover.*)?[.\s]*$",
    re.IGNORECASE,
)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_RES_NUM_RE = re.compile(r"^\d{3,5}-\d{2}$")


@dataclass(frozen=True)
class HooverResolutionRecord:
    resolution_number: str      # "8944-26" — unique; written to case_number
    approved: str               # YYYY-MM-DD council approval date
    title: str                  # full resolution title as printed
    kind: str                   # "structure" | "weed"
    address: str                # street only, as printed ("2177 Kelly Lane")
    city: str                   # Smarty-recovered ("Hoover"), or "Hoover" default
    zip: str                    # Smarty-recovered 5-digit ZIP, or ""
    doc_url: str                # DocumentCenter link (HTML viewer; PDF not directly fetchable)

    def to_dict(self) -> dict:
        return asdict(self)


# ── HTTP + parse ─────────────────────────────────────────────────────


def fetch_page_html(session: Optional[requests.Session] = None, timeout: float = 30.0) -> str:
    s = session or requests.Session()
    r = s.get(RESOLUTIONS_URL, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub("", cell))).strip()


def parse_rows(page_html: str) -> list[tuple[str, str, str, str]]:
    """Return (number, title, approved_raw, doc_url) for every data row."""
    out: list[tuple[str, str, str, str]] = []
    tables = re.findall(r"<table[^>]*>.*?</table>", page_html, flags=re.S | re.I)
    for t in tables:
        for row in _ROW_RE.findall(t):
            cells = [_clean(c) for c in _CELL_RE.findall(row)]
            if len(cells) < 3 or not _RES_NUM_RE.match(cells[0]):
                continue
            href = _HREF_RE.search(row)
            doc = href.group(1) if href else ""
            if doc.startswith("/"):
                doc = _BASE + doc
            out.append((cells[0], cells[1], cells[2], doc))
    return out


def _iso(d: str) -> str:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def classify(title: str) -> Optional[str]:
    if _STRUCTURE_RE.search(title):
        return "structure"
    if _WEED_RE.search(title) or _NUISANCE_ANY_RE.search(title):
        return "weed"
    return None


def extract_address(title: str) -> str:
    m = _LOCATED_AT_RE.search(title)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group("addr")).strip(" .,")


# Council titles spell suffixes out ("Tyler Road", "Kelly Lane", "Cahaba
# River Estate"); the Jefferson roll stores USPS forms ("TYLER RD",
# "KELLY LN", "CAHABA RIVER EST"). E-Ring situs search is exact on the
# street token, so normalize before the owner lookup.
_USPS_SUFFIX = {
    "ROAD": "RD", "LANE": "LN", "DRIVE": "DR", "STREET": "ST", "AVENUE": "AVE",
    "CIRCLE": "CIR", "COURT": "CT", "PLACE": "PL", "BOULEVARD": "BLVD",
    "PARKWAY": "PKWY", "TERRACE": "TER", "TRAIL": "TRL", "HIGHWAY": "HWY",
    "ESTATE": "EST", "ESTATES": "EST", "COVE": "CV", "POINT": "PT",
    "RIDGE": "RDG", "CROSSING": "XING", "HOLLOW": "HOLW", "SQUARE": "SQ",
    "TRACE": "TRCE", "WAY": "WAY", "LOOP": "LOOP", "RUN": "RUN",
}
_HOUSE_NUM_RE = re.compile(r"^(\d+)\b")


def normalize_street_for_roll(street: str) -> str:
    toks = street.upper().split()
    return " ".join(_USPS_SUFFIX.get(t, t) for t in toks)


def enrich_with_owner(notice) -> bool:
    """Owner/parcel via Jefferson E-Ring situs search, with a house-number guard.

    Unlike `hoover_code_enforcement_api.enrich_with_owner`, this REFUSES a
    non-exact fallback: E-Ring's situs search is a prefix match on the
    number, so "405 CAHABA RIVER EST" returns 2405's parcel — taking
    ``matches[0]`` would assign the wrong owner. Only a match whose house
    number equals ours is accepted.
    """
    if notice.owner_name or not notice.address:
        return False
    from jefferson_property_api import search_by_situs_address
    query = normalize_street_for_roll(notice.address)
    want = _HOUSE_NUM_RE.match(query)
    try:
        matches = search_by_situs_address(query)
    except Exception as exc:
        logger.warning("Owner-enrich failed for %r: %s", query, exc)
        return False
    same_num = [
        m for m in matches
        if want and _HOUSE_NUM_RE.match(m.situs_address.upper() or "")
        and _HOUSE_NUM_RE.match(m.situs_address.upper()).group(1) == want.group(1)
    ]
    if not same_num:
        if matches:
            logger.info("  Hoover owner-enrich: %r only matched other house numbers (%s) — skipped",
                        query, ", ".join(sorted({m.situs_address for m in matches})[:3]))
        return False
    exact = [m for m in same_num if m.situs_address.upper().strip() == query]
    pick = exact[0] if exact else same_num[0]
    notice.owner_name = pick.owner_name
    notice.tax_owner_name = pick.owner_name
    if not notice.parcel_id:
        notice.parcel_id = pick.parcel_number
    if not notice.assessed_value and pick.total_value:
        notice.assessed_value = f"{pick.total_value:.0f}"
    if not notice.is_homestead and pick.is_homestead:
        notice.is_homestead = "Y"
    return True


def _recover_zip(street: str) -> tuple[str, str]:
    """(city, zip) via one Smarty lookup anchored on Hoover; ('', '') if unset."""
    try:
        from address_standardizer import smarty_zip_for_assuranceweb_address
        city, zip_ = smarty_zip_for_assuranceweb_address(
            street, "Hoover AL",
            anchor_fallbacks=("Birmingham AL", "Vestavia Hills AL", "AL"),
        )
        return (city or "Hoover", zip_ or "")
    except Exception as exc:  # Smarty not configured / network
        logger.debug("Smarty ZIP recovery failed for %r: %s", street, exc)
        return ("Hoover", "")


def fetch_nuisance_resolutions(
    *,
    days_back: Optional[int] = None,
    target_zips: Optional[set[str]] = None,
    recover_zip: bool = True,
    session: Optional[requests.Session] = None,
) -> list[HooverResolutionRecord]:
    """Fetch + classify nuisance resolutions currently listed on the page.

    days_back: keep only resolutions approved within N days (None = all on page).
    target_zips: if given, keep only rows whose recovered ZIP is in the set
        (rows with NO recoverable ZIP are kept and logged — the pipeline's
        tier gate makes the final call).
    """
    page = fetch_page_html(session)
    rows = parse_rows(page)
    if len(rows) < 3:
        logger.warning(
            "Hoover council resolutions page appears EMPTY or CHANGED: only %d "
            "resolution rows parsed from %d bytes at %s",
            len(rows), len(page), RESOLUTIONS_URL,
        )
    today = date.today()
    kept: list[HooverResolutionRecord] = []
    n_nuisance = n_no_addr = n_old = n_offtier = 0
    for num, title, approved_raw, doc in rows:
        kind = classify(title)
        if not kind:
            continue
        n_nuisance += 1
        approved = _iso(approved_raw)
        if days_back is not None and approved:
            age = (today - datetime.strptime(approved, "%Y-%m-%d").date()).days
            if age > days_back:
                n_old += 1
                continue
        street = extract_address(title)
        if not street:
            n_no_addr += 1
            logger.info("  Hoover %s: nuisance resolution without a street address — %s",
                        num, title[:100])
            continue
        city, zip_ = _recover_zip(street) if recover_zip else ("Hoover", "")
        if target_zips and zip_ and zip_ not in target_zips:
            n_offtier += 1
            logger.debug("  Hoover %s: %s %s off-tier — skipped", num, street, zip_)
            continue
        if not zip_:
            logger.info("  Hoover %s: ZIP not recovered for %r — passing through to tier gate",
                        num, street)
        kept.append(HooverResolutionRecord(
            resolution_number=num, approved=approved, title=title, kind=kind,
            address=street, city=city, zip=zip_, doc_url=doc,
        ))
    logger.info(
        "Hoover council resolutions: %d nuisance resolutions kept "
        "(page rows=%d, nuisance=%d, too_old=%d, no_address=%d, off_tier=%d)",
        len(kept), len(rows), n_nuisance, n_old, n_no_addr, n_offtier,
    )
    return kept


# ── NoticeData conversion ────────────────────────────────────────────


def to_notice_data(rec: HooverResolutionRecord, *, enrich_owner: bool = False):
    """HooverResolutionRecord → NoticeData.

    Subtype: ``unsafe_building`` for structure orders (formatter fires
    ``demolish``), ``nuisance_abatement`` for lot-level weed/debris orders
    (formatter fires ``early_distress``). Both are formal council orders,
    not citizen complaints — distinct from the retired
    ``code_enforcement_complaint`` subtype.
    """
    from notice_parser import NoticeData

    today = date.today().strftime("%Y-%m-%d")
    subtype = "unsafe_building" if rec.kind == "structure" else "nuisance_abatement"
    notice = NoticeData(
        county="Jefferson",
        state="AL",
        notice_type="code_violation",
        notice_subtype=subtype,
        date_added=rec.approved or today,
        received_date=today,
        owner_name="",
        address=rec.address,
        city=rec.city or "Hoover",
        zip=rec.zip,
        case_number=rec.resolution_number,   # surfaces in "Probate Case Number" column
        source_url=rec.doc_url or RESOLUTIONS_URL,
        municipality="Hoover",
        raw_text=(
            f"Hoover City Council Resolution {rec.resolution_number} "
            f"(approved {rec.approved or '?'}) — {rec.title}"
        ),
    )
    if enrich_owner:
        enrich_with_owner(notice)
    return notice


# ── CLI ──────────────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days-back", type=int, default=None)
    ap.add_argument("--all-rows", action="store_true", help="Dump every resolution on the page")
    ap.add_argument("--enrich-owners", action="store_true")
    ap.add_argument("--no-zip", action="store_true", help="Skip Smarty ZIP recovery")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    if args.all_rows:
        for num, title, approved, doc in parse_rows(fetch_page_html()):
            print(f"{num:10} {approved:10} {classify(title) or '-':9} {title[:110]}")
        return 0
    recs = fetch_nuisance_resolutions(days_back=args.days_back, recover_zip=not args.no_zip)
    for r in recs:
        n = to_notice_data(r, enrich_owner=args.enrich_owners)
        print(f"{r.resolution_number:9} {r.approved} {r.kind:9} {r.address:40} "
              f"{r.city} {r.zip or '?????'}  owner={n.owner_name or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
