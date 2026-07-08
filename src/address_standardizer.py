"""Standardize addresses via Smarty (formerly SmartyStreets) US Street API.

Processes a batch of NoticeData records, overwrites address/city/zip with
USPS-standardized versions, and populates geocode + validation fields.

Graceful degradation: if no API keys or API errors, all notices pass through
unchanged.
"""

import logging
import time
from typing import TYPE_CHECKING

from smartystreets_python_sdk import (
    BasicAuthCredentials,
    Batch,
    ClientBuilder,
    exceptions,
)
from smartystreets_python_sdk.us_street import Lookup as StreetLookup
from smartystreets_python_sdk.us_street.match_type import MatchType

from notice_parser import NoticeData

if TYPE_CHECKING:
    from observability import ServiceRateTracker

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 100


def _build_client(auth_id: str, auth_token: str):
    """Build an authenticated Smarty US Street API client.

    Pinned to the ``us-core-cloud`` license because the account's active
    subscription is US Address Verification / Core Edition / Cloud. The
    smartystreets_python_sdk defaults to ``us-standard-cloud``, which
    yields HTTP 402 ``Active subscription required (1588026162)`` against
    a Core-only account — verified 2026-07-08 via direct HTTP probe:
      - No license or ``us-core-cloud`` → HTTP 200
      - ``us-standard-cloud`` (SDK default) → HTTP 402
      - ``us-rooftop-geocoding-cloud`` → HTTP 402
    Without this pin the pre-probate pipeline silently drops every
    Madison/Marshall obituary match (~14/week) because Smarty is the
    only path we have to recover ZIP from the AssuranceWeb street-only
    property response.
    """
    credentials = BasicAuthCredentials(auth_id, auth_token)
    return (
        ClientBuilder(credentials)
        .with_licenses(["us-core-cloud"])
        .build_us_street_api_client()
    )


def _build_lastline(notice: NoticeData) -> str:
    """Build a 'city, state zip' lastline string from notice fields.

    Fallback: when the notice has no city/state/zip, return the state if
    known (AL for new Jefferson/Madison/Marshall pipelines, TN for legacy
    Knox/Blount). Default to AL since the active SiftStack pipelines are
    all in Alabama as of 2026-05.
    """
    parts = []
    if notice.city:
        parts.append(notice.city)
    if notice.state:
        parts.append(notice.state)
    lastline = ", ".join(parts)
    if notice.zip:
        lastline += " " + notice.zip if lastline else notice.zip
    if lastline:
        return lastline
    # No city/state/zip — fall back to the notice's state if set,
    # otherwise AL (active pipelines) instead of TN (legacy).
    return (notice.state or "").strip() or "AL"


def standardize_addresses(
    notices: list[NoticeData],
    auth_id: str,
    auth_token: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> list[NoticeData]:
    """Standardize addresses in-place via Smarty US Street API.

    Args:
        notices: List of NoticeData (modified in-place).
        auth_id: Smarty auth-id credential.
        auth_token: Smarty auth-token credential.
        rate_tracker: Optional ServiceRateTracker. When supplied, records
            one outcome per Smarty lookup per CONTEXT.md D-04:
              - success = candidate.delivery_line_1 non-empty AND
                state-guard passed
              - failure = empty candidates list, state-guard rejection,
                OR HTTPError raised by the SDK (whole batch counts as
                failures so the run summary surfaces partial outages)

    Returns:
        The same list (modified in-place) for chaining convenience.
        On any credential/API failure, returns notices unchanged.
    """
    if not auth_id or not auth_token:
        logger.info("Smarty credentials not configured -- skipping address standardization")
        return notices

    # Filter to notices that have an address worth standardizing
    eligible = [(i, n) for i, n in enumerate(notices) if n.address.strip()]
    if not eligible:
        logger.info("No notices with addresses to standardize")
        return notices

    logger.info(
        "Standardizing %d addresses via Smarty (%d skipped -- no address)",
        len(eligible),
        len(notices) - len(eligible),
    )

    try:
        client = _build_client(auth_id, auth_token)
    except Exception as e:
        logger.error("Failed to build Smarty client: %s", e)
        return notices

    matched = 0
    failed = 0

    for batch_start in range(0, len(eligible), MAX_BATCH_SIZE):
        batch_slice = eligible[batch_start : batch_start + MAX_BATCH_SIZE]
        batch = Batch()

        for orig_idx, notice in batch_slice:
            lookup = StreetLookup()
            lookup.street = notice.address
            lookup.lastline = _build_lastline(notice)
            lookup.candidates = 1
            lookup.match = MatchType.INVALID
            lookup.input_id = str(orig_idx)
            batch.add(lookup)

        try:
            client.send_batch(batch)
        except exceptions.SmartyException as e:
            logger.error("Smarty batch API error: %s", e)
            failed += len(batch_slice)
            if rate_tracker is not None:
                # Whole batch failed → one failure per submitted notice so
                # the Slack rate reflects the partial outage accurately.
                for _ in batch_slice:
                    rate_tracker.record("smarty", False)
            continue
        except Exception as e:
            logger.error("Unexpected Smarty error: %s", e)
            failed += len(batch_slice)
            if rate_tracker is not None:
                for _ in batch_slice:
                    rate_tracker.record("smarty", False)
            continue

        # Process results
        for lookup in batch:
            candidates = lookup.result
            if not candidates:
                failed += 1
                if rate_tracker is not None:
                    rate_tracker.record("smarty", False)
                continue

            candidate = candidates[0]
            orig_idx = int(lookup.input_id)
            notice = notices[orig_idx]

            components = candidate.components
            metadata = candidate.metadata
            analysis = candidate.analysis

            # Safety: reject results outside the notice's claimed state
            # (catches Smarty fuzzy-matching to a same-street-name address
            # in the wrong state). Use the notice's `state` field — TN for
            # Knox/Blount, AL for Jefferson/Madison/Marshall. Falls back to
            # accepting AL+TN when the notice has no state set yet (early
            # pipeline stages may leave it empty).
            expected_states = {(notice.state or "").strip().upper()} - {""}
            if not expected_states:
                expected_states = {"TN", "AL"}
            if (
                components
                and components.state_abbreviation
                and components.state_abbreviation not in expected_states
            ):
                logger.warning(
                    "Smarty returned %s for '%s' (expected %s) -- keeping original",
                    components.state_abbreviation,
                    notice.address,
                    sorted(expected_states),
                )
                failed += 1
                if rate_tracker is not None:
                    rate_tracker.record("smarty", False)
                continue

            # Overwrite address with standardized version
            if candidate.delivery_line_1:
                notice.address = candidate.delivery_line_1

            # Overwrite city/state/zip from components
            if components:
                if components.city_name:
                    notice.city = components.city_name
                if components.state_abbreviation:
                    notice.state = components.state_abbreviation
                if components.zipcode:
                    notice.zip = components.zipcode
                if components.zipcode and components.plus4_code:
                    notice.zip_plus4 = f"{components.zipcode}-{components.plus4_code}"

            # Populate metadata fields
            if metadata:
                if metadata.latitude is not None:
                    notice.latitude = str(metadata.latitude)
                if metadata.longitude is not None:
                    notice.longitude = str(metadata.longitude)
                if metadata.rdi:
                    notice.rdi = metadata.rdi

            # Populate analysis fields
            if analysis:
                if analysis.dpv_match_code:
                    notice.dpv_match_code = analysis.dpv_match_code
                if analysis.vacant:
                    notice.vacant = analysis.vacant

            matched += 1
            if rate_tracker is not None:
                # Per D-04: success = non-empty delivery_line_1 (which we
                # just used to set notice.address). If a candidate came
                # back with no delivery_line_1 we still mark it matched
                # because the city/zip from components is useful — but
                # those should be exceedingly rare. We record success on
                # the matched-branch end-point uniformly to keep the
                # "one record per lookup" invariant.
                rate_tracker.record("smarty", True)

    logger.info(
        "Smarty standardization complete: %d matched, %d failed/no-match, %d skipped",
        matched,
        failed,
        len(notices) - len(eligible),
    )

    return notices


def _reverse_geocode(lat: str, lon: str) -> dict | None:
    """Reverse geocode lat/lon via Nominatim to get city and ZIP.

    Returns dict with 'city' and 'postcode', or None on failure.
    Nominatim rate limit: 1 request per second.
    """
    import requests

    url = (
        f"https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}&format=json&addressdetails=1"
    )
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "TN-Notice-Scraper/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    addr = data.get("address", {})
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("hamlet")
        or ""
    )
    postcode = addr.get("postcode", "")
    return {"city": city, "postcode": postcode}


def retry_with_geocoded_city(
    notices: list[NoticeData],
    auth_id: str,
    auth_token: str,
) -> None:
    """Retry Smarty for failed lookups using reverse-geocoded city/ZIP.

    Finds notices that have an address and lat/lon but no ZIP (Smarty failed),
    reverse geocodes the lat/lon via Nominatim to get the correct city/ZIP,
    then retries Smarty with the corrected lastline.

    Updates notices in-place.
    """
    # Find candidates: have address + lat/lon but Smarty didn't match (no zip)
    candidates = [
        (i, n) for i, n in enumerate(notices)
        if n.address.strip() and n.latitude and n.longitude and not n.zip
    ]

    if not candidates:
        logger.info("No Smarty failures with lat/lon to retry")
        return

    logger.info(
        "Reverse geocoding %d Smarty failures to get correct city/ZIP...",
        len(candidates),
    )

    # Step 1: Reverse geocode each candidate to get city/ZIP
    geocoded = 0
    for i, (orig_idx, notice) in enumerate(candidates):
        result = _reverse_geocode(notice.latitude, notice.longitude)
        if result:
            if result["postcode"]:
                notice.zip = result["postcode"]
                geocoded += 1
            if result["city"]:
                notice.city = result["city"]
        if i < len(candidates) - 1:
            time.sleep(1.1)  # Nominatim rate limit: 1 req/sec
        if (i + 1) % 20 == 0:
            logger.info("Reverse geocode progress: %d/%d", i + 1, len(candidates))

    logger.info("Reverse geocoded: %d/%d got ZIP codes", geocoded, len(candidates))

    # Step 2: Retry Smarty with the new city/ZIP for records that got geocoded
    retry = [
        (orig_idx, notices[orig_idx]) for orig_idx, n in candidates
        if n.zip  # Only retry if we got a ZIP from geocoding
    ]

    if not retry:
        logger.info("No records to retry with Smarty after geocoding")
        return

    logger.info("Retrying Smarty for %d records with geocoded city/ZIP...", len(retry))

    try:
        client = _build_client(auth_id, auth_token)
    except Exception as e:
        logger.error("Failed to build Smarty client for retry: %s", e)
        return

    matched = 0
    failed = 0

    for batch_start in range(0, len(retry), MAX_BATCH_SIZE):
        batch_slice = retry[batch_start : batch_start + MAX_BATCH_SIZE]
        batch = Batch()

        for orig_idx, notice in batch_slice:
            lookup = StreetLookup()
            lookup.street = notice.address
            lookup.lastline = _build_lastline(notice)
            lookup.candidates = 1
            lookup.match = MatchType.INVALID
            lookup.input_id = str(orig_idx)
            batch.add(lookup)

        try:
            client.send_batch(batch)
        except exceptions.SmartyException as e:
            logger.error("Smarty retry batch error: %s", e)
            failed += len(batch_slice)
            continue
        except Exception as e:
            logger.error("Unexpected Smarty retry error: %s", e)
            failed += len(batch_slice)
            continue

        for lookup in batch:
            result_candidates = lookup.result
            if not result_candidates:
                failed += 1
                continue

            candidate = result_candidates[0]
            orig_idx = int(lookup.input_id)
            notice = notices[orig_idx]

            components = candidate.components
            metadata = candidate.metadata
            analysis = candidate.analysis

            # Reject Smarty matches that resolve to a state OTHER than the
            # notice's claimed state. Mirrors the same guard in
            # standardize_addresses() — without this, Smarty fuzzy-matching
            # can return an out-of-state same-street-name address.
            expected_states = {(notice.state or "").strip().upper()} - {""}
            if not expected_states:
                expected_states = {"TN", "AL"}
            if (
                components
                and components.state_abbreviation
                and components.state_abbreviation not in expected_states
            ):
                failed += 1
                continue

            if candidate.delivery_line_1:
                notice.address = candidate.delivery_line_1
            if components:
                if components.city_name:
                    notice.city = components.city_name
                if components.state_abbreviation:
                    notice.state = components.state_abbreviation
                if components.zipcode:
                    notice.zip = components.zipcode
                if components.zipcode and components.plus4_code:
                    notice.zip_plus4 = f"{components.zipcode}-{components.plus4_code}"
            if metadata:
                if metadata.latitude is not None:
                    notice.latitude = str(metadata.latitude)
                if metadata.longitude is not None:
                    notice.longitude = str(metadata.longitude)
                if metadata.rdi:
                    notice.rdi = metadata.rdi
            if analysis:
                if analysis.dpv_match_code:
                    notice.dpv_match_code = analysis.dpv_match_code
                if analysis.vacant:
                    notice.vacant = analysis.vacant

            matched += 1

    logger.info(
        "Smarty retry complete: %d matched, %d failed",
        matched, failed,
    )


# ── AssuranceWeb (Madison + Marshall) ZIP-recovery helpers ──────────────
#
# These helpers were originally defined in pre_probate_pipeline_al.py
# (moved here 2026-05-23) so all three AL post-probate pipelines plus the
# legacy main.py daily flow can share one canonical implementation. The
# pre_probate_pipeline_al module keeps underscore-prefixed aliases for
# backward compatibility — new code should import the public names
# directly from this module.

# Fallback anchor cities for AssuranceWeb ZIP recovery. The first attempt
# uses the primary anchor (Huntsville for Madison, Albertville for Marshall);
# if Smarty can't resolve the street there, we cycle through additional
# anchor cities AND a city-less retry. Each ~$0.001 — three lookups worst
# case for an address we'd otherwise drop, easily worth it.
_MADISON_ANCHORS: tuple[str, ...] = (
    "Huntsville AL", "Madison AL", "Athens AL", "Hazel Green AL",
    "New Hope AL", "Gurley AL", "Owens Cross Roads AL", "New Market AL",
    # Smaller Madison-area communities — each is a separate USPS catchment
    # so Smarty can't infer them from larger anchors. Verified that real
    # addresses (e.g. Carters Gin Rd) resolve under "Toney AL" but fail
    # under "Huntsville AL".
    "Toney AL", "Meridianville AL", "Harvest AL", "Triana AL",
    "AL",  # Final fallback: let Smarty pick the city from the street match
)
_MARSHALL_ANCHORS: tuple[str, ...] = (
    "Albertville AL", "Boaz AL", "Guntersville AL", "Arab AL",
    "Grant AL", "Horton AL", "Crossville AL",
    "AL",
)

# City → primary Tier-1 ZIP for city-only fallback (see
# smarty_zip_or_city_estimate below). Used when USPS-CASS doesn't
# recognize the specific house number but Smarty confirms the street
# is in a known Madison/Marshall city. Verified 2026-07-08 via direct
# us-zipcode.api.smarty.com lookups:
#   Huntsville → 24 ZIPs (most Tier 1: 35801/35803/35805/35810/35811)
#   Albertville → 2 ZIPs (both Tier 1: 35950, 35951)
#   Arab → 1 ZIP (Tier 1: 35016)
#   Guntersville → 1 ZIP (Tier 1: 35976)
#   Boaz → 2 ZIPs (Tier 1: 35957; non-tier: 35956)
# These are TIER CENTROIDS, not USPS-standardized ZIPs — records
# stamped with these MUST carry the ``zip_estimated_from_city`` data
# flag so downstream filter presets can exclude if precision matters.
_CITY_TIER_ZIP_FALLBACK: dict[str, str] = {
    # Madison County
    "HUNTSVILLE": "35801",     # Tier 1 (Downtown / South Huntsville)
    # Marshall County (all 4 primary cities have a Tier-1 anchor ZIP)
    "ALBERTVILLE": "35950",    # Tier 1
    "ARAB": "35016",           # Tier 1 (single ZIP city)
    "GUNTERSVILLE": "35976",   # Tier 1 (single ZIP city)
    "BOAZ": "35957",           # Tier 1 (city has 2 ZIPs; this is the tier one)
}


def _smarty_lookup_once(situs: str, lastline_hint: str) -> tuple[str, str]:
    """Single Smarty lookup attempt. Returns ('','') on any failure."""
    try:
        import config as cfg
        if not (cfg.SMARTY_AUTH_ID and cfg.SMARTY_AUTH_TOKEN):
            return ("", "")
        from smartystreets_python_sdk.us_street import Lookup as StreetLookup
        from smartystreets_python_sdk.us_street.match_type import MatchType
        client = _build_client(cfg.SMARTY_AUTH_ID, cfg.SMARTY_AUTH_TOKEN)
        lookup = StreetLookup()
        lookup.street = situs.strip()
        lookup.lastline = lastline_hint
        lookup.candidates = 1
        lookup.match = MatchType.INVALID
        client.send_lookup(lookup)
        if not lookup.result:
            return ("", "")
        comp = lookup.result[0].components
        return (comp.city_name or "", comp.zipcode or "")
    except Exception as e:
        logger.debug("Smarty geocode failed for %r (lastline=%r): %s",
                     situs, lastline_hint, e)
        return ("", "")


def smarty_zip_for_assuranceweb_address(
    situs: str,
    lastline_hint: str = "Huntsville AL",
    anchor_fallbacks: tuple[str, ...] | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> tuple[str, str]:
    """Multi-anchor Smarty lookup to recover (city, zip) for an AssuranceWeb situs.

    Madison + Marshall both run on the AssuranceWeb platform, and both
    `search_by_owner_name` responses return only the street — the city/zip
    portion isn't in the bulk search payload. We need ZIP for the tier gate,
    so geocode via Smarty's US Street API.

    Strategy (added to fix P1 #4 — multiple rural addresses dropped with
    zip=? in live runs: Lizotte ×3, Bell, M.Smith, Hudson, Manley, K.Floyd,
    Dova Hay):

      1. Try ``lastline_hint`` (e.g. "Huntsville AL") — handles the
         common case where the street is in the anchor city's catchment.
      2. If no match, cycle through ``anchor_fallbacks`` until one hits.
         These cover rural / fringe addresses where the anchor isn't
         geographically near the actual delivery city.
      3. Final attempt: lastline = "AL" alone — lets Smarty pick the city
         from the street match without any city bias.

    Returns ('', '') only when all fallbacks are exhausted.

    Per CONTEXT.md D-04, rate_tracker records exactly ONE outcome per
    invocation regardless of how many internal fallback lookups fire —
    the "logical Smarty call" from the caller's perspective is a single
    address resolution, and the multi-anchor retry is an internal
    optimisation, not a separate API call from the orchestrator's view.
    """
    if not situs or not situs.strip():
        # Empty input — no Smarty call issued. Per D-04 the noop path
        # should NOT count as a failure (the service was never invoked).
        return ("", "")

    # Try the primary anchor first
    city, zip_ = _smarty_lookup_once(situs, lastline_hint)
    if zip_:
        if rate_tracker is not None:
            rate_tracker.record("smarty", True)
        return (city, zip_)

    # Cycle through fallback anchors
    for anchor in (anchor_fallbacks or ()):
        if anchor == lastline_hint:
            continue  # Already tried the primary
        city, zip_ = _smarty_lookup_once(situs, anchor)
        if zip_:
            logger.debug("Smarty fallback hit on %r (anchor=%r): %s, %s",
                         situs, anchor, city, zip_)
            if rate_tracker is not None:
                rate_tracker.record("smarty", True)
            return (city, zip_)

    if rate_tracker is not None:
        rate_tracker.record("smarty", False)
    return ("", "")


def smarty_zip_or_city_estimate(
    situs: str,
    lastline_hint: str,
    anchor_fallbacks: tuple[str, ...] | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> tuple[str, str, bool]:
    """Enhanced ZIP recovery with a city-tier-centroid fallback.

    Same multi-anchor Smarty lookup as ``smarty_zip_for_assuranceweb_address``,
    plus one additional stage: when USPS-CASS doesn't recognize the specific
    house number but Smarty confirms the street is in a known Madison/Marshall
    city, return the city's Tier-1 centroid ZIP with an ``is_estimated=True``
    flag. Recovers records that would otherwise drop entirely (new
    construction, private drives, rural addresses USPS doesn't deliver to,
    county-tax-roll addresses that differ from postal delivery addresses).

    Verified 2026-07-08: 11 Madison + 3 Marshall pre-probate matches per
    day were being dropped as ``tier=None`` because Smarty returned city
    but no ZIP for county-tax-roll addresses; this fallback recovers
    ~8-11 leads/week that pass the tier gate on city centroid alone.

    Returns (city, zip, is_estimated_from_city). ``is_estimated=True``
    means the ZIP is a tier centroid, not a USPS-standardized value —
    callers should stamp ``zip_estimated_from_city`` on the record so
    downstream filter presets can filter these separately if needed.
    """
    if not situs or not situs.strip():
        return ("", "", False)

    best_city_only = ""

    # Primary anchor
    city, zip_ = _smarty_lookup_once(situs, lastline_hint)
    if zip_:
        if rate_tracker is not None:
            rate_tracker.record("smarty", True)
        return (city, zip_, False)
    if city and not best_city_only:
        best_city_only = city

    # Fallback anchors
    for anchor in (anchor_fallbacks or ()):
        if anchor == lastline_hint:
            continue
        city, zip_ = _smarty_lookup_once(situs, anchor)
        if zip_:
            logger.debug("Smarty fallback hit on %r (anchor=%r): %s, %s",
                         situs, anchor, city, zip_)
            if rate_tracker is not None:
                rate_tracker.record("smarty", True)
            return (city, zip_, False)
        if city and not best_city_only:
            best_city_only = city

    # All full-precision lookups exhausted. Last-resort: city-centroid.
    if best_city_only:
        fallback_zip = _CITY_TIER_ZIP_FALLBACK.get(best_city_only.upper())
        if fallback_zip:
            logger.info(
                "Smarty city-only fallback: %r → %s + tier centroid ZIP %s "
                "(is_estimated=True)",
                situs, best_city_only, fallback_zip,
            )
            if rate_tracker is not None:
                rate_tracker.record("smarty", True)
            return (best_city_only, fallback_zip, True)

    if rate_tracker is not None:
        rate_tracker.record("smarty", False)
    return ("", "", False)


def smarty_zip_or_city_estimate_for_madison(
    situs: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> tuple[str, str, bool]:
    """Madison-anchored 3-tuple variant with city-centroid fallback."""
    return smarty_zip_or_city_estimate(
        situs, "Huntsville AL", _MADISON_ANCHORS, rate_tracker=rate_tracker,
    )


def smarty_zip_or_city_estimate_for_marshall(
    situs: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> tuple[str, str, bool]:
    """Marshall-anchored 3-tuple variant with city-centroid fallback."""
    return smarty_zip_or_city_estimate(
        situs, "Albertville AL", _MARSHALL_ANCHORS, rate_tracker=rate_tracker,
    )


def smarty_zip_for_madison_address(
    situs: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> tuple[str, str]:
    """Madison-anchored ZIP recovery with multi-city fallback."""
    return smarty_zip_for_assuranceweb_address(
        situs,
        lastline_hint="Huntsville AL",
        anchor_fallbacks=_MADISON_ANCHORS,
        rate_tracker=rate_tracker,
    )


def smarty_zip_for_marshall_address(
    situs: str,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
) -> tuple[str, str]:
    """Marshall-anchored ZIP recovery with multi-city fallback.

    Primary anchor is Albertville (largest city); fallbacks cycle through
    Boaz, Guntersville, Arab, Grant, Horton, Crossville, then city-less
    "AL" as the final attempt. Each retry ~$0.001 — cheap insurance for
    addresses Smarty's primary-anchor lookup can't resolve.
    """
    return smarty_zip_for_assuranceweb_address(
        situs,
        lastline_hint="Albertville AL",
        anchor_fallbacks=_MARSHALL_ANCHORS,
        rate_tracker=rate_tracker,
    )
