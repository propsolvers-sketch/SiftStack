"""Shared post-scrape pipeline for both CLI and Apify Actor entry points.

The two entry points in main.py — ``actor_main()`` and ``_run_scrape_pipeline()``
(called by ``cli_main()``) — historically each had their own copy of the
post-scrape orchestration: enrichment → Tracerfy → Trestle → deep-prospecting
PDF generation → DataSift CSV generation. This caused drift. Steps added to
one branch were forgotten on the other (e.g. the Apify cold-start crash on
the deleted ``TNPN_EMAIL`` config constant in 2026-04-30, found by codebase
audit and fixed in P0.1).

This module consolidates the post-scrape, post-probate-lookup logic.
Both entry points:

  1. Acquire notices via ``scrape_all`` (already shared)
  2. Run async probate property lookup (handle their own async plumbing)
  3. Call ``run_full_pipeline(notices, opts)`` from this module
  4. Handle their own I/O — KVS upload vs local file write, Slack format,
     state persistence, failure mode (Actor.fail vs sys.exit)

The semantically-divergent parts (output sinks, state, Slack format,
failure semantics) are intentionally NOT consolidated — Apify's KVS-based
storage and CLI's file-based storage are genuinely different products
with different consumers.

The Tracerfy DP-only-vs-all-notices behavior IS consolidated as a config
option (``tracerfy_dp_only``) since the difference is intentional cost
control on Apify's production runs, not accidental drift.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notice_parser import NoticeData
    from observability import FunnelCounter, ServiceRateTracker

logger = logging.getLogger(__name__)


# ── Options ──────────────────────────────────────────────────────────


@dataclass
class PostScrapeOptions:
    """Toggles for the shared post-scrape pipeline.

    Mirrors enrichment_pipeline.PipelineOptions for the enrichment step,
    plus extras for Tracerfy / PDF gen / DataSift CSV gen.
    """

    # ── Filter toggles (passed through to PipelineOptions) ────────
    skip_vacant_filter: bool = False
    skip_commercial_filter: bool = False
    skip_entity_filter: bool = False

    # ── Enrichment toggles (passed through) ───────────────────────
    skip_parcel_lookup: bool = True   # web scrape notices have no parcel
    skip_smarty: bool = False
    skip_zillow: bool = False
    skip_tax: bool = False
    skip_geocode: bool = False
    skip_obituary: bool = False
    skip_ancestry: bool = False
    skip_entity_research: bool = True

    # ── Heir / DM (passed through) ────────────────────────────────
    skip_heir_verification: bool = False
    max_heir_depth: int = 2
    skip_dm_address: bool = False

    # ── Tracerfy + Trestle ─────────────────────────────────────────
    skip_tracerfy: bool = False
    tracerfy_dp_only: bool = False   # True on Apify (cost control); False on CLI
    tracerfy_tier1: bool = False

    # ── Output generation toggles ─────────────────────────────────
    skip_reports: bool = False        # True to skip deep-prospecting PDFs
    skip_datasift_csv: bool = False   # True to skip DataSift CSV generation
    report_dir: Path = field(default_factory=lambda: Path("output/reports"))

    # ── Labels ────────────────────────────────────────────────────
    source_label: str = ""

    # ── Phase 2 observability (OPS-03 / OBS-01) ───────────────────
    # FunnelCounter + ServiceRateTracker are instantiated by the caller
    # (main.py for the legacy daily flow) and threaded down so the same
    # instances are mutated across full_pipeline (tracerfy_matched) and
    # enrichment_pipeline (county_filtered → zillow_enriched). Defaults
    # are None → legacy callers (csv-import, pdf-import, photo-import)
    # remain byte-identical.
    funnel: "FunnelCounter | None" = None
    rate_tracker: "ServiceRateTracker | None" = None


@dataclass
class PostScrapeResult:
    """Structured output of ``run_full_pipeline``.

    Each caller decides what to do with the artifacts (upload to KVS,
    write to disk, push to DataSift via Playwright, etc.).
    """

    notices: list = field(default_factory=list)            # filtered + enriched
    tracerfy_stats: dict = field(default_factory=dict)     # batch_skip_trace return
    phone_tiers: dict = field(default_factory=dict)        # phone-string → tier-info
    pdf_paths: list = field(default_factory=list)          # [(NoticeData, Path), ...]
    datasift_csv_infos: list = field(default_factory=list) # from write_datasift_split_csvs


# ── Main entry point ─────────────────────────────────────────────────


def run_full_pipeline(
    notices: list,
    opts: PostScrapeOptions,
) -> PostScrapeResult:
    """Run the unified post-scrape pipeline.

    Steps (each soft-fails with a warning so one bad step doesn't kill
    the batch):

      1. Filter + enrich (``run_enrichment_pipeline``)
      2. Tracerfy batch skip trace (optional DP-only filter)
      3. Trestle phone scoring (DP candidates only)
      4. Deep-prospecting PDF generation (DP candidates only)
      5. DataSift CSV generation (split or unified)

    Args:
        notices: Scraped + probate-property-looked-up notices.
        opts: Pipeline configuration.

    Returns:
        ``PostScrapeResult`` with the filtered/enriched notice list and
        all generated artifacts. Empty if no notices survived filtering.

    Note: Probate property lookup is NOT in this function. Both callers
    do it before invoking ``run_full_pipeline`` because the lookup is
    async and each path handles asyncio differently (await vs
    asyncio.run).
    """
    result = PostScrapeResult()

    # ── Step 1: Enrichment pipeline ───────────────────────────────
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    pipeline_opts = PipelineOptions(
        skip_parcel_lookup=opts.skip_parcel_lookup,
        skip_vacant_filter=opts.skip_vacant_filter,
        skip_commercial_filter=opts.skip_commercial_filter,
        skip_entity_filter=opts.skip_entity_filter,
        skip_smarty=opts.skip_smarty,
        skip_zillow=opts.skip_zillow,
        skip_tax=opts.skip_tax,
        skip_geocode=opts.skip_geocode,
        skip_obituary=opts.skip_obituary,
        skip_ancestry=opts.skip_ancestry,
        skip_entity_research=opts.skip_entity_research,
        skip_heir_verification=opts.skip_heir_verification,
        max_heir_depth=opts.max_heir_depth,
        skip_dm_address=opts.skip_dm_address,
        tracerfy_tier1=opts.tracerfy_tier1,
        source_label=opts.source_label,
        # Phase 2: forward FunnelCounter + ServiceRateTracker so
        # run_enrichment_pipeline can stamp the 6 enrichment-stage gates
        # (county_filtered → zillow_enriched) AND thread the tracker
        # into Smarty / LLM call sites.
        funnel=opts.funnel,
        rate_tracker=opts.rate_tracker,
    )
    notices = run_enrichment_pipeline(notices, pipeline_opts)
    result.notices = notices

    if not notices:
        return result

    # ── Step 2: Tracerfy batch skip trace ─────────────────────────
    import config as cfg

    tracerfy_target = notices
    if opts.tracerfy_dp_only:
        tracerfy_target = [
            n for n in notices
            if (n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name)
        ]

    if not opts.skip_tracerfy and cfg.TRACERFY_API_KEY and tracerfy_target:
        try:
            from tracerfy_skip_tracer import batch_skip_trace
            stats = batch_skip_trace(tracerfy_target, rate_tracker=opts.rate_tracker)
            result.tracerfy_stats = stats
            if stats.get("credits_exhausted"):
                logger.error(
                    "TRACERFY OUT OF CREDITS — skip trace disabled for this run. "
                    "Add credits at https://tracerfy.com/billing to resume."
                )
            logger.info(
                "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                stats.get("matched", 0), stats.get("submitted", 0),
                stats.get("phones_found", 0), stats.get("emails_found", 0),
                stats.get("cost", 0.0),
            )
            if opts.funnel is not None:
                opts.funnel.set("tracerfy_matched", int(stats.get("matched", 0)))
        except Exception as e:
            logger.warning("Tracerfy skip trace failed: %s — continuing", e)
            # Stage exception → zero-fill the gate so it always appears
            # in the Slack block (D-01 invariant).
            if opts.funnel is not None:
                opts.funnel.set("tracerfy_matched", 0)
    elif not opts.skip_tracerfy and not cfg.TRACERFY_API_KEY:
        logger.info("Tracerfy skipped — TRACERFY_API_KEY not configured")
        if opts.funnel is not None:
            opts.funnel.set("tracerfy_matched", 0)
    elif opts.skip_tracerfy or not tracerfy_target:
        # skip_tracerfy=True OR no eligible candidates → zero-fill the gate.
        if opts.funnel is not None:
            opts.funnel.set("tracerfy_matched", 0)

    # DP candidates kept for downstream report generator only (it generates
    # deep-prospecting PDFs for deceased / heir / DM records specifically).
    dp_candidates = [
        n for n in notices
        if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
    ]

    # ── Step 3: Trestle phone scoring (ALL records with phones) ───
    # Score every record that has at least one phone field populated so
    # DataSift filter presets can dial-prioritize across the full sweep.
    #
    # Litigator-check turned OFF 2026-06-12 per operator request — the
    # add-on cost wasn't justified for the daily flow's volume. Re-enable
    # by flipping add_litigator=True if TCPA screening becomes a
    # compliance need.
    phone_candidates = [
        n for n in notices
        if any(
            (getattr(n, attr, "") or "").strip()
            for attr in (
                "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
                "mobile_5", "landline_1", "landline_2", "landline_3",
            )
        )
    ]
    if phone_candidates and cfg.TRESTLE_API_KEY:
        try:
            from phone_validator import score_record_phones
            tiers = score_record_phones(
                phone_candidates,
                cfg.TRESTLE_API_KEY,
                add_litigator=False,
            )
            result.phone_tiers = tiers
            logger.info(
                "Trestle scored %d unique phones across %d records (litigator-check OFF)",
                len(tiers), len(phone_candidates),
            )
        except Exception as e:
            logger.warning("Per-record Trestle scoring failed: %s — continuing", e)

    # ── Step 4: Generate deep-prospecting PDFs ────────────────────
    if not opts.skip_reports and dp_candidates:
        try:
            from report_generator import generate_record_pdf
            opts.report_dir.mkdir(parents=True, exist_ok=True)
            for n in dp_candidates:
                try:
                    pdf_path = generate_record_pdf(
                        n, output_dir=opts.report_dir, phone_tiers=result.phone_tiers,
                    )
                    result.pdf_paths.append((n, pdf_path))
                except Exception:
                    logger.exception("PDF generation failed for %s", n.address)
            logger.info(
                "Generated %d/%d deep-prospecting PDFs in %s",
                len(result.pdf_paths), len(dp_candidates), opts.report_dir,
            )
        except Exception:
            logger.exception("Report generator import failed")
    elif not opts.skip_reports:
        logger.info("No records need deep prospecting PDFs")

    # ── Step 5: DataSift CSV generation (split or unified) ────────
    if not opts.skip_datasift_csv:
        try:
            from datasift_formatter import write_datasift_split_csvs
            csv_infos = write_datasift_split_csvs(
                notices, phone_tiers=result.phone_tiers,
            )
            result.datasift_csv_infos = csv_infos
            for info in csv_infos:
                logger.info("DataSift CSV (%s): %s", info.get("label", "?"), info.get("path", "?"))
        except Exception as e:
            logger.warning("DataSift CSV generation failed: %s — continuing", e)

    return result
