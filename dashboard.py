"""
dashboard.py — Streamlit monitoring UI for the LinkedIn agent.

Run locally:
    streamlit run dashboard.py

Shows:
  - Status breakdown (Applied / Pending / Sent / No Resume)
  - Full applications table with colour-coded statuses
  - Manual trigger buttons for Poll and Send workflows (needs GH_TOKEN + GH_REPO)
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
GH_REPO  = os.environ.get("GH_REPO", "")   # e.g. "Thayaa21/linkedin-agent"


@st.cache_data(ttl=60)
def load_sheet() -> pd.DataFrame:
    creds = Credentials.from_service_account_file(GOOGLE_CREDS, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    ws    = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
    rows  = ws.get_all_values()
    if len(rows) < 2:
        return pd.DataFrame()
    headers = ["Timestamp", "Company", "Role", "Job URL", "Status", "Source", "LI Name", "LI URL"]
    data = []
    for row in rows[1:]:
        padded = row + [""] * (8 - len(row))
        data.append(padded[:8])
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

df = load_sheet()

if df.empty:
    st.warning("No data found in the sheet.")
    st.stop()

# ── Stats row ──────────────────────────────────────────────────────────────────
counts = df["Status"].value_counts().to_dict()

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("📋 Applied",         counts.get("Applied", 0))
col2.metric("⏳ Pending Message", counts.get("Pending Message", 0))
col3.metric("✅ Message Sent",    counts.get("Message Sent", 0))
col4.metric("❌ No Resume",       counts.get("No Resume", 0))
col5.metric("💬 Already Messaged",counts.get("Already Messaged", 0))

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

# ── Per-status sections ────────────────────────────────────────────────────────
st.subheader("Pending — waiting to send DM")
pending = df[df["Status"] == "Pending Message"][["Company", "Role", "LI Name", "LI URL", "Timestamp"]]
if pending.empty:
    st.info("No rows pending.")
else:
    st.dataframe(pending, use_container_width=True, hide_index=True)

st.subheader("Sent")
sent = df[df["Status"] == "Message Sent"][["Company", "Role", "LI Name", "LI URL", "Timestamp"]]
if sent.empty:
    st.info("No messages sent yet.")
else:
    st.dataframe(sent, use_container_width=True, hide_index=True)

st.subheader("Applied — waiting for a connection")
applied = df[df["Status"] == "Applied"][["Company", "Role", "Job URL", "Timestamp"]]
if applied.empty:
    st.info("No applied rows.")
else:
    st.dataframe(applied, use_container_width=True, hide_index=True)

st.subheader("No Resume / Issues")
issues = df[df["Status"].isin(["No Resume", "Already Messaged"])][["Company", "Role", "Status", "Timestamp"]]
if issues.empty:
    st.info("No issues.")
else:
    st.dataframe(issues, use_container_width=True, hide_index=True)

# ── Full table (collapsed) ─────────────────────────────────────────────────────
with st.expander("View all rows"):
    st.dataframe(df, use_container_width=True, hide_index=True)

st.caption("Data refreshes every 60 seconds. Click the refresh button in the top-right to force reload.")
