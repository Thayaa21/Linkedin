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
    await _pause(3, 5)

    # Scroll down so LinkedIn lazy-loads the rest of the connection cards.
    # Each scroll triggers LinkedIn's own API calls for mini-profiles, which
    # our interceptor captures (including current company / position data).
    logger.info("Scrolling to trigger lazy-loaded profile API calls...")
    for scroll_i in range(12):
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1.2)
        if len(intercepted_profiles) >= 45:
            logger.info("Enough profiles collected (%d), stopping scroll", len(intercepted_profiles))
            break

    await _pause(2, 3)  # final wait for any in-flight requests

    logger.info(
        "Intercepted: %d profile URNs, %d profiles with data",
        len(intercepted_connections_urns), len(intercepted_profiles),
    )

    # ── Step 3: If scrolling didn't yield profiles, fall back to API ──────────
    if len(intercepted_profiles) < 5:
        logger.info("Still few profiles after scroll — calling Voyager API directly...")
        api_result = await page.evaluate("""
            async (csrfToken) => {
                // Get connection URNs first
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

                console.log('Connection URNs: ' + memberUrns.length);
                if (!memberUrns.length) return { ok: false, error: 'no URNs' };

                // Try several batch-profile endpoint formats — LinkedIn changes these
                const profileIds  = memberUrns.map(u => u.split(':').pop());
                const encodedUrns = memberUrns.map(encodeURIComponent).join(',');
                const encodedIds  = profileIds.map(encodeURIComponent).join(',');

                const endpoints = [
                    // New dash format with full URNs
                    '/voyager/api/identity/dash/profiles?ids=List(' + encodedUrns + ')',
                    // Old format with bare IDs
                    '/voyager/api/identity/profiles?ids=List(' + encodedIds + ')',
                    // miniProfile (old)
                    '/voyager/api/identity/miniProfiles?ids=List(' + encodedIds + ')',
                ];

                for (const ep of endpoints) {
                    try {
                        const r = await fetch(ep, {
                            headers: {
                                'Accept': 'application/vnd.linkedin.normalized+json+2.1',
                                'csrf-token': csrfToken,
                                'x-restli-protocol-version': '2.0.0',
                            },
                            credentials: 'include',
                        });
                        const body = await r.text();
                        console.log('Batch profiles ' + ep.split('?')[0] + ' → ' + r.status + ' ' + body.slice(0, 200));
                        if (r.ok) return { ok: true, profiles: JSON.parse(body), urns: memberUrns };
                    } catch(e) {
                        console.log('Error ' + ep + ': ' + e.message);
                    }
                }

                // All batch endpoints failed — return URNs only so we can build profile URLs
                return { ok: true, urnsOnly: true, urns: memberUrns, profiles: {} };
            }
        """, csrf_token)

        if api_result and api_result.get("ok"):
            profiles_raw = api_result.get("profiles") or {}
            # normalized JSON puts profiles in included[]
            for item in (profiles_raw.get("included") or []):
                if isinstance(item, dict):
                    pid = item.get("publicIdentifier", "")
                    if pid and pid not in intercepted_profiles:
                        intercepted_profiles[pid] = item
            # plain JSON may put them in results{}
            for item in (profiles_raw.get("results") or {}).values():
                if isinstance(item, dict):
                    pid = item.get("publicIdentifier", "")
                    if pid and pid not in intercepted_profiles:
                        intercepted_profiles[pid] = item
            if not intercepted_connections_urns:
                intercepted_connections_urns.extend(api_result.get("urns") or [])

        logger.info("After API fallback: %d profiles collected", len(intercepted_profiles))

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


# ─── Profile company lookup ──────────────────────────────────────────────────

async def get_csrf_token(page: Page) -> str:
    """Return the CSRF token (JSESSIONID cookie value) for the current session."""
    cookies = await page.context.cookies()
    return next(
        (c["value"].strip('"') for c in cookies if c["name"] == "JSESSIONID"),
        "",
    )


async def get_profile_company(page: Page, public_identifier: str, csrf_token: str) -> str:
    """
    Fetches the person's current employer from their LinkedIn work experience.
    Tries three endpoints in order — stops at the first that returns data.
    Returns "" on any error or if no position data is found.
    """
    result = await page.evaluate("""
        async ([publicId, csrfToken]) => {
            const headers = {
                'Accept': 'application/vnd.linkedin.normalized+json+2.1',
                'csrf-token': csrfToken,
                'x-restli-protocol-version': '2.0.0',
            };

            function extractCompany(included) {
                if (!Array.isArray(included)) return '';
                const positions = included.filter(e =>
                    e.$type && e.$type.toLowerCase().includes('position')
                );
                // Prefer current position (no end date)
                const current = positions.find(p => !p.timePeriod?.endDate);
                const best = current || positions[0];
                if (!best) return '';
                return best.companyName
                    || (best.company && best.company.name)
                    || '';
            }

            // Attempt 1: dedicated positions endpoint (most direct)
            try {
                const r = await fetch(
                    '/voyager/api/identity/profiles/' + publicId + '/positions',
                    { headers, credentials: 'include' }
                );
                if (r.ok) {
                    const d = JSON.parse(await r.text());
                    const company = extractCompany(d.included || [])
                        || extractCompany(d.elements || []);
                    if (company) return company;
                }
            } catch(e) {}

            // Attempt 2: full profile with explicit projection for positions
            try {
                const r = await fetch(
                    '/voyager/api/identity/profiles/' + publicId +
                    '?projection=(positions)',
                    { headers, credentials: 'include' }
                );
                if (r.ok) {
                    const d = JSON.parse(await r.text());
                    const company = extractCompany(d.included || []);
                    if (company) return company;
                }
            } catch(e) {}

            // Attempt 3: dash profile endpoint
            try {
                const r = await fetch(
                    '/voyager/api/identity/dash/profiles/' +
                    encodeURIComponent('urn:li:fsd_profile:' + publicId) +
                    '?projection=(profileView,positions)',
                    { headers, credentials: 'include' }
                );
                if (r.ok) {
                    const d = JSON.parse(await r.text());
                    const company = extractCompany(d.included || []);
                    if (company) return company;
                }
            } catch(e) {}

            return '';
        }
    """, [public_identifier, csrf_token])
    return result or ""


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
