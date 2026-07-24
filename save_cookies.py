"""
save_cookies.py — ONE-TIME setup script.

Run this once to log into LinkedIn manually. It saves your session cookies
to linkedin_cookies.json so the main agent can reuse them without logging in
every run.

Usage:
    python save_cookies.py

Re-run whenever LinkedIn asks you to log in again (typically every few weeks).
"""

import json
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from config import COOKIES_FILE


async def main():
    print("Opening LinkedIn in a real browser window.")
    print("Log in manually. Cookies will be saved automatically once you reach the feed.\n")

    async with async_playwright() as p:
        # Launch a VISIBLE (headed) browser so you can log in
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login")

        print("⏳ Waiting for you to log in...")
        print("   Complete any 2FA if prompted.")
        print("   Cookies will save automatically once LinkedIn loads.\n")

        # Wait until the URL changes away from login/authwall (max 5 minutes)
        for _ in range(300):
            await asyncio.sleep(1)
            url = page.url
            if ("feed" in url or "mynetwork" in url or "messaging" in url
                    or "jobs" in url or "notifications" in url):
                break
        else:
            print("❌ Timed out waiting for login. Try again.")
            await browser.close()
            return

        # Give page a moment to settle
        await asyncio.sleep(3)

        if "authwall" in page.url or "login" in page.url:
            print("❌ Doesn't look like you're logged in yet. Try again.")
            await browser.close()
            return

        cookies = await context.cookies()
        Path(COOKIES_FILE).write_text(json.dumps(cookies, indent=2))
        print(f"✅ Saved {len(cookies)} cookies → {COOKIES_FILE}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
