"""
mark_sent.py — Fix wrong status: mark a person as Message Sent.

Use when you sent a message manually or the status wasn't updated correctly.

Usage:
    python mark_sent.py "Juhyung Lee"
    python mark_sent.py "leejuhyung"
    python mark_sent.py --sync         # Sync from Tracker (companies with Message Sent)
    python mark_sent.py --deduplicate # Remove duplicate rows (same person+company)
"""

import sys
import sheets

def main():
    if len(sys.argv) < 2:
        print("Usage: python mark_sent.py \"Name\" or \"linkedin_id\" or --sync or --deduplicate")
        sys.exit(1)

    arg = sys.argv[1].strip()

    if arg == "--sync":
        n = sheets.sync_sent_from_tracker()
        print(f"Synced {n} row(s) from Tracker (Message Sent) to Sent sheet.")
        sys.exit(0)

    if arg == "--deduplicate":
        n = sheets.deduplicate_sent_sheet()
        print(f"Removed {n} duplicate row(s) from Sent sheet.")
        sys.exit(0)

    # Try as name or LinkedIn URL fragment
    ok = sheets.mark_person_as_sent(name=arg)
    if not ok:
        ok = sheets.mark_person_as_sent(li_url=arg)
    if ok:
        print(f"Marked as Message Sent: {arg}")
    else:
        print(f"Not found: {arg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
