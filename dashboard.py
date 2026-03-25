"""
dashboard.py — Streamlit monitoring UI for the LinkedIn agent.

Run locally:
    streamlit run dashboard.py

Shows:
  - Tracker: Applied Date, Company, Role, Job URL, Status, Outreach window
  - Sent sheet: Name, Company, LinkedIn ID, Job URL (people we've messaged)
"""

import os
import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Google Sheets ──────────────────────────────────────────────────────────────
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID        = os.environ["SHEET_ID"]
SHEET_TAB       = os.environ.get("SHEET_TAB", "Applications")
SENT_TAB        = os.environ.get("SENT_TAB", "Sent Messages")
GOOGLE_CREDS    = os.environ.get("GOOGLE_CREDS_FILE", "google_creds.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

STATUS_COLORS = {
    "Applied":          "#4A90D9",
    "Pending Message":  "#F5A623",
    "Message Sent":     "#7ED321",
    "No Resume":        "#D0021B",
    "Outside Message Window": "#B8B8B8",
    "Still working":    "#7ED321",
    "No longer consider": "#9B9B9B",
    "Already Messaged": "#9B9B9B",
}

# ── GitHub Actions dispatch ────────────────────────────────────────────────────
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO  = os.environ.get("GH_REPO", "")


@st.cache_data(ttl=30)
def load_tracker() -> pd.DataFrame:
    """Load Tracker sheet (incl. Outreach window)."""
    creds = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    try:
        ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return pd.DataFrame()
    rows = ws.get_all_values()
    if len(rows) < 2:
        return pd.DataFrame()
    headers = [
        "Applied Date",
        "Company",
        "Role",
        "Job URL",
        "Status",
        "Outreach window",
    ]
    data = []
    for row in rows[1:]:
        padded = (row + [""] * 6)[:6]
        data.append(padded)
    return pd.DataFrame(data, columns=headers)


@st.cache_data(ttl=30)
def load_sent() -> pd.DataFrame:
    """Load Sent Messages sheet (Name, Company, LinkedIn ID, Job URL, Role, Status)."""
    creds = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    try:
        ws = gc.open_by_key(SHEET_ID).worksheet(SENT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return pd.DataFrame()
    rows = ws.get_all_values()
    if len(rows) < 2:
        return pd.DataFrame()
    headers = ["Name", "Company", "LinkedIn ID", "Job URL", "Role", "Status"]
    data = []
    for row in rows[1:]:
        padded = (row + [""] * 6)[:6]
        data.append(padded)
    return pd.DataFrame(data, columns=headers)


def trigger_workflow(workflow_file: str) -> tuple[bool, str]:
    if not GH_TOKEN or not GH_REPO:
        return False, "GH_TOKEN or GH_REPO not set in .env"
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_file}/dispatches"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
        json={"ref": "main"},
        timeout=10,
    )
    if resp.status_code == 204:
        return True, ""
    return False, f"GitHub API returned {resp.status_code}: {resp.text}"


# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="LinkedIn Agent Monitor", page_icon="🤖", layout="wide")
st.title("🤖 LinkedIn Agent Monitor")

df_tracker = load_tracker()
df_sent = load_sent()

if df_tracker.empty and df_sent.empty:
    st.warning("No data found in the sheets.")
    st.stop()

# ── Stats row ──────────────────────────────────────────────────────────────────
# Applied: Tracker only. Pending/Sent: Sent sheet (people); fallback to Tracker during migration
tracker_counts = df_tracker["Status"].value_counts().to_dict() if not df_tracker.empty else {}
sent_counts = df_sent["Status"].value_counts().to_dict() if not df_sent.empty else {}

pending_count = sent_counts.get("Pending Message", 0) or tracker_counts.get("Pending Message", 0)
sent_count = sent_counts.get("Message Sent", 0) or tracker_counts.get("Message Sent", 0)

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("📋 Applied", tracker_counts.get("Applied", 0))
col2.metric("⏳ Pending Message", pending_count)
col3.metric("✅ Message Sent", sent_count)
col4.metric("❌ No Resume", sent_counts.get("No Resume", 0) or tracker_counts.get("No Resume", 0))
col5.metric("💬 Already Messaged", tracker_counts.get("Already Messaged", 0) + sent_counts.get("Already Messaged", 0))

st.divider()

# ── Manual trigger buttons ─────────────────────────────────────────────────────
st.subheader("Manual Controls")
c1, c2, c3, _ = st.columns([1, 1, 1, 5])
if c1.button("🔄 Run Poll Now"):
    if GH_TOKEN and GH_REPO:
        ok, err = trigger_workflow("poll.yaml")
        if ok:
            st.success("Poll triggered!")
        else:
            st.error(f"Failed to trigger poll — {err}")
    else:
        st.error("GH_TOKEN or GH_REPO not set in .env")
if c2.button("📨 Run Send Now"):
    if GH_TOKEN and GH_REPO:
        ok, err = trigger_workflow("send.yaml")
        if ok:
            st.success("Send triggered!")
        else:
            st.error(f"Failed to trigger send — {err}")
    else:
        st.error("GH_TOKEN or GH_REPO not set in .env")
if c3.button("🔄 Sync Sent from Tracker"):
    import sheets
    n = sheets.sync_sent_from_tracker()
    st.success(f"Synced {n} row(s). Refresh the page to see updates.")
if st.button("🧹 Deduplicate Sent Sheet"):
    import sheets
    n = sheets.deduplicate_sent_sheet()
    load_sent.clear()
    st.success(f"Removed {n} duplicate row(s). Refreshing...")
    st.rerun()
if st.button("🔄 Sync Tracker from Sent"):
    import sheets
    n = sheets.sync_tracker_from_sent()
    load_tracker.clear()
    load_sent.clear()
    st.success(f"Updated {n} Tracker row(s) to Message Sent. Refreshing...")
    st.rerun()
st.divider()

# ── Sent sheet: People we've messaged (main view) ────────────────────────────────
st.subheader("📨 Sent Messages — People & Companies")
if not df_sent.empty:
    st.dataframe(
        df_sent[["Name", "Company", "LinkedIn ID", "Job URL", "Status"]],
        use_container_width=True,
        hide_index=True,
    )
elif not df_tracker.empty and (tracker_counts.get("Pending Message", 0) or tracker_counts.get("Message Sent", 0)):
    st.info("Person data is in Tracker. Run **Poll** or `python migrate_sheet.py` to migrate to Sent sheet.")
else:
    st.info("No people in Sent sheet yet.")

st.subheader("Pending — waiting to send DM")
pending = df_sent[df_sent["Status"] == "Pending Message"] if not df_sent.empty else pd.DataFrame()
pending_tracker = df_tracker[df_tracker["Status"] == "Pending Message"] if not df_tracker.empty else pd.DataFrame()
if not pending.empty:
    st.dataframe(pending[["Name", "Company", "LinkedIn ID", "Job URL"]], use_container_width=True, hide_index=True)
elif not pending_tracker.empty:
    st.dataframe(pending_tracker[["Company", "Role", "Job URL", "Applied Date"]], use_container_width=True, hide_index=True)
    st.caption("Tracker has stale Pending status. Click 'Sync Tracker from Sent' above to update.")
else:
    st.info("No rows pending.")

st.subheader("Sent")
sent = df_sent[df_sent["Status"] == "Message Sent"] if not df_sent.empty else pd.DataFrame()
sent_tracker = df_tracker[df_tracker["Status"] == "Message Sent"] if not df_tracker.empty else pd.DataFrame()
if not sent.empty:
    st.dataframe(sent[["Name", "Company", "LinkedIn ID", "Job URL"]], use_container_width=True, hide_index=True)
elif not sent_tracker.empty:
    st.dataframe(sent_tracker[["Company", "Role", "Job URL", "Applied Date"]], use_container_width=True, hide_index=True)
    st.caption("From Tracker. Run Poll or migrate_sheet.py to move to Sent sheet.")
else:
    st.info("No messages sent yet.")

# ── Tracker: Applications ──────────────────────────────────────────────────────
_outreach_days = os.environ.get("MESSAGE_APPLY_WITHIN_DAYS", "12")
st.subheader(
    f"📋 Tracker — Applications (Outreach window = last {_outreach_days} days from Applied Date)"
)
if df_tracker.empty:
    st.info("No tracker data.")
else:
    st.dataframe(df_tracker, use_container_width=True, hide_index=True)

if st.button("🔄 Refresh data"):
    load_tracker.clear()
    load_sent.clear()
    st.rerun()

st.caption("Data refreshes every 60 seconds, or click Refresh data above.")
