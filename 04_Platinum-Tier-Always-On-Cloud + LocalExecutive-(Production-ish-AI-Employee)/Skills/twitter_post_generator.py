"""
AI Employee Gold Tier -- Skill 7: Twitter Post Generator
=========================================================

Processes Twitter/X items from /Needs Action/ (TWITTER_*.md), generates
an enhanced summary, scores them as business leads, drafts a public tweet
(max 280 chars) + a DM reply for high/medium leads, and routes the draft
to /Pending Approval/ for HITL review before anything is published.

INPUT  : /Needs Action/TWITTER_*.md
         (written by watchers/twitter_watcher.py)
OUTPUT : /Plans/twitter_draft_[date]_[sender].md
         /Pending Approval/twitter_draft_[date]_[sender].md

MODES:
  (default)    Process all TWITTER_* files in /Needs Action
  --file PATH  Process a single specific file
  --dry-run    Summarise and score only -- no files written

RUN:
  python Skills/twitter_post_generator.py
  python Skills/twitter_post_generator.py --dry-run
  python Skills/twitter_post_generator.py --file "Needs Action/TWITTER_20260222_JohnDoe.md"

PM2:
  pm2 start Skills/twitter_post_generator.py --interpreter python --name twitter-generator
  pm2 save

DEPENDENCIES:
  No additional installs. Reads Company Handbook.md for tone/payment rules.
"""

import re
import sys
import shutil
import argparse
import traceback
from datetime import datetime
from pathlib import Path

# --- Gold Tier Error Recovery ---
_SKILLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SKILLS_DIR))
from error_recovery import log_skill_error, write_manual_action_plan  # noqa: E402
from audit_logger import log_action  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR         = Path(__file__).resolve().parent.parent
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
PLANS_DIR        = BASE_DIR / "Plans"
PENDING_DIR      = BASE_DIR / "Pending Approval"
LOGS_DIR         = BASE_DIR / "Logs"
HANDBOOK_FILE    = BASE_DIR / "Company Handbook.md"

TWEET_MAX_CHARS  = 280          # Twitter character limit
TWEET_URL_CHARS  = 23           # Twitter wraps all URLs to t.co (23 chars)

# Business context per keyword (same taxonomy as Skills 3, 5, 6)
KEYWORD_CONTEXT = {
    "sales": (
        "sales strategy",           # service (short -- tweet budget)
        "growing your revenue",     # benefit
        "a reliable growth partner",# outcome
        "Sales", "Growth",          # hashtag1, hashtag2
    ),
    "client": (
        "client management",
        "stronger client relationships",
        "a responsive partner",
        "ClientSuccess", "Networking",
    ),
    "project": (
        "project delivery",
        "delivering on time",
        "expert project leadership",
        "ProjectManagement", "Delivery",
    ),
}
DEFAULT_CONTEXT = KEYWORD_CONTEXT["sales"]

URGENCY_WORDS = [
    "urgent", "asap", "immediately", "critical",
    "emergency", "right now", "today", "deadline",
]
POSITIVE_WORDS = [
    "interested", "love to", "looking for", "would like",
    "great", "excited", "keen", "please", "appreciate", "thank",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_handbook() -> str:
    if HANDBOOK_FILE.exists():
        try:
            return HANDBOOK_FILE.read_text(encoding="utf-8").strip()
        except Exception as exc:
            log(f"WARN: Could not read Company Handbook -- {exc}. Using defaults.")
    return "Always be polite. Flag payments > $500 for approval."


def extract_yaml_field(content: str, field: str) -> str:
    pattern = rf'^{re.escape(field)}:\s*["\']?(.+?)["\']?\s*$'
    m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def contains_payment_flag(text: str) -> bool:
    """Return True if text contains a dollar amount above $500."""
    for amt in re.findall(r'\$\s?(\d[\d,]*\.?\d*)', text):
        try:
            if float(amt.replace(",", "")) > 500:
                return True
        except ValueError:
            continue
    return False


def unique_path(directory: Path, filename: str) -> Path:
    target  = directory / filename
    stem    = Path(filename).stem
    suffix  = Path(filename).suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def safe_slug(text: str, max_len: int = 25) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return slug[:max_len] if slug else "item"


def count_tweet_chars(text: str) -> int:
    """
    Approximate Twitter character count: URLs count as TWEET_URL_CHARS each,
    everything else is counted by Unicode code-point (CJK chars count as 2).
    """
    # Replace URLs with a placeholder of TWEET_URL_CHARS length
    cleaned = re.sub(r'https?://\S+', 'x' * TWEET_URL_CHARS, text)
    # CJK and similar wide characters count as 2
    count = 0
    for ch in cleaned:
        count += 2 if ord(ch) > 0x2E7F else 1
    return count


def trim_to_tweet_limit(text: str, hard_limit: int = TWEET_MAX_CHARS) -> str:
    """
    Shorten text until it fits within hard_limit Twitter characters.
    Removes trailing words one at a time, then appends '...'.
    """
    if count_tweet_chars(text) <= hard_limit:
        return text

    words = text.split()
    while words:
        candidate = " ".join(words) + "..."
        if count_tweet_chars(candidate) <= hard_limit:
            return candidate
        words.pop()

    # Fallback: hard slice
    return text[: hard_limit - 3] + "..."


def extract_content_preview(content: str) -> str:
    """Extract the text under the content preview heading in the source .md."""
    for heading in [
        "## Full Content Preview",
        "## Content Preview",
        "## Message Preview",
        "## AI Summary",
    ]:
        idx = content.find(heading)
        if idx != -1:
            block = content[idx + len(heading):].strip()
            block = re.sub(r"^```.*?\n", "", block, flags=re.DOTALL)
            block = re.sub(r"```\s*$", "", block, flags=re.DOTALL)
            block = re.sub(r"^> ?", "", block.strip(), flags=re.MULTILINE)
            return block.strip()
    return content[:500]


# ---------------------------------------------------------------------------
# Source File Scanner
# ---------------------------------------------------------------------------

def scan_twitter_files(specific_file: Path | None = None) -> list[dict]:
    """
    Scan /Needs Action for TWITTER_*.md files.
    Returns list of parsed item dicts.
    """
    if specific_file:
        files = [specific_file] if specific_file.exists() else []
        if not files:
            log(f"ERROR: File not found: {specific_file}")
            return []
    else:
        if not NEEDS_ACTION_DIR.exists():
            log(f"ERROR: /Needs Action not found: {NEEDS_ACTION_DIR}")
            return []
        files = [
            f for f in sorted(NEEDS_ACTION_DIR.glob("*.md"))
            if f.name.upper().startswith("TWITTER_")
        ]

    if not files:
        return []

    log(f"Found {len(files)} Twitter file(s) in /Needs Action.")
    items = []

    for f in files:
        try:
            content      = f.read_text(encoding="utf-8", errors="replace")
            content_type = extract_yaml_field(content, "content_type") or "dm"
            sender       = extract_yaml_field(content, "from") or "Twitter User"
            handle       = extract_yaml_field(content, "handle") or ""
            keyword      = extract_yaml_field(content, "keyword_matched") or "sales"
            priority     = extract_yaml_field(content, "priority") or "medium"
            summary_pre  = extract_yaml_field(content, "summary") or ""
            preview      = extract_content_preview(content)

            items.append({
                "filename":     f.name,
                "filepath":     f,
                "content":      content,
                "preview":      preview,
                "content_type": content_type,
                "sender":       sender,
                "handle":       handle,
                "keyword":      keyword,
                "priority":     priority,
                "summary_pre":  summary_pre,
                "payment_flag": contains_payment_flag(content),
            })

            log(
                f"  Loaded: {f.name}  "
                f"[{content_type}]  keyword={keyword}  handle={handle}"
            )

        except Exception as exc:
            log(f"  ERROR reading {f.name}: {exc}")

    return items


# ---------------------------------------------------------------------------
# Enhanced Summary Generator
# ---------------------------------------------------------------------------

def infer_sentiment(text: str) -> str:
    tl = text.lower()
    if any(w in tl for w in URGENCY_WORDS):
        return "urgent"
    if any(w in tl for w in POSITIVE_WORDS):
        return "positive"
    return "neutral"


def score_lead(keyword: str, sentiment: str, content_type: str) -> str:
    if keyword in ("sales", "client"):
        return "high"
    if keyword == "project" and sentiment in ("positive", "urgent"):
        return "medium"
    return "low"


def generate_enhanced_summary(item: dict) -> dict:
    """
    Build the 4-part enhanced summary. Returns a result dict.
    """
    text       = item["preview"] or item["summary_pre"]
    sentiment  = infer_sentiment(text)
    lead_score = score_lead(item["keyword"], sentiment, item["content_type"])

    # Payment flag always forces high priority
    if item["payment_flag"]:
        lead_score = "high"
        sentiment  = "urgent"

    # Build excerpt: up to 2 sentences
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    excerpt   = " ".join(sentences[:2]).strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:297] + "..."

    # Character count estimate for tweet (computed later after draft built)
    summary_text = (
        f"Platform   : Twitter / X\n"
        f"Type       : {item['content_type'].title()}\n"
        f"From       : {item['sender']}\n"
        f"Handle     : {item['handle']}\n"
        f"Keyword    : {item['keyword']}\n"
        f"Excerpt    : {excerpt}\n"
        f"Sentiment  : {sentiment}\n"
        f"Lead Score : {lead_score.upper()}"
    )

    return {
        "summary_text": summary_text,
        "sentiment":    sentiment,
        "lead_score":   lead_score,
        "excerpt":      excerpt,
    }


# ---------------------------------------------------------------------------
# Tweet Draft Builder
# ---------------------------------------------------------------------------

def build_tweet_draft(item: dict, summary: dict) -> tuple[str, int]:
    """
    Build a public tweet draft that fits within 280 characters.
    Returns (tweet_text, char_count).
    """
    keyword = item["keyword"]
    ctx     = KEYWORD_CONTEXT.get(keyword, DEFAULT_CONTEXT)
    service, benefit, outcome, tag1, tag2 = ctx

    # Assemble tweet using the short service label (already brief for tweet budget)
    raw_tweet = (
        f"Excited to offer {service} for {benefit}!\n"
        f"Looking for {outcome}? Let's connect.\n"
        f"DM me to get started! #{tag1} #Business"
    )

    # Trim if needed
    tweet = trim_to_tweet_limit(raw_tweet)
    chars = count_tweet_chars(tweet)

    return tweet, chars


# ---------------------------------------------------------------------------
# DM Reply Draft Builder
# ---------------------------------------------------------------------------

def build_dm_reply(item: dict, summary: dict) -> str:
    """
    Build a warm, polite DM reply. Only generated for DM content_type items.
    No character limit -- DMs on Twitter allow up to 10,000 chars.
    """
    keyword  = item["keyword"]
    ctx      = KEYWORD_CONTEXT.get(keyword, DEFAULT_CONTEXT)
    service  = ctx[0]  # Full service description for DMs

    # Use first name only
    first_name = item["sender"].split()[0] if item["sender"] else "there"

    platform_greeting = "your Twitter/X message"
    if item["content_type"] == "dm":
        platform_greeting = "your DM"
    elif item["content_type"] == "tweet":
        platform_greeting = "your tweet"
    elif item["content_type"] == "notification":
        platform_greeting = "your mention"

    return (
        f"Hi {first_name}! Thank you for {platform_greeting}.\n\n"
        f"I would love to help you with {service}.\n"
        f"Could we set up a quick call to discuss your specific needs?\n\n"
        f"Feel free to DM me anytime -- I am always happy to connect and explore\n"
        f"how we can work together.\n\n"
        f"Looking forward to hearing from you!"
    )


# ---------------------------------------------------------------------------
# File Writers
# ---------------------------------------------------------------------------

def write_plan_draft(
    item:       dict,
    summary:    dict,
    tweet_text: str,
    tweet_chars: int,
    dm_text:    str,
    dry_run:    bool = False,
) -> Path:
    """Save the draft to /Plans/twitter_draft_[date]_[sender-slug].md."""
    today       = _date()
    ts          = _iso()
    slug        = safe_slug(item["sender"])
    filename    = f"twitter_draft_{today}_{slug}.md"
    plan_path   = unique_path(PLANS_DIR, filename)

    lead_score  = summary["lead_score"]
    has_tweet   = bool(tweet_text)
    has_dm      = bool(dm_text)
    priority    = (
        "high"
        if item["payment_flag"] or lead_score == "high"
        else item["priority"]
    )

    handle_clean = item["handle"].lstrip("@") if item["handle"] else ""

    plan_content = f"""---
type: twitter_draft
platform: twitter
content_type: {item['content_type']}
source_file: "Needs Action/{item['filename']}"
from: "{item['sender']}"
handle: "{item['handle']}"
keyword_matched: "{item['keyword']}"
lead_score: {lead_score}
sentiment: {summary['sentiment']}
has_tweet_draft: {"true" if has_tweet else "false"}
has_dm_draft: {"true" if has_dm else "false"}
tweet_char_count: {tweet_chars}
priority: {priority}
status: pending_approval
created: "{ts}"
source_skill: Skill7_TwitterPostGenerator
payment_flag: {"true" if item['payment_flag'] else "false"}
---

# Twitter Draft: {item['content_type'].title()} from {item['sender']}

Generated: {ts}

## Enhanced Summary

```
{summary['summary_text']}
Char Count : {tweet_chars} / {TWEET_MAX_CHARS}
```

{"## Public Tweet Draft (" + str(tweet_chars) + "/" + str(TWEET_MAX_CHARS) + " chars)" if has_tweet else "## No Tweet Draft (Lead Score: LOW)"}

{"```" if has_tweet else ""}
{tweet_text if has_tweet else "_Lead score is LOW. No tweet drafted. Review and decide manually._"}
{"```" if has_tweet else ""}

{"## DM Reply Draft" if has_dm else ""}

{dm_text if has_dm else ""}

## Source Information

- **File**        : Needs Action/{item['filename']}
- **Platform**    : Twitter / X
- **From**        : {item['sender']}
- **Handle**      : {item['handle'] or "(unknown)"}
- **Type**        : {item['content_type'].title()}
- **Keyword**     : `{item['keyword']}`
- **Lead Score**  : {lead_score.upper()}
- **Sentiment**   : {summary['sentiment']}
- **Tweet chars** : {tweet_chars}/{TWEET_MAX_CHARS}
{"- **PAYMENT FLAG** : Amount > $500 detected -- review carefully before approving." if item['payment_flag'] else ""}

## HITL Instructions

1. Review the tweet and DM drafts above.
2. Edit them directly in this file if needed (open in any text editor).
3. **Move this file to `/Approved/`** -- Skill 4 (HITL Handler) will log the approval.
4. **Move this file to `/Rejected/`** -- to archive without posting.
5. NOTE: Actual publishing to Twitter/X must be done manually or via the Twitter API.
   The HITL Handler logs the approval; it does not auto-post to Twitter.

## Checklist

- [ ] Verify tweet is within 280 chars (current: {tweet_chars})
- [ ] Check tone against Handbook (polite, no aggressive sales language)
- [ ] Confirm no personal data or payment amounts exposed in public tweet
- [ ] Edit DM reply for personalisation if needed
- [ ] Move to /Approved or /Rejected
"""

    if not dry_run:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan_content, encoding="utf-8")

    return plan_path


def copy_to_pending(plan_path: Path, dry_run: bool = False) -> Path:
    """Copy draft to /Pending Approval. Returns destination path."""
    pending_path = unique_path(PENDING_DIR, plan_path.name)
    if not dry_run:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan_path, pending_path)
    return pending_path


# ---------------------------------------------------------------------------
# Core Skill Runner
# ---------------------------------------------------------------------------

def process_item(item: dict, dry_run: bool = False) -> dict | None:
    """
    Full pipeline for one Twitter item:
      load -> summarise -> score -> draft -> save -> HITL gate.
    """
    log(f"  Processing: {item['filename']}")
    print("-" * 40)

    # Enhanced summary
    summary    = generate_enhanced_summary(item)
    lead_score = summary["lead_score"]

    log(f"  Lead Score : {lead_score.upper()}")
    log(f"  Sentiment  : {summary['sentiment']}")
    log(f"  Type       : {item['content_type']}")

    # Draft based on lead score
    tweet_text  = ""
    tweet_chars = 0
    dm_text     = ""

    if lead_score in ("high", "medium"):
        log("  Lead qualifies -- drafting tweet + DM reply...")

        tweet_text, tweet_chars = build_tweet_draft(item, summary)
        log(f"  Tweet draft : {tweet_chars}/{TWEET_MAX_CHARS} chars")

        # Only draft DM reply for actual DM items (not tweets/notifications)
        if item["content_type"] == "dm":
            dm_text = build_dm_reply(item, summary)
            log(f"  DM reply    : {len(dm_text)} chars (no limit)")
        else:
            log(f"  DM reply    : skipped (type={item['content_type']}, tweet/notif only)")
    else:
        log("  Lead score LOW -- summary only, no drafts created.")

    if item["payment_flag"]:
        log("  HANDBOOK FLAG: Payment > $500 detected -- priority escalated to HIGH.")

    # Save to /Plans
    plan_path = write_plan_draft(
        item, summary, tweet_text, tweet_chars, dm_text, dry_run=dry_run
    )
    if dry_run:
        log(f"  [DRY RUN] Draft would be saved: {plan_path.name}")
    else:
        log(f"  Draft saved : {plan_path.name}")

    # HITL gate
    pending_path = copy_to_pending(plan_path, dry_run=dry_run)
    if dry_run:
        log(f"  [DRY RUN] Would queue: {pending_path.name}")
    else:
        log(f"  HITL queued : {pending_path.name}")

    return {
        "filename":     item["filename"],
        "content_type": item["content_type"],
        "sender":       item["sender"],
        "handle":       item["handle"],
        "lead_score":   lead_score,
        "tweet_chars":  tweet_chars,
        "has_tweet":    bool(tweet_text),
        "has_dm":       bool(dm_text),
        "plan_path":    plan_path,
        "pending_path": pending_path,
        "dry_run":      dry_run,
    }


def run_skill(specific_file: Path | None = None, dry_run: bool = False) -> None:
    """Main entry point with error recovery wrapper."""
    try:
        _run_skill_inner(specific_file, dry_run)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[ERROR] Skill 7 failed: {exc}", flush=True)
        log_action("skill_error", "Skill7_TwitterPostGenerator",
                   target=str(specific_file or ""),
                   parameters={"exception": type(exc).__name__, "dry_run": dry_run},
                   result=f"failed: {exc}")
        err_path = log_skill_error(
            "Skill7_TwitterPostGenerator", exc,
            context=f"specific_file={specific_file}, dry_run={dry_run}", tb=tb,
        )
        print(f"  Error logged: {err_path}", flush=True)
        if not dry_run:
            plan_path = write_manual_action_plan(
                skill_name="Skill7_TwitterPostGenerator",
                action_description=(
                    "Manually review Twitter items in /Needs Action/, draft a tweet reply "
                    "(max 280 chars), and place it in /Pending Approval/ for HITL review."
                ),
                context=f"Error: {exc}",
                error=str(exc),
            )
            print(f"  Manual action plan: {plan_path}", flush=True)


def _run_skill_inner(specific_file: Path | None = None, dry_run: bool = False) -> None:
    """Inner implementation of Twitter Post Generator."""
    log_action("skill_start", "Skill7_TwitterPostGenerator",
               target=str(specific_file or ""),
               parameters={"dry_run": dry_run})

    print("=" * 60)
    print("  Skill 7: Twitter Post Generator")
    print("=" * 60)
    if dry_run:
        print("  MODE: DRY RUN - no files will be written")
    print("=" * 60)
    print()

    log(f"BASE DIR         : {BASE_DIR}")
    log(f"NEEDS ACTION     : {NEEDS_ACTION_DIR}")
    log(f"PLANS            : {PLANS_DIR}")
    log(f"PENDING APPROVAL : {PENDING_DIR}")
    log(f"HANDBOOK         : {HANDBOOK_FILE}")
    print()

    # Step 1: Handbook
    log("Step 1: Loading Company Handbook rules...")
    handbook = load_handbook()
    log(f"  Rules: {handbook[:80]}...")
    print()

    # Step 2: Scan
    log("Step 2: Scanning /Needs Action for Twitter files...")
    items = scan_twitter_files(specific_file)

    if not items:
        print()
        print("NO TWITTER ITEMS: No TWITTER_*.md files found in /Needs Action.")
        print("  Ensure twitter_watcher.py is running and has detected messages")
        print("  containing: " + ", ".join(["sales", "client", "project"]))
        print("=" * 60)
        return

    log(f"  {len(items)} item(s) to process.")
    print()

    # Steps 3-6: Process
    log("Step 3-6: Summarising, scoring, drafting, and routing...")
    results = []

    for item in items:
        try:
            result = process_item(item, dry_run=dry_run)
            if result:
                results.append(result)
                log_action("item_drafted", "Skill7_TwitterPostGenerator",
                           target=item["filename"],
                           parameters={"content_type": item["content_type"],
                                        "lead_score": result["lead_score"],
                                        "tweet_chars": result["tweet_chars"],
                                        "handle": item.get("handle", ""),
                                        "dry_run": dry_run},
                           approval_status="pending",
                           result=f"lead_score={result['lead_score']}")
        except Exception as exc:
            log(f"  ERROR processing {item['filename']}: {exc}")

    # Step 7: Summary output
    log_action("skill_complete", "Skill7_TwitterPostGenerator",
               parameters={"items_processed": len(results), "dry_run": dry_run},
               result=f"success — {len(results)} item(s) drafted")

    print()
    print("=" * 60)
    print("  TWITTER POST GENERATOR COMPLETE")
    print("=" * 60)

    for r in results:
        plan_rel    = str(r["plan_path"]).replace(str(BASE_DIR), "").lstrip("/\\")
        pending_rel = str(r["pending_path"]).replace(str(BASE_DIR), "").lstrip("/\\")
        dry_tag     = "  [DRY RUN]" if r.get("dry_run") else ""
        char_str    = f"{r['tweet_chars']}/{TWEET_MAX_CHARS}" if r["has_tweet"] else "N/A"

        print(f"Platform    : Twitter / X")
        print(f"Source      : /Needs Action/{r['filename']}")
        print(f"Type        : {r['content_type'].title()}")
        print(f"Handle      : {r['handle'] or '(unknown)'}")
        print(f"Lead Score  : {r['lead_score'].upper()}")
        print(f"Tweet chars : {char_str}")
        print(f"Has Tweet   : {'Yes' if r['has_tweet'] else 'No'}")
        print(f"Has DM      : {'Yes' if r['has_dm'] else 'No'}")
        print(f"Draft       : {plan_rel}{dry_tag}")
        print(f"Pending     : {pending_rel}{dry_tag}")
        print(f"Status      : Awaiting human approval (HITL)")
        print(f"Next Step   : Review /Pending Approval/ -> move to /Approved to publish")
        print("-" * 60)

    print()
    if not dry_run and results:
        print("HITL next steps:")
        print("  Monitor: python Skills/hitl_approval_handler.py --monitor")
        print("  Check  : python Skills/hitl_approval_handler.py --check")
    print("=" * 60)
    print()


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="twitter-post-generator",
        description="Skill 7: Twitter Post Generator -- Gold Tier AI Employee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python Skills/twitter_post_generator.py
  python Skills/twitter_post_generator.py --dry-run
  python Skills/twitter_post_generator.py --file "Needs Action/TWITTER_20260222_JohnDoe.md"
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Summarise and score only -- no files written.",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        metavar="PATH",
        help="Process a single specific TWITTER_*.md file.",
    )
    args = parser.parse_args()

    specific_file = None
    if args.file:
        specific_file = Path(args.file)
        if not specific_file.is_absolute():
            specific_file = BASE_DIR / specific_file

    run_skill(specific_file=specific_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
