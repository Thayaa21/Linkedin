"""
sheets.py — Google Sheets read/write via gspread (service account auth).

Tracker layout:
  A: Applied Date  B: Company  C: Role  D: Job URL  E: Status  F: Outreach window
     ("Still working" / "No longer consider" vs MESSAGE_APPLY_WITHIN_DAYS)

Sent Messages layout (people we've messaged):
  A: Name  B: Company  C: LinkedIn ID  D: Job URL  E: Role  F: Status
"""

import re
from datetime import date, datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from config import SHEET_ID, SHEET_TAB, SENT_TAB, GOOGLE_CREDS_FILE, MESSAGE_APPLY_WITHIN_DAYS

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Tracker column indices (0-based)
COL_APPLIED_DATE = 0
COL_COMPANY     = 1
COL_ROLE        = 2
COL_URL         = 3
COL_STATUS      = 4
COL_OUTREACH    = 5

STATUS_APPLIED          = "Applied"
STATUS_PENDING          = "Pending Message"
STATUS_SENT             = "Message Sent"
STATUS_NO_RESUME        = "No Resume"
STATUS_ALREADY_MESSAGED = "Already Messaged"
STATUS_OUTSIDE_MESSAGE_WINDOW = "Outside Message Window"

# Tracker column F: derived from Applied Date + MESSAGE_APPLY_WITHIN_DAYS (updated every poll)
OUTREACH_STILL_WORKING = "Still working"
OUTREACH_NO_LONGER_CONSIDER = "No longer consider"

# Applied Date can appear 1 calendar day ahead of local today (sheet TZ, copy/paste, UTC storage).
OUTREACH_MAX_FUTURE_SLACK_DAYS = 1

TRACKER_HEADERS = [
    "Applied Date",
    "Company",
    "Role",
    "Job URL",
    "Status",
    "Outreach window",
]

# Sent sheet columns
SENT_COL_NAME    = 0
SENT_COL_COMPANY = 1
SENT_COL_LI_URL  = 2
SENT_COL_JOB_URL = 3
SENT_COL_ROLE    = 4
SENT_COL_STATUS = 5

SENT_HEADERS = ["Name", "Company", "LinkedIn ID", "Job URL", "Role", "Status"]


def _client():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _worksheet():
    return _client().open_by_key(SHEET_ID).worksheet(SHEET_TAB)


# Sheets / Excel serial days: day N is this many days after 1899-12-30 (45292 → 2024-01-01).
_SHEETS_SERIAL_ORIGIN = date(1899, 12, 30)


def _date_from_sheets_serial(n: float) -> date | None:
    """Convert numeric Google Sheets date cell (possibly .0 float) to calendar date."""
    try:
        whole = int(float(n))
    except (TypeError, ValueError):
        return None
    if whole < 0 or whole > 600_000:
        return None
    try:
        d = _SHEETS_SERIAL_ORIGIN + timedelta(days=whole)
    except OverflowError:
        return None
    if not (1970 <= d.year <= 2105):
        return None
    return d


def _parse_applied_date_to_date(raw) -> date | None:
    """
    Parse Tracker Applied Date as returned by gspread (formatted or serial).
    Handles ISO, M/D/Y, D/M/Y, a few month-name forms, and Sheets serial numbers.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return _date_from_sheets_serial(float(raw))
    s = str(raw).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return _date_from_sheets_serial(float(s))
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        part = s[:10]
        try:
            return datetime.strptime(part, "%Y-%m-%d").date()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00")[:19]).date()
        except ValueError:
            pass
    token = s.split()[0]
    for fmt in (
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%m/%d/%y",
        "%d/%m/%y",
        "%m-%d-%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def _to_applied_date(ts: str) -> str:
    """Normalize Applied Date cell to YYYY-MM-DD for storage/compare, or \"\" if unknown."""
    d = _parse_applied_date_to_date(ts)
    return d.isoformat() if d else ""


def _applied_date_in_outreach_window(applied_d: date, limit: int) -> bool:
    """True if applied_d is not older than limit days, allowing small future skew."""
    age = (date.today() - applied_d).days
    return -OUTREACH_MAX_FUTURE_SLACK_DAYS <= age <= limit


def outreach_window_label_for_applied_date(
    applied_raw: str, max_days: int | None = None,
) -> str:
    """Per Tracker row: still inside MESSAGE_APPLY_WITHIN_DAYS or not."""
    limit = MESSAGE_APPLY_WITHIN_DAYS if max_days is None else max_days
    if limit < 0:
        limit = 0
    applied_d = _parse_applied_date_to_date(applied_raw)
    if not applied_d:
        return OUTREACH_NO_LONGER_CONSIDER
    if _applied_date_in_outreach_window(applied_d, limit):
        return OUTREACH_STILL_WORKING
    return OUTREACH_NO_LONGER_CONSIDER


def refresh_tracker_outreach_column():
    """
    Write Tracker column F from each row's Applied Date (MESSAGE_APPLY_WITHIN_DAYS).
    Call on every poll so the sheet stays correct as days pass.
    """
    ws = _worksheet()
    rows = ws.get_all_values()
    if not rows:
        return
    h0 = list(rows[0])
    hdr = (h0 + [""] * 5)[:5] + [TRACKER_HEADERS[COL_OUTREACH]]
    out: list[list[str]] = [hdr]
    for row in rows[1:]:
        r = (list(row) + [""] * 5)[:5]
        label = outreach_window_label_for_applied_date(
            r[COL_APPLIED_DATE] if r else "",
        )
        out.append(r + [label])
    ws.update(f"A1:F{len(out)}", out)


def ensure_sent_sheet_exists():
    """Create Sent Messages sheet with header row (title) if it doesn't exist."""
    spreadsheet = _client().open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SENT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SENT_TAB, rows=5000, cols=6)
    rows = ws.get_all_values()
    if not rows:
        ws.update("A1:F1", [SENT_HEADERS])
    elif rows[0][0].strip() != "Name":
        # First row is not our header — insert header row at top
        ws.insert_row(SENT_HEADERS, 1)


def _sent_worksheet():
    return _client().open_by_key(SHEET_ID).worksheet(SENT_TAB)


def refine_tracker_sheet():
    """
    Refines the tracker: timestamp→date, removes Source/Name/LI/Resume, 6 cols (incl. Outreach window).
    Migrates any Pending/Sent rows with person data to Sent sheet first.
    """
    ws = _worksheet()
    rows = ws.get_all_values()
    if len(rows) < 2:
        return

    # Migrate person data to Sent sheet before simplifying
    ensure_sent_sheet_exists()
    sent_ws = _sent_worksheet()
    sent_rows = sent_ws.get_all_values()
    sent_li_urls = {r[SENT_COL_LI_URL].strip() for r in sent_rows[1:] if len(r) > SENT_COL_LI_URL and r[SENT_COL_LI_URL].strip()}

    def _person_col(row: list, name_idx: int, li_idx: int) -> tuple[str, str]:
        """Return (li_name, li_url) from row. Legacy: Name=6, LI=7. New 8-col: Name=5, LI=6."""
        n = row[name_idx].strip() if len(row) > name_idx else ""
        u = row[li_idx].strip() if len(row) > li_idx else ""
        return n, u

    new_rows = [TRACKER_HEADERS]
    for row in rows[1:]:
        if len(row) < 5:
            continue
        is_legacy = len(row) >= 9
        applied_date = _to_applied_date(row[0]) if row else ""
        company = row[1] if len(row) > 1 else ""
        role = row[2] if len(row) > 2 else ""
        job_url = row[3] if len(row) > 3 else ""
        status = row[4] if len(row) > 4 else ""

        # Migrate to Sent if has person data (legacy: Name=6 LI=7, 8-col: Name=5 LI=6)
        name_idx, li_idx = (6, 7) if is_legacy else (5, 6)
        li_name, li_url = _person_col(row, name_idx, li_idx)
        if status in (STATUS_PENDING, STATUS_SENT) and li_url and li_url not in sent_li_urls:
            sent_ws.append_row([
                li_name, company, li_url, job_url, role,
                STATUS_SENT if status == STATUS_SENT else STATUS_PENDING,
            ])
            sent_li_urls.add(li_url)

        outreach = outreach_window_label_for_applied_date(applied_date)
        new_rows.append([applied_date, company, role, job_url, status, outreach])

    ws.clear()
    ws.update(f"A1:F{len(new_rows)}", new_rows)


# ─── Read ─────────────────────────────────────────────────────────────────────

def get_applied_companies() -> list[dict]:
    """Returns tracker rows with Status == 'Applied'."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        if row[COL_STATUS].strip() != STATUS_APPLIED:
            continue
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "url":         row[COL_URL].strip() if len(row) > COL_URL else "",
            "timestamp":   _to_applied_date(row[COL_APPLIED_DATE].strip() if len(row) > COL_APPLIED_DATE else ""),
        })
    return results


def get_all_jobs() -> list[dict]:
    """Returns ALL tracker rows that have a job URL (for multi-referral matching)."""
    ws = _worksheet()
    records = ws.get_all_values()
    results = []
    for i, row in enumerate(records[1:], start=2):
        if len(row) < 5:
            continue
        job_url = row[COL_URL].strip() if len(row) > COL_URL else ""
        if not job_url:
            continue
        results.append({
            "row_index":   i,
            "company":     row[COL_COMPANY].strip(),
            "role":        row[COL_ROLE].strip(),
            "url":         job_url,
            "status":      row[COL_STATUS].strip(),
            "timestamp":   _to_applied_date(row[COL_APPLIED_DATE].strip() if len(row) > COL_APPLIED_DATE else ""),
        })
    return results


def normalize_li_url(url: str) -> str:
    """Normalize LinkedIn URL for comparison (handles www, trailing slash, case, query params)."""
    u = (url or "").strip().lower().rstrip("/")
    # Extract profile ID for canonical matching (www vs non-www, different domains)
    if "linkedin.com/in/" in u:
        parts = u.split("linkedin.com/in/", 1)
        profile = parts[-1].split("?")[0].rstrip("/")
        u = f"linkedin.com/in/{profile}"
    return u


def normalize_job_url(url: str) -> str:
    u = (url or "").strip().lower().rstrip("/")
    if "?" in u:
        u = u.split("?", 1)[0]
    return u


def get_applied_date_for_application(company: str, job_url: str) -> str | None:
    """
    Tracker Applied Date (YYYY-MM-DD) for this company/position.
    Prefers a row whose Job URL matches job_url; otherwise the latest date among
    tracker rows for that company (when Sent row has no job URL).
    """
    if not company or not company.strip():
        return None
    ws = _worksheet()
    records = ws.get_all_values()
    want_job = normalize_job_url(job_url)
    company_l = company.strip().lower()
    same_company: list[tuple[str, str]] = []  # (norm_job_url, applied_date)
    for row in records[1:]:
        if len(row) <= COL_COMPANY:
            continue
        row_co = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""
        if row_co.lower() != company_l:
            continue
        ts = _to_applied_date(row[COL_APPLIED_DATE].strip() if len(row) > COL_APPLIED_DATE else "")
        row_url = row[COL_URL].strip() if len(row) > COL_URL else ""
        same_company.append((normalize_job_url(row_url), ts))

    if not same_company:
        return None

    if want_job:
        for norm_u, ts in same_company:
            if norm_u == want_job and ts:
                return ts
        return None

    dated = [ts for _, ts in same_company if ts]
    if not dated:
        return None
    return max(dated)


def application_is_within_messaging_window(
    company: str, job_url: str, max_days: int | None = None,
) -> bool:
    """True if Tracker applied date is within the outreach window (incl. small future-date slack)."""
    limit = MESSAGE_APPLY_WITHIN_DAYS if max_days is None else max_days
    if limit < 0:
        limit = 0
    applied = get_applied_date_for_application(company, job_url)
    if not applied:
        return False
    try:
        applied_d = datetime.strptime(applied[:10], "%Y-%m-%d").date()
    except ValueError:
        applied_d = _parse_applied_date_to_date(applied)
        if not applied_d:
            return False
    return _applied_date_in_outreach_window(applied_d, limit)


def get_tracked_li_urls() -> set[str]:
    """Returns all LinkedIn URLs in Sent sheet (Pending + Sent)."""
    try:
        ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return set()
    records = ws.get_all_values()
    urls = set()
    for row in records[1:]:
        if len(row) > SENT_COL_LI_URL and row[SENT_COL_LI_URL].strip():
            urls.add(normalize_li_url(row[SENT_COL_LI_URL]))
    return urls


def get_sent_li_urls() -> set[str]:
    """Returns LinkedIn URLs we've ALREADY sent a message to. Never send to these again."""
    try:
        ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return set()
    records = ws.get_all_values()
    urls = set()
    for row in records[1:]:
        if len(row) > SENT_COL_STATUS and row[SENT_COL_STATUS].strip() == STATUS_SENT:
            if len(row) > SENT_COL_LI_URL and row[SENT_COL_LI_URL].strip():
                urls.add(normalize_li_url(row[SENT_COL_LI_URL]))
    return urls


def add_pending_to_sent_sheet(
    li_name: str,
    li_url: str,
    company: str,
    role: str,
    job_url: str,
):
    """Add a row to Sent sheet with Status=Pending (when we match a connection)."""
    if not li_url or not li_url.strip():
        return
    if not application_is_within_messaging_window(company, job_url):
        return
    li_norm = normalize_li_url(li_url)
    # Never add if we've already contacted this person (any status — prevents duplicate messages)
    if li_norm in get_tracked_li_urls():
        return
    ensure_sent_sheet_exists()
    ws = _sent_worksheet()
    rows = ws.get_all_values()
    if not rows:
        ws.update("A1:F1", [SENT_HEADERS])
        rows = [SENT_HEADERS]
    for row in rows[1:]:
        if len(row) > SENT_COL_LI_URL and row[SENT_COL_LI_URL].strip():
            if normalize_li_url(row[SENT_COL_LI_URL]) == li_norm and (row[SENT_COL_COMPANY].strip() if len(row) > SENT_COL_COMPANY else "") == company.strip():
                return  # already recorded (same person + company)
    ws.append_row([li_name or "", company or "", li_url or "", job_url or "", role or "", STATUS_PENDING])


def get_pending_rows(include_no_resume: bool = True) -> list[dict]:
    """Returns rows from Sent sheet with Status == 'Pending Message' (and 'No Resume' if include_no_resume)."""
    try:
        ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return []
    records = ws.get_all_values()
    results = []
    statuses = (STATUS_PENDING, STATUS_NO_RESUME) if include_no_resume else (STATUS_PENDING,)
    for i, row in enumerate(records[1:], start=2):
        if len(row) <= SENT_COL_STATUS:
            continue
        if row[SENT_COL_STATUS].strip() not in statuses:
            continue
        results.append({
            "row_index":   i,
            "company":     row[SENT_COL_COMPANY].strip() if len(row) > SENT_COL_COMPANY else "",
            "role":        row[SENT_COL_ROLE].strip() if len(row) > SENT_COL_ROLE else "",
            "li_name":     row[SENT_COL_NAME].strip() if len(row) > SENT_COL_NAME else "",
            "li_url":      row[SENT_COL_LI_URL].strip() if len(row) > SENT_COL_LI_URL else "",
            "job_url":     row[SENT_COL_JOB_URL].strip() if len(row) > SENT_COL_JOB_URL else "",
        })
    return results


def mark_sent_in_sent_sheet(row_index: int):
    """Update Sent sheet row to Status=Message Sent."""
    ws = _sent_worksheet()
    ws.update_cell(row_index, SENT_COL_STATUS + 1, STATUS_SENT)


def update_tracker_status_for_company(company: str, new_status: str):
    """Update Tracker row(s) for this company to new_status (e.g. Message Sent)."""
    if not company or not company.strip():
        return
    try:
        ws = _worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return
    records = ws.get_all_values()
    for i, row in enumerate(records[1:], start=2):
        if len(row) <= COL_STATUS:
            continue
        row_company = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""
        if row_company.lower() == company.strip().lower():
            ws.update_cell(i, COL_STATUS + 1, new_status)


def mark_no_resume_in_sent_sheet(row_index: int):
    """Update Sent sheet row to Status=No Resume (skip sending)."""
    ws = _sent_worksheet()
    ws.update_cell(row_index, SENT_COL_STATUS + 1, STATUS_NO_RESUME)


def mark_outside_message_window_in_sent_sheet(row_index: int):
    """Sent sheet row is not messaged because application is outside the configured day window."""
    ws = _sent_worksheet()
    ws.update_cell(row_index, SENT_COL_STATUS + 1, STATUS_OUTSIDE_MESSAGE_WINDOW)


def mark_person_as_sent(li_url: str | None = None, name: str | None = None) -> bool:
    """
    Mark a person as Message Sent by LinkedIn URL or name (for fixing wrong status).
    Returns True if updated, False if not found.
    """
    if not li_url and not name:
        return False
    try:
        ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return False
    records = ws.get_all_values()
    name_lower = name.lower().strip() if name else ""
    li_url_norm = li_url.strip() if li_url else ""
    for i, row in enumerate(records[1:], start=2):
        if len(row) <= SENT_COL_STATUS:
            continue
        if row[SENT_COL_STATUS].strip() == STATUS_SENT:
            continue
        match_url = li_url_norm and len(row) > SENT_COL_LI_URL and row[SENT_COL_LI_URL].strip() and li_url_norm in row[SENT_COL_LI_URL].strip()
        match_name = name_lower and len(row) > SENT_COL_NAME and row[SENT_COL_NAME].strip() and name_lower in row[SENT_COL_NAME].strip().lower()
        if match_url or match_name:
            ws.update_cell(i, SENT_COL_STATUS + 1, STATUS_SENT)
            return True
    return False


def deduplicate_sent_sheet() -> int:
    """
    Remove duplicate rows: same (li_url, company). Keep Message Sent over Pending.
    Returns number of duplicate rows removed.
    """
    try:
        ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return 0
    rows = ws.get_all_values()
    if len(rows) < 2:
        return 0
    header = rows[0]
    seen: dict[tuple[str, str], tuple[int, list]] = {}  # (li_norm, company) -> (row_index, row)
    to_delete: list[int] = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) <= SENT_COL_LI_URL:
            continue
        li_url = row[SENT_COL_LI_URL].strip() if len(row) > SENT_COL_LI_URL else ""
        company = row[SENT_COL_COMPANY].strip() if len(row) > SENT_COL_COMPANY else ""
        status = row[SENT_COL_STATUS].strip() if len(row) > SENT_COL_STATUS else ""
        if not li_url:
            continue
        key = (normalize_li_url(li_url), company)
        if key in seen:
            prev_idx, prev_row = seen[key]
            prev_status = prev_row[SENT_COL_STATUS].strip() if len(prev_row) > SENT_COL_STATUS else ""
            # Keep Message Sent; delete the other. If both same, keep first, delete this.
            if status == STATUS_SENT and prev_status != STATUS_SENT:
                to_delete.append(prev_idx)
                seen[key] = (i, row)
            else:
                to_delete.append(i)
        else:
            seen[key] = (i, row)
    for row_idx in sorted(to_delete, reverse=True):
        ws.delete_rows(row_idx)
    return len(to_delete)


def sync_tracker_from_sent():
    """
    Update Tracker: for each company in Sent sheet with Message Sent, set Tracker row(s) to Message Sent.
    Fixes stale Tracker when messages were sent but Tracker wasn't updated.
    """
    try:
        sent_ws = _sent_worksheet()
        tracker_ws = _worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return 0
    sent_rows = sent_ws.get_all_values()
    if len(sent_rows) < 2:
        return 0
    companies_sent = set()
    for row in sent_rows[1:]:
        if len(row) > SENT_COL_STATUS and row[SENT_COL_STATUS].strip() == STATUS_SENT:
            company = row[SENT_COL_COMPANY].strip() if len(row) > SENT_COL_COMPANY else ""
            if company:
                companies_sent.add(company)
    updated = 0
    tracker_rows = tracker_ws.get_all_values()
    for i, row in enumerate(tracker_rows[1:], start=2):
        if len(row) <= COL_STATUS:
            continue
        row_company = row[COL_COMPANY].strip() if len(row) > COL_COMPANY else ""
        row_status = row[COL_STATUS].strip()
        if row_company and row_status in (STATUS_APPLIED, STATUS_PENDING):
            if any(row_company.lower() == c.lower() for c in companies_sent):
                tracker_ws.update_cell(i, COL_STATUS + 1, STATUS_SENT)
                updated += 1
    return updated


def sync_sent_from_tracker():
    """
    When Tracker has Message Sent for a company but Sent sheet has Pending for that company,
    and there's exactly one Pending row for that company — update it to Message Sent.
    Handles migration mismatch (sent before refactor).
    """
    try:
        tracker_ws = _worksheet()
        sent_ws = _sent_worksheet()
    except gspread.exceptions.WorksheetNotFound:
        return 0
    tracker_rows = tracker_ws.get_all_values()
    sent_rows = sent_ws.get_all_values()
    if len(tracker_rows) < 2 or len(sent_rows) < 2:
        return 0
    sent_companies = {r[COL_COMPANY].strip() for i, r in enumerate(tracker_rows[1:]) if len(r) > COL_STATUS and r[COL_STATUS].strip() == STATUS_SENT}
    updated = 0
    for company in sent_companies:
        pending_for_company = [
            (i, r) for i, r in enumerate(sent_rows[1:], start=2)
            if len(r) > SENT_COL_STATUS
            and (r[SENT_COL_COMPANY].strip() if len(r) > SENT_COL_COMPANY else "") == company
            and r[SENT_COL_STATUS].strip() == STATUS_PENDING
        ]
        if len(pending_for_company) == 1:
            row_idx, _ = pending_for_company[0]
            sent_ws.update_cell(row_idx, SENT_COL_STATUS + 1, STATUS_SENT)
            updated += 1
    return updated


# ─── Snapshot ─────────────────────────────────────────────────────────────────
# Columns: A=Profile URL  B=Name  C=Headline  D=Company

SNAPSHOT_TAB = "Snapshot"
SNAPSHOT_HEADERS = ["Profile URL", "Name", "Headline", "Company"]


def load_snapshot_from_sheet() -> dict[str, dict]:
    """Load connections snapshot. Supports both old (JSON) and new (columns) format."""
    try:
        ws = _client().open_by_key(SHEET_ID).worksheet(SNAPSHOT_TAB)
        rows = ws.get_all_values()
        result = {}
        for row in rows:
            if not row or not row[0].strip():
                continue
            url = row[0].strip()
            if len(row) >= 4:
                result[url] = {
                    "url": url,
                    "name": row[1].strip() if len(row) > 1 else "",
                    "headline": row[2].strip() if len(row) > 2 else "",
                    "current_company": row[3].strip() if len(row) > 3 else "",
                }
            else:
                import json as _json
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
    """Persist snapshot with clean columns: URL, Name, Headline, Company."""
    spreadsheet = _client().open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SNAPSHOT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SNAPSHOT_TAB, rows=5000, cols=4)

    ws.clear()
    if connections:
        rows = [[SNAPSHOT_HEADERS[0], SNAPSHOT_HEADERS[1], SNAPSHOT_HEADERS[2], SNAPSHOT_HEADERS[3]]]
        for url, d in connections.items():
            rows.append([
                url,
                d.get("name", ""),
                d.get("headline", ""),
                d.get("current_company", ""),
            ])
        ws.update(f"A1:D{len(rows)}", rows)
