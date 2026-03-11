"""
dashboard.py — Streamlit monitoring UI for the LinkedIn agent.

Run locally:
    streamlit run dashboard.py

Shows:
  - Tracker: Applied Date, Company, Role, Job URL, Status (5 cols)
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
    "Already Messaged": "#9B9B9B",
}

# ── GitHub Actions dispatch ────────────────────────────────────────────────────
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO  = os.environ.get("GH_REPO", "")


@st.cache_data(ttl=60)
def load_tracker() -> pd.DataFrame:
    """Load Tracker sheet (5 columns)."""
    creds = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=SCOPES)
    gc = gspread.authorize(creds)
    try:
        ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return pd.DataFrame()
    rows = ws.get_all_values()
    if len(rows) < 2:
        return pd.DataFrame()
    headers = ["Applied Date", "Company", "Role", "Job URL", "Status"]
    data = []
    for row in rows[1:]:
        padded = (row + [""] * 5)[:5]
        data.append(padded)
    return pd.DataFrame(data, columns=headers)


@st.cache_data(ttl=60)
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
tracker_counts = df_tracker["Status"].value_counts().to_dict() if not df_tracker.empty else {}
sent_counts = df_sent["Status"].value_counts().to_dict() if not df_sent.empty else {}

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("📋 Applied", tracker_counts.get("Applied", 0))
col2.metric("⏳ Pending Message", sent_counts.get("Pending Message", 0))
col3.metric("✅ Message Sent", sent_counts.get("Message Sent", 0))
col4.metric("❌ No Resume", sent_counts.get("No Resume", 0))
col5.metric("💬 Already Messaged", tracker_counts.get("Already Messaged", 0) + sent_counts.get("Already Messaged", 0))

st.divider()

# ── Manual trigger buttons ─────────────────────────────────────────────────────
if GH_TOKEN and GH_REPO:
    st.subheader("Manual Controls")
    c1, c2, _ = st.columns([1, 1, 6])
    if c1.button("🔄 Run Poll Now"):
        ok, err = trigger_workflow("poll.yaml")
        if ok:
            st.success("Poll triggered!")
        else:
            st.error(f"Failed to trigger poll — {err}")
    if c2.button("📨 Run Send Now"):
        ok, err = trigger_workflow("send.yaml")
        if ok:
            st.success("Send triggered!")
        else:
            st.error(f"Failed to trigger send — {err}")
    st.divider()

# ── Sent sheet: People we've messaged (main view) ────────────────────────────────
st.subheader("📨 Sent Messages — People & Companies")
if df_sent.empty:
    st.info("No people in Sent sheet yet.")
else:
    st.dataframe(
        df_sent[["Name", "Company", "LinkedIn ID", "Job URL", "Status"]],
        use_container_width=True,
        hide_index=True,
    )

st.subheader("Pending — waiting to send DM")
pending = df_sent[df_sent["Status"] == "Pending Message"] if not df_sent.empty else pd.DataFrame()
if pending.empty:
    st.info("No rows pending.")
else:
    st.dataframe(pending[["Name", "Company", "LinkedIn ID", "Job URL"]], use_container_width=True, hide_index=True)

st.subheader("Sent")
sent = df_sent[df_sent["Status"] == "Message Sent"] if not df_sent.empty else pd.DataFrame()
if sent.empty:
    st.info("No messages sent yet.")
else:
    st.dataframe(sent[["Name", "Company", "LinkedIn ID", "Job URL"]], use_container_width=True, hide_index=True)

# ── Tracker: Applications ──────────────────────────────────────────────────────
st.subheader("📋 Tracker — Applications (Applied Date, Company, Role, Job URL, Status)")
if df_tracker.empty:
    st.info("No tracker data.")
else:
    st.dataframe(df_tracker, use_container_width=True, hide_index=True)

st.caption("Data refreshes every 60 seconds. Click the refresh button in the top-right to force reload.")
