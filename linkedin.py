"""
linkedin.py — All Playwright interactions with LinkedIn.

Functions:
  load_cookies(context)        — restores saved session
  get_connections(page)        — scrapes all 1st-degree connections
  diff_connections(old, new)   — returns newly added connections
  save_snapshot(connections)   — persists snapshot to disk
  load_snapshot()              — loads previous snapshot from disk
  send_message(page, url, msg) — opens profile, sends a DM

LinkedIn is a SPA — we use human-like delays throughout to reduce
detection risk.
"""

import json
import asyncio
import logging
import random
from pathlib import Path
from playwright.async_api import BrowserContext, Page, async_playwright
from config import COOKIES_FILE, CONNECTIONS_SNAPSHOT

logger = logging.getLogger(__name__)

LINKEDIN_BASE = "https://www.linkedin.com"
CONNECTIONS_URL = f"{LINKEDIN_BASE}/mynetwork/invite-connect/connections/"


# ─── Human-like delays ────────────────────────────────────────────────────────

async def _pause(lo=1.0, hi=3.0):
    await asyncio.sleep(random.uniform(lo, hi))


# ─── Cookie management ────────────────────────────────────────────────────────

def _cookies_exist() -> bool:
    return Path(COOKIES_FILE).exists()


async def load_cookies(context: BrowserContext) -> bool:
    """
    Loads saved cookies into the browser context.
    Returns True if cookies were loaded, False if the file doesn't exist.
    """
    if not _cookies_exist():
        logger.error("Cookie file not found: %s. Run save_cookies.py first.", COOKIES_FILE)
        return False
    cookies = json.loads(Path(COOKIES_FILE).read_text())
    await context.add_cookies(cookies)
    logger.info("Loaded %d cookies from %s", len(cookies), COOKIES_FILE)
    return True


async def is_logged_in(page: Page) -> bool:
    """Navigates to feed and checks we're not bounced to login."""
    await page.goto(f"{LINKEDIN_BASE}/feed/", wait_until="domcontentloaded")
    await _pause(2, 4)
    return "login" not in page.url and "authwall" not in page.url


# ─── Connections scraping ─────────────────────────────────────────────────────

async def get_connections(page: Page) -> dict[str, dict]:
    """
    Scrapes the LinkedIn Connections page, scrolling until all connections load.

    Returns a dict keyed by profile URL:
      {
        "https://linkedin.com/in/jane-doe": {
          "name":     "Jane Doe",
          "headline": "Software Engineer at Stripe",
          "url":      "https://linkedin.com/in/jane-doe",
        },
        ...
      }
    """
    logger.info("Navigating to connections page...")
    await page.goto(CONNECTIONS_URL, wait_until="domcontentloaded")
    await _pause(3, 5)

    # Debug: log page title and URL to confirm we landed on the right page
    title = await page.title()
    logger.info("Page title: %s | URL: %s", title, page.url)

    # Debug: try to find the connection count header LinkedIn shows (e.g. "1,234 connections")
    count_el = await page.query_selector("h1")
    if count_el:
        logger.info("Page h1: %s", (await count_el.inner_text()).strip())

    # Debug: count total <li> elements on the page as a sanity check
    all_li = await page.query_selector_all("li")
    logger.info("Total <li> elements on page: %d", len(all_li))

    connections: dict[str, dict] = {}
    prev_count = -1

    while True:
        cards = await page.query_selector_all(
            "li.mn-connection-card, "
            "[data-view-name='connections-list-item'], "
            ".mn-connection-card"
        )

        for card in cards:
            try:
                # Profile URL
                link_el = await card.query_selector("a[href*='/in/']")
                if not link_el:
                    continue
                href = await link_el.get_attribute("href")
                url = f"{LINKEDIN_BASE}{href.split('?')[0]}" if href.startswith("/") else href.split("?")[0]

                if url in connections:
                    continue  # already captured

                # Name
                name_el = await card.query_selector(
                    ".mn-connection-card__name, "
                    "[class*='connection-card__name'], "
                    ".entity-result__title-text"
                )
                name = (await name_el.inner_text()).strip() if name_el else "Unknown"

                # Headline
                headline_el = await card.query_selector(
                    ".mn-connection-card__occupation, "
                    "[class*='connection-card__occupation'], "
                    ".entity-result__primary-subtitle"
                )
                headline = (await headline_el.inner_text()).strip() if headline_el else ""

                connections[url] = {"name": name, "headline": headline, "url": url}

            except Exception as e:
                logger.debug("Error parsing connection card: %s", e)

        current_count = len(connections)
        logger.info("Connections scraped so far: %d", current_count)

        if current_count == prev_count:
            break  # no new cards loaded — we've reached the end
        prev_count = current_count

        # Scroll down to load more
        await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        await _pause(2, 4)

    logger.info("Total connections scraped: %d", len(connections))
    return connections


# ─── Snapshot management ─────────────────────────────────────────────────────

def load_snapshot() -> dict[str, dict]:
    p = Path(CONNECTIONS_SNAPSHOT)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def save_snapshot(connections: dict[str, dict]):
    Path(CONNECTIONS_SNAPSHOT).write_text(json.dumps(connections, indent=2))
    logger.info("Snapshot saved: %d connections", len(connections))


def diff_connections(
    old: dict[str, dict],
    new: dict[str, dict],
) -> list[dict]:
    """Returns connections in `new` that aren't in `old`."""
    new_keys = set(new.keys()) - set(old.keys())
    return [new[k] for k in new_keys]


# ─── Send DM ─────────────────────────────────────────────────────────────────

async def send_message(page: Page, profile_url: str, message: str) -> bool:
    """
    Opens a LinkedIn profile and sends a DM via the Message button.
    Returns True on success, False on failure.

    Note: LinkedIn does NOT support file attachments in regular DMs.
    Include the resume as a Google Drive link inside the message text.
    """
    logger.info("Sending DM to %s", profile_url)
    try:
        await page.goto(profile_url, wait_until="domcontentloaded")
        await _pause(2, 4)

        # Click the "Message" button on the profile
        msg_btn = await page.query_selector(
            "button:has-text('Message'), "
            "a:has-text('Message'), "
            "[aria-label*='Message']"
        )
        if not msg_btn:
            logger.warning("No Message button found on %s", profile_url)
            return False

        await msg_btn.click()
        await _pause(1.5, 3)

        # Type message in the composer
        composer = await page.query_selector(
            ".msg-form__contenteditable, "
            "[data-artdeco-is-focused='true'] [contenteditable='true'], "
            "[role='textbox']"
        )
        if not composer:
            logger.warning("Message composer not found for %s", profile_url)
            return False

        await composer.click()
        await _pause(0.5, 1)

        # Type naturally (character by character with small delays)
        await composer.type(message, delay=random.randint(30, 80))
        await _pause(1, 2)

        # Click Send
        send_btn = await page.query_selector(
            "button.msg-form__send-button, "
            "button[type='submit']:has-text('Send'), "
            "[aria-label='Send']"
        )
        if not send_btn:
            logger.warning("Send button not found for %s", profile_url)
            return False

        await send_btn.click()
        await _pause(2, 3)
        logger.info("Message sent to %s", profile_url)
        return True

    except Exception as e:
        logger.error("Failed to send message to %s: %s", profile_url, e)
        return False


# ─── Browser factory (used by main.py) ───────────────────────────────────────

async def make_browser_context(playwright):
    """Creates a headless browser context with a realistic user agent."""
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    # Mask navigator.webdriver flag
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context
