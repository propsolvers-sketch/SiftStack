"""DataSift REST API client — kills the Playwright upload wizard.

The DataSift Open API dropped 2026-07-25 (Business plan+, key generated
via app.reisift.io → Settings → Integrations → Open API). Base URL:
``https://apiv2.reisift.io/api/internal/``. Auth: ``Authorization: Api-Key
<key>`` on every request. See ``spec.yaml`` at repo root for full schema.

Why this exists: our previous upload path drove Playwright through the
6-step upload wizard, which broke repeatedly (viewport clipping, sidebar
selector drift, cold-start SPA races, Tags/Lists/Phone columns silently
unmapped at Step 5, etc.). Every one of those failure modes disappears
with direct REST calls.

Design principles:
  * Env-var auth via ``DATASIFT_API_KEY`` — same convention as Enformion
    / Trestle / Smarty. ``is_configured()`` returns False when unset so
    callers can no-op cleanly.
  * Per-process budget cap via ``DATASIFT_API_BUDGET`` (default 5000
    requests per run). Prevents a runaway loop from tripping rate limits
    we don't yet know the ceiling of — API returns no ``X-RateLimit-*``
    headers as of 2026-07-28 probe.
  * Retry once on 429 / 500-series with 5s backoff. Everything else
    surfaces to the caller as an exception with the response body.
  * Lookup-or-create for lists/tags: DataSift's add-lists / add-tags
    endpoints require UUIDs, not names. ``list_uuid("Foreclosure")``
    returns the UUID, creating the list if it doesn't exist yet. Caches
    per-process to avoid re-querying.
  * No response-body dataclasses — endpoints return the raw JSON dict
    since callers mostly need UUIDs and a few fields, not typed models.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests

from street_suffixes import canonical_street

logger = logging.getLogger(__name__)


BASE_URL = "https://apiv2.reisift.io/api/internal"
_TIMEOUT = 45
_MAX_RETRIES = 2

_lock = threading.Lock()
_budget_total = int(os.environ.get("DATASIFT_API_BUDGET", "5000"))
_budget_used = 0
_disabled_reason: str | None = None

# Per-process caches for lookup-or-create resolvers. Populated lazily on
# first use; never expires within a run. If the operator creates lists
# or tags in DataSift's UI mid-run, the cache won't see them until the
# next process restart — acceptable given cron cadence is daily.
_list_uuid_cache: dict[str, str] = {}       # title (lowercased) → uuid
_tag_uuid_cache: dict[str, str] = {}
_phone_tag_uuid_cache: dict[str, str] = {}


# ── Auth + budget ────────────────────────────────────────────────────


def is_configured() -> bool:
    """True when DATASIFT_API_KEY is set. Callers use this to no-op cleanly."""
    return bool(os.environ.get("DATASIFT_API_KEY"))


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Api-Key {os.environ['DATASIFT_API_KEY']}",
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


class DataSiftAPIError(RuntimeError):
    """Raised on any non-2xx response from the DataSift API after retries."""
    def __init__(self, status_code: int, body: str, method: str, path: str):
        self.status_code = status_code
        self.body = body
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} → HTTP {status_code}: {body[:400]}")


# ── Core request path ────────────────────────────────────────────────


def _request(
    method: str, path: str, *,
    json_body: dict | list | None = None,
    params: dict | None = None,
) -> Any:
    """Send one request with retry-on-transient. Returns parsed JSON on 2xx.

    Raises DataSiftAPIError on 4xx / persistent 5xx. Returns ``None`` for
    204 No Content responses (DELETE endpoints).
    """
    global _budget_used, _disabled_reason

    if not is_configured():
        raise DataSiftAPIError(
            0, "DATASIFT_API_KEY not set — cannot make API calls",
            method, path,
        )

    with _lock:
        if _disabled_reason:
            raise DataSiftAPIError(0, f"client disabled: {_disabled_reason}", method, path)
        if _budget_used >= _budget_total:
            _disabled_reason = "budget_exhausted"
            raise DataSiftAPIError(
                0, f"budget exhausted ({_budget_used}/{_budget_total})",
                method, path,
            )
        _budget_used += 1

    url = f"{BASE_URL}{path}"
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.request(
                method, url,
                headers=_headers(),
                json=json_body,
                params=params or {},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as e:
            if attempt + 1 < _MAX_RETRIES:
                logger.info("DataSift %s %s network error (attempt %d): %s",
                            method, path, attempt + 1, e)
                time.sleep(5)
                continue
            raise DataSiftAPIError(0, f"network error after retries: {e}",
                                   method, path)

        # Transient errors → retry once
        if resp.status_code in (429, 500, 502, 503, 504) and attempt + 1 < _MAX_RETRIES:
            logger.info("DataSift HTTP %d on %s %s — retrying in 5s",
                        resp.status_code, method, path)
            time.sleep(5)
            continue

        # Auth / disabled — kill the whole run
        if resp.status_code in (401, 403):
            with _lock:
                _disabled_reason = f"http_{resp.status_code}"
            logger.error("DataSift HTTP %d — credentials invalid or key revoked. "
                         "Disabling API for this run. Body: %s",
                         resp.status_code, (resp.text or "")[:400])
            raise DataSiftAPIError(resp.status_code, resp.text or "", method, path)

        # 204 No Content — DELETE endpoints return this
        if resp.status_code == 204:
            return None

        if not resp.ok:
            raise DataSiftAPIError(resp.status_code, resp.text or "", method, path)

        try:
            return resp.json()
        except ValueError:
            raise DataSiftAPIError(
                resp.status_code, f"non-JSON body: {(resp.text or '')[:400]}",
                method, path,
            )

    # Shouldn't reach here — the loop either returns or raises
    raise DataSiftAPIError(0, "exhausted retries without response", method, path)


def _get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def _post(path: str, json_body: dict | list | None = None) -> Any:
    return _request("POST", path, json_body=json_body)


def _patch(path: str, json_body: dict) -> Any:
    return _request("PATCH", path, json_body=json_body)


def _delete(path: str) -> None:
    return _request("DELETE", path)


# ── Identity ─────────────────────────────────────────────────────────


def me() -> dict:
    """GET /user/ — returns the authenticated user's identity + plan."""
    return _get("/user/")


# ── Lists (lookup-or-create) ─────────────────────────────────────────


def list_uuid(title: str, *, create_if_missing: bool = True) -> str | None:
    """Return the UUID of a DataSift list by title, creating it if absent.

    Case-insensitive title match. Caches per-process. Returns None if
    not found and create_if_missing=False.
    """
    key = title.strip().lower()
    if not key:
        return None
    if key in _list_uuid_cache:
        return _list_uuid_cache[key]

    # Page through /list/ once to build the cache. 54 lists as of
    # 2026-07-28, so one page at limit=200 is plenty.
    resp = _get("/list/", {"limit": 200, "ordering": "title"})
    for row in (resp.get("results") or []):
        rk = (row.get("title") or "").strip().lower()
        if rk:
            _list_uuid_cache[rk] = row["uuid"]

    if key in _list_uuid_cache:
        return _list_uuid_cache[key]

    if not create_if_missing:
        return None

    created = _post("/list/", {"title": title.strip()})
    uuid = created["uuid"]
    _list_uuid_cache[key] = uuid
    logger.info("Created new DataSift list %r → %s", title, uuid)
    return uuid


# ── Tags (lookup-or-create) ──────────────────────────────────────────


def tag_uuid(title: str, *, create_if_missing: bool = True) -> str | None:
    """Return UUID of a DataSift tag by title, creating it if absent.

    Uses an "optimistic create" pattern because DataSift's /tag/ `search`
    query param is IGNORED (verified 2026-07-28 — GET /tag/?search=X
    returns all 666 tags unfiltered). So instead of search-then-create,
    we POST first; if the tag already exists the API returns 400 with
    ``non_field_errors: The fields title must make a unique set`` and
    we page through /tag/ to find the existing UUID.

    Per-run cache means each unique tag costs at most ONE optimistic
    POST + ONE paginated GET; subsequent hits are O(1) dict lookup.
    """
    key = title.strip().lower()
    if not key:
        return None
    if key in _tag_uuid_cache:
        return _tag_uuid_cache[key]

    if not create_if_missing:
        # Read-only path: scan all pages once and populate cache.
        _populate_tag_cache_all_pages()
        return _tag_uuid_cache.get(key)

    # Optimistic-create path: try POST, catch unique-violation
    try:
        created = _post("/tag/", {"title": title.strip()})
        uuid = created["uuid"]
        _tag_uuid_cache[key] = uuid
        logger.info("Created new DataSift tag %r → %s", title, uuid)
        return uuid
    except DataSiftAPIError as e:
        if e.status_code != 400 or "unique" not in e.body.lower():
            raise
        # Tag already exists — page /tag/ to find it
        _populate_tag_cache_all_pages()
        if key not in _tag_uuid_cache:
            raise DataSiftAPIError(
                0,
                f"tag {title!r} returned 'unique set' error on create but was "
                f"not found in paged /tag/ scan — DataSift state may be inconsistent",
                "POST", "/tag/",
            )
        return _tag_uuid_cache[key]


def _populate_tag_cache_all_pages(page_size: int = 200) -> None:
    """Walk /tag/ paginated and populate _tag_uuid_cache with everything.

    Called on first search miss to convert a series of point-lookups into
    a single full-scan. 666 tags × 200/page = ~4 pages. Idempotent."""
    offset = 0
    while True:
        resp = _get("/tag/", {"limit": page_size, "offset": offset})
        rows = resp.get("results") or []
        if not rows:
            break
        for row in rows:
            rk = (row.get("title") or "").strip().lower()
            if rk and rk not in _tag_uuid_cache:
                _tag_uuid_cache[rk] = row["uuid"]
        # Stop when we've seen fewer rows than page_size (last page)
        if len(rows) < page_size:
            break
        offset += page_size


# ── Phone Tags (Trestle tier UUIDs) ──────────────────────────────────


def phone_tag_uuid(title: str, *, create_if_missing: bool = True) -> str | None:
    """Return UUID of a phone-tag (Trestle tier: 'Dial First', 'Drop', etc.).

    Only 36 phone-tags exist; single page load is fine.
    """
    key = title.strip().lower()
    if not key:
        return None
    if key in _phone_tag_uuid_cache:
        return _phone_tag_uuid_cache[key]

    resp = _get("/phone/tag/", {"limit": 200})
    for row in (resp.get("results") or []):
        rk = (row.get("title") or "").strip().lower()
        if rk:
            _phone_tag_uuid_cache[rk] = row["uuid"]

    if key in _phone_tag_uuid_cache:
        return _phone_tag_uuid_cache[key]
    if not create_if_missing:
        return None

    created = _post("/phone/tag/", {"title": title.strip()})
    uuid = created["uuid"]
    _phone_tag_uuid_cache[key] = uuid
    logger.info("Created new phone tag %r → %s", title, uuid)
    return uuid


# ── Properties ───────────────────────────────────────────────────────


def create_property(
    *,
    street: str, city: str, state: str, postal_code: str,
    owner_first: str = "", owner_last: str = "",
    mailing_street: str = "", mailing_city: str = "",
    mailing_state: str = "", mailing_zip: str = "",
    phones: list[dict] | None = None,
    emails: list[str] | None = None,
    notes: str = "",
) -> dict:
    """POST /property/ — create one property.

    Returns the created property dict including the new UUID at ``uuid``.
    Mailing address defaults to the property address when unspecified.

    phones is a list of ``{"number": "5551234", "type": "MOBILE",
    "status": "UNKNOWN", "tags": []}`` dicts per the API's Phone schema.
    """
    mailing = {
        "street": mailing_street or street,
        "city": mailing_city or city,
        "state": mailing_state or state,
        "postal_code": mailing_zip or postal_code,
        "country": "US",
    }
    body: dict[str, Any] = {
        "address": {
            "street": street, "city": city, "state": state,
            "postal_code": postal_code, "country": "US",
        },
        "owner": {
            "first_name": owner_first or None,
            "last_name": owner_last or None,
            "address": mailing,
            "phones": phones or [],
            "emails": emails or [],
        },
    }
    if notes:
        body["notes"] = notes
    return _post("/property/", body)


def get_property(uuid: str) -> dict:
    return _get(f"/property/{uuid}/")


# ── Address→UUID lookup index (works around broken /property/ filters) ──
#
# DataSift's /property/ endpoint IGNORES every filter param we've tested
# (?search, ?q, ?query, ?filter, ?zip5, ?zip, ?address, ?street, etc.).
# Only ?ordering= and ?limit= have effect.
#
# Workaround: paginate ALL records once per run, build in-memory
# {"street_lower|zip5": uuid} dict, look up by address client-side.
#
# Slow first call (~5-15 min for 141K records) but subsequent lookups
# are O(1) dict access. Cache is per-process.

# Second index, same pagination pass: suffix-normalized keys. Our source
# portals and DataSift disagree on street-suffix spelling (T&B publishes
# "807 Briarwood Drive", DataSift stores "807 Briarwood Dr"), so the exact
# key misses. The normalized index collapses both to "807 briarwood dr".
# _property_index_ambiguous holds normalized keys that two DISTINCT UUIDs
# collapsed onto — those return None rather than guessing, because tagging
# the wrong property writes durable state to the CRM.
_property_index: dict[str, str] | None = None
_property_index_normalized: dict[str, str] | None = None
_property_index_ambiguous: set[str] | None = None


def _property_index_key(street: str, zip5: str) -> str:
    """Canonical index key: lowercased-street + | + zip5."""
    return f"{(street or '').strip().lower()}|{(zip5 or '').strip()[:5]}"


def _property_index_key_normalized(street: str, zip5: str) -> str:
    """Fallback index key: suffix-normalized street + | + zip5.

    zip5 handling is byte-identical to _property_index_key, so a ZIP
    mismatch can never match through the fallback either.
    """
    return f"{canonical_street(street)}|{(zip5 or '').strip()[:5]}"


def _ingest_property_records(
    records: list[dict],
    exact: dict[str, str],
    normalized: dict[str, str],
    ambiguous: set[str],
) -> None:
    """Fold one page of /property/ records into the three index containers.

    Mutates ``exact``, ``normalized`` and ``ambiguous`` in place. Pure and
    network-free — this is the seam the offline tests drive.
    """
    for rec in records:
        addr = rec.get("address") or {}
        street = (addr.get("street") or "").strip()
        zip5 = (addr.get("zip5") or addr.get("postal_code") or "").strip()[:5]
        uuid = rec.get("uuid")
        if not (street and zip5 and uuid):
            continue

        key = _property_index_key(street, zip5)
        # Prefer earliest occurrence (older records — more stable UUIDs)
        if key not in exact:
            exact[key] = uuid

        norm_key = _property_index_key_normalized(street, zip5)
        if norm_key in ambiguous:
            continue
        prior = normalized.get(norm_key)
        if prior is None:
            normalized[norm_key] = uuid
        elif prior != uuid:
            # Two different properties collapse onto one normalized key —
            # refuse to guess. Logged once, on the transition only.
            del normalized[norm_key]
            ambiguous.add(norm_key)
            logger.warning(
                "Ambiguous normalized address key %r — %s vs %s; "
                "fallback disabled for this key",
                norm_key, prior, uuid,
            )


def build_property_index(*, page_size: int = 500, hard_cap: int = 20000,
                         ordering: str = "-created") -> dict[str, str]:
    """Paginate /property/ and build {street|zip5: uuid} index.

    hard_cap protects against runaway pagination. DataSift's total record
    count is ~141K but only ~20K should be in Tier 1+2 ZIPs anyway (our
    calling scope). Adjust if needed.

    ``ordering`` (added 2026-09-05): DataSift hard-caps any single query at
    10,000 rows, so one pass by ``-created`` only reaches the newest ~10K.
    Callers doing historical backfills can build a SECOND index with
    ``ordering="-updated"`` to reach older records that were recently
    touched (list-adds, cascade tags) — a different 10K window. Note the
    module-level cache is REPLACED on each build; a two-pass caller should
    collect misses from pass 1, rebuild with the other ordering, and retry.

    Returns the built index. Also caches it in the module-level
    _property_index so subsequent calls to find_property_uuid_by_address
    reuse it for the same process.
    """
    global _property_index, _property_index_normalized, _property_index_ambiguous
    index: dict[str, str] = {}
    normalized: dict[str, str] = {}
    ambiguous: set[str] = set()
    offset = 0
    while offset < hard_cap:
        resp = _get("/property/", {
            "limit": page_size, "offset": offset, "ordering": ordering,
        })
        page = resp.get("data") or resp.get("results") or []
        if not page:
            break
        # Both indexes are built in this SINGLE pass — no second pagination.
        _ingest_property_records(page, index, normalized, ambiguous)
        if len(page) < page_size:
            break
        offset += page_size
    _property_index = index
    _property_index_normalized = normalized
    _property_index_ambiguous = ambiguous
    logger.info("Built property address→UUID index: %d entries from %d records scanned",
                len(index), offset)
    logger.info("Normalized address fallback index: %d entries, %d ambiguous keys skipped",
                len(normalized), len(ambiguous))
    return index


def find_property_uuid_by_address(street: str, zip5: str, *, rebuild: bool = False,
                                  ordering: str = "-created") -> str | None:
    """Look up a property UUID by street + zip5.

    On first call (or if rebuild=True), paginates all DataSift records to
    build an in-memory index. Subsequent calls are O(1) dict lookups.

    Exact street+zip5 is tried first and returns immediately. Only on a
    miss does it fall back to the suffix-normalized key, which bridges
    "Briarwood Drive" (portal) vs "Briarwood Dr" (DataSift). A normalized
    key that two distinct UUIDs collapsed onto returns None — never a guess.

    Returns UUID or None if not found in DataSift.

    Replaces the broken pattern:
        resp = _get("/property/", {"search": street, "limit": 25})
        for candidate in resp["data"]: if candidate matches → return
    …which was silently failing because ?search= is ignored.
    """
    global _property_index
    if _property_index is None or rebuild:
        build_property_index(ordering=ordering)

    # Fast path — unchanged behavior for every lookup that succeeds today.
    hit = (_property_index or {}).get(_property_index_key(street, zip5))
    if hit:
        return hit

    norm_key = _property_index_key_normalized(street, zip5)
    if norm_key in (_property_index_ambiguous or set()):
        return None
    hit = (_property_index_normalized or {}).get(norm_key)
    if hit:
        logger.info("DS_ADDR_FALLBACK matched %r (%s) via normalized key %r → %s",
                    street, zip5, norm_key, hit)
    return hit


def update_owner_name(owner_uuid: str, *, first_name: str, last_name: str) -> dict:
    """PATCH /owner/{uuid}/ — update owner's first + last name.

    Used by recover_missing_owners.py to backfill owner names recovered
    via Enformion AddressID. Both first + last are required (empty strings
    would overwrite existing values — caller must ensure both are set).
    """
    if not (first_name and last_name):
        raise ValueError("Both first_name and last_name are required")
    return _patch(f"/owner/{owner_uuid}/", {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
    })


def delete_property(uuid: str) -> None:
    """DELETE /property/{uuid}/ — permanent. Use for test-cleanup only."""
    _delete(f"/property/{uuid}/")


def property_exists(*, reapi_id: str = "", sift_id: str = "") -> dict:
    """POST /property/exists/ — check if a property exists by reapi_id or sift_id."""
    body = {}
    if reapi_id:
        body["reapi_id"] = reapi_id
    if sift_id:
        body["sift_id"] = sift_id
    return _post("/property/exists/", body)


# ── Property mutations (replaces wizard Steps 3/5 + post-upload) ────


_tag_uuid_to_title_cache: dict[str, str] = {}
_list_uuid_to_title_cache: dict[str, str] = {}


def _resolve_tag_uuid_to_title(tag_uuid: str) -> str | None:
    """Reverse lookup: given a tag UUID, return its title. Cached per-process."""
    if tag_uuid in _tag_uuid_to_title_cache:
        return _tag_uuid_to_title_cache[tag_uuid]
    try:
        r = _get(f"/tag/{tag_uuid}/")
        title = (r.get("title") or "").strip()
        if title:
            _tag_uuid_to_title_cache[tag_uuid] = title
            return title
    except Exception:
        pass
    return None


def _resolve_list_uuid_to_title(list_uuid: str) -> str | None:
    """Reverse lookup: given a list UUID, return its title."""
    if list_uuid in _list_uuid_to_title_cache:
        return _list_uuid_to_title_cache[list_uuid]
    try:
        r = _get(f"/list/{list_uuid}/")
        title = (r.get("title") or "").strip()
        if title:
            _list_uuid_to_title_cache[list_uuid] = title
            return title
    except Exception:
        pass
    return None


_phone_tag_uuid_to_title_cache: dict[str, str] = {}


def _resolve_phone_tag_uuid_to_title(phone_tag_uuid: str) -> str | None:
    """Reverse lookup: given a phone-tag UUID, return its title.

    Phone-tags live at /phone/tag/ (different table from record tags).
    """
    if phone_tag_uuid in _phone_tag_uuid_to_title_cache:
        return _phone_tag_uuid_to_title_cache[phone_tag_uuid]
    # Prime the whole phone-tag cache once — only ~50 phone-tags total
    if not _phone_tag_uuid_to_title_cache:
        try:
            resp = _get("/phone/tag/", {"limit": 500})
            for row in (resp.get("results") or []):
                _phone_tag_uuid_to_title_cache[row["uuid"]] = (row.get("title") or "").strip()
        except Exception:
            pass
    return _phone_tag_uuid_to_title_cache.get(phone_tag_uuid)


def add_lists(property_uuid: str, list_uuids: list[str]) -> dict:
    """POST /property/{uuid}/add-lists/ — attach the record to lists.

    2026-08-10 DISCOVERY: DataSift's endpoint expects list TITLES (not UUIDs)
    in the payload. Passing UUIDs causes DataSift to CREATE new lists with
    those UUIDs as their titles instead of linking existing lists.

    We accept UUIDs for consistency with the rest of the API surface, then
    translate to titles before POSTing.
    """
    titles = []
    for u in list_uuids:
        title = _resolve_list_uuid_to_title(u)
        if title:
            titles.append(title)
        else:
            logger.warning("add_lists: could not resolve list UUID %s to title", u)
    if not titles:
        return None
    return _post(f"/property/{property_uuid}/add-lists/", {"lists": titles})


def add_tags(property_uuid: str, tag_uuids: list[str]) -> dict:
    """POST /property/{uuid}/add-tags/ — attach tags to the record.

    2026-08-10 DISCOVERY: DataSift's endpoint expects tag TITLES (not UUIDs)
    in the payload. Passing UUIDs causes DataSift to CREATE new tags with
    those UUIDs as their titles instead of linking existing tags.

    We accept UUIDs for consistency with the rest of the API surface, then
    translate to titles before POSTing.
    """
    titles = []
    for u in tag_uuids:
        title = _resolve_tag_uuid_to_title(u)
        if title:
            titles.append(title)
        else:
            logger.warning("add_tags: could not resolve tag UUID %s to title", u)
    if not titles:
        return None
    return _post(f"/property/{property_uuid}/add-tags/", {"tags": titles})


def add_notes(property_uuid: str, notes: str, *, dedup_on: str | None = None) -> dict | None:
    """POST /property/{uuid}/add-notes/ — append to the record's Notes field.

    Dedup guard (added 2026-08-11): DataSift stores notes as concatenated text
    without merge logic, so re-uploading the same pipeline note appends a
    duplicate. To prevent this, we check whether a `dedup_on` signature already
    exists in the record's current notes; if so, we skip the write.

    Args:
      notes: full note text to append (may be multi-line block)
      dedup_on: signature to check for in existing notes. If found → skip write.
                Defaults to the first 80 non-whitespace chars of `notes` when
                None — usually catches the note's unique header.
    """
    if not notes:
        return None
    if dedup_on is None:
        # Auto-derive a signature: first meaningful line + case-insensitive
        first_meaningful = next(
            (l.strip() for l in notes.splitlines() if l.strip() and not
             all(c in "═─=─" for c in l.strip())),
            "",
        )
        dedup_on = first_meaningful[:80] if first_meaningful else notes.strip()[:80]

    # Fetch current notes (they come back as a list of single-char strings; join)
    try:
        prop = get_property(property_uuid)
        existing = prop.get("notes") or []
        joined = "".join(n if isinstance(n, str) else "" for n in existing)
        if dedup_on and dedup_on in joined:
            logger.debug("Skipping duplicate note on %s (signature %r already present)",
                         property_uuid[:8], dedup_on[:50])
            return None
    except Exception as e:
        # If we can't verify, err on the side of writing (better dupe than lost data)
        logger.debug("add_notes dedup check failed on %s: %s — proceeding with write",
                     property_uuid[:8], e)

    return _post(f"/property/{property_uuid}/add-notes/", {"notes": notes})


def add_phone_tag(
    property_uuid: str, phone_number: str, phone_tag_uuids: list[str],
) -> Any:
    """POST /property/{uuid}/add-phone-tag/ — DEPRECATED / BROKEN.

    2026-08-10 E2E DISCOVERY: this endpoint returns 200 (null body) but does
    NOT actually persist tags. Verified in DataSift UI — no tags appear on
    phones after N calls. The correct write path is `upsert-phones` with a
    populated `tags` field per phone (see ``apply_phone_tags`` below).

    Kept for backward compat — now delegates to ``apply_phone_tags`` so any
    caller still using this function will actually persist tags.
    """
    return apply_phone_tags(property_uuid, {phone_number: phone_tag_uuids})


def apply_phone_tags(
    property_uuid: str, phone_tag_map: dict[str, list[str]],
) -> Any:
    """Apply phone tags via the ACTUAL working endpoint: /owner/{uuid}/upsert-phones/.

    Discovered 2026-08-10 — the `/property/{uuid}/add-phone-tag/` endpoint
    returns 200 but silently drops tags. `upsert-phones` with a `tags` field
    per phone entry IS the write path that actually persists (verified via
    property GET showing new tag UUIDs on the phone).

    Args:
      property_uuid: The property record UUID (used to fetch owner + existing phones)
      phone_tag_map: {phone_number: [tag_uuid, ...]} — tags to APPEND per phone

    Behavior:
      * Fetches the property + owner to get owner_uuid + existing phone metadata
      * For each phone, UNIONs new tags with existing tags (never loses old tags)
      * Preserves each phone's existing type/status
      * One upsert-phones API call per property (batched)

    Returns the upsert response dict, or None if property/owner not found.
    """
    if not phone_tag_map:
        return None

    prop = get_property(property_uuid)
    owner = prop.get("owner") or {}
    owner_uuid = owner.get("uuid")
    if not owner_uuid:
        return None
    existing_phones = {p["number"]: p for p in (owner.get("phones") or [])
                       if p.get("number")}

    phones_payload = []
    for phone_number, new_tag_uuids in phone_tag_map.items():
        existing = existing_phones.get(phone_number, {})
        # Extract existing phone-tag values (may be UUIDs or titles depending
        # on how they were originally applied). If UUID, resolve to title.
        existing_tag_values = set()
        for t in (existing.get("tags") or []):
            val = t.get("uuid") if isinstance(t, dict) else t
            if val:
                # If it looks like a UUID, resolve to title
                if len(val) == 36 and val.count("-") == 4:
                    title = _resolve_phone_tag_uuid_to_title(val)
                    if title:
                        existing_tag_values.add(title)
                    # else drop — this is a junk UUID-with-UUID-title tag
                else:
                    existing_tag_values.add(val)  # already a title
        # Resolve new UUIDs to titles (same bug as record tags: /upsert-phones/
        # tags field expects TITLES, creates junk tags from UUIDs)
        new_titles = set()
        for u in new_tag_uuids:
            if len(u) == 36 and u.count("-") == 4:
                title = _resolve_phone_tag_uuid_to_title(u)
                if title:
                    new_titles.add(title)
                else:
                    logger.warning("apply_phone_tags: could not resolve %s to title", u)
            else:
                new_titles.add(u)  # already a title
        union_tags = sorted(existing_tag_values | new_titles)
        phones_payload.append({
            "number": phone_number,
            "type": existing.get("type") or "UNKNOWN",
            "status": existing.get("status") or "UNKNOWN",
            "tags": union_tags,
        })

    return _post(
        f"/owner/{owner_uuid}/upsert-phones/",
        {"phones": phones_payload},
    )


def skip_trace(property_uuid: str) -> dict:
    """POST /property/skip-trace/ — trigger DataSift's native skip-trace.

    Replaces the datasift_skip_trace.py Playwright script. Async on
    DataSift's end — completion tracked via GET /activity/?type=skip_trace.
    """
    return _post("/property/skip-trace/", {"properties": [property_uuid]})


# ── Activity (for monitoring bulk operations) ────────────────────────


def list_activity(activity_type: str = "upload", limit: int = 20) -> dict:
    """GET /activity/?type=<type> — list recent uploads / skip-traces / etc.

    ``activity_type`` accepts 'upload' or 'skip_trace' per API spec.
    """
    return _get("/activity/", {"type": activity_type, "limit": limit})
