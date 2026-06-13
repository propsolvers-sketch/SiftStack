"""Entry point for SiftStack — full-stack REI operations platform.

Runs as either:
  - Apify Actor (when APIFY_IS_AT_HOME is set — reads input from Actor.get_input())
  - Standalone CLI (python src/main.py daily --counties Knox --types foreclosure)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import config
from config import (
    LOG_DIR,
    SAVED_SEARCHES,
    SavedSearch,
)
from data_formatter import write_csv, write_csv_by_type
from observability import (
    FunnelCounter,
    ServiceRateTracker,
    load_rolling_rates,
    rolling_rates_summary,
    save_rolling_rates,
)
from scraper import scrape_all
from slack_notifier import (
    _send_blocks_webhook,
    build_funnel_block,
    build_service_rates_block,
    build_summary,
)

logger = logging.getLogger(__name__)


# ── Phase 2: daily-flow gate sequence (CONTEXT.md D-01) ─────────────
# Single source of truth so the Apify Actor and CLI paths instantiate
# the same 10-gate sequence in the same order. Each owning file in
# the daily pipeline mutates a subset of these — see the 02-04 plan's
# ownership map for which file stamps which gate.
MAIN_DAILY_GATES: tuple[str, ...] = (
    "scraped",
    "seen_ids_deduped",
    "county_filtered",
    "parsed",
    "tier_gated",
    "al_property_enriched",
    "smarty_standardized",
    "zillow_enriched",
    "tracerfy_matched",
    "datasift_uploaded",
)


def _post_daily_slack_with_funnel(
    notices: list,
    funnel: FunnelCounter,
    rate_tracker: ServiceRateTracker,
    *,
    elapsed_min: float = 0,
    cost_breakdown: dict | None = None,
    upload_result: dict | None = None,
    webhook_url: str | None = None,
) -> bool:
    """Phase 2: post the legacy daily summary + funnel + service-rates blocks.

    Honours the Wave 3 contract:
      - D-02 (one message): a single _send_blocks_webhook POST carrying
        the existing summary text + funnel block + service-rates block.
      - D-03 (rolling-rates ordering): load_rolling_rates BEFORE the
        blocks build (so today's post shows the PRIOR-days baseline),
        save_rolling_rates AFTER a successful send (so today's totals
        advance the window for tomorrow).
      - W6 (failed sends don't pollute the baseline): save_rolling_rates
        is guarded behind ``if sent``.
      - W5 (legacy text path stays byte-identical): send_slack_notification
        is NOT called from this path — the existing daily summary text
        is built via slack_notifier.build_summary and posted via
        _send_blocks_webhook directly.

    Returns True on a successful Slack POST. False on any error (the
    caller already swallows exceptions further up).
    """
    text = build_summary(
        notices,
        upload_result=upload_result,
        elapsed_min=elapsed_min,
        cost_breakdown=cost_breakdown,
    )

    rolling = rolling_rates_summary(load_rolling_rates())
    per_run = rate_tracker.per_run_rates()

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        build_funnel_block(funnel.pipeline_name, funnel.as_ordered_dict()),
        build_service_rates_block(per_run, rolling),
    ]
    sent = _send_blocks_webhook(text, blocks, webhook_url=webhook_url)
    if sent:
        save_rolling_rates(rate_tracker.totals())
    return sent


# ── Shared helpers ────────────────────────────────────────────────────


def _link(path: str, label: str | None = None) -> str:
    """Return an OSC 8 hyperlink when stdout is a TTY, plain path otherwise.

    VS Code's integrated terminal, iTerm2, and modern Terminal.app all render
    this as a clickable link that opens the file in the system default app.
    """
    text = label or path
    if not sys.stdout.isatty():
        return text
    from urllib.parse import quote
    url = "file://" + quote(path)
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _filter_searches(
    counties: list[str] | None,
    types: list[str] | None,
) -> list[SavedSearch]:
    """Filter SAVED_SEARCHES by county and/or notice type."""
    searches = list(SAVED_SEARCHES)

    if counties:
        county_set = {c.lower() for c in counties}
        searches = [s for s in searches if s.county.lower() in county_set]

    if types:
        type_set = {t.lower() for t in types}
        searches = [s for s in searches if s.notice_type.lower() in type_set]

    return searches


# ── Preflight health checks ─────────────────────────────────────────


def _preflight_check(mode: str) -> list[str]:
    """Verify required API keys and service connectivity before running.

    Returns a list of failure descriptions. Empty list = all checks passed.
    """
    failures: list[str] = []

    # ── Credential checks (mode-dependent) ──────────────────────────
    scrape_modes = {"daily", "historical"}
    enrichment_modes = scrape_modes | {"pdf-import", "photo-import", "dropbox-watch", "csv-import"}
    datasift_modes = {"manage-presets", "manage-sold", "phone-validate"}

    if mode in scrape_modes:
        if not config.CAPTCHA_API_KEY:
            failures.append("CAPTCHA_API_KEY not set (CAPTCHA solving will fail)")

    if mode in enrichment_modes:
        # These are warnings, not blockers — pipeline degrades gracefully
        if not config.SMARTY_AUTH_ID or not config.SMARTY_AUTH_TOKEN:
            logger.warning("Preflight: SMARTY credentials missing — address standardization will be skipped")
        if not config.OPENWEBNINJA_API_KEY:
            logger.warning("Preflight: OPENWEBNINJA_API_KEY missing — Zillow enrichment will be skipped")
        if not config.ANTHROPIC_API_KEY:
            logger.warning("Preflight: ANTHROPIC_API_KEY missing — obituary search and LLM parsing will be skipped")

    if mode in datasift_modes:
        if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
            failures.append("DATASIFT_EMAIL / DATASIFT_PASSWORD not set (required for DataSift operations)")

    if mode == "dropbox-watch":
        if not config.DROPBOX_APP_KEY or not config.DROPBOX_APP_SECRET or not config.DROPBOX_REFRESH_TOKEN:
            failures.append("DROPBOX credentials incomplete (need APP_KEY, APP_SECRET, REFRESH_TOKEN)")

    if mode == "phone-validate":
        if not config.TRESTLE_API_KEY:
            failures.append("TRESTLE_API_KEY not set (required for phone validation)")

    # ── Connectivity checks (only for scrape modes) ─────────────────
    if mode in scrape_modes:
        import requests as _requests
        try:
            resp = _requests.head(config.BASE_URL, timeout=10, allow_redirects=True)
            if resp.status_code >= 500:
                failures.append(f"alabamapublicnotices.com returned {resp.status_code} — site may be down")
        except Exception as e:
            failures.append(f"Cannot reach alabamapublicnotices.com: {e}")

    # ── 2Captcha balance check ──────────────────────────────────────
    if mode in scrape_modes and config.CAPTCHA_API_KEY:
        import requests as _requests
        try:
            resp = _requests.get(
                f"https://2captcha.com/res.php?key={config.CAPTCHA_API_KEY}&action=getbalance",
                timeout=10,
            )
            balance_text = resp.text.strip()
            try:
                balance = float(balance_text)
                if balance < 0.50:
                    failures.append(f"2Captcha balance too low: ${balance:.2f} (need at least $0.50)")
                else:
                    logger.info("Preflight: 2Captcha balance: $%.2f", balance)
            except ValueError:
                if "ERROR" in balance_text:
                    failures.append(f"2Captcha API key invalid: {balance_text}")
        except Exception as e:
            logger.warning("Preflight: Could not check 2Captcha balance: %s", e)

    return failures


# ── Apify Actor mode ─────────────────────────────────────────────────


async def actor_main() -> None:
    """Run as an Apify Actor — full automated pipeline.

    Scrape → Enrich → Tracerfy → DataSift Upload → Slack Notification.
    """
    from apify import Actor
    from time import time as _time

    # Set up Python logging so all modules output at INFO level
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async with Actor:
        pipeline_start = _time()
        actor_input = await Actor.get_input() or {}

        # Override config credentials from Actor input.
        # Set both config.* AND os.environ so downstream modules that read
        # from either source (e.g., datasift_uploader uses os.environ) pick them up.
        _cred_map = {
            "CAPTCHA_API_KEY": actor_input.get("captcha_api_key", ""),
            "ANTHROPIC_API_KEY": actor_input.get("anthropic_api_key", ""),
            "SMARTY_AUTH_ID": actor_input.get("smarty_auth_id", ""),
            "SMARTY_AUTH_TOKEN": actor_input.get("smarty_auth_token", ""),
            "OPENWEBNINJA_API_KEY": actor_input.get("openwebninja_api_key", ""),
            "SERPER_API_KEY": actor_input.get("serper_api_key", ""),
            "FIRECRAWL_API_KEY": actor_input.get("firecrawl_api_key", ""),
            "TRACERFY_API_KEY": actor_input.get("tracerfy_api_key", ""),
            "DATASIFT_EMAIL": actor_input.get("datasift_email", ""),
            "DATASIFT_PASSWORD": actor_input.get("datasift_password", ""),
            "SLACK_WEBHOOK_URL": actor_input.get("slack_webhook_url", ""),
            "TRESTLE_API_KEY": actor_input.get("trestle_api_key", ""),
        }
        for key, val in _cred_map.items():
            setattr(config, key, val)
            if val:
                os.environ[key] = val

        mode = actor_input.get("mode", "daily")
        counties = actor_input.get("counties") or None
        types = actor_input.get("types") or None
        since_date_override = actor_input.get("since_date", "").strip()
        start_page = int(actor_input.get("start_page", 1) or 1)
        drive_folder_id = actor_input.get("google_drive_folder_id", "")
        drive_key_b64 = actor_input.get("google_service_account_key", "")

        # Pipeline toggles
        do_tracerfy = actor_input.get("run_tracerfy", True)
        do_notify_slack = actor_input.get("notify_slack", True)

        # Buy box / filter toggles
        include_vacant = actor_input.get("include_vacant", False)
        include_commercial = actor_input.get("include_commercial", False)
        include_entities = actor_input.get("include_entities", False)

        # Validate. The AL site (alabamapublicnotices.com) is loginless, so
        # only CAPTCHA credentials are required for the scrape itself. The
        # legacy TNPN_EMAIL / TNPN_PASSWORD validator that lived here used to
        # crash on cold-start with AttributeError after config.py removed
        # those constants in the AL migration.
        if not config.CAPTCHA_API_KEY:
            Actor.log.warning("captcha_api_key not set — CAPTCHA solving will fail")

        # Filter searches
        searches = _filter_searches(counties, types)
        if not searches:
            Actor.log.error("No saved searches match the given counties/types filters")
            await Actor.fail(status_message="No matching saved searches")
            return

        Actor.log.info(
            "Running %d saved searches: %s",
            len(searches),
            ", ".join(f"{s.county} {s.notice_type}" for s in searches),
        )

        # Set up residential proxy if requested
        proxy_url: str | None = None
        use_proxy = actor_input.get("use_residential_proxy", True)
        if use_proxy:
            try:
                proxy_config = await Actor.create_proxy_configuration(
                    groups=["RESIDENTIAL"]
                )
                proxy_url = await proxy_config.new_url()
                Actor.log.info("Residential proxy configured")
            except Exception:
                Actor.log.warning("Could not configure residential proxy — running without proxy")

        # Track seen notice IDs for incremental dedup
        seen_ids: set[str] = set()

        def _notice_id(url: str) -> str:
            import re
            m = re.search(r"[?&]ID=(\d+)", url)
            return m.group(1) if m else ""

        async def push_batch(batch_notices):
            """Push new unique notices to dataset immediately after each search."""
            unique = []
            for n in batch_notices:
                nid = _notice_id(n.source_url)
                if nid and nid in seen_ids:
                    continue
                if nid:
                    seen_ids.add(nid)
                unique.append(n)
            if unique:
                await Actor.push_data([
                    {
                        "date_added": n.date_added,
                        "address": n.address,
                        "city": n.city,
                        "state": n.state,
                        "zip": n.zip,
                        "owner_name": n.owner_name,
                        "notice_type": n.notice_type,
                        "county": n.county,
                        "decedent_name": n.decedent_name,
                        "owner_street": n.owner_street,
                        "owner_city": n.owner_city,
                        "owner_state": n.owner_state,
                        "owner_zip": n.owner_zip,
                        "auction_date": n.auction_date,
                        "zip_plus4": n.zip_plus4,
                        "latitude": n.latitude,
                        "longitude": n.longitude,
                        "dpv_match_code": n.dpv_match_code,
                        "vacant": n.vacant,
                        "rdi": n.rdi,
                        "mls_status": n.mls_status,
                        "mls_listing_price": n.mls_listing_price,
                        "mls_last_sold_date": n.mls_last_sold_date,
                        "mls_last_sold_price": n.mls_last_sold_price,
                        "estimated_value": n.estimated_value,
                        "estimated_equity": n.estimated_equity,
                        "equity_percent": n.equity_percent,
                        "property_type": n.property_type,
                        "bedrooms": n.bedrooms,
                        "bathrooms": n.bathrooms,
                        "sqft": n.sqft,
                        "year_built": n.year_built,
                        "lot_size": n.lot_size,
                        "source_url": n.source_url,
                        "raw_text": n.raw_text[:5000] if n.raw_text else "",
                    }
                    for n in unique
                ])
                Actor.log.info("Pushed %d records to dataset (incremental)", len(unique))

        # Log LLM parser status
        if config.ANTHROPIC_API_KEY:
            Actor.log.info("LLM fallback enabled (Claude Haiku) for missing fields")
        else:
            Actor.log.info("LLM fallback disabled — set anthropic_api_key to enable")

        if start_page > 1:
            Actor.log.info("Starting from page %d (skipping earlier pages)", start_page)

        try:
            kvs = await Actor.open_key_value_store()

            # ── Load last_run_date from Apify KVS (persists between runs) ──
            if mode == "daily" and not since_date_override:
                stored = await kvs.get_value("last_run_date")
                if stored:
                    since_date_override = stored
                    Actor.log.info("Daily mode: using stored last_run_date = %s", stored)
                else:
                    Actor.log.info("Daily mode: no stored last_run_date, defaulting to 7 days")

            # ── Load cross-run seen-ID cache from KVS (makes daily re-runs idempotent) ──
            seen_ids = await kvs.get_value("seen_notice_ids") or {}
            Actor.log.info("Loaded %d previously-seen notice IDs from KVS", len(seen_ids))

            async def persist_seen_ids(ids: dict) -> None:
                """Mid-run persistence — if a later search crashes, progress is kept."""
                try:
                    await kvs.set_value("seen_notice_ids", ids)
                    await kvs.set_value(
                        "last_run_date",
                        datetime.now().strftime("%Y-%m-%d"),
                    )
                except Exception as e:
                    Actor.log.warning("Failed to persist seen_notice_ids to KVS: %s", e)

            # ── Phase 2: instantiate FunnelCounter + ServiceRateTracker
            # ── before scrape so all 4 services route through the same
            # ── tracker, and all 10 gates stamp into the same funnel.
            funnel = FunnelCounter("main_daily", gates=list(MAIN_DAILY_GATES))
            rate_tracker = ServiceRateTracker()

            # ── Scrape ────────────────────────────────────────────────
            notices = await scrape_all(
                mode=mode, searches=searches, proxy_url=proxy_url, on_batch=push_batch,
                since_date_override=since_date_override or None,
                llm_api_key=config.ANTHROPIC_API_KEY or None,
                start_page=start_page,
                seen_ids=seen_ids,
                on_search_complete=persist_seen_ids,
                rate_tracker=rate_tracker,
            )
            # Phase 2 funnel: scraped + seen_ids_deduped both take the
            # same count because the scraper applies seen_ids dedup
            # internally — the returned list is the post-dedup survivor
            # set. If a pre-dedup count becomes accessible from scrape_all
            # stats in a future build, swap "scraped" to that value.
            funnel.set("scraped", len(notices))
            funnel.set("seen_ids_deduped", len(notices))
            # Handle async probate lookup before pipeline (requires await)
            probate_notices = [n for n in notices if n.notice_type == "probate" and n.decedent_name and not n.address]
            if probate_notices:
                try:
                    from property_lookup import lookup_decedent_properties
                    Actor.log.info("Looking up property addresses for %d probate notices...", len(probate_notices))
                    await lookup_decedent_properties(probate_notices)
                except ImportError:
                    Actor.log.warning("property_lookup module not found -- skipping property lookup")
                except Exception as e:
                    Actor.log.warning("Property lookup failed: %s -- continuing without lookups", e)

            # ── Shared post-scrape pipeline ───────────────────────────
            # full_pipeline.run_full_pipeline runs enrichment + Tracerfy +
            # Trestle + PDF gen + DataSift CSV gen identically to the CLI
            # path. Apify-specific bits (KVS upload, Drive upload, Slack
            # cost breakdown) stay below since their semantics differ.
            from full_pipeline import PostScrapeOptions, run_full_pipeline

            opts = PostScrapeOptions(
                skip_vacant_filter=include_vacant,
                skip_commercial_filter=include_commercial,
                skip_entity_filter=include_entities,
                skip_tracerfy=not do_tracerfy,
                tracerfy_dp_only=True,    # Apify: cost control — DP candidates only
                source_label="Apify Actor",
                # Phase 2: thread funnel + rate_tracker so
                # full_pipeline / enrichment_pipeline stamp gates and
                # service callers route into the same tracker.
                funnel=funnel,
                rate_tracker=rate_tracker,
            )
            result = run_full_pipeline(notices, opts)
            notices = result.notices
            tracerfy_stats = result.tracerfy_stats or None  # for cost breakdown

            if not notices:
                Actor.log.warning("No notices found")
                return

            total = len(notices)

            # ── Upload deep-prospecting PDFs to KVS, build URL list ───
            pdf_urls = []
            if result.pdf_paths:
                try:
                    kvs = await Actor.open_key_value_store()
                    kvs_id = kvs._id if hasattr(kvs, '_id') else ''
                    for n, pdf_path in result.pdf_paths:
                        key = pdf_path.name
                        with open(pdf_path, "rb") as f:
                            await kvs.set_value(key, f.read(), content_type="application/pdf")
                        url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                        pdf_urls.append({"address": n.address, "url": url})
                    Actor.log.info("Uploaded %d deep prospecting PDFs to KVS", len(pdf_urls))
                except Exception as e:
                    Actor.log.warning("PDF KVS upload failed: %s — continuing", e)

            # ── Write master CSV + upload to KVS ──────────────────────
            csv_path = write_csv(notices)
            # Phase 2 funnel: datasift_uploaded gate fires after CSV write
            # so the Slack block reflects the count actually written.
            funnel.set("datasift_uploaded", len(notices))
            if not kvs:
                kvs = await Actor.open_key_value_store()
            with open(csv_path, "rb") as f:
                await kvs.set_value("output.csv", f.read(), content_type="text/csv")
            Actor.log.info("CSV saved to key-value store as 'output.csv'")

            # ── Google Drive Upload ───────────────────────────────────
            if drive_folder_id and drive_key_b64:
                Actor.log.info("Uploading to Google Drive...")
                from drive_uploader import upload_csv, upload_summary

                by_type: dict[str, int] = {}
                by_county: dict[str, int] = {}
                for n in notices:
                    by_type[n.notice_type] = by_type.get(n.notice_type, 0) + 1
                    by_county[n.county] = by_county.get(n.county, 0) + 1

                file_id = upload_csv(csv_path, drive_folder_id, drive_key_b64, total)
                if file_id:
                    Actor.log.info("CSV uploaded to Drive (file ID: %s)", file_id)
                else:
                    Actor.log.error("CSV upload to Drive failed — CSV still in key-value store")

                upload_summary(by_type, by_county, total, drive_folder_id, drive_key_b64)
            elif drive_folder_id:
                Actor.log.warning("google_drive_folder_id set but google_service_account_key missing — skipping Drive upload")

            # ── Upload DataSift CSVs to KVS, build URL list ───────────
            # KVS-only delivery is more reliable than Playwright in headless
            # cloud containers — operator downloads from KVS and uploads
            # manually via DataSift web UI.
            datasift_csv_urls = []
            if result.datasift_csv_infos:
                try:
                    if not kvs:
                        kvs = await Actor.open_key_value_store()
                    kvs_id = kvs._id if hasattr(kvs, '_id') else ''
                    for info in result.datasift_csv_infos:
                        key = f"datasift_{info['label'].lower().replace(' ', '_')}.csv"
                        with open(info["path"], "rb") as f:
                            await kvs.set_value(key, f.read(), content_type="text/csv")
                        url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                        datasift_csv_urls.append({
                            "label": info["label"],
                            "url": url,
                            "records": info.get("count", "?"),
                        })
                        Actor.log.info("DataSift CSV (%s) saved to KVS: %s", info["label"], key)
                except Exception as e:
                    Actor.log.error("DataSift CSV KVS upload failed: %s", e)

            # ── Slack Notification ────────────────────────────────────
            elapsed_min = (_time() - pipeline_start) / 60

            # Compute estimated run cost
            cost_breakdown = {}
            # 2Captcha: $0.003 per solve, ~1 solve per notice scraped
            captcha_count = total  # each notice detail page requires a CAPTCHA
            cost_breakdown["2Captcha"] = round(captcha_count * 0.003, 2)
            # Anthropic Haiku: ~$0.001 per record (LLM parsing + obituary search)
            if config.ANTHROPIC_API_KEY:
                cost_breakdown["Anthropic (Haiku)"] = round(total * 0.001, 3)
            # Tracerfy: actual cost from batch stats
            if tracerfy_stats and tracerfy_stats.get("cost", 0) > 0:
                cost_breakdown["Tracerfy"] = round(tracerfy_stats["cost"], 2)
            # Smarty: free tier 250/month, $0.01 after
            smarty_count = sum(1 for n in notices if n.dpv_match_code)
            if smarty_count > 0:
                cost_breakdown["Smarty"] = round(max(0, smarty_count - 250) * 0.01, 2) if smarty_count > 250 else 0.0
            # Zillow (OpenWeb Ninja): free tier 100/month, $0.01 after
            zillow_count = sum(1 for n in notices if n.estimated_value)
            if zillow_count > 0:
                cost_breakdown["Zillow"] = round(max(0, zillow_count - 100) * 0.01, 2) if zillow_count > 100 else 0.0
            # Remove zero-cost entries for cleaner display
            cost_breakdown = {k: v for k, v in cost_breakdown.items() if v > 0}

            if do_notify_slack and config.SLACK_WEBHOOK_URL:
                try:
                    from slack_notifier import _send_webhook

                    # Phase 2: post the daily summary + funnel block +
                    # service-rates block in ONE Block Kit message (D-02:
                    # one message, more content). Rolling-rates ordering
                    # (D-03) and save-on-success-only (W6) are enforced
                    # inside the helper. The legacy text-only
                    # send_slack_notification path stays byte-identical
                    # for any caller that still uses it (W5).
                    _post_daily_slack_with_funnel(
                        notices,
                        funnel,
                        rate_tracker,
                        elapsed_min=elapsed_min,
                        cost_breakdown=cost_breakdown,
                    )

                    # Send DataSift CSV download links as a follow-up message
                    if datasift_csv_urls:
                        csv_lines = [
                            "*DataSift CSVs ready for manual upload:*",
                        ]
                        for csv_info in datasift_csv_urls:
                            csv_lines.append(f"  <{csv_info['url']}|{csv_info['label']}> ({csv_info['records']} records)")
                        csv_lines.append("_Upload at app.reisift.io → Upload File → Add Data_")
                        _send_webhook("\n".join(csv_lines))

                    # Send PDF download links
                    if pdf_urls:
                        pdf_lines = [
                            f"*Deep Prospecting PDFs ({len(pdf_urls)} records):*",
                        ]
                        for pdf_info in pdf_urls:
                            pdf_lines.append(f"  <{pdf_info['url']}|{pdf_info['address']}>")
                        pdf_lines.append("_Attach to DataSift record → Notes or Files_")
                        _send_webhook("\n".join(pdf_lines))

                    Actor.log.info("Slack notification sent")
                except Exception as e:
                    Actor.log.warning("Slack notification failed: %s", e)

            # ── Save last_run_date + seen_notice_ids to Apify KVS for next run ─────
            await kvs.set_value("last_run_date", datetime.now().strftime("%Y-%m-%d"))
            await kvs.set_value("seen_notice_ids", seen_ids)
            Actor.log.info(
                "Saved last_run_date + %d seen_notice_ids to KVS for next daily run",
                len(seen_ids),
            )

            # D-04 — terminal mirrors Slack: always log the funnel at
            # end-of-run regardless of whether --notify-slack was set.
            logger.info(
                "Funnel (%s): %s",
                funnel.pipeline_name, dict(funnel.as_ordered_dict()),
            )

            Actor.log.info("Done — %d notices exported (%.1f min)", total, elapsed_min)

        except Exception as e:
            Actor.log.error("Pipeline failed: %s", e, exc_info=True)
            try:
                from slack_notifier import notify_error
                notify_error("Apify Actor Pipeline", e, context=f"mode={mode}")
            except Exception:
                pass
            await Actor.fail(status_message=f"Pipeline error: {e}")


# ── CLI mode ──────────────────────────────────────────────────────────


def setup_logging(verbose: bool = False) -> None:
    """Configure logging to both console and date-stamped log file."""
    level = logging.DEBUG if verbose else logging.INFO
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"scrape_{timestamp}.log"

    # Force UTF-8 on console output to avoid cp1252 encoding errors on Windows
    console = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )
    handlers: list[logging.Handler] = [
        console,
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

    # Silence noisy 3rd-party DEBUG output even when -v is on. pdfminer
    # logs every PDF token at DEBUG (Madison newspaper PDF parses can
    # emit 100K+ lines of these per run). urllib3 / httpcore / httpx /
    # anthropic._base_client log every HTTP connect/close/send/receive
    # at DEBUG — fine for one-shot debugging but disastrous for a 3h
    # cron run (operator runtime audit 2026-06-11 found a single run
    # log was 83 MB, almost entirely this noise). We still get
    # WARN/ERROR from these modules so real connection problems surface.
    for noisy in (
        "pdfminer", "pdfminer.psparser", "pdfminer.pdfinterp",
        "pdfminer.pdfparser", "pdfminer.cmapdb",
        "urllib3", "urllib3.connectionpool",
        "httpcore", "httpcore.http11", "httpcore.connection",
        "httpx",
        "anthropic._base_client",
        "playwright",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.info("Logging to %s", log_file)


def _run_pdf_import(args) -> None:
    """Run the PDF import pipeline: OCR → parse → enrich → CSV."""
    from pdf_importer import process_pdf
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.pdf_path:
        logging.error("--pdf-path is required for pdf-import mode")
        sys.exit(1)
    if not args.pdf_county:
        logging.error("--pdf-county is required for pdf-import mode")
        sys.exit(1)

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        logging.error("PDF file not found: %s", pdf_path)
        sys.exit(1)

    county = args.pdf_county.strip().title()  # "knox" → "Knox"

    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_pdf(
        pdf_path=pdf_path,
        county=county,
        api_key=api_key,
        date_added=args.pdf_date,
        regex_only=args.regex_only,
    )

    if not notices:
        logging.warning("No records extracted from PDF")
        sys.exit(0)

    # Run unified enrichment pipeline
    opts = PipelineOptions(
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"PDF import ({pdf_path.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_tax_sale_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_photo_import(args) -> None:
    """Run the photo import pipeline: preprocess → OCR → parse → enrich → CSV."""
    from photo_importer import process_photos
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.folder:
        logging.error("--folder is required for photo-import mode")
        sys.exit(1)
    if not args.photo_county:
        logging.error("--photo-county is required for photo-import mode")
        sys.exit(1)
    if not args.photo_type:
        logging.error("--photo-type is required for photo-import mode")
        sys.exit(1)

    folder = Path(args.folder)
    if not folder.exists() or not folder.is_dir():
        logging.error("Folder not found: %s", folder)
        sys.exit(1)

    county = args.photo_county.strip().title()

    notice_type = args.photo_type.strip().lower()
    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_photos(
        folder=folder,
        county=county,
        notice_type=notice_type,
        date_added=args.photo_date,
        api_key=api_key,
        correct_perspective=not getattr(args, "no_perspective_correct", False),
    )

    if not notices:
        logging.warning("No records extracted from photos")
        sys.exit(0)

    # Run unified enrichment pipeline
    # Skip vacant land filter for notice types without property addresses
    # (probate from court terminals never has property address — would filter everything)
    no_address_types = {"probate", "divorce"}
    opts = PipelineOptions(
        skip_vacant_filter=getattr(args, "include_vacant", False) or notice_type in no_address_types,
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"Photo import ({folder.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_{notice_type}_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_csv_import(args) -> None:
    """Run the CSV re-import pipeline: read CSV → enrich → write new CSV.

    Supports multiple CSV paths (comma-separated) for merging datasets.
    Supports --upload-datasift to format and upload to DataSift after enrichment.
    """
    from data_formatter import read_csv
    from enrichment_pipeline import (
        PipelineOptions,
        detect_existing_enrichment,
        run_enrichment_pipeline,
    )

    # Validate required args
    if not args.csv_path:
        logging.error("--csv-path is required for csv-import mode")
        sys.exit(1)

    # Support multiple CSV paths (comma-separated)
    csv_paths = [Path(p.strip()) for p in args.csv_path.split(",")]
    for cp in csv_paths:
        if not cp.exists():
            logging.error("CSV file not found: %s", cp)
            sys.exit(1)

    county = None
    if args.csv_county:
        county = args.csv_county.strip().title()

    # Read all CSVs → NoticeData, merge
    all_notices = []
    for cp in csv_paths:
        batch = read_csv(cp)
        logging.info("Loaded %d records from %s", len(batch), cp.name)
        all_notices.extend(batch)

    if not all_notices:
        logging.warning("No records found in CSV(s)")
        sys.exit(0)

    # Deduplicate by source_url (notice ID) — keeps most recent
    seen_urls = {}
    for n in all_notices:
        url = getattr(n, "source_url", "") or ""
        if url and url in seen_urls:
            # Keep the one with more enrichment data
            existing = seen_urls[url]
            if (getattr(n, "estimated_value", "") or "") and not (getattr(existing, "estimated_value", "") or ""):
                seen_urls[url] = n
        elif url:
            seen_urls[url] = n
        else:
            # No source_url — keep all (dedup by address later)
            seen_urls[id(n)] = n
    notices = list(seen_urls.values())
    if len(notices) < len(all_notices):
        logging.info("Deduped %d → %d records (by source_url)", len(all_notices), len(notices))

    # Override county if provided (for CSVs without county column)
    if county:
        for n in notices:
            if not n.county.strip():
                n.county = county

    logging.info("Total: %d records from %d CSV(s)", len(notices), len(csv_paths))

    # Build pipeline options
    primary_name = csv_paths[0].name
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"CSV import ({primary_name})",
    )
    detect_existing_enrichment(notices, opts)
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{csv_paths[0].stem}_reimport_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)

    # DataSift upload (same logic as daily/historical mode)
    if getattr(args, "upload_datasift", False):
        from datasift_formatter import write_datasift_split_csvs
        from datasift_uploader import upload_to_datasift_per_distressor

        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        csv_infos = write_datasift_split_csvs(notices)
        for info in csv_infos:
            logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

        per_csv = []
        for info in csv_infos:
            r = asyncio.run(
                upload_to_datasift_per_distressor(
                    info["path"], enrich=do_enrich, skip_trace=do_skip_trace,
                )
            )
            r["label"] = info["label"]
            per_csv.append(r)
        all_ok = all(r.get("success") for r in per_csv)
        upload_result = {
            "success": all_ok,
            "message": "; ".join(
                f"{r['label']}: {r.get('message', '')}" for r in per_csv
            ),
            "csvs": per_csv,
            "enrich_result": per_csv[0].get("enrich_result", {}) if per_csv else {},
            "skip_trace_result": per_csv[0].get("skip_trace_result", {}) if per_csv else {},
        }

        if upload_result.get("success"):
            logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
        else:
            logging.error("DataSift upload failed: %s", upload_result.get("message"))

    logging.info("Done — %d records exported", len(notices))


def _run_phone_validate(args) -> None:
    """Run phone validation via Trestle API with DataSift export/upload."""
    import json as _json

    csv_path = getattr(args, "csv_path", None)
    list_name = getattr(args, "list_name", None)
    preset_folder = getattr(args, "preset_folder", None)
    all_records = getattr(args, "all_records", False)

    # Must specify at least one targeting mode
    if not csv_path and not list_name and not preset_folder and not all_records:
        logging.error(
            "phone-validate requires one of: --csv-path, --list-name, --preset-folder, or --all-records"
        )
        sys.exit(1)

    # Parse custom tiers if provided
    tiers = None
    custom_tiers_str = getattr(args, "custom_tiers", None)
    if custom_tiers_str:
        try:
            raw = _json.loads(custom_tiers_str)
            tiers = {k: tuple(v) for k, v in raw.items()}
            logging.info("Using custom tiers: %s", tiers)
        except (_json.JSONDecodeError, ValueError) as e:
            logging.error("Invalid --custom-tiers JSON: %s", e)
            sys.exit(1)

    # Estimate-only mode
    if getattr(args, "estimate", False):
        from phone_validator import estimate_cost, print_estimate

        if csv_path:
            est = estimate_cost(csv_path)
            print_estimate(est)
        else:
            logging.error("--estimate requires --csv-path (export from DataSift first, then estimate)")
            sys.exit(1)
        return

    # Full validation workflow
    from datasift_uploader import run_phone_validation_workflow

    result = asyncio.run(run_phone_validation_workflow(
        list_name=list_name,
        preset_folder=preset_folder,
        all_records=all_records,
        csv_path=csv_path,
        upload_tags=not getattr(args, "no_upload", False),
        api_key=config.TRESTLE_API_KEY or None,
        tiers=tiers,
        add_litigator=getattr(args, "add_litigator", False),
        batch_size=getattr(args, "batch_size", 10),
    ))

    if result.get("success"):
        logging.info("Phone validation: %s", result.get("message", "OK"))
        if result.get("validation_result"):
            vr = result["validation_result"]
            logging.info("  Results: %d scored, %d errors", vr.get("results_count", 0), vr.get("errors_count", 0))
            for tag, count in vr.get("tier_counts", {}).items():
                logging.info("    %s: %d", tag, count)
        if result.get("upload_result"):
            logging.info("  Tag upload: %s", result["upload_result"].get("message", ""))
    else:
        logging.error("Phone validation failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_presets(args) -> None:
    """Run the DataSift filter preset management workflow."""
    from datasift_uploader import run_manage_presets_workflow

    discover = getattr(args, "discover", False)
    add_sold = getattr(args, "add_sold_exclusion", False)
    create_seq = getattr(args, "create_sold_sequence", False)

    # Default to discover if no flags specified
    if not (discover or add_sold or create_seq):
        discover = True

    preset_folders = None
    if getattr(args, "preset_folders", None):
        preset_folders = [f.strip() for f in args.preset_folders.split(",")]

    result = asyncio.run(run_manage_presets_workflow(
        discover=discover,
        add_sold_exclusion=add_sold,
        create_sequence=create_seq,
        preset_folders=preset_folders,
    ))

    if result.get("success"):
        logging.info("Manage presets: %s", result.get("message", "OK"))
        if result.get("discovery"):
            disc = result["discovery"]
            for folder, presets in disc.get("preset_folders", {}).items():
                logging.info("  Folder '%s': %s", folder, presets)
            logging.info("  Sequences: %s", disc.get("sequences", []))
        if result.get("presets"):
            p = result["presets"]
            logging.info("  Updated: %s", p.get("updated", []))
            logging.info("  Failed: %s", p.get("failed", []))
        if result.get("sequence"):
            logging.info("  Sequence: %s", result["sequence"].get("message"))
    else:
        logging.error("Manage presets failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_sold(args) -> None:
    """Run the SiftMap sold properties management workflow."""
    from datasift_uploader import run_manage_sold_workflow

    # Parse counties if provided, otherwise use default (Knox, Blount)
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip().title() for c in args.counties.split(",")]

    result = asyncio.run(run_manage_sold_workflow(
        counties=counties,
        months_back=getattr(args, "months_back", 1),
        min_sale_price=getattr(args, "min_sale_price", 1000),
        sold_tag_date=getattr(args, "sold_tag_date", None),
    ))

    if result.get("success"):
        logging.info("Manage sold: %s", result.get("message", "OK"))
        logging.info("  Counties: %s", ", ".join(result.get("counties_processed", [])))
        logging.info("  Total records: %d", result.get("total_records", 0))
    else:
        logging.error("Manage sold failed: %s", result.get("message"))
        sys.exit(1)


def cli_main() -> None:
    """Run as standalone CLI."""
    parser = argparse.ArgumentParser(
        description="SiftStack — full-stack REI operations platform"
    )
    parser.add_argument(
        "mode",
        choices=[
            "daily", "historical", "pdf-import", "photo-import", "dropbox-watch",
            "csv-import", "phone-validate", "manage-sold", "manage-presets",
            # New analysis & workflow modes
            "comp", "rehab", "analyze-deal", "analyze", "market-analysis", "buyer-prospect",
            "deep-prospect", "lead-manage", "setup-sequences", "niche-sequential",
            "playbook",
        ],
        help=(
            "daily/historical = scrape notices; pdf-import/photo-import = import from files; "
            "dropbox-watch = poll Dropbox; csv-import = re-enrich CSV; "
            "phone-validate = Trestle scoring; manage-sold/manage-presets = DataSift ops; "
            "comp = comparable sales ARV; rehab = rehab cost estimate; "
            "analyze-deal = full deal analysis; market-analysis = zip code scoring; "
            "buyer-prospect = cash buyer lists; deep-prospect = 4-level research; "
            "lead-manage = 4 Pillars qualification; setup-sequences = CRM automation; "
            "niche-sequential = marketing cycle; playbook = SOP generator"
        ),
    )
    parser.add_argument(
        "--counties",
        type=str,
        default=None,
        help='Comma-separated counties to scrape (e.g. "Knox,Blount" or "all")',
    )
    parser.add_argument(
        "--types",
        type=str,
        default=None,
        help='Comma-separated notice types (e.g. "foreclosure,probate" or "all")',
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Output separate CSV files per notice type",
    )
    parser.add_argument(
        "--no-raw-csv",
        action="store_true",
        help="Skip the legacy raw `al_notices_<ts>.csv` export. The DataSift "
             "upload CSVs (datasift_upload_DMs_*.csv / _Heirs_*.csv) are "
             "produced regardless. Use this flag for daily workflows where "
             "you only need the DataSift uploads and want fewer output files.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override date cutoff (YYYY-MM-DD). Overrides daily/historical mode logic.",
    )
    parser.add_argument(
        "--max-notices",
        type=int,
        default=0,
        help="Stop after scraping this many notices (0 = no limit)",
    )
    parser.add_argument(
        "--tiers",
        type=str,
        default="1,2",
        help="Comma-separated ZIP tiers to KEEP after enrichment "
             "(default '1,2' — drop notices whose property ZIP isn't in our "
             "Tier 1 or Tier 2 investor-target set). Use 'all' to disable.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    # PDF import arguments
    parser.add_argument(
        "--pdf-path",
        type=str,
        default=None,
        help="Path to scanned tax sale PDF (required for pdf-import mode)",
    )
    parser.add_argument(
        "--pdf-county",
        type=str,
        default=None,
        help='County name for PDF import, e.g. "Knox" (required for pdf-import mode)',
    )
    parser.add_argument(
        "--pdf-date",
        type=str,
        default=None,
        help="Date for PDF records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--regex-only",
        action="store_true",
        help="Skip LLM parsing and use regex only (pdf-import mode)",
    )
    # Photo import arguments
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Path to folder of phone photos (required for photo-import mode)",
    )
    parser.add_argument(
        "--photo-county",
        type=str,
        default=None,
        dest="photo_county",
        help='County name for photo import, e.g. "Knox" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-type",
        type=str,
        default=None,
        dest="photo_type",
        help='Notice type for photo import, e.g. "eviction" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-date",
        type=str,
        default=None,
        help="Date for photo records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--no-perspective-correct",
        action="store_true",
        dest="no_perspective_correct",
        help="Skip perspective correction in photo preprocessing (photo-import mode)",
    )
    # Dropbox watcher arguments
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        dest="poll_interval",
        help="Seconds between Dropbox polls (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        dest="max_polls",
        help="Maximum number of poll cycles (default: infinite)",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        dest="no_delete",
        help="Don't delete photos from Dropbox after processing",
    )
    # CSV import arguments
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help="Path to existing CSV file to re-enrich (required for csv-import mode)",
    )
    parser.add_argument(
        "--csv-county",
        type=str,
        default=None,
        help='County name for CSV import, e.g. "Knox" (sets county for records missing it)',
    )

    parser.add_argument(
        "--skip-smarty",
        action="store_true",
        help="Skip Smarty address standardization",
    )
    parser.add_argument(
        "--skip-zillow",
        action="store_true",
        help="Skip Zillow property enrichment",
    )
    parser.add_argument(
        "--skip-tax",
        action="store_true",
        help="Skip tax delinquency enrichment",
    )
    parser.add_argument(
        "--skip-obituary",
        action="store_true",
        help="Skip obituary search for deceased owner detection",
    )
    parser.add_argument(
        "--skip-ancestry",
        action="store_true",
        help="Skip Ancestry.com lookup (SSDI + obituary collection)",
    )
    parser.add_argument(
        "--skip-geocode",
        action="store_true",
        help="Skip reverse geocode retry for failed Smarty lookups",
    )
    parser.add_argument(
        "--skip-dm-address",
        action="store_true",
        help="Skip decision-maker mailing address lookup",
    )
    parser.add_argument(
        "--skip-heir-verification",
        action="store_true",
        help="Skip heir alive/dead verification loop (still runs obituary search)",
    )
    parser.add_argument(
        "--max-heir-depth",
        type=int,
        default=2,
        help="Max recursion depth for heir verification (default: 2)",
    )
    parser.add_argument(
        "--tracerfy-tier1",
        action="store_true",
        help="Use Tracerfy as primary DM address lookup ($0.02/record)",
    )
    parser.add_argument(
        "--skip-tracerfy",
        action="store_true",
        help="Skip Tracerfy batch skip trace (phones + emails) before DataSift upload",
    )
    parser.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama", "openrouter"],
        default=os.getenv("LLM_BACKEND", "anthropic"),
        help="LLM backend: 'anthropic' (Claude Haiku, paid) or 'ollama' (local, free)",
    )
    parser.add_argument(
        "--research-entities",
        action="store_true",
        help="Research entity-owned properties to find the person behind LLCs/Corps (web search + LLM)",
    )
    # Buy box / filter toggles — control which property types pass through
    parser.add_argument(
        "--include-vacant",
        action="store_true",
        help="Keep vacant land parcels (default: filtered out). Use if your buy box includes land deals.",
    )
    parser.add_argument(
        "--include-commercial",
        action="store_true",
        help="Keep commercial properties (default: filtered out). Use if your buy box includes commercial.",
    )
    parser.add_argument(
        "--include-entities",
        action="store_true",
        help="Keep entity-owned records (LLC, Corp, etc.) without filtering. Default: removed unless --research-entities finds a person.",
    )
    parser.add_argument(
        "--upload-datasift",
        action="store_true",
        help="Upload results to DataSift.ai via Playwright (requires DATASIFT_EMAIL/PASSWORD)",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip DataSift property enrichment after upload",
    )
    parser.add_argument(
        "--no-skip-trace",
        action="store_true",
        help="Skip DataSift skip trace after upload",
    )
    parser.add_argument(
        "--notify-slack",
        action="store_true",
        help="Send run summary to Slack/Discord webhook (requires SLACK_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--audit-records",
        action="store_true",
        help="Audit DataSift for incomplete records (future: daily check via Playwright)",
    )

    # Phone validation arguments
    parser.add_argument(
        "--list-name",
        type=str,
        default=None,
        help="DataSift list name to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--preset-folder",
        type=str,
        default=None,
        help="DataSift preset folder to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--all-records",
        action="store_true",
        help="Export all DataSift records for phone validation (phone-validate mode)",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Show phone validation cost estimate only, no API calls (phone-validate mode)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip uploading phone tags back to DataSift (phone-validate mode)",
    )
    parser.add_argument(
        "--custom-tiers",
        type=str,
        default=None,
        help='JSON custom tier boundaries, e.g. \'{"Hot": [80,100], "Cold": [0,79]}\' (phone-validate mode)',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Concurrent Trestle API requests per batch (phone-validate mode, default: 10)",
    )
    parser.add_argument(
        "--add-litigator",
        action="store_true",
        help="Include litigator risk check in phone validation (phone-validate mode)",
    )

    # Manage sold arguments
    parser.add_argument(
        "--months-back",
        type=int,
        default=1,
        help="Months of sales to pull from SiftMap (manage-sold mode, default: 1)",
    )
    parser.add_argument(
        "--min-sale-price",
        type=int,
        default=1000,
        help="Min sale price to exclude deed transfers (manage-sold mode, default: 1000)",
    )
    parser.add_argument(
        "--sold-tag-date",
        type=str,
        default=None,
        help="Tag date in YYYY-MM format (manage-sold mode, default: current month)",
    )

    # Manage presets arguments
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover and list all preset folders, presets, and sequences (manage-presets mode)",
    )
    parser.add_argument(
        "--add-sold-exclusion",
        action="store_true",
        help="Update existing presets to exclude Sold status/tag (manage-presets mode)",
    )
    parser.add_argument(
        "--create-sold-sequence",
        action="store_true",
        help="Create Sold Property Cleanup sequence (manage-presets mode)",
    )
    parser.add_argument(
        "--preset-folders",
        type=str,
        default=None,
        help='Comma-separated preset folder names to target (manage-presets mode, default: all)',
    )

    # ── New analysis & workflow mode arguments ────────────────────────
    # Comp analysis
    parser.add_argument("--address", type=str, default=None,
                        help="Property address (comp/rehab/analyze-deal modes)")
    parser.add_argument("--city", type=str, default=None,
                        help="Property city (comp/rehab/analyze-deal modes)")
    parser.add_argument("--state", type=str, default="TN",
                        help="Property state (comp/analyze-deal modes, default: TN)")
    parser.add_argument("--zip-code", type=str, default=None,
                        help="Property ZIP code (comp/rehab/analyze-deal modes)")
    parser.add_argument("--radius", type=float, default=0.5,
                        help="Comp search radius in miles (comp mode, default: 0.5)")
    parser.add_argument("--months", type=int, default=6,
                        help="Comp lookback months (comp mode, default: 6)")

    # Rehab estimation
    parser.add_argument("--rehab-level", "--tier", dest="rehab_level",
                        type=int, default=3, choices=[1, 2, 3, 4, 5, 6],
                        help="Rehab Level 1-6 (rehab mode, default: 3=Full Rehab Interior Cosmetics). 1=Low/Rental, 2=Mid/Salvageable, 3=Full/Interior, 4=Full+Exterior, 5=Full+Some Big 6, 6=Gut Job. (--tier kept as alias for backward compat.)")
    parser.add_argument("--scope", type=str, default="full", choices=["full", "wholetail"],
                        help="Rehab scope (rehab mode, default: full)")
    parser.add_argument("--region", type=str, default="auto",
                        help="Regional pricing override (rehab/analyze-deal). Default 'auto' resolves from city+state. AL: birmingham, huntsville, albertville, mobile, montgomery, tuscaloosa, auburn, shoals, dothan, gadsden, decatur, cullman. TN: knoxville, blount, nashville, chattanooga. Fallback: national.")
    parser.add_argument("--sqft", type=int, default=0,
                        help="Property sqft override (rehab mode)")
    parser.add_argument("--bedrooms", type=int, default=0,
                        help="Bedrooms override (rehab mode)")
    parser.add_argument("--bathrooms", type=float, default=0,
                        help="Bathrooms override (rehab mode)")
    parser.add_argument("--year-built", type=int, default=0,
                        help="Year built (rehab mode — drives age-based contingency: pre-1960=25%%, 1960-1990=15%%, post-1990=10%%)")
    parser.add_argument("--big-tickets", type=str, default="",
                        help="Comma-separated Big 6 + optional adders: roof,hvac,hwh,plumbing,electrical,foundation,septic,sewer,windows,rewire")

    # Deal analysis
    parser.add_argument("--purchase-price", type=float, default=0,
                        help="Purchase price (analyze-deal mode, default: auto-calculate MAO)")
    parser.add_argument("--deal-rehab-level", "--rehab-tier", dest="rehab_tier",
                        type=int, default=None, choices=[1, 2, 3, 4, 5, 6],
                        help="Rehab Level 1-6 for analyze/analyze-deal mode. If omitted in analyze mode AND running interactively, you'll be prompted to pick. Default for non-interactive: 3 (Full Rehab Interior). (--rehab-tier kept as alias for backward compat.)")
    parser.add_argument("--exit-strategy", type=str, default="flip",
                        choices=["flip", "wholesale", "hold"],
                        help="Exit strategy (analyze-deal mode, default: flip)")
    parser.add_argument("--share", action="store_true",
                        help="Upload report to Dropbox and print shareable link (analyze mode). Uses existing DROPBOX_* env vars.")

    # Market analysis
    parser.add_argument("--zip-codes", type=str, default=None,
                        help="Comma-separated ZIP codes to analyze (market-analysis mode)")
    parser.add_argument("--monthly-budget", type=float, default=5000,
                        help="Monthly marketing budget for allocation (market-analysis mode)")

    # Buyer prospecting
    parser.add_argument("--min-transactions", type=int, default=2,
                        help="Min transactions to qualify as investor (buyer-prospect mode)")

    # Deep prospecting
    parser.add_argument("--depth", type=int, default=3, choices=[1, 2, 3, 4],
                        help="Research depth level 1-4 (deep-prospect mode, default: 3)")

    # Lead management
    parser.add_argument("--lead-action", type=str, default="qualify",
                        choices=["qualify", "report"],
                        help="Lead management action (lead-manage mode)")

    # Sequence setup
    parser.add_argument("--seq-folder", type=str, default="all",
                        choices=["lead-management", "acquisitions", "transactions",
                                 "deep-prospecting", "default", "all"],
                        help="Sequence folder to create (setup-sequences mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without creating (setup-sequences/niche-sequential)")

    # Niche sequential
    parser.add_argument("--channel", type=str, default="sms",
                        choices=["sms", "call", "mail", "dp"],
                        help="Marketing channel (niche-sequential mode)")
    parser.add_argument("--day", type=int, default=1, choices=[1, 2, 3],
                        help="Cycle day 1-3 (niche-sequential mode)")
    parser.add_argument("--ns-action", type=str, default="execute",
                        choices=["execute", "setup-presets", "status"],
                        help="Niche sequential action (niche-sequential mode)")

    # Playbook
    parser.add_argument("--blueprint", type=str, default="wholesale",
                        choices=["wholesale", "flip", "hold", "hybrid"],
                        help="Investment blueprint (playbook mode)")
    parser.add_argument("--market", type=str, default="knoxville",
                        help="Target market (playbook mode)")
    parser.add_argument("--team-size", type=int, default=1,
                        help="Team size 1/2/5 (playbook mode)")

    args = parser.parse_args()

    # Apply LLM backend override from CLI flag
    if hasattr(args, "llm_backend") and args.llm_backend:
        import config as cfg
        cfg.LLM_BACKEND = args.llm_backend
        if args.llm_backend == "ollama":
            logging.info("LLM backend: Ollama (%s)", cfg.OLLAMA_MODEL)
        elif args.llm_backend == "openrouter":
            logging.info("LLM backend: OpenRouter (%s)", cfg.OPENROUTER_MODEL)

    setup_logging(args.verbose)

    # ── Preflight health checks ──────────────────────────────────────
    preflight_failures = _preflight_check(args.mode)
    if preflight_failures:
        for f in preflight_failures:
            logging.error("Preflight FAILED: %s", f)
        # Send Slack alert so unattended runs are visible
        try:
            from slack_notifier import notify_preflight_failure
            notify_preflight_failure(preflight_failures)
        except Exception:
            pass  # Don't fail on notification failure
        sys.exit(1)
    logging.info("Preflight checks passed")

    # ── New analysis & workflow modes ─────────────────────────────────

    if args.mode == "comp":
        if not args.address:
            print("ERROR: --address is required for comp mode")
            return
        from comp_analyzer import run_comp_analysis
        result = run_comp_analysis(
            address=args.address, city=args.city or "", state=args.state,
            zip_code=args.zip_code or "",
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Comp analysis failed: %s", result["error"])
        else:
            print(f"Comp report: {_link(result['report_path'])}")
            arv = result["arv"]
            print(f"ARV: ${arv.arv_low:,.0f} (low) / ${arv.arv_mid:,.0f} (mid) / ${arv.arv_high:,.0f} (high)")
            print(f"Confidence: {arv.confidence} — {arv.confidence_reason}")
        return

    if args.mode == "rehab":
        if not args.address:
            print("ERROR: --address is required for rehab mode")
            return
        from rehab_estimator import run_rehab_estimate
        big_tickets = [t.strip() for t in (args.big_tickets or "").split(",") if t.strip()]
        result = run_rehab_estimate(
            address=args.address, sqft=args.sqft, bedrooms=args.bedrooms or 3,
            bathrooms=args.bathrooms or 2.0, year_built=args.year_built,
            tier=args.rehab_level, scope=args.scope, region=args.region,
            big_tickets=big_tickets or None,
            city=args.city or "", state=args.state,
        )
        full = result["full_estimate"]
        wt = result["wholetail_estimate"]
        print(f"Rehab report: {_link(result['report_path'])}")
        print(f"Full rehab: ${full.grand_total:,.0f} ({full.total_weeks:.0f} weeks)")
        print(f"Wholetail:  ${wt.grand_total:,.0f} ({wt.total_weeks:.0f} weeks)")
        return

    if args.mode == "analyze-deal":
        if not args.address:
            print("ERROR: --address is required for analyze-deal mode")
            return
        from deal_analyzer import run_deal_analysis
        result = run_deal_analysis(
            address=args.address, city=args.city or "", state=args.state,
            zip_code=args.zip_code or "",
            purchase_price=args.purchase_price,
            rehab_tier=args.rehab_tier if args.rehab_tier is not None else 3,
            exit_strategy=args.exit_strategy, region=args.region,
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Deal analysis failed: %s", result["error"])
        else:
            pkg = result["package"]
            print(f"Deal report: {_link(result['report_path'])}")
            print(f"Recommendation: {pkg.recommendation}")
            print(f"ARV: ${pkg.arv.arv_mid:,.0f} | Rehab: ${pkg.rehab_full.grand_total:,.0f}")
            print(f"Flip MAO: ${pkg.mao.flip_mao:,.0f} | Profit: ${pkg.flip.net_profit:,.0f} ({pkg.flip.roi_pct:.0f}% ROI)")
        return

    if args.mode == "analyze":
        # Single-address full pipeline. Parses "STREET, CITY, ST ZIP" into parts,
        # runs analyze-deal, and opens the Excel automatically.
        if not args.address:
            print("ERROR: --address is required for analyze mode")
            print('Usage: analyze --address "940 2nd Ave SW, Alabaster, AL 35007" [--purchase-price 95000]')
            return
        import re as _re
        street, city, state, zipc = args.address, "", "", ""
        m = _re.match(
            r"^\s*(.+?)\s*,\s*([^,]+?)\s*,\s*([A-Za-z]{2})\s+(\d{5})\s*$",
            args.address,
        )
        if m:
            street, city, state, zipc = m.group(1), m.group(2), m.group(3).upper(), m.group(4)
            logger.info("Parsed address: street=%r city=%r state=%r zip=%r", street, city, state, zipc)
        else:
            # Fall back to CLI overrides if format doesn't match the canonical one
            city = args.city or ""
            state = args.state if args.state != "TN" else (args.state or "")
            zipc = args.zip_code or ""
            if not (city and state and zipc):
                print("ERROR: Could not parse address. Use either:")
                print('  --address "940 2nd Ave SW, Alabaster, AL 35007"')
                print('  OR pass --address/--city/--state/--zip-code separately')
                return

        # Resolve rehab level — interactive prompt if not set via CLI flag.
        rehab_level = args.rehab_tier
        if rehab_level is None:
            if sys.stdin.isatty():
                from rehab_estimator import REHAB_LEVEL_NAMES
                print()
                print(f"What rehab scope does the seller advise for {street}?")
                for i in range(1, 7):
                    print(f"   {i}. {REHAB_LEVEL_NAMES[i]}")
                while True:
                    choice = input("Enter 1-6 [default 3 = Full Rehab Interior]: ").strip()
                    if not choice:
                        rehab_level = 3
                        break
                    if choice.isdigit() and 1 <= int(choice) <= 6:
                        rehab_level = int(choice)
                        break
                    print("  ↳ Invalid — enter a number 1-6 (or just press Enter for default 3).")
                print(f"   → Using Rehab Level {rehab_level}: {REHAB_LEVEL_NAMES[rehab_level]}")
                print()
            else:
                rehab_level = 3  # Non-interactive (cron, pipe) — silent default

        from deal_analyzer import run_deal_analysis
        result = run_deal_analysis(
            address=street, city=city, state=state, zip_code=zipc,
            purchase_price=args.purchase_price, rehab_tier=rehab_level,
            exit_strategy=args.exit_strategy, region=args.region,
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Analyze failed: %s", result["error"])
            return
        pkg = result["package"]
        report_path = result["report_path"]
        print()
        print(f"📊 Deal report: {_link(report_path)}")
        print(f"   Recommendation: {pkg.recommendation}")
        print(f"   ARV: ${pkg.arv.arv_mid:,.0f}  |  Rehab: ${pkg.rehab_full.grand_total:,.0f}")
        print(f"   Flip MAO (15% target): ${pkg.mao.flip_mao:,.0f}  |  Wholetail MAO: ${pkg.mao.wholesale_mao:,.0f}  |  Rental MAO (20% eq): ${pkg.mao.hold_mao:,.0f}")
        # Optional: upload to Dropbox and print a shareable link
        if getattr(args, "share", False):
            try:
                from datetime import datetime as _dt
                from dropbox_share import upload_and_share
                # Group by year-month so the root folder doesn't get cluttered
                subfolder = _dt.now().strftime("%Y-%m")
                share_url = upload_and_share(report_path, subfolder=subfolder)
                if share_url:
                    print(f"   🔗 Share link: {share_url}")
                else:
                    print(f"   ⚠️  Dropbox share failed (see logs). Local file still available above.")
            except Exception as _e:
                logger.error("Dropbox share error: %s", _e)
                print(f"   ⚠️  Dropbox share error: {_e}")
        # Auto-open the Excel
        try:
            import subprocess as _sp
            _sp.run(["open", report_path], check=False)
            print(f"   📂 Opened in default app.")
        except Exception as _e:
            print(f"   (Open manually: {report_path})")
        return

    if args.mode == "market-analysis":
        from market_analyzer import run_market_analysis
        counties = args.counties.split(",") if args.counties else None
        zip_codes = args.zip_codes.split(",") if args.zip_codes else None
        result = run_market_analysis(
            counties=counties, zip_codes=zip_codes,
            monthly_budget=args.monthly_budget,
        )
        if "error" in result:
            logger.error("Market analysis failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Market report: {_link(result['report_path'])}")
            print(f"Analyzed {report.total_zips} zips, {report.total_notices} total notices")
            if report.top_zips:
                top = report.top_zips[0]
                print(f"Top zip: {top.zip_code} (score {top.score:.1f}, grade {top.grade})")
        return

    if args.mode == "buyer-prospect":
        from buyer_prospector import run_buyer_prospecting
        counties = args.counties.split(",") if args.counties else None
        result = run_buyer_prospecting(
            counties=counties,
            months_back=args.months_back,
            min_transactions=args.min_transactions,
        )
        if "error" in result:
            logger.error("Buyer prospecting failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Buyer report: {_link(result['report_path'])}")
            print(f"Found {report.total_investors} investors")
            print(f"CSV: {result.get('csv_path', 'N/A')}")
        return

    if args.mode == "deep-prospect":
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        if not csv_path:
            csvs = sorted(config.OUTPUT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            csv_path = str(csvs[0]) if csvs else ""
        if not csv_path:
            print("ERROR: --csv-path required or place CSVs in output/")
            return
        import asyncio
        from deep_prospector import run_deep_prospecting
        result = asyncio.run(run_deep_prospecting(
            csv_path=csv_path, depth=args.depth,
            max_records=args.max_notices if hasattr(args, "max_notices") else 0,
        ))
        if "error" in result:
            logger.error("Deep prospecting failed: %s", result["error"])
        else:
            stats = result["stats"]
            print(f"Report: {_link(result['report_path'])}")
            print(f"Processed {stats['total']} records at depth {args.depth}")
            print(f"Phones: {stats['phones_found']} | Deceased: {stats['deceased_confirmed']} | DMs: {stats['dms_identified']}")
        return

    if args.mode == "lead-manage":
        from lead_manager import run_lead_management
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        result = run_lead_management(
            action=args.lead_action, csv_path=csv_path,
        )
        if "error" in result:
            logger.error("Lead management failed: %s", result["error"])
        else:
            print(f"STABM report: {_link(result['report_path'])}")
            print(f"Total: {result['total']} | Hot: {result['hot']} | Warm: {result['warm']} | Cold: {result['cold']}")
        return

    if args.mode == "setup-sequences":
        from sequence_templates import get_templates, list_templates, preview_sequence
        templates = get_templates(args.seq_folder)
        if args.dry_run:
            print(f"DRY RUN — Would create {len(templates)} sequences in DataSift:")
            for t in templates:
                preview = preview_sequence(t)
                print(f"  [{preview['folder']}] {preview['name']}")
                print(f"    Trigger: {preview['trigger']}")
                print(f"    Actions: {len(preview['actions'])}")
        else:
            print(f"Sequence creation requires Playwright — {len(templates)} templates ready")
            print("Templates defined. DataSift Playwright creation coming in next build.")
            print("\nTemplate list:")
            print(list_templates())
        return

    if args.mode == "niche-sequential":
        from niche_sequential import run_niche_sequential
        result = run_niche_sequential(
            list_name=args.list_name or "",
            channel=args.channel, day=args.day,
            csv_path=args.csv_path if hasattr(args, "csv_path") and args.csv_path else "",
            action=args.ns_action,
        )
        if "error" in result:
            logger.error("Niche sequential failed: %s", result["error"])
        elif "output" in result:
            print(f"Exported: {result['output']}")
            print(f"Channel: {result['channel']}, Day {result['day']}, {result['records']} records")
        elif "presets" in result:
            for p in result["presets"]:
                print(f"  {p['name']}: {p['description']}")
        return

    if args.mode == "playbook":
        from playbook_generator import run_playbook_generator
        result = run_playbook_generator(
            blueprint=args.blueprint, market=args.market,
            team_size=args.team_size,
        )
        print(f"Playbook: {result['playbook_path']}")
        print(f"Blueprint: {result['blueprint'].title()} | Market: {result['market'].title()} | Team: {result['team_size']}")
        return

    # Phone validation mode — separate pipeline
    if args.mode == "phone-validate":
        _run_phone_validate(args)
        return

    # Manage presets mode — filter preset + sequence management
    if args.mode == "manage-presets":
        _run_manage_presets(args)
        return

    # Manage sold properties mode — SiftMap workflow
    if args.mode == "manage-sold":
        _run_manage_sold(args)
        return

    # PDF import mode — separate pipeline
    if args.mode == "pdf-import":
        _run_pdf_import(args)
        return

    # Photo import mode — separate pipeline
    if args.mode == "photo-import":
        _run_photo_import(args)
        return

    # Dropbox watcher mode — polls for new photos
    if args.mode == "dropbox-watch":
        from dropbox_watcher import run_watcher
        run_watcher(
            poll_interval=args.poll_interval,
            delete_after=not getattr(args, "no_delete", False),
            max_polls=args.max_polls,
        )
        return

    # CSV re-import mode — separate pipeline
    if args.mode == "csv-import":
        _run_csv_import(args)
        return

    # Filter saved searches
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip() for c in args.counties.split(",")]

    types = None
    if args.types and args.types.lower() != "all":
        types = [t.strip() for t in args.types.split(",")]

    searches = _filter_searches(counties, types)
    if not searches:
        logging.error("No saved searches match the given --counties / --types filters")
        sys.exit(1)

    logging.info(
        "Running %d saved searches: %s",
        len(searches),
        ", ".join(f"{s.county} {s.notice_type}" for s in searches),
    )

    try:
        _run_scrape_pipeline(args, searches)
    except Exception as e:
        logging.exception("Pipeline failed with unhandled error")
        try:
            from slack_notifier import notify_error
            notify_error("Pipeline (top-level)", e, context=f"mode={args.mode}")
        except Exception:
            pass
        sys.exit(1)


def _run_scrape_pipeline(args, searches) -> None:
    """Run the daily/historical scrape → enrich → export → upload pipeline.

    Post-scrape orchestration (enrichment, Tracerfy, Trestle, PDF gen,
    DataSift CSV gen) lives in full_pipeline.run_full_pipeline so the
    Apify Actor entry point can share it. This function handles the
    CLI-specific bits: argparse-driven options, local file output,
    Playwright-based DataSift upload, sys.exit on empty, and the
    Slack notification format.
    """
    from full_pipeline import PostScrapeOptions, run_full_pipeline

    # ── Phase 2: instantiate FunnelCounter + ServiceRateTracker
    # ── BEFORE scrape so the captcha rate (and downstream Smarty /
    # ── LLM / Tracerfy rates) route into the same tracker, and all
    # ── 10 gates stamp into the same funnel.
    funnel = FunnelCounter("main_daily", gates=list(MAIN_DAILY_GATES))
    rate_tracker = ServiceRateTracker()

    # ── Scrape ────────────────────────────────────────────────────
    notices = asyncio.run(scrape_all(
        mode=args.mode, searches=searches,
        llm_api_key=config.ANTHROPIC_API_KEY or None,
        since_date_override=args.since,
        max_notices=args.max_notices,
        rate_tracker=rate_tracker,
    ))
    # Phase 2 funnel: scraped + seen_ids_deduped both take the same
    # count — the scraper's internal seen_ids dedup is already applied
    # to the returned list.
    funnel.set("scraped", len(notices))
    funnel.set("seen_ids_deduped", len(notices))

    # ── Probate property lookup (async; CLI uses asyncio.run) ─────
    probate_notices = [
        n for n in notices
        if n.notice_type == "probate" and n.decedent_name and not n.address
    ]
    if probate_notices:
        try:
            from property_lookup import lookup_decedent_properties
            logging.info("Looking up property addresses for %d probate notices...", len(probate_notices))
            asyncio.run(lookup_decedent_properties(probate_notices))
        except ImportError:
            logging.warning("property_lookup module not found -- skipping property lookup")
        except Exception as e:
            logging.warning("Property lookup failed: %s -- continuing without lookups", e)

    # ── Tier-ZIP filter ───────────────────────────────────────────
    # Apply BEFORE the expensive run_full_pipeline (Zillow / Tracerfy / obit
    # enrichment). At this point every notice that's going to have a ZIP
    # already has one: foreclosure ZIPs come from the parsed notice text,
    # probate ZIPs come from the property_lookup pass above. Records still
    # missing a ZIP are off-target by definition for an investor-marketing
    # workflow — we have no way to confirm they're in our target areas.
    tiers_raw = getattr(args, "tiers", "1,2") or ""
    if tiers_raw.lower() not in ("", "all"):
        from target_zips import zip_tier
        tier_set = {int(t) for t in tiers_raw.split(",") if t.strip().isdigit()}
        if tier_set:
            before = len(notices)
            kept: list = []
            dropped_off_tier = 0
            dropped_no_zip = 0
            for n in notices:
                t = zip_tier(n.zip)
                if t is None:
                    dropped_no_zip += 1
                    continue
                if t not in tier_set:
                    dropped_off_tier += 1
                    continue
                kept.append(n)
            logging.info(
                "Tier filter (--tiers %s): %d → %d "
                "(dropped %d off-tier, %d no-ZIP)",
                tiers_raw, before, len(kept), dropped_off_tier, dropped_no_zip,
            )
            notices = kept

    # ── Shared post-scrape pipeline ───────────────────────────────
    opts = PostScrapeOptions(
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=getattr(args, "skip_smarty", False),
        skip_zillow=getattr(args, "skip_zillow", False),
        skip_tax=getattr(args, "skip_tax", False),
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        skip_tracerfy=getattr(args, "skip_tracerfy", False),
        tracerfy_dp_only=False,    # CLI: trace ALL records (not just DP candidates)
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"CLI {args.mode}",
        # Phase 2: thread funnel + rate_tracker so the shared post-
        # scrape pipeline stamps the 6 enrichment-stage gates +
        # tracerfy_matched and instruments the Smarty / LLM / Tracerfy
        # call sites.
        funnel=funnel,
        rate_tracker=rate_tracker,
    )
    result = run_full_pipeline(notices, opts)
    notices = result.notices

    if not notices:
        logging.warning("No notices found")
        # Send Slack ping even on empty runs so operators know the job
        # ran successfully (vs silently dying).
        if getattr(args, "notify_slack", False):
            try:
                from slack_notifier import send_slack_notification
                send_slack_notification([])
            except Exception:
                logging.exception("Slack notification for empty run failed")
        sys.exit(0)

    # ── Master CSV (CLI-only: split-by-type or unified) ───────────
    if args.split:
        for p in write_csv_by_type(notices):
            logging.info("Output: %s", p)
    elif not getattr(args, "no_raw_csv", False):
        logging.info("Output: %s", write_csv(notices))
    else:
        logging.debug("Skipping raw al_notices CSV (--no-raw-csv set)")
    # Phase 2 funnel: datasift_uploaded gate stamps from the surviving
    # notice count after CSV write. CLI mode is symmetric with Apify on
    # this gate — both stamp from len(notices) post-CSV.
    funnel.set("datasift_uploaded", len(notices))

    # ── DataSift Playwright upload (CLI-only) ─────────────────────
    upload_result = None
    if getattr(args, "upload_datasift", False) and result.datasift_csv_infos:
        from datasift_uploader import upload_to_datasift_per_distressor

        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        csv_infos = result.datasift_csv_infos
        per_csv = []
        for info in csv_infos:
            r = asyncio.run(
                upload_to_datasift_per_distressor(
                    info["path"], enrich=do_enrich, skip_trace=do_skip_trace,
                )
            )
            r["label"] = info["label"]
            per_csv.append(r)
        all_ok = all(r.get("success") for r in per_csv)
        upload_result = {
            "success": all_ok,
            "message": "; ".join(
                f"{r['label']}: {r.get('message', '')}" for r in per_csv
            ),
            "csvs": per_csv,
            "enrich_result": per_csv[0].get("enrich_result", {}) if per_csv else {},
            "skip_trace_result": per_csv[0].get("skip_trace_result", {}) if per_csv else {},
        }

        if upload_result.get("success"):
            logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
            if upload_result.get("enrich_result"):
                logging.info("  Enrich: %s", upload_result["enrich_result"].get("message", ""))
            if upload_result.get("skip_trace_result"):
                logging.info("  Skip trace: %s", upload_result["skip_trace_result"].get("message", ""))
        else:
            logging.error("DataSift upload failed: %s", upload_result.get("message"))

    # ── Slack/Discord notification (Phase 2: blocks-aware) ────────
    # Posts the legacy summary + funnel block + service-rates block in
    # a single Block Kit message per D-02. send_slack_notification is
    # NOT called from this path; the legacy text helper stays
    # byte-identical for callers that still use it directly (W5).
    if getattr(args, "notify_slack", False):
        _post_daily_slack_with_funnel(
            notices,
            funnel,
            rate_tracker,
            upload_result=upload_result,
        )

    # D-04 — terminal mirrors Slack: always log the funnel at end-of-run
    # regardless of whether --notify-slack is set.
    logger.info(
        "Funnel (%s): %s",
        funnel.pipeline_name, dict(funnel.as_ordered_dict()),
    )

    # Audit DataSift for incomplete records (future daily check)
    if getattr(args, "audit_records", False):
        logging.info(
            "--audit-records: Not yet implemented. "
            "Will check DataSift Incomplete tab via Playwright in a future build."
        )

    logging.info("Done — %d notices exported", len(notices))


# ── Entry point ───────────────────────────────────────────────────────


if __name__ == "__main__":
    if os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"):
        # Running inside Apify platform or with apify run
        asyncio.run(actor_main())
    else:
        # Standalone CLI
        cli_main()
