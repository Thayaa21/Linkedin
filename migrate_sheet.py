"""
migrate_sheet.py — Convert tracker to the standard layout and ensure Sent sheet exists.

Run once to:
  - Create Sent Messages sheet (3rd tab)
  - Convert Tracker: Applied Date, Company, Role, Job URL, Status, Outreach window
  - Migrate any person data (Name, LinkedIn ID) from old tracker rows to Sent sheet

Usage:
    python migrate_sheet.py
"""

import sheets


def main():
    print("Ensuring Sent Messages sheet exists...")
    sheets.ensure_sent_sheet_exists()
    print("Refining Tracker (incl. Outreach window, migrate person data to Sent)...")
    sheets.refine_tracker_sheet()
    sheets.refresh_tracker_outreach_column()
    print(
        "Done. Tracker: Applied Date, Company, Role, Job URL, Status, Outreach window. "
        "Person data in Sent sheet."
    )


if __name__ == "__main__":
    main()
