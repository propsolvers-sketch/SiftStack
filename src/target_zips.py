"""Single source of truth for priority ZIP codes (Jefferson + Madison + Marshall, AL).

Defined from the SFR market analyses dated April–May 2026:
  - Jefferson: $125K-$500K SFR market analysis (top wholesale-viable ZIPs)
  - Madison:   SFR market research (best wholesale-margin ZIPs)
  - Marshall:  $125K-$500K SFR market analysis (Albertville-anchored)

Tier 1 = highest priority (deal-flow + margin sweet spot).
Tier 2 = strong secondary markets (smaller volume but still actionable).

When sources or tier definitions change, update this file ONLY — every
downstream filter (orchestrators, output formatters, DataSift presets)
imports from here.
"""

from __future__ import annotations


# ── Jefferson County, AL ─────────────────────────────────────────────────
# Source: Jefferson_County_AL_SFR_125K_500K_Market_Analysis.md (2026-04-18)
JEFFERSON_TIER_1: frozenset[str] = frozenset({
    "35215",  # Center Point / Roebuck      — 176 trans, $170K, 61 DOM
    "35214",  # Ensley / West Birmingham    — 98 trans,  $161K, 53 DOM
    "35022",  # Hoover                      — 81 trans,  $279K, 57 DOM
    "35023",  # Hueytown                    — 72 trans,  $187K, 58 DOM
    "35226",  # Vestavia Hills area         — 61 trans,  $440K, 48 DOM
    "35235",  # Irondale / Cahaba Heights   — 49 trans,  $210K, 54 DOM
})

JEFFERSON_TIER_2: frozenset[str] = frozenset({
    "35216",  # South Birmingham / Vestavia — 45 trans,  $484K, 40 DOM
    "35126",  # Pinson                      — 44 trans,  $231K, 62 DOM
    "35210",  # Crestwood / Eastwood        — 43 trans,  $286K, 59 DOM
    "35173",  # Trussville                  — 40 trans,  $398K, 61 DOM
    "35244",  # South Hoover                — 31 trans,  $448K, 51 DOM
})


# ── Madison County, AL (Huntsville) ──────────────────────────────────────
# Source: Madison_County_AL_SFR_Market_Analysis.md (2026-04-16)
MADISON_TIER_1: frozenset[str] = frozenset({
    "35810",  # North Huntsville            — 86 trans, $186K, 56 DOM
    "35811",  # Meridianville / Hazel Green — 66 trans, $287K, 60 DOM
    "35803",  # South Huntsville / Hampton  — 64 trans, $322K, 53 DOM
    "35758",  # Madison City                — 51 trans, $403K, 52 DOM
    "35805",  # West Huntsville             — 46 trans, $169K, 53 DOM
    "35801",  # Downtown Huntsville         — 44 trans, $425K, 61 DOM
})

MADISON_TIER_2: frozenset[str] = frozenset({
    "35757",  # Athens / Limestone corridor — 36 trans, $358K, 60 DOM
    "35759",  # Harvest                     — 28 trans, $—,    90 DOM
    "35763",  # Owens Cross Roads / Hampton — 27 trans, $440K, —
    "35806",  # South / Research Park       — 25 trans
    "35750",  # New Market area             — 23 trans
})


# ── Marshall County, AL (Albertville-anchored) ───────────────────────────
# Source: Marshall_County_AL_SFR_125K_500K_Market_Analysis.md (2026-05-12)
# Codified as the UNION of the MD analysis's volume-ranked picks AND the
# operator's strategic adjustments (2026-05-12 confirmation). Several Tier 2
# entries are Marshall-adjacent border ZIPs (35175 Cullman line, 35747 Grant,
# 35769 Scottsboro/Jackson line, 35980 DeKalb line) included to catch
# cross-county property ownership.
MARSHALL_TIER_1: frozenset[str] = frozenset({
    "35950",  # Albertville (central)       — 56 trans, $218K, 57 DOM
    "35976",  # Guntersville / Union Grove  — 37 trans, $263K, 45 DOM (fastest DOM in county)
    "35016",  # Arab (Huntsville commuter)  — 30 trans, $267K, 58 DOM
    "35961",  # Boaz (value play)           — 29 trans, $162K, 54 DOM
    "35951",  # Albertville fringe          — 20 trans, $280K, 67 DOM (operator-promoted)
    "35957",  # Crossville / Geraldine      — 15 trans, $247K, 60 DOM (operator-promoted)
})

MARSHALL_TIER_2: frozenset[str] = frozenset({
    "35962",  # Boaz area (slower velocity) — 11 trans, $208K, 77 DOM
    "35175",  # Union Grove / Joppa         — Cullman-line border ZIP (operator-added)
    "35747",  # Grant                       — Marshall County, low-volume supplemental
    "35769",  # Scottsboro / Section        — Jackson-line border ZIP (operator-added)
    "35980",  # Horton / Geraldine          — DeKalb-line border ZIP (operator-added)
})


# ── Convenience unions ───────────────────────────────────────────────────
ALL_TIER_1: frozenset[str] = JEFFERSON_TIER_1 | MADISON_TIER_1 | MARSHALL_TIER_1
ALL_TIER_2: frozenset[str] = JEFFERSON_TIER_2 | MADISON_TIER_2 | MARSHALL_TIER_2
ALL_TARGET: frozenset[str] = ALL_TIER_1 | ALL_TIER_2


def zip_tier(zip_code: str | None) -> int | None:
    """Classify a ZIP into 1 (Tier 1), 2 (Tier 2), or None (off-target).

    Accepts ZIP+4 (uses the leading 5 digits). Returns None for empty/invalid input.
    """
    if not zip_code:
        return None
    z = str(zip_code).strip()[:5]
    if len(z) != 5 or not z.isdigit():
        return None
    if z in ALL_TIER_1:
        return 1
    if z in ALL_TIER_2:
        return 2
    return None


def zip_tier_county(zip_code: str | None) -> tuple[int | None, str | None]:
    """Return (tier, county) for a ZIP. County is 'Jefferson', 'Madison', 'Marshall', or None."""
    z = (zip_code or "").strip()[:5]
    tier = zip_tier(z)
    if tier is None:
        return (None, None)
    if z in JEFFERSON_TIER_1 or z in JEFFERSON_TIER_2:
        return (tier, "Jefferson")
    if z in MADISON_TIER_1 or z in MADISON_TIER_2:
        return (tier, "Madison")
    if z in MARSHALL_TIER_1 or z in MARSHALL_TIER_2:
        return (tier, "Marshall")
    return (tier, None)


# USPS-preferred city per Tier 1 + Tier 2 ZIP, used as a fallback when
# the upstream property API returns a ZIP but no city. Added 2026-06-23
# after 8/19 pre-probate rows shipped with empty Property City (Jefferson
# E-Ring sometimes returns empty `situs_city` even when ZIP is populated).
# Built from USPS preferred-city for each ZIP — for shared ZIPs (e.g.
# 35226 spans Hoover + Vestavia Hills), the more populous community wins.
_ZIP_TO_CITY: dict[str, str] = {
    # Jefferson Tier 1
    "35215": "Birmingham",       # Center Point area (Birmingham metro)
    "35214": "Birmingham",       # Forestdale (Birmingham metro)
    "35022": "Bessemer",
    "35023": "Hueytown",
    "35226": "Hoover",           # Also covers Vestavia; Hoover more populous
    "35235": "Birmingham",       # Roebuck (Birmingham metro)
    # Jefferson Tier 2
    "35216": "Vestavia Hills",
    "35126": "Pinson",
    "35210": "Birmingham",       # East Lake (Birmingham metro)
    "35173": "Trussville",
    "35244": "Hoover",
    # Madison Tier 1
    "35810": "Huntsville",
    "35811": "Huntsville",
    "35803": "Huntsville",
    "35758": "Madison",          # City of Madison (Madison County)
    "35805": "Huntsville",
    "35801": "Huntsville",
    # Madison Tier 2
    "35757": "Madison",
    "35759": "Meridianville",
    "35763": "Owens Cross Roads",
    "35806": "Huntsville",
    "35750": "Hazel Green",
    # Marshall Tier 1
    "35950": "Albertville",
    "35976": "Guntersville",
    "35016": "Arab",
    "35961": "Boaz",
    "35951": "Albertville",
    "35957": "Crossville",
    # Marshall Tier 2
    "35962": "Boaz",
    "35175": "Joppa",            # Joppa is the part of 35175 in Marshall County
    "35747": "Grant",
    "35769": "Scottsboro",
    "35980": "Valley Head",      # DeKalb border ZIP
}


def city_for_zip(zip_code: str | None) -> str:
    """USPS-preferred city for a known Tier 1+2 ZIP.

    Returns empty string when the ZIP isn't in our table (off-target ZIPs,
    less-common AL ZIPs, ZIP+4 with bad leading 5). Callers typically use
    this as a fallback when the property API returned a ZIP but no city.

    Note: for ZIPs that span multiple municipalities (e.g. 35226 = Hoover
    AND Vestavia Hills), we return the larger/more-populous city. If finer
    accuracy is needed, route through Smarty.
    """
    if not zip_code:
        return ""
    z = str(zip_code).strip()[:5]
    return _ZIP_TO_CITY.get(z, "")
