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
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode
from dropbox.sharing import RequestedVisibility, SharedLinkSettings

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


def ensure_shared_link(
    dbx: dropbox.Dropbox, dropbox_path: str
) -> str | None:
    """Return a public shared-link URL for a Dropbox path.

    Creates a new public link if none exists; returns the existing one
    otherwise (Dropbox's create-link call errors with
    ``shared_link_already_exists`` on re-runs, and we then fall through
    to the list-links call to fetch the current URL).

    Returns None if the link couldn't be created (missing perms, path
    not found, etc.) — the caller is expected to render the link
    optionally so a failure here doesn't break the Slack post.
    """
    try:
        result = dbx.sharing_create_shared_link_with_settings(
            dropbox_path,
            settings=SharedLinkSettings(
                requested_visibility=RequestedVisibility.public
            ),
        )
        return result.url
    except ApiError as e:
        if "shared_link_already_exists" in str(e):
            try:
                links = dbx.sharing_list_shared_links(path=dropbox_path).links
                if links:
                    return links[0].url
            except ApiError as inner:
                logger.debug(
                    "list_shared_links failed for %s: %s", dropbox_path, inner
                )
        else:
            logger.debug(
                "create_shared_link failed for %s: %s", dropbox_path, e
            )
    except Exception as e:
        logger.debug("ensure_shared_link unexpected error on %s: %s", dropbox_path, e)
    return None


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
      ``{"path": Path, "dropbox_path": str | None, "shared_link": str | None,
         "success": bool, "error": str | None}``

    ``shared_link`` is a public Dropbox URL (rlkey=... share link) for
    each successfully-uploaded file. Rendered in the daily Slack post so
    the operator can jump straight to any file without opening Dropbox
    first. None when creation fails — the upload itself still counts as
    a success.

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
                "shared_link": None,
                "success": False,
                "error": str(e),
            })
        return results

    for p in local_paths:
        try:
            dest = upload_file(p, dbx=dbx)
            link = ensure_shared_link(dbx, dest)
            results.append({
                "path": p,
                "dropbox_path": dest,
                "shared_link": link,
                "success": True,
                "error": None,
            })
        except Exception as e:
            logger.warning("Dropbox upload failed for %s: %s", p.name, e)
            results.append({
                "path": p,
                "dropbox_path": None,
                "shared_link": None,
                "success": False,
                "error": str(e),
            })
    return results


def get_archive_folder_link(dbx: dropbox.Dropbox | None = None) -> str | None:
    """Return the shared-link URL for the Dropbox archive root folder.

    Called after ``upload_files()`` so the Slack post can render one
    "browse all archives" link alongside the per-file links.
    """
    if dbx is None:
        try:
            dbx = _get_client()
        except Exception as e:
            logger.debug("get_archive_folder_link: no client — %s", e)
            return None
    return ensure_shared_link(dbx, _resolve_archive_root())
