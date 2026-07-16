"""Enformion / Endato Person Search — production client.

Wraps the ``POST https://devapi.enformion.com/PersonSearch`` endpoint used
by the Deep Prospecting v4 skill's Primary Path (steps A + C). Provides a
few pipeline-friendly extras on top of the reference script:

  * Env-var-based auth (ENFORMION_AP_NAME + ENFORMION_AP_PASSWORD) —
    same convention as Smarty / 2Captcha / Firecrawl. When unset, every
    entry point becomes a no-op so calling code can wire this in and
    ship it without breaking anything on the day of the deploy.

  * Per-process budget cap via ENFORMION_BUDGET (default 500 searches
    per run). Prevents a runaway loop from burning the account. Same
    shape as _firecrawl_budget_total.

  * HTTP-status-based failure detection. Enformion returns an ``error``
    object on EVERY response (successes included) — we branch on status
    code, never on the presence of `error`.

  * Field helpers that recover year from masked / partial / dict dates
    (``9/XX/1955``, ``3/XX/2026``, ``{"year": "2019"}``), do WHOLE-TOKEN
    surname matching (``"Maxwell" != "Well"``), and normalize phones to
    10-digit E.164-lite strings for downstream dedup.

The resolver in ``enformion_heir_resolver.py`` composes these primitives
into the full A→E waterfall on a NoticeData; adapters call the resolver,
not this module directly.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


PERSON_SEARCH_URL = "https://devapi.enformion.com/PersonSearch"
CLOSEST_KIN_LEVEL = "ab"    # relativeLevel code for closest kin (spouse, siblings, parents, children)

_TIMEOUT = 45
_MAX_RETRIES = 2            # one retry on transient 429 / 500-503

_lock = threading.Lock()
_budget_total = int(os.environ.get("ENFORMION_BUDGET", "500"))
_budget_used = 0
_disabled_reason: str | None = None


# ── Auth + budget ────────────────────────────────────────────────────


def is_configured() -> bool:
    """True when credentials are present. Callers use this to no-op cleanly."""
    return bool(os.environ.get("ENFORMION_AP_NAME") and
                os.environ.get("ENFORMION_AP_PASSWORD"))


def _headers() -> dict[str, str]:
    return {
        "galaxy-ap-name": os.environ["ENFORMION_AP_NAME"],
        "galaxy-ap-password": os.environ["ENFORMION_AP_PASSWORD"],
        "galaxy-search-type": "Person",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def budget_remaining() -> int:
    with _lock:
        return max(0, _budget_total - _budget_used)


def reset_budget(new_total: int | None = None) -> None:
    """Test-only helper. Not thread-safe against in-flight requests."""
    global _budget_used, _budget_total, _disabled_reason
    with _lock:
        _budget_used = 0
        _disabled_reason = None
        if new_total is not None:
            _budget_total = new_total


# ── Core POST ────────────────────────────────────────────────────────


def household_search(
    last_name: str, *,
    street: str, city: str, state: str, zip_code: str,
    max_results: int = 25,
) -> dict[str, Any]:
    """Address-anchored search — returns everyone at the household (LastName + full address).

    Discovered 2026-07-16: Enformion's Person Search with FirstName+LastName
    penalizes elderly recently-deceased people because their ranking
    algorithm prefers active-digital-footprint candidates. On 5 real Alabama
    pre_probate test cases, first-name-included search returned WRONG-person
    namesakes 4/5 times (e.g. Jerry B Ange age 24 instead of our elderly
    target). Dropping first name and anchoring on street+city+state+zip
    returned the correct household in all 5 cases.

    Use this as the PRIMARY heir-discovery search for pre_probate / probate.
    The response includes phones + addresses + relativesSummary for every
    person at the address, so signer resolution happens in ONE call instead
    of the old (decedent + N signer) waterfall — much cheaper AND much
    higher hit rate on our demographic.

    Callers filter the returned persons by age (>= 55 for likely spouse,
    25-55 for adult children) and by name-partial match against obit-known
    survivors.
    """
    global _budget_used, _disabled_reason

    if not is_configured():
        return {}

    with _lock:
        if _disabled_reason:
            return {}
        if _budget_used >= _budget_total:
            logger.warning(
                "Enformion budget exhausted (%d/%d) — skipping household search",
                _budget_used, _budget_total,
            )
            _disabled_reason = "budget_exhausted"
            return {}
        _budget_used += 1

    body: dict[str, Any] = {
        "LastName": last_name,
        "Addresses": [{
            "AddressLine1": street,
            "AddressLine2": f"{city}, {state} {zip_code}",
        }],
        "Page": 1,
        "ResultsPerPage": max_results,
    }

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                PERSON_SEARCH_URL, headers=_headers(),
                json=body, timeout=_TIMEOUT,
            )
        except requests.RequestException as e:
            if attempt + 1 < _MAX_RETRIES:
                logger.info(
                    "Enformion household search request error (attempt %d/%d): %s",
                    attempt + 1, _MAX_RETRIES, e,
                )
                time.sleep(5)
                continue
            logger.warning("Enformion household search failed for %s @ %s: %s",
                           last_name, street, e)
            return {}

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                logger.warning("Enformion household search returned non-JSON body")
                return {}

        if resp.status_code in (429, 500, 502, 503, 504) and attempt + 1 < _MAX_RETRIES:
            logger.info("Enformion HTTP %d — retrying in 5s", resp.status_code)
            time.sleep(5)
            continue

        if resp.status_code in (401, 403):
            with _lock:
                _disabled_reason = f"http_{resp.status_code}"
            logger.error(
                "Enformion HTTP %d — credentials invalid. Disabling.",
                resp.status_code,
            )
            return {}

        logger.warning(
            "Enformion household HTTP %d for %s @ %s: %s",
            resp.status_code, last_name, street, (resp.text or "")[:160],
        )
        return {}

    return {}


def person_search(
    first: str, last: str, *,
    city: str = "", state: str = "", zip_code: str = "",
    dob_year: str = "",
) -> dict[str, Any]:
    """One Person Search POST. Returns parsed JSON, or {} on failure.

    Failure paths (all return {} — never raise):

      * credentials unset                 → {} (soft-disabled)
      * budget exhausted                  → {} (with a WARNING log)
      * request-level exception           → {} (WARNING with URL + err)
      * non-200 HTTP status               → {} (WARNING with status + body prefix)

    Successful responses are returned verbatim. Enformion's per-response
    ``error`` object is IGNORED here — it appears on successes too.
    Callers detect "no match" via ``first_match(response) is None``.

    Minimum-criteria contract per skill reference:
      * (first, last, city+state+zip) for the deceased (Step A)
      * (first, last, dob_year) for signer resolution (Step C)
      * Name+city alone is REJECTED by Enformion as insufficient.
    """
    global _budget_used, _disabled_reason

    if not is_configured():
        return {}

    with _lock:
        if _disabled_reason:
            return {}
        if _budget_used >= _budget_total:
            logger.warning(
                "Enformion budget exhausted (%d/%d) — skipping further calls this run",
                _budget_used, _budget_total,
            )
            _disabled_reason = "budget_exhausted"
            return {}
        _budget_used += 1

    body: dict[str, Any] = {
        "FirstName": first, "LastName": last,
        "Page": 1, "ResultsPerPage": 5,
    }
    addr2 = " ".join(p for p in [
        f"{city}," if city else "", state, zip_code,
    ] if p).strip()
    if addr2:
        body["Addresses"] = [{"AddressLine2": addr2}]
    if dob_year:
        body["Dob"] = str(dob_year)

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                PERSON_SEARCH_URL, headers=_headers(),
                json=body, timeout=_TIMEOUT,
            )
        except requests.RequestException as e:
            if attempt + 1 < _MAX_RETRIES:
                logger.info(
                    "Enformion request error (attempt %d/%d): %s — retrying in 5s",
                    attempt + 1, _MAX_RETRIES, e,
                )
                time.sleep(5)
                continue
            logger.warning("Enformion request failed for %s %s: %s", first, last, e)
            return {}

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                logger.warning(
                    "Enformion returned 200 but non-JSON body for %s %s (len=%d)",
                    first, last, len(resp.text),
                )
                return {}

        # 429 rate limit / 5xx transient — retry once
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt + 1 < _MAX_RETRIES:
                logger.info(
                    "Enformion HTTP %d (attempt %d/%d) — retrying in 5s",
                    resp.status_code, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(5)
                continue

        # 401/403 → credential issue; disable for the rest of the run
        if resp.status_code in (401, 403):
            with _lock:
                _disabled_reason = f"http_{resp.status_code}"
            logger.error(
                "Enformion HTTP %d — credentials invalid or access-profile "
                "revoked. Disabling Enformion for this run. Body: %s",
                resp.status_code, (resp.text or "")[:200],
            )
            return {}

        logger.warning(
            "Enformion HTTP %d for %s %s: %s",
            resp.status_code, first, last, (resp.text or "")[:160],
        )
        return {}

    return {}


def first_match(
    response: dict[str, Any],
    *,
    prefer_first_name: str = "",
    prefer_deceased: bool = False,
) -> dict[str, Any] | None:
    """Return the best-matching person from a response, or None.

    Enformion's Person Search returns up to N candidates (default 5)
    ranked by their internal relevance model, which for our use case
    often ranks by activity/completeness rather than exact-name match.
    A search for "Thomas Williams @ Vestavia Hills" can legitimately
    return "Cindnate Williams" first because she's a more active record
    at the same address. That's a wrong-person hit for our purposes.

    Selection priority:
      1. If ``prefer_first_name`` is given, prefer candidates whose first
         name matches (case-insensitive, exact OR starts-with — handles
         "Thomas" matching "Thomas Russell")
      2. If ``prefer_deceased=True``, among first-name matches prefer
         one with a non-empty ``datesOfDeath[]`` (we're searching for
         a KNOWN decedent — the living namesake is the wrong person)
      3. Fall back to persons[0] (Enformion's own top pick) if no
         preference matches
    """
    if not response:
        return None
    persons = (response.get("persons") or response.get("people")
               or response.get("results") or [])
    if not persons:
        return None

    if not prefer_first_name:
        return persons[0]

    target = prefer_first_name.lower().strip()

    def _matches_first(p):
        pf = _get_first_name(p).lower()
        return pf and (pf == target or pf.startswith(target + " ")
                       or target.startswith(pf + " "))

    first_matches = [p for p in persons if _matches_first(p)]
    if not first_matches:
        return persons[0]  # no name matches — fall back to Enformion's top pick

    if prefer_deceased:
        deceased = [p for p in first_matches if p.get("datesOfDeath")]
        if deceased:
            return deceased[0]

    return first_matches[0]


# ── Field helpers (schema quirks) ────────────────────────────────────


def full_name(record: dict[str, Any]) -> str:
    """Compose 'First Middle Last' from an Enformion person or relative dict.

    Handles two different shapes Enformion actually returns (verified against
    live response 2026-07-16):

      * PERSON records (top-level): name lives NESTED inside a "name" dict
        (`{"name": {"firstName": ..., "lastName": ...}, "fullName": "..."}`).
        Prefer top-level `fullName` string when present — it's the pre-joined
        display name.
      * RELATIVE records (inside relativesSummary[]): name fields are FLAT
        at the top of the dict (`{"firstName": ..., "lastName": ...}`) with
        no nested "name" wrapper.

    Reference-doc schema said flat everywhere; live schema is nested for
    persons. This helper tries both shapes in priority order.
    """
    # 1) Top-level pre-joined fullName (person records only)
    if record.get("fullName"):
        return str(record["fullName"]).strip()

    # 2) Nested "name" dict (person records)
    name_dict = record.get("name")
    if isinstance(name_dict, dict):
        parts = [
            name_dict.get("firstName", ""),
            name_dict.get("middleName", ""),
            name_dict.get("lastName", ""),
        ]
        composed = " ".join(p.strip() for p in parts if p and p.strip())
        if composed:
            return composed

    # 3) Flat firstName/middleName/lastName (relatives + some persons)
    parts = [
        record.get("firstName", ""),
        record.get("middleName", ""),
        record.get("lastName", ""),
    ]
    composed = " ".join(p.strip() for p in parts if p and p.strip())
    if composed:
        return composed

    # 4) Last resort — rawNames array (rare)
    raw = record.get("rawNames") or record.get("name")
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        return (raw.get("fullName") or "").strip()
    return (raw or "").strip()


def _get_first_name(record: dict[str, Any]) -> str:
    """Pull the first name from a person/relative dict, handling both shapes."""
    name_dict = record.get("name")
    if isinstance(name_dict, dict) and name_dict.get("firstName"):
        return str(name_dict["firstName"]).strip()
    return str(record.get("firstName") or "").strip()


def _get_last_name(record: dict[str, Any]) -> str:
    """Pull the last name from a person/relative dict, handling both shapes."""
    name_dict = record.get("name")
    if isinstance(name_dict, dict) and name_dict.get("lastName"):
        return str(name_dict["lastName"]).strip()
    return str(record.get("lastName") or "").strip()


_YEAR_RE = re.compile(r"(19|20)\d{2}")


def year_of(value: Any) -> str:
    """Recover a 4-digit year from a possibly-masked/dict date.

    Handles: "9/XX/1955", "3/XX/2026", "2019-06-15", "1955",
    {"year": "1955"}, {"dob": "9/XX/1955"}, {"dod": "3/XX/2026"}.
    Returns "" if no year can be recovered.
    """
    if isinstance(value, dict):
        value = (value.get("year") or value.get("dob") or value.get("dod") or "")
    m = _YEAR_RE.search(str(value))
    return m.group(0) if m else ""


def extract_dod(person: dict[str, Any]) -> str:
    """Date of death as YYYY-MM-DD, or "" if unavailable.

    Handles masked (year-only) forms by returning YYYY-01-01 — enough
    for the year-level DOD conflict check downstream. Handles dict-shaped
    entries under datesOfDeath[] which some records use instead of the
    flat `dod`.
    """
    candidates = []
    if person.get("dod"):
        candidates.append(person["dod"])
    for d in person.get("datesOfDeath") or []:
        candidates.append(d.get("dod") if isinstance(d, dict) else d)

    for raw in candidates:
        if isinstance(raw, dict):
            raw = raw.get("dod") or raw.get("year") or ""
        raw = str(raw).strip()

        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            return m.group(0)
        y = year_of(raw)
        if y:
            return f"{y}-01-01"

    return ""


def is_deceased_flag(rel: dict[str, Any]) -> bool:
    """Coerce Enformion's ``isDeceased`` to a bool. Handles bool + string forms."""
    v = rel.get("isDeceased")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1")


def relatives(person: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract relativesSummary → sortable list of {name, type, level, dob, ...}.

    Closest kin first (level "ab"), then by score desc. Skips entries
    without a recoverable name.
    """
    out: list[dict[str, Any]] = []
    for rel in (person.get("relativesSummary") or person.get("relatives") or []):
        if not isinstance(rel, dict):
            continue
        name = full_name(rel)
        if not name:
            continue
        try:
            score = int(rel.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        out.append({
            "name": name,
            "type": (rel.get("relativeType") or rel.get("relationship") or "").strip(),
            "level": (rel.get("relativeLevel") or "").strip().lower(),
            "dob": year_of(rel.get("dob") or rel.get("dateOfBirth") or ""),
            "score": score,
            "deceased": is_deceased_flag(rel),
            "lastname": (rel.get("lastName") or "").strip(),
        })
    out.sort(key=lambda r: (r["level"] or "zz", -r["score"]))
    return out


def surname_matches(name: str, lastname: str, surname: str) -> bool:
    """Whole-last-name-token match (NOT a substring).

    "Maxwell" MUST NOT match surname "Well" — this is the well-documented
    Enformion gotcha. Prefer the explicit ``lastname`` field; fall back
    to the final space-delimited token of the composed name.
    """
    sur = (surname or "").lower().strip()
    if not sur:
        return False
    if lastname and lastname.lower().strip() == sur:
        return True
    tokens = (name or "").lower().split()
    return bool(tokens) and tokens[-1] == sur


def phones(person: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract phones from a person record → list of {number, type, is_connected}.

    Normalizes to 10-digit strings so downstream dedup can key on them.
    Drops non-10-digit entries (international, malformed, etc.).
    """
    out: list[dict[str, Any]] = []
    for p in (person.get("phoneNumbers") or []):
        if not isinstance(p, dict):
            continue
        raw = str(p.get("phoneNumber") or p.get("number") or "")
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            continue
        out.append({
            "number": digits,
            "type": (p.get("phoneType") or "").strip(),
            "is_connected": p.get("isConnected"),
            "last_reported": p.get("lastReportedDate") or "",
        })
    return out


def addresses(person: dict[str, Any]) -> list[str]:
    """Extract address history strings (most-recent first per Enformion order)."""
    out: list[str] = []
    for a in (person.get("addresses") or []):
        if isinstance(a, dict):
            full = a.get("fullAddress") or a.get("AddressLine2") or ""
            if full and full not in out:
                out.append(full.strip())
    return out
