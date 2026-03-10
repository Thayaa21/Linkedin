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

    # ── Step 2: Navigate to connections page and intercept LinkedIn's own API ────
    # LinkedIn's JavaScript makes its own Voyager calls with all the right headers.
    # We intercept those responses to collect profile + position data automatically.
    # This is more reliable than calling the API ourselves.
    logger.info("Setting up network interceptor for connections page...")

    intercepted_profiles: dict[str, dict] = {}   # publicIdentifier → profile dict
    intercepted_connections_urns: list[str] = []  # list of connectedMember URNs (order = recency)

    import json as _json

    async def _on_response(response):
        url = response.url
        if "voyager/api" not in url:
            return
        try:
            text = await response.text()
            data = _json.loads(text)
        except Exception:
            return

        # Collect profile URNs from connections endpoint (preserves recency order)
        if "relationships" in url and "connections" in url:
            for item in data.get("included", []):
                member = item.get("connectedMember", "")
                if member and member not in intercepted_connections_urns:
                    intercepted_connections_urns.append(member)

        # Collect full profile data from any identity/profile endpoint
        for item in data.get("included", []):
            if not isinstance(item, dict):
                continue
            pid = item.get("publicIdentifier", "")
            if not pid:
                continue
            # Merge: keep whichever copy has more fields (some calls return richer data)
            existing = intercepted_profiles.get(pid, {})
            if len(item) > len(existing):
                intercepted_profiles[pid] = item

    page.on("response", _on_response)

    # Navigate — LinkedIn's app will fetch connections + mini-profiles automatically
    logger.info("Navigating to connections page (intercepting API traffic)...")
    await page.goto(CONNECTIONS_URL, wait_until="domcontentloaded")
    await _pause(5, 7)   # give JS time to fire all the API calls

    logger.info(
        "Intercepted: %d profile URNs, %d profiles with data",
        len(intercepted_connections_urns), len(intercepted_profiles),
    )

    # ── Step 3: If intercepted profiles are sparse, do a manual Voyager fetch ──
    # Sometimes LinkedIn doesn't fire the profile detail calls immediately.
    # Fall back to calling the connections + miniProfiles APIs ourselves.
    if len(intercepted_profiles) < 5:
        logger.info("Few profiles intercepted — calling Voyager API directly...")
        api_result = await page.evaluate("""
            async (csrfToken) => {
                // Step A: get connection URNs
                const connResp = await fetch(
                    '/voyager/api/relationships/dash/connections?count=50&q=search&sortType=RECENTLY_ADDED',
                    {
                        headers: {
                            'Accept': 'application/vnd.linkedin.normalized+json+2.1',
                            'csrf-token': csrfToken,
                            'x-restli-protocol-version': '2.0.0',
                        },
                        credentials: 'include',
                    }
                );
                if (!connResp.ok) return { ok: false, error: 'connections ' + connResp.status };
                const connData = JSON.parse(await connResp.text());

                const memberUrns = (connData.included || [])
                    .filter(e => e.$type && e.$type.toLowerCase().includes('connection') && e.connectedMember)
                    .map(e => e.connectedMember);

                console.log('Connection URNs found: ' + memberUrns.length);
                if (!memberUrns.length) return { ok: false, error: 'no connection URNs' };

                // Step B: batch-fetch miniProfiles for those URNs
                // Profile IDs are everything after the last colon in the URN
                const profileIds = memberUrns.map(u => u.split(':').pop());
                const ids = profileIds.map(encodeURIComponent).join(',');

                const miniUrl = '/voyager/api/identity/miniProfiles?ids=List(' + ids + ')';
                const miniResp = await fetch(miniUrl, {
                    headers: {
                        'Accept': 'application/json',
                        'csrf-token': csrfToken,
                        'x-restli-protocol-version': '2.0.0',
                    },
                    credentials: 'include',
                });
                const miniBody = await miniResp.text();
                console.log('MiniProfiles status=' + miniResp.status + ' preview=' + miniBody.slice(0, 400));

                if (!miniResp.ok) {
                    // Last resort: return the raw Connection URNs so we can at least build URLs
                    return { ok: true, urnsOnly: true, urns: memberUrns, profiles: {} };
                }

                return { ok: true, urnsOnly: false, profiles: JSON.parse(miniBody), urns: memberUrns };
            }
        """, csrf_token)

        if api_result and api_result.get("ok"):
            # Add any profiles from the API call that weren't intercepted
            profiles_raw = api_result.get("profiles") or {}
            for key, item in (profiles_raw.get("results") or {}).items():
                if isinstance(item, dict):
                    pid = item.get("publicIdentifier", "")
                    if pid and pid not in intercepted_profiles:
                        intercepted_profiles[pid] = item
            for item in (profiles_raw.get("included") or []):
                if isinstance(item, dict):
                    pid = item.get("publicIdentifier", "")
                    if pid and pid not in intercepted_profiles:
                        intercepted_profiles[pid] = item
            # Also store URN ordering if not already set
            if not intercepted_connections_urns:
                intercepted_connections_urns.extend(api_result.get("urns") or [])

        logger.info("After manual fetch: %d profiles collected", len(intercepted_profiles))

    # ── Step 4: Build connections dict from collected profile data ────────────
    connections: dict[str, dict] = {}

    for profile in intercepted_profiles.values():
        try:
            name = f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip()
            identifier = profile.get("publicIdentifier", "")
            if not name or not identifier:
                continue

            url = f"{LINKEDIN_BASE}/in/{identifier}"
            headline = profile.get("headline", "") or ""
            if not isinstance(headline, str):
                headline = ""

            # ── Extract current company from experience/position data ──────────
            # LinkedIn API uses several field names depending on the endpoint used.
            current_company = ""
            for pos_field in (
                "currentPositionV2", "currentPosition", "positions",
                "currentPositions", "experience",
            ):
                pos_data = profile.get(pos_field)
                if isinstance(pos_data, list) and pos_data:
                    first = pos_data[0]
                    if isinstance(first, dict):
                        current_company = (
                            first.get("companyName")
                            or first.get("company", {}).get("name", "")
                            or first.get("companyUrn", "").split(":")[-1]
                        )
                        if current_company:
                            break
                elif isinstance(pos_data, dict):
                    current_company = pos_data.get("companyName", "")
                    if current_company:
                        break

            logger.info(
                "  %-30s headline='%s' | company='%s'",
                name, headline[:50], current_company,
            )

            if url not in connections:
                connections[url] = {
                    "name": name,
                    "headline": headline,
                    "current_company": current_company,
                    "url": url,
                }
        except Exception as e:
            logger.debug("Error building connection from profile: %s", e)

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
