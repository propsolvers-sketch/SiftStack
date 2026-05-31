"""Dropbox upload + shareable-link helper for deal reports.

Reuses the existing DROPBOX_REFRESH_TOKEN / DROPBOX_APP_KEY / DROPBOX_APP_SECRET
configured for the courthouse photo pipeline. Files land under a separate
sub-folder so they don't mix with photo-import workflows.
"""

import logging
import os
from pathlib import Path

import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode
from dropbox.sharing import (
    CreateSharedLinkWithSettingsError,
    RequestedVisibility,
    SharedLinkSettings,
)

import config

logger = logging.getLogger(__name__)

# Reports land here in your Dropbox. Independent of DROPBOX_ROOT_FOLDER (photos).
REPORTS_ROOT = "/SiftStack Deal Reports"


def _get_client() -> dropbox.Dropbox:
    """Build a Dropbox client from the existing OAuth2 refresh token."""
    if not config.DROPBOX_REFRESH_TOKEN:
        raise RuntimeError("DROPBOX_REFRESH_TOKEN not set in .env")
    if not config.DROPBOX_APP_KEY:
        raise RuntimeError("DROPBOX_APP_KEY not set in .env")
    return dropbox.Dropbox(
        oauth2_refresh_token=config.DROPBOX_REFRESH_TOKEN,
        app_key=config.DROPBOX_APP_KEY,
        app_secret=config.DROPBOX_APP_SECRET or None,
    )


def upload_and_share(local_path: str, subfolder: str = "") -> str | None:
    """Upload a local file to Dropbox and return a shareable URL.

    Args:
        local_path: Local file to upload.
        subfolder: Optional sub-folder under REPORTS_ROOT (e.g. "2026-05" for
                   monthly grouping). Empty = files land directly under REPORTS_ROOT.

    Returns:
        Shareable URL string, or None on failure (logged).
    """
    if not os.path.exists(local_path):
        logger.error("File not found: %s", local_path)
        return None

    try:
        dbx = _get_client()
    except RuntimeError as e:
        logger.error("Dropbox client init failed: %s", e)
        return None

    # Build Dropbox destination path
    filename = Path(local_path).name
    if subfolder:
        dropbox_path = f"{REPORTS_ROOT}/{subfolder.strip('/')}/{filename}"
    else:
        dropbox_path = f"{REPORTS_ROOT}/{filename}"

    # Upload (overwrite if exists)
    try:
        with open(local_path, "rb") as f:
            dbx.files_upload(f.read(), dropbox_path, mode=WriteMode.overwrite)
        logger.info("Uploaded to Dropbox: %s", dropbox_path)
    except ApiError as e:
        logger.error("Dropbox upload failed: %s", e)
        return None

    # Create or retrieve shareable link
    try:
        link = dbx.sharing_create_shared_link_with_settings(
            dropbox_path,
            settings=SharedLinkSettings(requested_visibility=RequestedVisibility.public),
        )
        return link.url
    except ApiError as e:
        # If link already exists, retrieve it instead
        if "shared_link_already_exists" in str(e):
            try:
                links = dbx.sharing_list_shared_links(path=dropbox_path).links
                if links:
                    return links[0].url
            except ApiError as e2:
                logger.error("Could not retrieve existing share link: %s", e2)
        else:
            logger.error("Share link creation failed: %s", e)
        return None
