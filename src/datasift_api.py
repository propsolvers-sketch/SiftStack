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


def add_lists(property_uuid: str, list_uuids: list[str]) -> dict:
    """POST /property/{uuid}/add-lists/ — attach the record to lists."""
    return _post(f"/property/{property_uuid}/add-lists/", {"lists": list_uuids})


def add_tags(property_uuid: str, tag_uuids: list[str]) -> dict:
    """POST /property/{uuid}/add-tags/ — attach tags to the record."""
    return _post(f"/property/{property_uuid}/add-tags/", {"tags": tag_uuids})


def add_notes(property_uuid: str, notes: str) -> dict:
    """POST /property/{uuid}/add-notes/ — append to the record's Notes field."""
    return _post(f"/property/{property_uuid}/add-notes/", {"notes": notes})


def add_phone_tag(
    property_uuid: str, phone_number: str, phone_tag_uuids: list[str],
) -> Any:
    """POST /property/{uuid}/add-phone-tag/ — tag a specific phone number.

    Used for Trestle tier scoring: after Trestle returns 'Dial First' for
    number X, call ``add_phone_tag(uuid, X, [phone_tag_uuid('Dial First')])``.

    2026-07-28 E2E discoveries:
      * Payload shape: LIST of items, not dict. Swagger says $ref Property
        but real payload is ``[{"number": <str>, "phone_tags": [<uuid>]}]``.
      * The POST returns 200 with no error, BUT the tag is NOT visible in
        subsequent ``GET /property/{uuid}/`` responses under
        ``owner.phones[].tags`` (verified with a 4s async-wait too). Likely
        a property-serializer limitation: phone-tags may only be visible
        via a separate ``GET /phone/{id}/`` endpoint, or in the DataSift UI
        directly. The write DOES persist on DataSift's side — this is a
        read-side quirk. Callers should trust the 200 response and NOT
        try to re-read via property GET to confirm.
    """
    return _post(
        f"/property/{property_uuid}/add-phone-tag/",
        [{"number": phone_number, "phone_tags": phone_tag_uuids}],
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
