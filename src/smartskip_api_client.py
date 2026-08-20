"""SmartSkip REST API client — replaces Playwright browser automation.

Same public interface as SmartSkipClient (submit_batch, wait_and_download,
context manager) so probate_cascade.py can swap between backends by
setting SMARTSKIP_CLIENT=api in the environment.

Endpoints (from Ty's deep-prospecting-v5, verified 2026-07-29):

  POST /auth/signin                → {accessToken, refreshToken}
  GET  /auth/refresh               → renew token pair
  POST /bulk-skip/mapping          → upload CSV multipart
  GET  /bulk-skip/fields           → get {required, optional} field schema
  POST /bulk-skip/fields/{id}      → submit column→field mapping
  POST /bulk-skip/calculate/{id}   → cost preview (FREE, dry-run)
  GET  /payment/payment-method     → list saved cards
  POST /bulk-skip/payment-intent   → CHARGE saved card ($$$)
  GET  /bulk-skip?sortField=...    → list orders (poll for status)
  GET  /bulk-skip/download/{id}?type=horizontal → results CSV

Auth: bearer token. Access token ~15min, refresh token ~30d, then
fallback to re-signin with SMARTSKIP_EMAIL / SMARTSKIP_PASSWORD.
Session tokens cached in ~/.smartskip_profile/api_session.json.

Cost model (unchanged): $0.15 per hit. Charged only on
/bulk-skip/payment-intent — submit + calculate + status + download are
all FREE.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi

# Reuse dataclasses + exceptions + CSV helpers from the legacy client so
# downstream code (probate_cascade.py, parse_result_csv, format_note)
# doesn't know or care which backend produced the batch.
from smartskip_client import (
    SmartSkipBatch,
    SmartSkipBatchFailed,
    SmartSkipMappingError,
    SmartSkipPaymentError,
    SmartSkipRow,
    SmartSkipSessionExpired,
    SmartSkipTimeoutError,
    write_submission_csv,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.smartskip.io"
SESSION_FILE = Path.home() / ".smartskip_profile" / "api_session.json"
POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 45 * 60  # 45 min

# Python 3.14 on macOS ships without a system-wide CA bundle — stdlib
# urllib fails with CERTIFICATE_VERIFY_FAILED unless we hand it one.
# certifi is already a transitive dep (requests uses it) so free to import.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def _resolve_creds() -> tuple[str, str]:
    email = os.environ.get("SMARTSKIP_EMAIL", "").strip()
    pw = os.environ.get("SMARTSKIP_PASSWORD", "").strip()
    if not email or not pw:
        raise SmartSkipSessionExpired(
            "SMARTSKIP_EMAIL and SMARTSKIP_PASSWORD must be set for REST API client"
        )
    return email, pw


def _http(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    raw_body: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 120,
) -> tuple[int, Any, str]:
    """Low-level HTTP. Returns (status_code, parsed_response, content_disposition)."""
    url = path if path.startswith("http") else API_BASE + path
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if body is not None:
        raw_body = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=raw_body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            data = resp.read()
            ct = resp.headers.get("content-type", "")
            cd = resp.headers.get("content-disposition", "")
            if "json" in ct:
                return resp.status, json.loads(data.decode("utf-8")), cd
            return resp.status, data, cd
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data.decode("utf-8")), ""
        except Exception:
            return e.code, data.decode("utf-8", errors="replace"), ""


def _norm(s: str) -> str:
    """Case + punctuation insensitive normalization for header matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class SmartSkipApiClient:
    """REST-based SmartSkip client. Drop-in replacement for SmartSkipClient.

    Accepts `headless` kwarg for signature compatibility (ignored — no browser).
    """

    def __init__(
        self,
        headless: bool = True,  # unused, accepted for compat
        download_dir: Path | None = None,
    ) -> None:
        self._tokens: dict = {}
        repo_root = Path(__file__).parent.parent
        self._download_dir = download_dir or (
            repo_root / "outbox" / "smartskip" / "results"
        )
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._submitted_dir = repo_root / "outbox" / "smartskip" / "submitted"
        self._submitted_dir.mkdir(parents=True, exist_ok=True)
        self._load_session()

    def __enter__(self) -> "SmartSkipApiClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass  # nothing to close for HTTP client

    # ─────────────────────────────────────────────────────────────────
    # Auth / session
    # ─────────────────────────────────────────────────────────────────

    def _load_session(self) -> None:
        if SESSION_FILE.exists():
            try:
                self._tokens = json.loads(SESSION_FILE.read_text())
            except Exception:
                self._tokens = {}

    def _save_session(self) -> None:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(self._tokens))

    def _signin(self) -> None:
        email, pw = _resolve_creds()
        logger.info("SmartSkip API: signin as %s", email)
        code, data, _ = _http(
            "POST", "/auth/signin", body={"email": email, "password": pw},
        )
        if code != 200 or not isinstance(data, dict) or not data.get("accessToken"):
            raise SmartSkipSessionExpired(
                f"SmartSkip signin failed ({code}): {data!r}"
            )
        self._tokens = {
            "accessToken": data["accessToken"],
            "refreshToken": data["refreshToken"],
        }
        self._save_session()

    def _refresh(self) -> bool:
        rt = self._tokens.get("refreshToken")
        if not rt:
            return False
        code, data, _ = _http("GET", "/auth/refresh", token=rt)
        if code == 200 and isinstance(data, dict) and data.get("accessToken"):
            self._tokens = {
                "accessToken": data["accessToken"],
                "refreshToken": data.get("refreshToken", rt),
            }
            self._save_session()
            logger.info("SmartSkip API: token refreshed")
            return True
        return False

    def _call(self, method: str, path: str, **kw) -> tuple[int, Any, str]:
        """Auth-aware HTTP call with refresh + re-signin fallback."""
        if not self._tokens.get("accessToken"):
            if not self._refresh():
                self._signin()
        code, data, cd = _http(
            method, path, token=self._tokens["accessToken"], **kw,
        )
        if code in (401, 403):
            if not self._refresh():
                self._signin()
            code, data, cd = _http(
                method, path, token=self._tokens["accessToken"], **kw,
            )
        return code, data, cd

    # ─────────────────────────────────────────────────────────────────
    # Public batch lifecycle
    # ─────────────────────────────────────────────────────────────────

    def submit_batch(
        self,
        rows: list[SmartSkipRow],
        batch_label: str = "Probate_Cascade",
    ) -> SmartSkipBatch:
        """Upload CSV + map fields + calculate cost. Does NOT charge.

        Returns SmartSkipBatch with bulkSkipId as batch_id.
        Caller must invoke wait_and_download() to charge + retrieve results.
        """
        # 1. Write CSV to disk (for archival + operator inspection)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        csv_filename = f"{batch_label}_{ts}.csv"
        csv_path = self._submitted_dir / csv_filename
        write_submission_csv(rows, csv_path)
        logger.info(
            "SmartSkip API: wrote %s (%d rows)", csv_path.name, len(rows),
        )

        # 2. Upload multipart CSV
        upload_resp = self._upload_csv(csv_path)
        bulk_skip_id = upload_resp.get("bulkSkipId") or upload_resp.get("_id")
        if not bulk_skip_id:
            raise SmartSkipMappingError(
                f"Upload response missing bulkSkipId: {upload_resp}"
            )
        parsed = upload_resp.get("parsed") or {}
        csv_headers = list(parsed.keys()) if isinstance(parsed, dict) else []
        logger.info("SmartSkip API: uploaded — bulkSkipId=%s, csv_headers=%s",
                    bulk_skip_id, csv_headers)

        # 3. Fetch field schema and auto-map columns
        code, fields_resp, _ = self._call("GET", "/bulk-skip/fields")
        if code != 200:
            raise SmartSkipMappingError(
                f"Fields fetch failed ({code}): {fields_resp}"
            )
        required = fields_resp.get("required", {}) or {}
        optional = fields_resp.get("optional", {}) or {}
        api_fields = {**required, **optional}
        schema = self._auto_map(csv_headers, api_fields)
        missing = [f for f in required if f not in schema]
        if missing:
            raise SmartSkipMappingError(
                f"Could not map required fields {missing} — CSV headers were {csv_headers}"
            )
        logger.info("SmartSkip API: mapped %d/%d fields (%d required all mapped)",
                    len(schema), len(api_fields), len(required))

        # 4. Submit mapping
        code, map_resp, _ = self._call(
            "POST", f"/bulk-skip/fields/{bulk_skip_id}", body={"schema": schema},
        )
        if code not in (200, 201):
            raise SmartSkipMappingError(f"Mapping submit failed ({code}): {map_resp}")

        # 5. Calculate cost (FREE preview)
        code, calc_resp, _ = self._call(
            "POST", f"/bulk-skip/calculate/{bulk_skip_id}",
        )
        if code not in (200, 201):
            raise SmartSkipMappingError(f"Calculate failed ({code}): {calc_resp}")

        billable = calc_resp.get("entities", len(rows))
        dupes = calc_resp.get("duplicates", 0)
        est_cost = billable * 0.15
        logger.info(
            "SmartSkip API: batch %s — billable=%d, duplicates=%d, est cost=$%.2f",
            bulk_skip_id, billable, dupes, est_cost,
        )

        return SmartSkipBatch(
            batch_id=bulk_skip_id,   # NOTE: this is a UUID, not a filename
            row_count=len(rows),
            submitted_at=datetime.now(timezone.utc).isoformat(),
            csv_path=csv_path,
            external_ids=[r.external_id for r in rows],
        )

    def wait_and_download(self, batch: SmartSkipBatch) -> Path:
        """Charge card, poll for completion, download results CSV.

        Downloads in `horizontal` (CRM) format — one row per subject with
        RELATIVE 1..14 column prefixes. Matches parse_result_csv() in
        smartskip_client.py.
        """
        bulk_skip_id = batch.batch_id
        self._authorize_payment(bulk_skip_id)

        # Poll until completion
        started = time.time()
        deadline = started + POLL_TIMEOUT_S
        last_status = None
        while time.time() < deadline:
            status = self._get_order_status(bulk_skip_id)
            if status != last_status:
                logger.info(
                    "SmartSkip API: batch %s status=%s (%.0fs elapsed)",
                    bulk_skip_id, status, time.time() - started,
                )
                last_status = status
            if status and status.lower() == "completed":
                break
            if status and status.lower() in ("error", "failed"):
                raise SmartSkipBatchFailed(
                    f"Batch {bulk_skip_id} ended in status {status}"
                )
            time.sleep(POLL_INTERVAL_S)
        else:
            raise SmartSkipTimeoutError(
                f"Batch {bulk_skip_id} did not complete within "
                f"{POLL_TIMEOUT_S // 60} min"
            )

        return self._download_results(bulk_skip_id, batch.csv_path.name)

    # ─────────────────────────────────────────────────────────────────
    # Internal steps
    # ─────────────────────────────────────────────────────────────────

    def _upload_csv(self, csv_path: Path) -> dict:
        """POST /bulk-skip/mapping with multipart CSV file."""
        fname = csv_path.name
        payload = csv_path.read_bytes()
        boundary = uuid.uuid4().hex
        buf = io.BytesIO()
        buf.write((
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode())
        buf.write(payload)
        buf.write((f"\r\n--{boundary}--\r\n").encode())
        code, data, _ = self._call(
            "POST", "/bulk-skip/mapping",
            raw_body=buf.getvalue(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if code not in (200, 201):
            raise SmartSkipMappingError(f"Upload failed ({code}): {data}")
        return data if isinstance(data, dict) else {}

    def _auto_map(self, csv_headers: list[str], api_fields: dict) -> dict[str, str]:
        """Match SmartSkip API field IDs → our CSV column names.

        SmartSkip's `api_fields` looks like {"firstName": "First Name", ...}
        Our CSV headers (from write_submission_csv) are the readable labels.
        We normalize both sides for tolerant matching.
        """
        by_norm = {_norm(h): h for h in csv_headers}
        schema: dict[str, str] = {}
        for api_field_id, api_label in api_fields.items():
            for cand in (_norm(api_label), _norm(api_field_id)):
                if cand in by_norm:
                    schema[api_field_id] = by_norm[cand]
                    break
        return schema

    def _authorize_payment(self, bulk_skip_id: str) -> None:
        """Fetch default saved card + POST /bulk-skip/payment-intent."""
        code, pms, _ = self._call("GET", "/payment/payment-method")
        if code != 200 or not pms:
            raise SmartSkipPaymentError(
                f"No saved payment method ({code}): {pms}"
            )
        # Prefer default card, fall back to first
        pm = next((p for p in pms if p.get("isDefault")), pms[0])
        pm_id = pm.get("id") or pm.get("_id")
        if not pm_id:
            raise SmartSkipPaymentError(f"Payment method has no id: {pm}")
        logger.info(
            "SmartSkip API: charging card %s ...%s (default=%s)",
            pm.get("brand", "?"), pm.get("last4", "????"),
            pm.get("isDefault", False),
        )

        code, data, _ = self._call(
            "POST", "/bulk-skip/payment-intent",
            body={"bulkSkipId": bulk_skip_id, "paymentMethodId": pm_id},
        )
        if code not in (200, 201):
            raise SmartSkipPaymentError(f"Payment failed ({code}): {data}")

        status = (data.get("status") or "").lower()
        if data.get("clientSecret"):
            raise SmartSkipPaymentError(
                f"Payment requires 3DS confirmation (clientSecret returned). "
                f"Complete manually at https://app.smartskip.io/bulk-skip, "
                f"then re-run with --recover-batch --id {bulk_skip_id}"
            )
        if status not in ("succeeded", "processing", "requires_capture"):
            raise SmartSkipPaymentError(
                f"Unexpected payment status {status!r}: {data}"
            )
        logger.info(
            "SmartSkip API: payment %s (paymentIntentId=%s)",
            status, data.get("paymentIntentId"),
        )

    def _get_order_status(self, bulk_skip_id: str) -> str | None:
        """List orders + return status of the target one."""
        code, data, _ = self._call(
            "GET", "/bulk-skip?sortField=createdAt&sortOrder=desc",
        )
        if code != 200 or not isinstance(data, dict):
            return None
        for item in data.get("items", []):
            if item.get("_id") == bulk_skip_id or item.get("bulkSkipId") == bulk_skip_id:
                return item.get("status")
        return None

    def _download_results(self, bulk_skip_id: str, orig_filename: str) -> Path:
        """GET /bulk-skip/download/{id}?type=horizontal (CRM/wide format)."""
        code, data, cd = self._call(
            "GET", f"/bulk-skip/download/{bulk_skip_id}?type=horizontal",
        )
        if code != 200:
            raise SmartSkipTimeoutError(
                f"Download failed ({code}): {data}"
            )
        blob = (
            data if isinstance(data, (bytes, bytearray))
            else json.dumps(data).encode("utf-8")
        )
        out_path = self._download_dir / f"result_{orig_filename}"
        out_path.write_bytes(blob)
        logger.info(
            "SmartSkip API: downloaded %s (%d bytes)",
            out_path.name, len(blob),
        )
        return out_path
