"""Dropbox archive uploader for daily-sweep outputs.

Uploads ``datasift_archive_<ts>.csv`` (and any per-distressor upload CSVs
the operator wants to retain) to a SiftStack/Archives/ folder in Dropbox
after each daily run. Dropbox's desktop client syncs them to the
operator's Mac automatically so the audit trail is always one finder
window away.

Configuration:
  * ``DROPBOX_APP_KEY`` + ``DROPBOX_APP_SECRET`` + ``DROPBOX_REFRESH_TOKEN``
    in .env (same trio the existing dropbox_watcher uses).
  * ``DROPBOX_ARCHIVE_FOLDER`` env var (optional) — Dropbox path to upload
    into. Defaults to ``/SiftStack/Archives``.

Idempotency: if a file with the same name already exists at the target
path, Dropbox returns a 409 ``path/conflict/file`` error. We pass
``mode=overwrite`` so re-runs of the same day's finalize step don't fail
or create ``filename (1).csv`` duplicates.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import dropbox
from dropbox.files import WriteMode

import config


logger = logging.getLogger(__name__)


def _get_client() -> dropbox.Dropbox:
    """Authenticated Dropbox client. Re-uses the dropbox_watcher pattern."""
    if not config.DROPBOX_REFRESH_TOKEN:
        raise ValueError("DROPBOX_REFRESH_TOKEN not set in .env")
    if not config.DROPBOX_APP_KEY:
        raise ValueError("DROPBOX_APP_KEY not set in .env")
    return dropbox.Dropbox(
        oauth2_refresh_token=config.DROPBOX_REFRESH_TOKEN,
        app_key=config.DROPBOX_APP_KEY,
        app_secret=config.DROPBOX_APP_SECRET or None,
    )


def _resolve_archive_root() -> str:
    """Dropbox folder path for archive uploads. Trailing slash stripped.

    Defaults to /SiftStack/Archives. Override via DROPBOX_ARCHIVE_FOLDER
    env var if the operator wants a different folder layout."""
    raw = os.environ.get("DROPBOX_ARCHIVE_FOLDER", "/SiftStack/Archives")
    return raw.rstrip("/") or "/SiftStack/Archives"


def upload_file(local_path: Path, dbx: dropbox.Dropbox | None = None) -> str:
    """Upload one file to Dropbox and return the destination path.

    Args:
        local_path: File to upload. Filename preserved verbatim at the
            destination — caller controls naming via the file name.
        dbx: Optional pre-built client (lets a batch upload re-use one
            authenticated session). New client created if None.

    Returns:
        The Dropbox path the file landed at (e.g.
        ``/SiftStack/Archives/datasift_archive_2026-06-12_032030.csv``).

    Raises:
        ValueError if Dropbox credentials are missing.
        dropbox.exceptions.ApiError on upload failure.
    """
    if dbx is None:
        dbx = _get_client()

    dest = f"{_resolve_archive_root()}/{local_path.name}"
    with open(local_path, "rb") as f:
        # mode=overwrite makes the call idempotent — re-running finalize
        # for the same run replaces the file instead of erroring or
        # creating "(1)" suffixed duplicates.
        dbx.files_upload(
            f.read(),
            dest,
            mode=WriteMode("overwrite"),
            mute=True,  # don't email operator on each upload
        )
    logger.info("Uploaded to Dropbox: %s → %s", local_path.name, dest)
    return dest


def upload_files(local_paths: list[Path]) -> list[dict]:
    """Upload multiple files in one authenticated session.

    Returns one result dict per input path:
      ``{"path": Path, "dropbox_path": str | None, "success": bool,
         "error": str | None}``

    Failures are caught + reported per file — one bad upload doesn't
    abort the rest. Caller (daily_finalize) decides whether to mark the
    overall run as failed based on the success count.
    """
    if not local_paths:
        return []

    results: list[dict] = []
    try:
        dbx = _get_client()
    except Exception as e:
        # Missing creds → mark every file as failed but don't raise. The
        # daily-sweep step shouldn't go from green to red just because
        # Dropbox isn't configured.
        logger.warning("Dropbox archive upload skipped: %s", e)
        for p in local_paths:
            results.append({
                "path": p,
                "dropbox_path": None,
                "success": False,
                "error": str(e),
            })
        return results

    for p in local_paths:
        try:
            dest = upload_file(p, dbx=dbx)
            results.append({
                "path": p,
                "dropbox_path": dest,
                "success": True,
                "error": None,
            })
        except Exception as e:
            logger.warning("Dropbox upload failed for %s: %s", p.name, e)
            results.append({
                "path": p,
                "dropbox_path": None,
                "success": False,
                "error": str(e),
            })
    return results
