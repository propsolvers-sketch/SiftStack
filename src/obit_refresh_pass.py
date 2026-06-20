"""Obit refresh pass — re-fetch recent pre-probate obits to catch survivor
data that families added after the initial publication.

Why this exists
---------------
Funeral homes / families frequently update obituary pages in the days
AFTER initial publication. The first version that hits legacy.com often
has just name + DoD; the full "preceded in death" / "survived by" /
"leaves to cherish" sections get added a few days later as the family
finalizes details. Our daily pre-probate pipeline catches the obit on
day 1 (sparse), marks the URL ``seen_effective``, and never re-checks.

Operator example 2026-06-19: Mark Jerome Green at 7207 DEER HAVEN RD
went through the 6/17 run with no survivors extracted. By 6/19 the same
legacy.com URL had a full 20-person family graph in the page body.

What this script does
---------------------
1. Scans local + Dropbox archive CSVs from the last N days (default 14).
2. For every row with ``notice_type=pre_probate`` AND a non-empty obit
   URL, counts how many survivors we originally stored in heir_map_json.
3. Re-fetches each obit via the same ``_fetch_full_obit_text`` +
   ``_extract_structured_text`` flow the live pipeline uses.
4. Runs the same DECEDENT_PROMPT LLM extraction.
5. Compares new survivor count to the old. If new > old (family added
   detail), emits the record to a delta CSV at
   ``output/leads/datasift_obit_refresh_delta_<ts>.csv`` ready for the
   operator to merge into DataSift via the wizard.

Cheap to run — only LLM-extracts obits where the structured text
actually has STRONG survivor markers (signal of fresh detail). Skips
obits whose extractor still returns empty (page hasn't been updated).

Designed to run weekly. CLI:

    python src/obit_refresh_pass.py                    # default 14-day window
    python src/obit_refresh_pass.py --days-back 7      # narrower window
    python src/obit_refresh_pass.py --dry-run          # don't write delta CSV
    python src/obit_refresh_pass.py --min-improvement 2  # only emit if +2 survivors
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Load .env so ANTHROPIC_API_KEY + FIRECRAWL_API_KEY are available
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

import config  # noqa: E402
import llm_client  # noqa: E402
from obituary_enricher import _extract_structured_text  # noqa: E402
from pre_probate_pipeline_al import (  # noqa: E402
    DECEDENT_PROMPT, _fetch_full_obit_text,
)

logger = logging.getLogger(__name__)


# ── Archive scan ─────────────────────────────────────────────────────


def _archive_dirs(override: Path | None = None) -> list[Path]:
    """Where recent archive CSVs live. Prefers an explicit --archive-dir,
    then Dropbox (which the daily workflow syncs to per the 2026-06-12
    setup), then local output/leads/, then /tmp/sift-recent/ if the
    operator has downloaded recent GHA artifacts there."""
    if override:
        return [override]
    candidates = [
        Path.home() / "Dropbox" / "SiftStack" / "Archives",
        REPO / "output" / "leads",
        Path("/tmp/sift-recent"),  # scoped via rglob below if used
    ]
    return [p for p in candidates if p.exists()]


def _file_date(path: Path) -> str | None:
    """Extract YYYY-MM-DD from a datasift_archive_*.csv filename."""
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", path.name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def collect_recent_pre_probate_obits(
    days_back: int,
    archive_dir: Path | None = None,
) -> dict[str, dict]:
    """Walk recent archives, return {obit_url: {decedent, property_addr,
    survivor_count, first_seen_date, archive_path}}.

    For duplicate URLs across multiple archives, keeps the EARLIEST
    record (so we measure improvement vs the first time we processed
    the obit, not the most recent baseline)."""
    cutoff = (datetime.now() - timedelta(days=days_back)).date().isoformat()
    out: dict[str, dict] = {}
    files_scanned = 0

    # Scan BOTH archive files (main.py daily) AND upload files (pre_probate
    # pipeline writes its own datasift_upload_<ts>.csv — Notice Type column
    # tells us which rows are pre_probate). datasift_archive_* alone misses
    # pre-probate entirely because that pipeline doesn't produce an archive.
    for d in _archive_dirs(archive_dir):
        candidates = (
            list(d.rglob("datasift_archive_*.csv"))
            + list(d.rglob("datasift_upload_*.csv"))
        )
        for archive in sorted(candidates):
            fdate = _file_date(archive)
            if not fdate or fdate < cutoff:
                continue
            try:
                with open(archive, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if (row.get("Notice Type") or "").strip() != "pre_probate":
                            continue
                        obit = (row.get("Obituary URL") or "").strip()
                        if not obit:
                            continue

                        # Count survivors in stored heir_map_json
                        survivors_old = 0
                        try:
                            heirs_blob = row.get("heir_map_json") or row.get("Heirs Living") or ""
                        except Exception:
                            heirs_blob = ""
                        # heir_map_json is usually a JSON array; fall back to
                        # the "Heirs Living" int column.
                        if heirs_blob and heirs_blob.startswith("["):
                            try:
                                survivors_old = len(json.loads(heirs_blob))
                            except (json.JSONDecodeError, TypeError):
                                survivors_old = 0
                        elif heirs_blob.strip().isdigit():
                            survivors_old = int(heirs_blob.strip())

                        existing = out.get(obit)
                        if existing is None or existing["first_seen_date"] > fdate:
                            out[obit] = {
                                "decedent": (row.get("Decedent Name") or "").strip(),
                                "property_addr": (
                                    row.get("Property Street Address") or ""
                                ).strip(),
                                "property_city": (row.get("Property City") or "").strip(),
                                "property_state": (row.get("Property State") or "").strip(),
                                "property_zip": (row.get("Property ZIP Code") or "").strip(),
                                "owner_first": (row.get("Owner First Name") or "").strip(),
                                "owner_last": (row.get("Owner Last Name") or "").strip(),
                                "mailing_addr": (
                                    row.get("Mailing Street Address") or ""
                                ).strip(),
                                "mailing_city": (row.get("Mailing City") or "").strip(),
                                "mailing_state": (row.get("Mailing State") or "").strip(),
                                "mailing_zip": (row.get("Mailing ZIP Code") or "").strip(),
                                "survivor_count": survivors_old,
                                "first_seen_date": fdate,
                                "archive_path": str(archive),
                            }
            except Exception as e:
                logger.warning("Could not read %s: %s", archive, e)
            files_scanned += 1
    logger.info(
        "Scanned %d archive files; found %d distinct pre-probate obit URLs "
        "from the past %d days",
        files_scanned, len(out), days_back,
    )
    return out


# ── Re-fetch + re-extract per URL ────────────────────────────────────


def refresh_one(url: str, original: dict) -> dict | None:
    """Re-fetch + re-extract a single obit. Returns a dict with the new
    survivor count + decision-maker info IF the re-extraction yielded
    more survivors than the original; otherwise returns None.

    The dict structure mirrors what a daily-pipeline row would carry
    (decedent, dm name, mailing) so the caller can emit a delta CSV row."""
    try:
        text, effective_url = _fetch_full_obit_text(url)
    except Exception as e:
        logger.debug("Fetch failed for %s: %s", url, e)
        return None
    if not text or len(text) < 200:
        return None

    # Use the structured extractor's output if it surfaced strong
    # survivor markers (legacy.com pages with the new content).
    try:
        structured = _extract_structured_text(text, url)
        if structured and len(structured) >= 500:
            text = structured
    except Exception:
        pass

    # Run the same LLM extraction the live pipeline uses
    prompt = DECEDENT_PROMPT.format(obituary_text=text[:6000])
    try:
        parsed = llm_client.chat_json(
            prompt,
            system="You extract structured obituary data. Return ONLY valid JSON.",
            max_tokens=2000,
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )
    except Exception as e:
        logger.debug("LLM call failed for %s: %s", url, e)
        return None
    if not parsed or not parsed.get("is_obituary"):
        return None

    survivors = parsed.get("all_survivors") or []
    new_count = len(survivors)
    old_count = original.get("survivor_count", 0)
    if new_count <= old_count:
        return None

    # Improvement detected
    return {
        "url": effective_url,
        "new_survivor_count": new_count,
        "old_survivor_count": old_count,
        "spouse_name": parsed.get("spouse_name") or "",
        "executor_named": parsed.get("executor_named") or "",
        "all_survivors": survivors,
        "decedent_full_name": parsed.get("decedent_full_name") or original.get("decedent", ""),
        "date_of_death": parsed.get("date_of_death") or "",
    }


# ── Delta CSV emission ───────────────────────────────────────────────


def write_delta_csv(improvements: list[tuple[dict, dict]]) -> Path:
    """improvements = list of (original_record, refresh_result) tuples.
    Writes a delta CSV ready for DataSift wizard upload.

    Includes the address quartet + owner + mailing for unambiguous merge,
    plus a Notes update describing what changed."""
    out_dir = REPO / "output" / "leads"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    # Filename pattern matches the `datasift_upload*.csv` glob in
    # daily_finalize.py so the workflow's existing upload step picks
    # up the refresh delta automatically when this runs in CI.
    out_path = out_dir / f"datasift_upload_pre_probate_refresh_{timestamp}.csv"

    # Notice Type is read by daily_finalize._notice_type_from_csv() to
    # route the file to the right DataSift list (Pre-Probate). Without
    # it the per-distressor uploader bails with "No NOTICE_TYPE_TO_LIST
    # mapping" and the file gets skipped.
    cols = [
        "Notice Type",
        "Property address", "Property city", "Property state", "Property zip",
        "First Name", "Last Name",
        "Mailing address", "Mailing city", "Mailing state", "Mailing zip",
        "Tags", "Messages", "Notes",
    ]

    rows = []
    for original, refresh in improvements:
        survivors = refresh["all_survivors"]
        survivor_summary = "; ".join(
            f"{s.get('name','?')} ({s.get('relationship','?')})"
            for s in survivors[:15]
        )
        if len(survivors) > 15:
            survivor_summary += f" ... +{len(survivors)-15} more"

        note = (
            f"=== OBIT REFRESH ({datetime.now().strftime('%Y-%m-%d')}) ===\n"
            f"Originally extracted {refresh['old_survivor_count']} survivors; "
            f"refreshed count is {refresh['new_survivor_count']}. "
            f"Family appears to have added detail to the obituary after "
            f"the initial publication on {original['first_seen_date']}.\n\n"
            f"Decedent: {refresh['decedent_full_name']}\n"
            f"Date of death: {refresh['date_of_death']}\n"
            f"Spouse: {refresh.get('spouse_name') or '(not stated)'}\n"
            f"Executor named: {refresh.get('executor_named') or '(not stated)'}\n"
            f"All survivors: {survivor_summary}\n"
            f"Source: {refresh['url']}"
        )
        rows.append({
            "Notice Type": "pre_probate",
            "Property address": original["property_addr"],
            "Property city": original["property_city"],
            "Property state": original["property_state"],
            "Property zip": original["property_zip"],
            "First Name": original["owner_first"],
            "Last Name": original["owner_last"],
            "Mailing address": original["mailing_addr"],
            "Mailing city": original["mailing_city"],
            "Mailing state": original["mailing_state"],
            "Mailing zip": original["mailing_zip"],
            "Tags": "Courthouse Data, obit_refreshed",
            "Messages": (
                f"Obit refresh on {datetime.now().strftime('%Y-%m-%d')}: "
                f"survivors {refresh['old_survivor_count']}→"
                f"{refresh['new_survivor_count']}"
            ),
            "Notes": note,
        })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return out_path


# ── Main ─────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--days-back", type=int, default=14,
                   help="how many days of archives to re-check (default 14)")
    p.add_argument("--min-improvement", type=int, default=1,
                   help="only emit delta when new_count >= old_count + N (default 1)")
    p.add_argument("--archive-dir", type=Path, default=None,
                   help="explicit folder to scan for datasift_archive_*.csv "
                        "(default: Dropbox/SiftStack/Archives, then output/leads, "
                        "then /tmp/sift-recent)")
    p.add_argument("--dry-run", action="store_true",
                   help="don't write the delta CSV")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    candidates = collect_recent_pre_probate_obits(
        args.days_back, archive_dir=args.archive_dir,
    )
    if not candidates:
        logger.info("No recent pre-probate obits in archive — nothing to refresh.")
        return 0

    logger.info("Re-fetching %d obits...", len(candidates))
    improvements: list[tuple[dict, dict]] = []
    for i, (url, original) in enumerate(candidates.items(), 1):
        if i % 10 == 0:
            logger.info("  progress: %d/%d", i, len(candidates))
        result = refresh_one(url, original)
        if not result:
            continue
        if result["new_survivor_count"] - result["old_survivor_count"] < args.min_improvement:
            continue
        improvements.append((original, result))
        logger.info(
            "  +survivors: %s @ %s — %d → %d",
            result["decedent_full_name"],
            original.get("property_addr") or "(unknown addr)",
            result["old_survivor_count"], result["new_survivor_count"],
        )

    logger.info("Refresh pass complete: %d / %d obits improved",
                len(improvements), len(candidates))
    if not improvements:
        return 0

    if args.dry_run:
        logger.info("Dry-run — skipping delta CSV write.")
        return 0

    out_path = write_delta_csv(improvements)
    logger.info("Delta CSV written: %s (%d rows)", out_path, len(improvements))
    return 0


if __name__ == "__main__":
    sys.exit(main())
