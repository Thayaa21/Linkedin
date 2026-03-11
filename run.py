"""
run.py — Run poll or send locally (faster than waiting for GitHub Actions).

Usage:
    python run.py poll    # Scrape connections, match to sheet, add to Sent
    python run.py send    # Send DMs to all Pending rows
"""

import asyncio
import sys
from main import poll_connections, send_messages


def main():
    if len(sys.argv) < 2:
        print("Usage: python run.py poll | send")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "poll":
        asyncio.run(poll_connections())
    elif cmd == "send":
        asyncio.run(send_messages())
    else:
        print("Usage: python run.py poll | send")
        sys.exit(1)


if __name__ == "__main__":
    main()
