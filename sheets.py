"""
sheets.py — Google Sheets read/write via gspread (service account auth).

Sheet columns (must match the Chrome extension output):
  A: Timestamp  B: Company  C: Role  D: Job URL  E: Status  F: Source

Status lifecycle managed by this agent:
  Applied  →  Pending Message  →  Message Sent  |  No Resume  |  Already Messaged
"""

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
