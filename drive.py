"""
drive.py — Fetches the shareable link for {Company}_Resume.pdf from Google Drive.

Resumes must be named exactly:  {Company}_Resume.pdf
e.g.  Stripe_Resume.pdf, Google_Resume.pdf, Acme Corp_Resume.pdf

The Drive folder is set via DRIVE_FOLDER_ID in .env.
Files must have "Anyone with the link can view" sharing already set —
this script does NOT change sharing permissions.
"""

import logging
from functools import lru_cache
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDS_FILE, DRIVE_FOLDER_ID

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


@lru_cache(maxsize=1)
def _drive_service():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def _normalize(name: str) -> str:
    """Strips extra whitespace and lowercases for comparison."""
    return " ".join(name.lower().split())


def get_resume_link(company: str) -> str | None:
    """
    Returns the webViewLink for {company}_Resume.pdf, or None if not found.
    Matching is case-insensitive and whitespace-tolerant.
    """
    service = _drive_service()
    # List all PDFs in the folder
    query = (
        f"'{DRIVE_FOLDER_ID}' in parents "
        f"and mimeType='application/pdf' "
        f"and trashed=false"
    )
    results = service.files().list(
        q=query,
        fields="files(id, name, webViewLink)",
        pageSize=200,
    ).execute()

    files = results.get("files", [])
    target = _normalize(f"{company}_resume")

    for f in files:
        stem = _normalize(f["name"].replace(".pdf", ""))
        if stem == target:
            link = f.get("webViewLink")
            logger.info("Found resume for %s: %s", company, link)
            return link

    logger.warning("No resume found for company: %s (searched %d files)", company, len(files))
    return None
