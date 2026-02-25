"""
Social Media Executor v2
========================
Posts approved content to LinkedIn and/or Facebook using Playwright
with persistent browser sessions (no API keys required).

Usage:
    python scripts/social_media_executor_v2.py <path_to_approved_file>

Example:
    python scripts/social_media_executor_v2.py Approved/POST_001.md

Input file format (YAML frontmatter + body):
    ---
    id: POST_001
    platform: linkedin          # linkedin | facebook | both
    status: approved
    created: "2026-02-25 10:00:00"
    ---
    Your post content goes here.
    Multi-line is fine.
"""

import sys
import re
import shutil
import asyncio
import traceback
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("[FATAL] playwright is not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths (all relative to the project root, which is one level up from scripts/)
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

APPROVED_DIR = PROJECT_ROOT / "Approved"
DONE_DIR     = PROJECT_ROOT / "Done"
LOGS_DIR     = PROJECT_ROOT / "Logs"
SESSION_DIR  = PROJECT_ROOT / "session"

SESSION_LINKEDIN = SESSION_DIR / "linkedin"
SESSION_FACEBOOK = SESSION_DIR / "facebook"

MAX_RETRIES  = 3
SLOW_MO_MS   = 80   # ms between Playwright actions (helps with JS-heavy pages)
TIMEOUT_MS   = 30_000

# ---------------------------------------------------------------------------
# YAML frontmatter parser (no pyyaml dependency)
# ---------------------------------------------------------------------------
def parse_md(file_path: Path) -> tuple[dict, str]:
    """
    Returns (metadata_dict, body_text).
    Expects file to start with --- ... --- YAML block.
    """
    text = file_path.read_text(encoding="utf-8")
    meta: dict = {}
    body = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw_yaml = parts[1].strip()
            body     = parts[2].strip()
            for line in raw_yaml.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta, body

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str) -> None:
    print(f"[{ts()}] {msg}")

def error_screenshot_path(platform: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOGS_DIR / f"error_{platform}_{stamp}.png"

# ---------------------------------------------------------------------------
# LinkedIn poster
# ---------------------------------------------------------------------------
async def post_linkedin(content: str) -> None:
    """
    Opens a persistent LinkedIn session and creates a post.
    If not logged in, the browser will be visible for manual login.
    """
    SESSION_LINKEDIN.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(SESSION_LINKEDIN),
            headless=False,          # keep visible so user can log in if needed
            slow_mo=SLOW_MO_MS,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        try:
            log("[LinkedIn] Navigating to feed …")
            await page.goto("https://www.linkedin.com/feed/", timeout=TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded")

            # --- Detect login wall ---
            if "linkedin.com/login" in page.url or "linkedin.com/checkpoint" in page.url:
                log("[LinkedIn] NOT logged in — please log in manually in the browser window, then press Enter here.")
                input("  >> Press Enter once you are logged in …")
                await page.goto("https://www.linkedin.com/feed/", timeout=TIMEOUT_MS)
                await page.wait_for_load_state("domcontentloaded")

            # --- Open the post composer ---
            log("[LinkedIn] Looking for 'Start a post' button …")
            # LinkedIn uses several selectors depending on locale / A/B tests
            start_post_selectors = [
                "button.share-box-feed-entry__trigger",
                "[data-control-name='share.sharebox_trigger']",
                "div.share-box-feed-entry__top-bar button",
                "span:has-text('Start a post')",
                "button:has-text('Start a post')",
            ]
            clicked = False
            for sel in start_post_selectors:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=8_000)
                    await btn.click()
                    clicked = True
                    log(f"[LinkedIn] Clicked composer via selector: {sel}")
                    break
                except Exception:
                    continue

            if not clicked:
                raise RuntimeError("Could not find 'Start a post' button. LinkedIn layout may have changed.")

            # --- Wait for the editor ---
            await page.wait_for_timeout(1_500)
            editor_selectors = [
                "div.ql-editor[contenteditable='true']",
                "div[data-placeholder='What do you want to talk about?']",
                "div[role='textbox']",
                ".editor-container div[contenteditable='true']",
            ]
            editor = None
            for sel in editor_selectors:
                try:
                    e = page.locator(sel).first
                    await e.wait_for(state="visible", timeout=8_000)
                    editor = e
                    log(f"[LinkedIn] Editor found via: {sel}")
                    break
                except Exception:
                    continue

            if editor is None:
                raise RuntimeError("Post editor did not appear.")

            await editor.click()
            await page.keyboard.type(content, delay=30)
            log("[LinkedIn] Content typed.")

            # --- Click Post button ---
            await page.wait_for_timeout(800)
            post_btn_selectors = [
                "button.share-actions__primary-action",
                "button:has-text('Post')",
                "[data-control-name='share.post']",
            ]
            posted = False
            for sel in post_btn_selectors:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=8_000)
                    await btn.click()
                    posted = True
                    log(f"[LinkedIn] 'Post' clicked via: {sel}")
                    break
                except Exception:
                    continue

            if not posted:
                raise RuntimeError("Could not find the 'Post' submit button.")

            # --- Wait for confirmation (post disappears from modal) ---
            await page.wait_for_timeout(3_000)
            log("[LinkedIn] Post submitted successfully.")

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Facebook poster
# ---------------------------------------------------------------------------
async def post_facebook(content: str) -> None:
    """
    Opens a persistent Facebook session and creates a post.
    Multi-step: type content → Next (if shown) → Post/Share.
    """
    SESSION_FACEBOOK.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(SESSION_FACEBOOK),
            headless=False,
            slow_mo=SLOW_MO_MS,
            args=["--start-maximized"],
            no_viewport=True,
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        try:
            log("[Facebook] Navigating to home feed …")
            await page.goto("https://www.facebook.com/", timeout=TIMEOUT_MS)
            await page.wait_for_load_state("domcontentloaded")

            # --- Detect login wall ---
            if "facebook.com/login" in page.url or "facebook.com/?login" in page.url:
                log("[Facebook] NOT logged in — please log in manually in the browser window, then press Enter here.")
                input("  >> Press Enter once you are logged in …")
                await page.goto("https://www.facebook.com/", timeout=TIMEOUT_MS)
                await page.wait_for_load_state("domcontentloaded")

            # --- Click "What's on your mind?" composer area ---
            log("[Facebook] Opening post composer …")
            composer_selectors = [
                "div[aria-label=\"What's on your mind?\"]",
                "div[aria-placeholder=\"What's on your mind?\"]",
                "span:has-text(\"What's on your mind?\")",
                "[data-pagelet='FeedComposer'] div[role='button']",
                "div.x1i10hfl[role='button']",
            ]
            opened = False
            for sel in composer_selectors:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=8_000)
                    await btn.click()
                    opened = True
                    log(f"[Facebook] Composer opened via: {sel}")
                    break
                except Exception:
                    continue

            if not opened:
                raise RuntimeError("Could not open Facebook composer. Layout may have changed.")

            await page.wait_for_timeout(1_200)

            # --- Type content into the modal editor ---
            log("[Facebook] Typing content …")
            modal_editor_selectors = [
                "div[contenteditable='true'][role='textbox']",
                "div.notranslate[contenteditable='true']",
                "div[aria-multiline='true'][contenteditable='true']",
            ]
            editor = None
            for sel in modal_editor_selectors:
                try:
                    e = page.locator(sel).last   # last = the modal one (not the feed bar)
                    await e.wait_for(state="visible", timeout=8_000)
                    editor = e
                    log(f"[Facebook] Modal editor found via: {sel}")
                    break
                except Exception:
                    continue

            if editor is None:
                raise RuntimeError("Modal text editor did not appear.")

            await editor.click()
            # Use keyboard.type for reliability on React-based editors
            await page.keyboard.type(content, delay=35)
            log("[Facebook] Content typed.")

            await page.wait_for_timeout(800)

            # --- Step 1: Click "Next" button (audience selector screen) if present ---
            try:
                next_btn = page.locator("div[aria-label='Next']", ).first
                await next_btn.wait_for(state="visible", timeout=4_000)
                await next_btn.click()
                log("[Facebook] 'Next' step clicked.")
                await page.wait_for_timeout(1_000)
            except Exception:
                log("[Facebook] No 'Next' step detected — skipping to Post.")

            # --- Step 2: Click "Post" / "Share now" ---
            post_btn_selectors = [
                "div[aria-label='Post']",
                "div[aria-label='Share now']",
                "button:has-text('Post')",
                "span:has-text('Post')",
            ]
            posted = False
            for sel in post_btn_selectors:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=8_000)
                    await btn.click()
                    posted = True
                    log(f"[Facebook] 'Post' clicked via: {sel}")
                    break
                except Exception:
                    continue

            if not posted:
                raise RuntimeError("Could not find the 'Post'/'Share now' button.")

            await page.wait_for_timeout(3_000)
            log("[Facebook] Post submitted successfully.")

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Retry wrapper with screenshot on failure
# ---------------------------------------------------------------------------
async def run_with_retry(platform: str, poster_fn, content: str) -> bool:
    """
    Calls poster_fn(content) up to MAX_RETRIES times.
    On each failure, logs the error and saves a screenshot.
    Returns True on success, False if all retries exhausted.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        log(f"[{platform.upper()}] Attempt {attempt}/{MAX_RETRIES} …")
        try:
            await poster_fn(content)
            return True
        except Exception as exc:
            log(f"[{platform.upper()}] Attempt {attempt} FAILED: {exc}")
            traceback.print_exc()
            # Screenshot is taken inside the poster functions on fatal errors,
            # but we also log a text record here for safety.
            err_log = LOGS_DIR / f"error_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            err_log.write_text(
                f"Platform : {platform}\n"
                f"Attempt  : {attempt}/{MAX_RETRIES}\n"
                f"Time     : {ts()}\n"
                f"Error    : {exc}\n\n"
                + traceback.format_exc(),
                encoding="utf-8",
            )
            log(f"[{platform.upper()}] Error logged → {err_log.name}")
            if attempt < MAX_RETRIES:
                wait = 5 * attempt  # 5s, 10s
                log(f"[{platform.upper()}] Waiting {wait}s before retry …")
                await asyncio.sleep(wait)

    log(f"[{platform.upper()}] All {MAX_RETRIES} attempts failed.")
    return False


# ---------------------------------------------------------------------------
# Playwright screenshot helper (called inside poster functions on crash)
# ---------------------------------------------------------------------------
async def safe_screenshot(page, platform: str) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = error_screenshot_path(platform)
        await page.screenshot(path=str(path), full_page=True)
        log(f"[{platform.upper()}] Screenshot saved → {path.name}")
    except Exception as e:
        log(f"[{platform.upper()}] Could not save screenshot: {e}")


# ---------------------------------------------------------------------------
# File management
# ---------------------------------------------------------------------------
def move_to_done(src: Path) -> None:
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    dest = DONE_DIR / src.name
    # Avoid collision
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = DONE_DIR / f"{src.stem}_{stamp}{src.suffix}"
    shutil.move(str(src), str(dest))
    log(f"[FILE] Moved to Done/ → {dest.name}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print("ERROR: No file path provided.")
        print("Usage: python scripts/social_media_executor_v2.py <path_to_approved_file>")
        sys.exit(1)

    file_arg = Path(sys.argv[1])
    # Support both absolute and project-relative paths
    if not file_arg.is_absolute():
        file_arg = PROJECT_ROOT / file_arg

    if not file_arg.exists():
        log(f"[ERROR] File not found: {file_arg}")
        sys.exit(1)

    log(f"[MAIN] Reading: {file_arg}")
    meta, content = parse_md(file_arg)

    if not content.strip():
        log("[ERROR] Post content is empty. Aborting.")
        sys.exit(1)

    platform = meta.get("platform", "").lower().strip()
    post_id   = meta.get("id", file_arg.stem)

    log(f"[MAIN] Post ID  : {post_id}")
    log(f"[MAIN] Platform : {platform}")
    log(f"[MAIN] Content  :\n{'-'*40}\n{content[:300]}{'…' if len(content) > 300 else ''}\n{'-'*40}")

    if platform not in ("linkedin", "facebook", "both"):
        log(f"[ERROR] Unknown platform '{platform}'. Must be: linkedin | facebook | both")
        sys.exit(1)

    results: dict[str, bool] = {}

    if platform in ("linkedin", "both"):
        ok = await run_with_retry("linkedin", post_linkedin, content)
        results["linkedin"] = ok

    if platform in ("facebook", "both"):
        ok = await run_with_retry("facebook", post_facebook, content)
        results["facebook"] = ok

    # Move to Done only if ALL targeted platforms succeeded
    all_ok = all(results.values())
    if all_ok:
        move_to_done(file_arg)
        log("[MAIN] All posts published. File moved to Done/.")
    else:
        failed = [p for p, ok in results.items() if not ok]
        log(f"[MAIN] PARTIAL FAILURE — failed platforms: {failed}. File remains in Approved/ for retry.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
