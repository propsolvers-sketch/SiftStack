"""Refresh the foreclosure owner cache from today's APN daily output.

Run as a workflow step immediately after main.py daily completes. Reads
the APN-produced ``datasift_upload_foreclosure_<ts>.csv``, extracts the
owner names, and folds them into
``output/observability/foreclosure_owner_cache.json``.

Also prunes cache entries older than 90 days so the file stays bounded.

Trustee-portal adapters (RL / T&B / T&B Results / HWM) read this cache
via ``owner_cache.fill_missing_owners()`` after their county property-API
enrichment step. See ``src/owner_cache.py`` for the full motivation +
schema.

Usage:
    python scripts/refresh_owner_cache.py                    # default (today's APN CSV)
    python scripts/refresh_owner_cache.py --csv <path>       # explicit CSV
    python scripts/refresh_owner_cache.py --dry-run          # report, don't write

Exit code: 0 on success (including "no CSV found" — that's fine on days
where APN produced no foreclosure output). Non-zero only on genuine
errors so the workflow doesn't fail the run.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make ``src`` importable when run from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from owner_cache import (
    load, save, prune, update_from_datasift_csv, _CACHE_PATH,
)

logger = logging.getLogger(__name__)


def _find_todays_apn_csv() -> Path | None:
    """Return the most recent APN foreclosure CSV.

    APN's file name pattern is ``datasift_upload_foreclosure_YYYY-MM-DD_HHMMSS.csv``
    — no ``_rl_`` / ``_tb_`` / ``_hwm_`` / ``_results_`` infix. We match
    on that shape so trustee-adapter output doesn't leak into the APN
    ingestion path (they use tax-roll names which we deliberately EXCLUDE
    from the cache — see owner_cache.py module docstring).
    """
    leads = Path("output/leads")
    if not leads.exists():
        return None

    candidates = sorted(
        p for p in leads.glob("datasift_upload_foreclosure_*.csv")
        if not any(
            token in p.name for token in (
                "_rl_", "_tb_", "_hwm_", "_results_",
                "_cancelled_", "_postponed_", "_adhunter_",
            )
        )
    )
    return candidates[-1] if candidates else None


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument(
        "--csv", type=Path, default=None,
        help="Explicit path to a DataSift-format foreclosure CSV.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen; do not write the cache file.",
    )
    ap.add_argument(
        "--prune-days", type=int, default=90,
        help="Drop cache entries older than N days (default 90).",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    csv_path = args.csv or _find_todays_apn_csv()
    if not csv_path:
        logger.info(
            "No APN foreclosure CSV found under output/leads/ — nothing to "
            "ingest. This is normal on days APN produced no foreclosure output.",
        )
        return 0
    if not csv_path.exists():
        logger.error("CSV %s does not exist", csv_path)
        return 2

    logger.info("Loading cache from %s", _CACHE_PATH)
    cache = load()
    initial_count = len(cache)
    logger.info("  Loaded %d existing entries", initial_count)

    logger.info("Ingesting APN foreclosure CSV: %s", csv_path)
    cache, added, refreshed = update_from_datasift_csv(csv_path, cache=cache)
    logger.info("  Added %d new entries, refreshed %d existing", added, refreshed)

    logger.info("Pruning entries older than %d days", args.prune_days)
    cache, dropped = prune(cache, days=args.prune_days)
    logger.info("  Dropped %d stale entries", dropped)

    final_count = len(cache)
    logger.info(
        "Cache size: %d → %d (net %+d)",
        initial_count, final_count, final_count - initial_count,
    )

    if args.dry_run:
        logger.info("[dry-run] Skipping save.")
        return 0

    save(cache)
    logger.info("Saved %d entries → %s", final_count, _CACHE_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
