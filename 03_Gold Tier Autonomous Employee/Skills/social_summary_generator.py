"""
AI Employee Gold Tier -- Skill 6: Social Summary Generator
===========================================================

Processes Facebook and Instagram items from /Needs Action/, generates
an enhanced summary, scores them as business leads, drafts a reply and
outbound post for high/medium leads, and routes to /Pending Approval/
for HITL review.

INPUT  : /Needs Action/FACEBOOK_*.md, INSTAGRAM_*.md
         (written by watchers/facebook_instagram_watcher.py)
OUTPUT : /Plans/facebook_draft_[date].md
         /Pending Approval/facebook_draft_[date].md
LOG    : Printed to console; use Skill 4 HITL handler for event logging

MODES:
  (default)    Process all FB/IG files in /Needs Action
  --file PATH  Process a single specific file
  --dry-run    Summarise and score only -- no files written

RUN:
  python Skills/social_summary_generator.py
  python Skills/social_summary_generator.py --dry-run
  python Skills/social_summary_generator.py --file "Needs Action/FACEBOOK_20260222_JohnDoe.md"

PM2:
  pm2 start Skills/social_summary_generator.py --interpreter python --name social-summary
  pm2 save

DEPENDENCIES:
  No additional installs beyond the existing stack.
  Reads Company Handbook.md for tone/payment rules.
"""

import re
import sys
import shutil
import argparse
import traceback
from datetime import datetime
from pathlib import Path

# --- Gold Tier Error Recovery ---
from error_recovery import log_skill_error, write_manual_action_plan  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR         = Path(__file__).resolve().parent.parent
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
PLANS_DIR        = BASE_DIR / "Plans"
PENDING_DIR      = BASE_DIR / "Pending Approval"
LOGS_DIR         = BASE_DIR / "Logs"
HANDBOOK_FILE    = BASE_DIR / "Company Handbook.md"

# Keyword -> business context mapping (mirrors Skill 3 / Skill 5)
KEYWORD_CONTEXT = {
    "sales": (
        "sales strategy and business development",
        "growing your revenue",
        "a reliable growth partner",
        "Sales", "Growth",
    ),
    "client": (
        "dedicated client management",
        "building exceptional client relationships",
        "a responsive and reliable partner",
        "ClientSuccess", "Networking",
    ),
    "project": (
        "end-to-end project execution",
        "delivering your project on time",
        "expert project leadership",
        "ProjectManagement", "Delivery",
    ),
}
DEFAULT_CONTEXT = KEYWORD_CONTEXT["sales"]

# Urgency words used for sentiment inference
URGENCY_WORDS = [
    "urgent", "asap", "immediately", "critical", "emergency",
    "right now", "today", "deadline", "overdue",
]
POSITIVE_WORDS = [
    "interested", "love to", "looking for", "would like", "great",
    "excited", "keen", "please", "appreciate", "thank",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


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
    amounts = re.findall(r'\$\s?(\d[\d,]*\.?\d*)', text)
    for amt in amounts:
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


def extract_content_preview(content: str) -> str:
    """
    Extract the text under '## Full Content Preview' or 'Message Preview'
    heading in the source .md file. Falls back to full content if not found.
    """
    for heading in ["## Full Content Preview", "## Content Preview", "## Message Preview"]:
        idx = content.find(heading)
        if idx != -1:
            block = content[idx + len(heading):].strip()
            # Strip code fences
            block = re.sub(r"^```.*?\n", "", block, flags=re.DOTALL)
            block = re.sub(r"```\s*$", "", block, flags=re.DOTALL)
            block = block.strip()
            # Remove YAML-style > blockquote markers
            block = re.sub(r"^> ?", "", block, flags=re.MULTILINE)
            return block.strip()
    return content[:500]


# ---------------------------------------------------------------------------
# Source File Scanner
# ---------------------------------------------------------------------------

def scan_social_files(specific_file: Path | None = None) -> list[dict]:
    """
    Scan /Needs Action for FACEBOOK_* and INSTAGRAM_* .md files.
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
            if f.name.upper().startswith("FACEBOOK_")
            or f.name.upper().startswith("INSTAGRAM_")
        ]

    if not files:
        return []

    log(f"Found {len(files)} social file(s) in /Needs Action.")
    items = []

    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")

            platform     = extract_yaml_field(content, "platform") or (
                "facebook" if f.name.upper().startswith("FACEBOOK_") else "instagram"
            )
            content_type = extract_yaml_field(content, "content_type") or "message"
            sender       = extract_yaml_field(content, "from") or "Unknown"
            keyword      = extract_yaml_field(content, "keyword_matched") or "sales"
            summary_pre  = extract_yaml_field(content, "summary") or ""
            priority     = extract_yaml_field(content, "priority") or "medium"
            preview      = extract_content_preview(content)

            items.append({
                "filename":     f.name,
                "filepath":     f,
                "content":      content,
                "preview":      preview,
                "platform":     platform,
                "content_type": content_type,
                "sender":       sender,
                "keyword":      keyword,
                "summary_pre":  summary_pre,
                "priority":     priority,
                "payment_flag": contains_payment_flag(content),
            })

            log(f"  Loaded: {f.name}  [{platform}/{content_type}]  keyword={keyword}")

        except Exception as exc:
            log(f"  ERROR reading {f.name}: {exc}")

    return items


# ---------------------------------------------------------------------------
# Summary Generator
# ---------------------------------------------------------------------------

def infer_sentiment(text: str) -> str:
    """Return 'urgent', 'positive', or 'neutral' based on keyword presence."""
    text_lower = text.lower()
    if any(w in text_lower for w in URGENCY_WORDS):
        return "urgent"
    if any(w in text_lower for w in POSITIVE_WORDS):
        return "positive"
    return "neutral"


def score_lead(keyword: str, sentiment: str) -> str:
    """Return 'high', 'medium', or 'low' lead score."""
    if keyword in ("sales", "client"):
        return "high"
    if keyword == "project" and sentiment in ("positive", "urgent"):
        return "medium"
    if keyword == "project":
        return "low"
    return "low"


def generate_enhanced_summary(item: dict) -> dict:
    """
    Produce the 3-part enhanced summary dict for an item.
    Returns {summary_text, sentiment, lead_score}.
    """
    text      = item["preview"] or item["summary_pre"]
    sentiment = infer_sentiment(text)
    lead_score = score_lead(item["keyword"], sentiment)

    # Escalate if payment flag
    if item["payment_flag"]:
        lead_score = "high"
        sentiment  = "urgent"

    # Build excerpt: up to 2 sentences from the preview
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    excerpt   = " ".join(sentences[:2]).strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:277] + "..."

    summary_text = (
        f"Platform  : {item['platform'].title()}\n"
        f"Type      : {item['content_type'].title()}\n"
        f"From      : {item['sender']}\n"
        f"Keyword   : {item['keyword']}\n"
        f"Excerpt   : {excerpt}\n"
        f"Sentiment : {sentiment}\n"
        f"Lead Score: {lead_score.upper()}"
    )

    return {
        "summary_text": summary_text,
        "sentiment":    sentiment,
        "lead_score":   lead_score,
        "excerpt":      excerpt,
    }


# ---------------------------------------------------------------------------
# Draft Generator
# ---------------------------------------------------------------------------

def draft_reply(item: dict, summary: dict) -> str:
    """
    Compose a warm, personalised reply message to the sender.
    Applied Handbook rule: always polite, soft call-to-action.
    """
    keyword  = item["keyword"]
    sender   = item["sender"].split()[0] if item["sender"] else "there"  # first name
    ctx      = KEYWORD_CONTEXT.get(keyword, DEFAULT_CONTEXT)
    service  = ctx[0]

    # Personalise with subject if it adds context
    platform_label = "Facebook" if item["platform"] == "facebook" else "Instagram"

    return (
        f"Hi {sender}! Thank you for reaching out via {platform_label}.\n\n"
        f"I would love to help you with {service}.\n"
        f"Could we set up a quick call to discuss your specific needs?\n\n"
        f"Feel free to DM me anytime -- I am always happy to connect and explore\n"
        f"how we can work together.\n\n"
        f"Looking forward to hearing from you!"
    )


def draft_post(item: dict, summary: dict) -> str:
    """
    Compose an outbound social media post (Facebook/Instagram feed).
    Follows same template and Handbook rules as Skill 3 / Skill 5.
    """
    keyword  = item["keyword"]
    ctx      = KEYWORD_CONTEXT.get(keyword, DEFAULT_CONTEXT)
    service, benefit, outcome, tag1, tag2 = ctx

    # Personalise service from the excerpt when it is short and clean.
    # Use the 2-sentence excerpt from the summary (already cleaned) rather
    # than the raw summary_pre which may contain noisy prefix text.
    excerpt = summary.get("excerpt", "")
    # Strip common watcher-injected prefixes like "[FACEBOOK MESSAGE] from X: "
    excerpt_clean = re.sub(
        r'^\[.*?\]\s*from\s+[^:]+:\s*', "", excerpt, flags=re.IGNORECASE
    ).strip()
    # Strip remaining connector words
    excerpt_clean = re.sub(
        r'^(re:|fwd:|hi[,!]?\s+\w+[,!]?\s*)', "", excerpt_clean, flags=re.IGNORECASE
    ).strip()
    # Only use the excerpt-derived subject when it is short and meaningful
    if excerpt_clean and 8 < len(excerpt_clean) <= 80:
        service = excerpt_clean[:80]

    platform_tag = "#Facebook" if item["platform"] == "facebook" else "#Instagram"

    return (
        f"Excited to offer {service} for {benefit}!\n"
        f"If you are looking for {outcome}, I would love to connect.\n"
        f"DM me to get started -- always happy to help.\n\n"
        f"#{tag1} #Business {platform_tag} #{tag2}"
    )


# ---------------------------------------------------------------------------
# File Writers
# ---------------------------------------------------------------------------

def write_plan_draft(item: dict, summary: dict,
                     reply_text: str, post_text: str,
                     dry_run: bool = False) -> Path:
    """
    Write the combined summary + drafts to /Plans/facebook_draft_[date].md.
    Returns the path (real or simulated for dry-run).
    """
    today    = _date()
    ts       = _iso()
    slug     = safe_slug(item["sender"])
    filename = f"facebook_draft_{today}_{slug}.md"
    plan_path = unique_path(PLANS_DIR, filename)

    has_response = bool(reply_text)
    lead_score   = summary["lead_score"]

    plan_content = f"""---
type: facebook_draft
platform: {item['platform']}
content_type: {item['content_type']}
source_file: "Needs Action/{item['filename']}"
from: "{item['sender']}"
keyword_matched: "{item['keyword']}"
lead_score: {lead_score}
sentiment: {summary['sentiment']}
has_response_draft: {"true" if has_response else "false"}
priority: {"high" if item['payment_flag'] or lead_score == "high" else item['priority']}
status: pending_approval
created: "{ts}"
source_skill: Skill6_SocialSummaryGenerator
payment_flag: {"true" if item['payment_flag'] else "false"}
---

# Social Draft: {item['platform'].title()} -- {today}

## Enhanced Summary

```
{summary['summary_text']}
```

{"## Response Draft (Reply to Sender)" if has_response else "## Summary Only (Low Lead Score)"}

{reply_text if has_response else "_No response drafted -- lead score is LOW. Review and decide action manually._"}

{"## Post Draft (Outbound Feed Post)" if post_text else ""}

{post_text if post_text else ""}

## Source Information

- **File**       : Needs Action/{item['filename']}
- **Platform**   : {item['platform'].title()}
- **From**       : {item['sender']}
- **Keyword**    : `{item['keyword']}`
- **Lead Score** : {lead_score.upper()}
- **Sentiment**  : {summary['sentiment']}
{"- **PAYMENT FLAG** : Amount > $500 detected -- review carefully before approving." if item['payment_flag'] else ""}

## HITL Instructions

1. Review the summary and drafted content above.
2. Edit the response or post draft if needed (open this file in any editor).
3. **Move this file to `/Approved/`** to confirm for sending/posting.
4. **Move this file to `/Rejected/`** to discard.
5. Skill 4 (HITL Handler) will log your decision automatically.

## Checklist

- [ ] Review tone against Company Handbook (always polite, no aggressive sales language)
- [ ] Verify response is appropriate and accurate for the sender
- [ ] Confirm no confidential data exposed in the post draft
- [ ] Check: no payment amounts > $500 referenced publicly
- [ ] Move to /Approved or /Rejected
"""

    if not dry_run:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan_content, encoding="utf-8")

    return plan_path


def copy_to_pending(plan_path: Path, dry_run: bool = False) -> Path:
    """Copy the draft to /Pending Approval. Returns the destination path."""
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
    Full pipeline for one social item:
      summarise -> score -> draft -> save -> HITL gate.
    Returns result dict or None on failure.
    """
    log(f"  Processing: {item['filename']}")
    print("-" * 40)

    # Step 3: Enhanced summary
    summary = generate_enhanced_summary(item)
    log(f"  Lead Score : {summary['lead_score'].upper()}")
    log(f"  Sentiment  : {summary['sentiment']}")

    # Step 4: Route by lead score
    if summary["lead_score"] in ("high", "medium"):
        log("  Drafting reply + post (lead score qualifies)...")
        reply_text = draft_reply(item, summary)
        post_text  = draft_post(item, summary)
    else:
        log("  Lead score LOW -- summary only, no response drafted.")
        reply_text = ""
        post_text  = ""

    # Payment flag escalation
    if item["payment_flag"]:
        log("  HANDBOOK FLAG: Payment > $500 detected -- priority elevated to HIGH.")

    # Step 5: Save to /Plans
    plan_path = write_plan_draft(item, summary, reply_text, post_text, dry_run)
    if dry_run:
        log(f"  [DRY RUN] Draft would be saved to: {plan_path.name}")
    else:
        log(f"  Draft saved: {plan_path.name}")

    # Step 6: HITL gate -- copy to /Pending Approval
    pending_path = copy_to_pending(plan_path, dry_run)
    if dry_run:
        log(f"  [DRY RUN] Would copy to: {pending_path.name}")
    else:
        log(f"  Queued for HITL: {pending_path.name}")

    return {
        "filename":     item["filename"],
        "platform":     item["platform"],
        "sender":       item["sender"],
        "lead_score":   summary["lead_score"],
        "has_response": bool(reply_text),
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
        print(f"[ERROR] Skill 6 failed: {exc}", flush=True)
        err_path = log_skill_error(
            "Skill6_SocialSummaryGenerator", exc,
            context=f"specific_file={specific_file}, dry_run={dry_run}", tb=tb,
        )
        print(f"  Error logged: {err_path}", flush=True)
        if not dry_run:
            plan_path = write_manual_action_plan(
                skill_name="Skill6_SocialSummaryGenerator",
                action_description=(
                    "Manually review Facebook/Instagram items in /Needs Action/, "
                    "draft a response, and place it in /Pending Approval/ for HITL review."
                ),
                context=f"Error: {exc}",
                error=str(exc),
            )
            print(f"  Manual action plan: {plan_path}", flush=True)


def _run_skill_inner(specific_file: Path | None = None, dry_run: bool = False) -> None:
    """Inner implementation of Social Summary Generator."""

    print("=" * 60)
    print("  Skill 6: Social Summary Generator")
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
    log("Step 2: Scanning /Needs Action for Facebook/Instagram files...")
    items = scan_social_files(specific_file)

    if not items:
        print()
        print("NO SOCIAL ITEMS: No Facebook or Instagram files found in /Needs Action.")
        print("  Make sure facebook_instagram_watcher.py is running and has detected")
        print("  messages/posts containing: " + ", ".join(["sales", "client", "project"]))
        print("=" * 60)
        return

    log(f"  {len(items)} item(s) to process.")
    print()

    # Steps 3-6: Process each item
    log("Step 3-6: Summarising, scoring, drafting, and routing...")
    results = []

    for item in items:
        try:
            result = process_item(item, dry_run=dry_run)
            if result:
                results.append(result)
        except Exception as exc:
            log(f"  ERROR processing {item['filename']}: {exc}")

    # Step 7: Summary output
    print()
    print("=" * 60)
    print("  SOCIAL SUMMARY GENERATOR COMPLETE")
    print("=" * 60)

    for r in results:
        plan_rel    = str(r["plan_path"]).replace(str(BASE_DIR), "").lstrip("/\\")
        pending_rel = str(r["pending_path"]).replace(str(BASE_DIR), "").lstrip("/\\")
        dry_tag     = "  [DRY RUN]" if r.get("dry_run") else ""

        print(f"Platform     : {r['platform'].title()}")
        print(f"Source       : /Needs Action/{r['filename']}")
        print(f"Lead Score   : {r['lead_score'].upper()}")
        print(f"Has Response : {'Yes (reply + post drafted)' if r['has_response'] else 'No (summary only)'}")
        print(f"Draft Saved  : {plan_rel}{dry_tag}")
        print(f"Pending Path : {pending_rel}{dry_tag}")
        print(f"Status       : Awaiting human approval (HITL)")
        print(f"Next Step    : Review /Pending Approval/ -> move to /Approved to publish")
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
        prog="social-summary-generator",
        description="Skill 6: Social Summary Generator -- Gold Tier AI Employee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python Skills/social_summary_generator.py
  python Skills/social_summary_generator.py --dry-run
  python Skills/social_summary_generator.py --file "Needs Action/FACEBOOK_20260222_JohnDoe.md"
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
        help="Process a single specific .md file.",
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
