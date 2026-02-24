"""
AI Employee Gold Tier - Twitter / X Watcher
============================================

Monitors Twitter (X) DMs, notifications, and home-feed tweets for
business lead keywords and saves actionable items as Markdown notes
in /Needs Action.

PLATFORMS MONITORED:
  Twitter/X - Direct Messages (DMs) at x.com/messages
  Twitter/X - Notifications at x.com/notifications
  Twitter/X - Home feed tweets at x.com/home (keyword scan only)

KEYWORDS MONITORED: sales, client, project

OUTPUT FILES:
  /Needs Action/TWITTER_[YYYYMMDD_HHMMSS]_[sender].md

SESSION:
  /session/twitter  (persistent Chromium profile -- survives restarts)

INSTALL:
    pip install playwright
    playwright install chromium

SESSION / LOGIN:
    - On first run a visible Chromium window opens. Log in to Twitter/X
      manually (including 2FA / phone verification if prompted).
    - The session is saved persistently in /session/twitter so future
      runs start headless automatically without re-logging in.
    - If the session expires or X logs you out, delete /session/twitter
      and run again to repeat the login.

RUN (direct):
    python watchers/twitter_watcher.py

RUN with PM2:
    pm2 start watchers/twitter_watcher.py --interpreter python --name twitter-watcher
    pm2 save
    pm2 startup

PM2 MANAGEMENT:
    pm2 status                        # Check if running
    pm2 logs twitter-watcher          # View live logs
    pm2 restart twitter-watcher       # Restart watcher
    pm2 stop twitter-watcher          # Stop watcher

TEST:
    1. Ask a Twitter/X contact to send you a DM containing:
       "sales", "client", or "project"
    2. Leave it unread in your Messages inbox
    3. Within 60 seconds a file should appear in /Needs Action:
         TWITTER_[timestamp]_[sender].md
    4. Alternatively, post a tweet mentioning your account that contains
       one of the keywords -- it will be picked up in the home-feed scan.
    5. Check the console log for MATCH output.
    6. Then run: python Skills/twitter_post_generator.py

DEBUG:
    HEADLESS=0 python watchers/twitter_watcher.py   # forces visible browser

NOTES:
    - Twitter/X actively detects automation. The watcher uses a real user-
      agent string and the AutomationControlled flag is disabled to reduce
      detection risk. Run with HEADLESS=0 if you hit login challenges.
    - This watcher READS content only. It does NOT tweet, DM, like,
      retweet, or interact with any content.
    - Twitter's data-testid attributes are more stable than class names.
      The selectors below use them exclusively with class-name fallbacks.
    - x.com is the current canonical domain; twitter.com redirects to it.

"""

import os
import sys
import time
import re
import traceback
from datetime import datetime
from pathlib import Path

# --- Gold Tier Error Recovery ---
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "Skills"
sys.path.insert(0, str(_SKILLS_DIR))
from error_recovery import with_retry, log_watcher_error  # noqa: E402

# --- Dependency Check ---
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("ERROR: Playwright not installed.")
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR         = Path(__file__).resolve().parent.parent
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
SESSION_DIR      = BASE_DIR / "session" / "twitter"

CHECK_INTERVAL     = 60          # seconds between full scan cycles
KEYWORDS           = ["sales", "client", "project"]
MAX_ITEMS_PER_SCAN = 25          # cap items scraped per source per cycle
SESSION_REFRESH    = 20          # reload home every N cycles to keep alive

# Force-override headless mode for debugging: HEADLESS=0 or HEADLESS=1
HEADLESS_OVERRIDE = os.environ.get("HEADLESS", "").strip()

# URLs  (x.com is canonical; twitter.com redirects there)
X_HOME_URL          = "https://x.com/home"
X_MESSAGES_URL      = "https://x.com/messages"
X_NOTIFICATIONS_URL = "https://x.com/notifications"

LOGIN_WAIT_TIMEOUT = 180_000   # ms -- time allowed for manual first login
PAGE_LOAD_TIMEOUT  = 45_000    # ms -- navigation timeout
ELEMENT_TIMEOUT    = 10_000    # ms -- selector wait


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def contains_keyword(text: str) -> str | None:
    """Return the first matched keyword (case-insensitive), or None."""
    tl = text.lower()
    for kw in KEYWORDS:
        if kw in tl:
            return kw
    return None


def priority_from_keyword(keyword: str) -> str:
    return {
        "sales":   "high",
        "client":  "high",
        "project": "medium",
    }.get(keyword, "medium")


def sanitize_filename(text: str) -> str:
    """Make a safe filename component from arbitrary text."""
    text = re.sub(r'[\\/*?:"<>|@#]', "_", text)
    text = re.sub(r'\s+', "_", text)
    return text[:50].strip("_")


def build_summary(sender: str, content: str, keyword: str,
                  content_type: str) -> str:
    """
    Produce a concise one-line summary of the tweet/DM.
    Highlights the matched keyword and classifies intent.
    """
    clean   = re.sub(r'\s+', ' ', content).strip()
    excerpt = clean[:200] + ("..." if len(clean) > 200 else "")
    intent  = f"Possible {keyword} lead detected in {content_type}."
    return f"[TWITTER {content_type.upper()}] from {sender}: {excerpt} | {intent}"


# ---------------------------------------------------------------------------
# /Needs Action Writer
# ---------------------------------------------------------------------------

def save_needs_action(
    content_type: str,   # "dm" | "notification" | "tweet"
    sender:       str,
    handle:       str,   # @handle without @
    content:      str,
    keyword:      str,
    priority:     str,
    summary:      str,
) -> Path:
    """Write a structured Markdown note to /Needs Action and return the path."""
    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)

    ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_sender  = sanitize_filename(sender or handle)
    filename     = f"TWITTER_{ts}_{safe_sender}.md"
    filepath     = NEEDS_ACTION_DIR / filename
    received     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content_preview = content[:600].replace('"', "'")
    summary_clean   = summary.replace('"', "'")
    handle_clean    = handle.lstrip("@") if handle else ""

    file_content = f"""---
type: twitter_{content_type}
platform: twitter
content_type: {content_type}
from: "{sender}"
handle: "@{handle_clean}"
subject: "Twitter {content_type} from {sender}"
received: "{received}"
keyword_matched: "{keyword}"
priority: {priority}
status: pending
summary: "{summary_clean}"
---

# Twitter {content_type.title()}: {sender}

| Field         | Value |
|---------------|-------|
| Platform      | Twitter / X |
| Type          | {content_type.title()} |
| From          | {sender} |
| Handle        | @{handle_clean} |
| Received      | {received} |
| Priority      | {priority.upper()} |
| Keyword       | `{keyword}` |

## AI Summary

> {summary}

## Full Content Preview

```
{content_preview}
```

## Next Steps

- [ ] Review the {content_type} from {sender} (@{handle_clean}) on Twitter/X
- [ ] Draft a reply/tweet if this is a qualified lead (use Skill 7: Twitter Post Generator)
- [ ] Qualify as lead or archive
- [ ] Move to Done when resolved

## Skill Integration

Run the Twitter Post Generator to draft a response:
```
python Skills/twitter_post_generator.py --file "Needs Action/{filename}"
```
"""

    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(file_content)

    return filepath


# ---------------------------------------------------------------------------
# Session Helpers
# ---------------------------------------------------------------------------

def session_exists() -> bool:
    return SESSION_DIR.exists() and any(SESSION_DIR.iterdir())


# Nav selectors that confirm a logged-in Twitter/X session
X_LOGGED_IN_SELECTORS = [
    'a[data-testid="AppTabBar_Home_Link"]',
    'div[data-testid="SideNav_AccountSwitcher_Button"]',
    'a[href="/home"][role="link"]',
    'nav[aria-label="Primary navigation"]',
    'nav[aria-label="Primary"]',
]


def x_is_logged_in(page) -> bool:
    """Navigate to home and check for logged-in indicators."""
    try:
        page.goto(X_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)
        for sel in X_LOGGED_IN_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=ELEMENT_TIMEOUT)
                return True
            except PlaywrightTimeout:
                continue
    except Exception:
        pass
    return False


def wait_for_x_login(page) -> bool:
    """
    Open x.com/login and wait for the user to complete manual login.
    Returns True when a logged-in indicator is detected.
    """
    try:
        page.goto("https://x.com/login", timeout=PAGE_LOAD_TIMEOUT)
    except Exception:
        pass

    log("Please log in to Twitter/X in the browser window (3 minutes)...")

    for sel in X_LOGGED_IN_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=LOGIN_WAIT_TIMEOUT)
            return True
        except PlaywrightTimeout:
            continue
    return False


# ---------------------------------------------------------------------------
# DM (Messages) Scraper
# ---------------------------------------------------------------------------

def scrape_dms(page) -> list[dict]:
    """
    Navigate to x.com/messages and extract recent DM conversations.
    Returns list of {sender, handle, preview, content_type}.
    """
    results = []

    try:
        with_retry(
            lambda: page.goto(X_MESSAGES_URL, timeout=PAGE_LOAD_TIMEOUT),
            label="twitter_goto_messages", max_retries=3, base_delay=2, max_delay=30,
        )
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)  # DM list can be slow to hydrate

        # Primary: Twitter data-testid conversation rows
        conv_items = page.query_selector_all(
            'div[data-testid="conversation"]'
        )

        if not conv_items:
            # Fallback 1: cell inner divs inside messages pane
            conv_items = page.query_selector_all(
                'div[data-testid="cellInnerDiv"]'
            )

        if not conv_items:
            # Fallback 2: list items in the DM sidebar
            conv_items = page.query_selector_all(
                'div[role="listitem"]'
            )

        for item in conv_items[:MAX_ITEMS_PER_SCAN]:
            try:
                raw_text = item.inner_text().strip()
                if not raw_text or len(raw_text) < 3:
                    continue

                lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]

                # Twitter DM list: line 0 = display name, line 1 = @handle or time,
                # line 2+ = message preview
                sender  = lines[0] if lines else "Twitter User"
                handle  = ""
                preview = ""

                if len(lines) > 1:
                    candidate = lines[1]
                    if candidate.startswith("@"):
                        handle  = candidate
                        preview = " ".join(lines[2:]) if len(lines) > 2 else ""
                    else:
                        preview = " ".join(lines[1:3])

                # Skip UI chrome strings
                if sender.lower() in ("messages", "new message", "requests",
                                      "message requests", "search people"):
                    continue

                results.append({
                    "sender":       sender[:80],
                    "handle":       handle[:40],
                    "preview":      preview[:400],
                    "content_type": "dm",
                })

            except Exception:
                continue

    except Exception as exc:
        log(f"WARN: Could not scrape DMs -- {exc}")
        log_watcher_error("twitter", exc, context="scrape_dms")

    return results


# ---------------------------------------------------------------------------
# Notifications Scraper
# ---------------------------------------------------------------------------

def scrape_notifications(page) -> list[dict]:
    """
    Navigate to x.com/notifications and extract recent notification text.
    Returns list of {sender, handle, preview, content_type}.
    """
    results = []

    try:
        with_retry(
            lambda: page.goto(X_NOTIFICATIONS_URL, timeout=PAGE_LOAD_TIMEOUT),
            label="twitter_goto_notifications", max_retries=3, base_delay=2, max_delay=30,
        )
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        # Notification articles
        notif_items = page.query_selector_all(
            'article[data-testid="notification"], '
            'div[data-testid="cellInnerDiv"]'
        )

        if not notif_items:
            notif_items = page.query_selector_all(
                'section[role="region"] article, '
                'div[aria-label*="Timeline"] article'
            )

        for item in notif_items[:MAX_ITEMS_PER_SCAN]:
            try:
                raw_text = item.inner_text().strip()
                if not raw_text or len(raw_text) < 5:
                    continue

                lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]
                sender  = lines[0] if lines else "Twitter"
                handle  = ""
                preview = " ".join(lines[:4])

                # Extract @handle if present
                handle_m = re.search(r'@(\w+)', raw_text)
                if handle_m:
                    handle = f"@{handle_m.group(1)}"

                results.append({
                    "sender":       sender[:80],
                    "handle":       handle[:40],
                    "preview":      preview[:400],
                    "content_type": "notification",
                })

            except Exception:
                continue

    except Exception as exc:
        log(f"WARN: Could not scrape notifications -- {exc}")
        log_watcher_error("twitter", exc, context="scrape_notifications")

    return results


# ---------------------------------------------------------------------------
# Home Feed Scraper
# ---------------------------------------------------------------------------

def scrape_home_feed(page) -> list[dict]:
    """
    Navigate to x.com/home and scan visible tweets for keywords.
    Only returns tweets that already contain a keyword match (saves matching
    work for the scan step).
    Returns list of {sender, handle, preview, content_type}.
    """
    results = []

    try:
        page.goto(X_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        tweet_articles = page.query_selector_all(
            'article[data-testid="tweet"]'
        )

        if not tweet_articles:
            tweet_articles = page.query_selector_all(
                'div[data-testid="cellInnerDiv"] article'
            )

        for article in tweet_articles[:MAX_ITEMS_PER_SCAN]:
            try:
                # Tweet text
                text_el = article.query_selector('div[data-testid="tweetText"]')
                tweet_text = text_el.inner_text().strip() if text_el else ""

                if not tweet_text:
                    continue

                # Skip early if no keyword (avoid noise)
                if not contains_keyword(tweet_text):
                    continue

                # Display name
                name_el = article.query_selector(
                    'div[data-testid="User-Name"] span, '
                    'span[data-testid="UserName"]'
                )
                sender = name_el.inner_text().strip() if name_el else "Twitter User"

                # @handle
                handle_el = article.query_selector(
                    'div[data-testid="User-Name"] a[role="link"][tabindex="-1"]'
                )
                handle = ""
                if handle_el:
                    href = handle_el.get_attribute("href") or ""
                    m = re.match(r'^/(\w+)', href)
                    if m:
                        handle = f"@{m.group(1)}"

                results.append({
                    "sender":       sender[:80],
                    "handle":       handle[:40],
                    "preview":      tweet_text[:400],
                    "content_type": "tweet",
                })

            except Exception:
                continue

    except Exception as exc:
        log(f"WARN: Could not scrape home feed -- {exc}")
        log_watcher_error("twitter", exc, context="scrape_home_feed")

    return results


# ---------------------------------------------------------------------------
# Core Scanner
# ---------------------------------------------------------------------------

def scan_items(
    items: list[dict],
    seen_keys: set,
    source_label: str,
) -> tuple[set, int]:
    """
    Check a list of {sender, handle, preview, content_type} for keyword matches.
    Write /Needs Action files for matches.
    Returns (updated_seen_keys, match_count).
    """
    new_seen = set(seen_keys)
    matches  = 0

    for item in items:
        sender       = item["sender"]
        handle       = item.get("handle", "")
        preview      = item["preview"]
        content_type = item["content_type"]

        combined = f"{sender} {handle} {preview}"
        keyword  = contains_keyword(combined)

        if not keyword:
            continue

        # Deduplicate: same sender + keyword within the same hour
        hour_key = datetime.now().strftime("%Y%m%d_%H")
        dedup    = f"twitter:{content_type}:{sender}:{keyword}:{hour_key}"
        if dedup in new_seen:
            log(f"  SKIP (already logged this hour): [{content_type}] {sender}")
            continue

        priority = priority_from_keyword(keyword)
        summary  = build_summary(sender, preview, keyword, content_type)

        filepath = save_needs_action(
            content_type = content_type,
            sender       = sender,
            handle       = handle,
            content      = preview,
            keyword      = keyword,
            priority     = priority,
            summary      = summary,
        )

        new_seen.add(dedup)
        matches += 1
        log(
            f"  MATCH [{keyword.upper()}] [{source_label}/{content_type}]"
            f" '{sender}' {handle} -> {filepath.name}"
        )

    if not matches and items:
        log(f"  No keyword matches in {len(items)} {source_label} item(s).")

    return new_seen, matches


def run_scan_cycle(page, seen_keys: set) -> set:
    """Full scan: DMs -> Notifications -> Home Feed. Returns updated seen_keys."""
    new_seen = set(seen_keys)

    # DMs
    log("Checking Twitter/X DMs (Messages)...")
    dms = scrape_dms(page)
    log(f"  Fetched {len(dms)} conversation(s).")
    new_seen, dm_hits = scan_items(dms, new_seen, "DMs")

    # Notifications
    log("Checking Twitter/X Notifications...")
    notifs = scrape_notifications(page)
    log(f"  Fetched {len(notifs)} notification(s).")
    new_seen, notif_hits = scan_items(notifs, new_seen, "Notifications")

    # Home feed (keyword-pre-filtered inside scraper)
    log("Checking Twitter/X Home Feed...")
    tweets = scrape_home_feed(page)
    log(f"  Fetched {len(tweets)} keyword-matched tweet(s).")
    new_seen, tweet_hits = scan_items(tweets, new_seen, "Home")

    total = dm_hits + notif_hits + tweet_hits
    if total:
        log(f"  Cycle total: {total} new match(es) saved to /Needs Action.")

    return new_seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  AI Employee Gold Tier - Twitter / X Watcher")
    print("=" * 60)
    print()
    log(f"BASE DIR    : {BASE_DIR}")
    log(f"SESSION     : {SESSION_DIR}")
    log(f"OUTPUT      : {NEEDS_ACTION_DIR}")
    log(f"KEYWORDS    : {', '.join(KEYWORDS)}")
    log(f"INTERVAL    : {CHECK_INTERVAL}s")
    print()

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)

    first_login = not session_exists()

    # Determine headless mode
    if HEADLESS_OVERRIDE == "0":
        headless = False
        log("HEADLESS=0 override: running with visible browser.")
    elif HEADLESS_OVERRIDE == "1":
        headless = True
        log("HEADLESS=1 override: running headless.")
    else:
        headless = not first_login  # visible on first run, headless after

    if first_login:
        log("No existing session found. Opening browser for manual login...")
        log("  Complete any 2FA or phone-verification prompts in the browser.")
    else:
        log("Existing session found. Launching in headless mode...")

    seen_keys: set = set()
    run = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir = str(SESSION_DIR),
            headless      = headless,
            args          = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport      = {"width": 1440, "height": 900},
            user_agent    = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            # Mimic a real browser to reduce bot detection
            locale        = "en-US",
            timezone_id   = "America/New_York",
        )

        page = browser.new_page()

        # Extra stealth: override navigator.webdriver
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # ── First-run: manual login ──────────────────────────────────────
        if first_login:
            if not wait_for_x_login(page):
                log("ERROR: Login not completed within 3 minutes.")
                log("Delete /session/twitter and run again to retry.")
                browser.close()
                sys.exit(1)
            log("Twitter/X login successful! Session saved.")

        # ── Existing session: validate ───────────────────────────────────
        else:
            log("Validating existing session...")
            if x_is_logged_in(page):
                log("Session valid. Starting poll loop.")
            else:
                log("WARN: Session may have expired.")
                log("Delete /session/twitter and re-run to log in again.")

        print("-" * 60)

        # ── Poll loop ────────────────────────────────────────────────────
        try:
            while True:
                run += 1
                log(f"--- Poll #{run} ---")

                try:
                    seen_keys = run_scan_cycle(page, seen_keys)
                except Exception as poll_err:
                    tb = traceback.format_exc()
                    log(f"ERROR: Poll #{run} failed — {poll_err}. Logging and continuing...")
                    err_path = log_watcher_error(
                        "twitter", poll_err,
                        context=f"run_scan_cycle poll #{run}", tb=tb,
                    )
                    log(f"  Error logged to: {err_path.name}")

                # Periodic refresh: return to home to keep session alive
                if run % SESSION_REFRESH == 0:
                    log("Session refresh: navigating to X home...")
                    try:
                        page.goto(X_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
                        page.wait_for_load_state("domcontentloaded")
                        time.sleep(2)
                    except Exception as exc:
                        log(f"WARN: Session refresh failed -- {exc}")
                        log_watcher_error("twitter", exc, context="session refresh")

                log(f"Sleeping {CHECK_INTERVAL}s until next check...")
                time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print()
            log("Shutdown requested by user.")

        except Exception as exc:
            tb = traceback.format_exc()
            log(f"FATAL ERROR: {exc}")
            log_watcher_error("twitter", exc, context="fatal error in main loop", tb=tb)
            raise

        finally:
            browser.close()
            log("Twitter/X watcher stopped.")
            print("=" * 60)


if __name__ == "__main__":
    main()
