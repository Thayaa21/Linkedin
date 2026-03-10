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
    # Navigate to LinkedIn home to establish session (not connections page directly)
    logger.info("Navigating to LinkedIn to establish session...")
    page.on("console", lambda msg: logger.info("BROWSER: %s", msg.text) if msg.type == "log" else None)
    await page.goto(f"{LINKEDIN_BASE}/mynetwork/", wait_until="domcontentloaded")
    await _pause(2, 4)

    # Extract CSRF token from JSESSIONID cookie (required for Voyager API)
    cookies = await page.context.cookies()
    csrf_token = next(
        (c["value"].strip('"') for c in cookies if c["name"] == "JSESSIONID"),
        ""
    )
    logger.info("CSRF token found: %s", "yes" if csrf_token else "NO — cannot call API")

    if not csrf_token:
        logger.error("JSESSIONID cookie missing. Re-run save_cookies.py.")
        return {}

    # Call LinkedIn's internal Voyager API for 50 most recently added connections.
    # We try two endpoints and two Accept headers (plain JSON is easier to parse).
    logger.info("Calling LinkedIn Voyager API for connections...")
    result = await page.evaluate("""
        async (csrfToken) => {
            // Attempt 1: dash endpoint, plain JSON (no normalization)
            // Attempt 2: dash endpoint, normalized JSON
            // Attempt 3: legacy endpoint, plain JSON
            const attempts = [
                {
                    url: '/voyager/api/relationships/dash/connections?count=50&q=search&sortType=RECENTLY_ADDED',
                    accept: 'application/json',
                },
                {
                    url: '/voyager/api/relationships/dash/connections?count=50&q=search&sortType=RECENTLY_ADDED',
                    accept: 'application/vnd.linkedin.normalized+json+2.1',
                },
                {
                    url: '/voyager/api/relationships/connections?count=50&q=search&sortType=RECENTLY_ADDED&networkType=F',
                    accept: 'application/json',
                },
            ];
            for (const attempt of attempts) {
                try {
                    const resp = await fetch(attempt.url, {
                        headers: {
                            'Accept': attempt.accept,
                            'csrf-token': csrfToken,
                            'x-restli-protocol-version': '2.0.0',
                            'x-li-lang': 'en_US',
                        },
                        credentials: 'include'
                    });
                    const body = await resp.text();
                    // Always log the first 600 chars so we can see the shape
                    console.log('API ' + attempt.url.split('?')[0] +
                                ' accept=' + attempt.accept.split('/').pop() +
                                ' status=' + resp.status +
                                ' preview=' + body.slice(0, 600));
                    if (!resp.ok) {
                        continue;
                    }
                    const parsed = JSON.parse(body);
                    return { ok: true, accept: attempt.accept, parsed: parsed };
                } catch(e) {
                    console.log('API exception ' + attempt.url + ': ' + e.message);
                }
            }
            return { ok: false, error: 'all endpoints failed' };
        }
    """, csrf_token)

    if not result or not result.get("ok"):
        logger.error("Voyager API failed: %s", result.get("error") if result else "null")
        return {}

    accept_used = result.get("accept", "")
    data = result.get("parsed", {})
    top_keys = list(data.keys()) if isinstance(data, dict) else []
    logger.info("API response top-level keys: %s (accept=%s)", top_keys, accept_used)

    # ── Parse plain-JSON response  (elements[] at top level) ──────────────────
    raw_elements = data.get("elements", [])

    # ── Parse normalized-JSON response (included[] at top level) ──────────────
    # In normalized format the connection objects sit in `included` alongside
    # profile objects; we pick only Connection-typed entries.
    if not raw_elements:
        included = data.get("included", [])
        logger.info("Plain JSON elements=0, trying included[] (%d items)", len(included))
        raw_elements = [
            item for item in included
            if isinstance(item, dict)
            and "connection" in item.get("$type", "").lower()
        ]
        if not raw_elements:
            # Last resort: grab *all* Profile objects from included
            raw_elements = [
                item for item in included
                if isinstance(item, dict)
                and "profile" in item.get("$type", "").lower()
                and item.get("publicIdentifier")
            ]
            logger.info("Connection objects=0, falling back to Profile objects: %d", len(raw_elements))

    logger.info("API returned %d connection elements to parse", len(raw_elements))

    connections: dict[str, dict] = {}
    for element in raw_elements:
        try:
            # Plain-JSON dash endpoint wraps the profile under this key
            profile = element.get("connectedMemberResolutionResult") or {}

            # Normalized / profile-object fallback: the element itself IS the profile
            if not profile and element.get("publicIdentifier"):
                profile = element

            if not profile:
                continue

            name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
            identifier = profile.get("publicIdentifier", "")
            if not identifier:
                continue

            url = f"{LINKEDIN_BASE}/in/{identifier}"
            headline_raw = profile.get("headline", "")
            headline = headline_raw if isinstance(headline_raw, str) else ""
            if url not in connections:
                connections[url] = {"name": name, "headline": headline, "url": url}
        except Exception as e:
            logger.debug("Error parsing connection element: %s", e)

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
