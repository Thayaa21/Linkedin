"""
sheets.py — Google Sheets read/write via gspread (service account auth).

Sheet columns (must match the Chrome extension output):
  A: Timestamp  B: Company  C: Role  D: Job URL  E: Status  F: Source

Status lifecycle managed by this agent:
  Applied  →  Pending Message  →  Message Sent  |  No Resume  |  Already Messaged
"""

import json as _json
import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, SHEET_TAB, GOOGLE_CREDS_FILE

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Column indices (0-based)
COL_TIMESTAMP   = 0
COL_COMPANY     = 1
COL_ROLE        = 2
COL_URL         = 3
COL_STATUS      = 4
COL_SOURCE      = 5
COL_LI_NAME     = 6   # added by agent when a match is found
COL_LI_URL      = 7   # LinkedIn profile URL of matched connection
COL_RESUME_LINK = 8   # Google Drive resume link (populated by poll)

STATUS_APPLIED          = "Applied"
STATUS_PENDING          = "Pending Message"
STATUS_SENT             = "Message Sent"
STATUS_NO_RESUME        = "No Resume"
STATUS_ALREADY_MESSAGED = "Already Messaged"


def _client():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _worksheet():
    return _client().open_by_key(SHEET_ID).worksheet(SHEET_TAB)


# ─── Read ─────────────────────────────────────────────────────────────────────

def get_applied_companies() -> list[dict]:
    """
    Returns all rows with Status == 'Applied'.
    Each dict has keys: row_index, company, role, url, timestamp, resume_link
    (row_index is 1-based, accounting for the header row)
    """
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):  # skip header, 1-based
        if len(row) < 5:
            continue
        status = row[COL_STATUS].strip()
        if status == STATUS_APPLIED:
            results.append({
                "row_index":   i,
                "company":     row[COL_COMPANY].strip(),
                "role":        row[COL_ROLE].strip(),
                "url":         row[COL_URL].strip(),
                "timestamp":   row[COL_TIMESTAMP].strip(),
                "resume_link": row[COL_RESUME_LINK].strip() if len(row) > COL_RESUME_LINK else "",
            })
    return results


def get_all_jobs() -> list[dict]:
    """
    Returns ALL rows that have a job URL — regardless of status.
    Used for multi-referral matching: even if we already sent to one person
    at Nokia, we still want to match and message a second Nokia connection.
    """
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        job_url = row[COL_URL].strip() if len(row) > COL_URL else ""
        if not job_url:
            continue  # rows added by add_pending_row have no job URL — skip
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "url":         job_url,
            "status":      row[COL_STATUS].strip(),
            "timestamp":   row[COL_TIMESTAMP].strip(),
            "source":      row[COL_SOURCE].strip() if len(row) > COL_SOURCE else "",
            "resume_link": row[COL_RESUME_LINK].strip() if len(row) > COL_RESUME_LINK else "",
        })
    return results


def get_tracked_li_urls() -> set[str]:
    """
    Returns the set of ALL LinkedIn profile URLs already in the sheet
    (any status: Pending, Sent, No Resume, etc.).

    Used to avoid creating duplicate pending entries for someone we're
    already planning to message — or have already messaged.
    """
    ws = _worksheet()
    records = ws.get_all_values()
    urls: set[str] = set()
    for row in records[1:]:
        if len(row) > COL_LI_URL:
            url = row[COL_LI_URL].strip()
            if url:
                urls.add(url)
    return urls


def add_pending_row(
    timestamp: str,
    company: str,
    role: str,
    job_url: str,
    source: str,
    li_name: str,
    li_url: str,
):
    """
    Appends a NEW 'Pending Message' row for an additional connection at a
    company we've already applied to.  The original application row is left
    unchanged — this is a second (or third…) referral request for the same job.
    """
    ws = _worksheet()
    ws.append_row([
        timestamp,    # A — reuse original application timestamp
        company,      # B
        role,         # C
        job_url,      # D — keep the job URL so resume lookup works
        STATUS_PENDING,  # E
        source,       # F
        li_name,      # G
        li_url,       # H
        "",           # I — resume_link filled later by poll
    ])


def get_pending_rows() -> list[dict]:
    """Returns rows with Status == 'Pending Message' (ready to send DM)."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 8:
            continue
        if row[COL_STATUS].strip() == STATUS_PENDING:
            results.append({
                "row_index":   i,
                "company":     row[COL_COMPANY].strip(),
                "role":        row[COL_ROLE].strip(),
                "li_name":     row[COL_LI_NAME].strip() if len(row) > COL_LI_NAME else "",
                "li_url":      row[COL_LI_URL].strip()  if len(row) > COL_LI_URL  else "",
                "resume_link": row[COL_RESUME_LINK].strip() if len(row) > COL_RESUME_LINK else "",
            })
    return results


# ─── Write ────────────────────────────────────────────────────────────────────

def store_resume_link(row_index: int, link: str):
    """Store the Drive resume link in column I for a given row."""
    ws = _worksheet()
    ws.update_cell(row_index, COL_RESUME_LINK + 1, link)


def mark_pending(row_index: int, li_name: str, li_url: str):
    """Connection accepted — mark row as Pending Message + store LinkedIn info."""
    ws = _worksheet()
    ws.update(f"E{row_index}:H{row_index}", [[
        STATUS_PENDING,
        "",          # F (source) — leave as-is
        li_name,     # G
        li_url,      # H
    ]])


def mark_sent(row_index: int):
    ws = _worksheet()
    ws.update_cell(row_index, COL_STATUS + 1, STATUS_SENT)


def mark_no_resume(row_index: int):
    ws = _worksheet()
    ws.update_cell(row_index, COL_STATUS + 1, STATUS_NO_RESUME)


def mark_already_messaged(row_index: int):
    ws = _worksheet()
    ws.update_cell(row_index, COL_STATUS + 1, STATUS_ALREADY_MESSAGED)


# ─── Snapshot persistence ─────────────────────────────────────────────────────
# Stored in a separate "Snapshot" tab so it survives GitHub Actions runs.
# Columns: A=profile_url  B=json_blob (name, headline, current_company)

SNAPSHOT_TAB = "Snapshot"


def load_snapshot_from_sheet() -> dict[str, dict]:
    """
    Load the connections snapshot from the Snapshot sheet tab.
    Returns {} if the tab doesn't exist yet (first run).
    """
    try:
        ws = _client().open_by_key(SHEET_ID).worksheet(SNAPSHOT_TAB)
        rows = ws.get_all_values()
        result = {}
        for row in rows:
            if not row or not row[0]:
                continue
            url = row[0].strip()
            try:
                data = _json.loads(row[1]) if len(row) > 1 and row[1] else {}
            except Exception:
                data = {}
            data.setdefault("url", url)
            result[url] = data
        return result
    except gspread.exceptions.WorksheetNotFound:
        return {}


def save_snapshot_to_sheet(connections: dict[str, dict]):
    """
    Persist the full connections snapshot to the Snapshot sheet tab.
    Creates the tab automatically on first run.
    """
    spreadsheet = _client().open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SNAPSHOT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SNAPSHOT_TAB, rows=5000, cols=2)

    ws.clear()
    if connections:
        rows = [
            [
                url,
                _json.dumps({
                    "name":            d.get("name", ""),
                    "headline":        d.get("headline", ""),
                    "current_company": d.get("current_company", ""),
                }),
            ]
            for url, d in connections.items()
        ]
        ws.update(f"A1:B{len(rows)}", rows)
