"""
AI Employee Gold Tier - Skill 5: Cross Domain Integrator
=========================================================

Unifies personal (Gmail, WhatsApp) and business (LinkedIn, Twitter, Facebook)
channels in one processing flow. Reads /Needs Action/, classifies each item as
personal or business, routes accordingly, and writes a unified cross-domain log.

ROUTING:
  Personal (email / WhatsApp)            -> /Pending Approval/ (HITL gate)
  Business (LinkedIn / Twitter / Facebook) -> LinkedIn post draft -> /Plans/ -> /Pending Approval/

LOG:
  /Logs/cross_domain_[YYYY-MM-DD].md

MODES:
  (default)     Process all .md files in /Needs Action
  --dry-run     Classify and report only - no files written
  --file PATH   Process a single specific file

RUN:
  python Skills/cross_domain_integrator.py
  python Skills/cross_domain_integrator.py --dry-run
  python Skills/cross_domain_integrator.py --file "Needs Action/LINKEDIN_lead_2026-02-19.md"

PM2:
  pm2 start Skills/cross_domain_integrator.py --interpreter python --name cross-domain
  pm2 save

DEPENDENCIES (no additional installs beyond existing Silver/Gold Tier stack):
  - Reads Company Handbook.md for rules
  - Writes to /Pending Approval/, /Plans/, /Logs/
  - Uses same YAML frontmatter pattern as all other skills
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
from audit_logger import log_action  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR             = Path(__file__).resolve().parent.parent
NEEDS_ACTION_DIR     = BASE_DIR / "Needs Action"
PLANS_DIR            = BASE_DIR / "Plans"
PENDING_DIR          = BASE_DIR / "Pending Approval"
LOGS_DIR             = BASE_DIR / "Logs"
HANDBOOK_FILE        = BASE_DIR / "Company Handbook.md"

# File prefixes set by watchers -> domain mapping
PERSONAL_PREFIXES = ("email_", "whatsapp_", "gmail_", "msg_")
BUSINESS_PREFIXES = ("linkedin_", "twitter_", "facebook_", "fb_", "tw_")

# YAML type values -> domain mapping
PERSONAL_TYPES = {"email", "whatsapp_message", "gmail", "sms", "message"}
BUSINESS_TYPES = {"linkedin_lead", "linkedin_post", "twitter_dm", "facebook_message",
                  "facebook_lead", "lead", "sales_lead"}

# Keyword scoring for fallback classification
PERSONAL_KEYWORDS = [
    "personal", "family", "friend", "home", "vacation", "appointment",
    "health", "doctor", "birthday", "dinner", "school", "kids",
]
BUSINESS_KEYWORDS = [
    "sales", "client", "project", "lead", "proposal", "partnership",
    "revenue", "contract", "deal", "business", "invoice", "quote",
    "service", "opportunity", "collaboration",
]

# LinkedIn post template (mirrors Skill 3)
POST_TEMPLATE = (
    "Excited to offer {service} for {benefit}!\n"
    "If you are looking for {outcome}, I would love to connect.\n"
    "DM me to get started - always happy to help.\n\n"
    "#{tag1} #Business #{tag2}"
)

KEYWORD_CONTEXT = {
    "sales":    ("sales strategy and business development",
                 "growing your revenue", "a reliable sales partner",
                 "Sales", "Growth"),
    "client":   ("professional client management",
                 "building stronger client relationships",
                 "a dedicated and responsive partner",
                 "ClientSuccess", "Networking"),
    "project":  ("end-to-end project execution",
                 "delivering your project on time and on budget",
                 "expert project leadership",
                 "ProjectManagement", "Delivery"),
    "lead":     ("lead generation and outreach",
                 "connecting you with qualified prospects",
                 "consistent pipeline growth",
                 "LeadGen", "Growth"),
    "proposal": ("tailored business proposals",
                 "winning the right clients",
                 "a well-crafted proposal",
                 "BusinessDev", "Proposals"),
}
DEFAULT_CONTEXT = KEYWORD_CONTEXT["sales"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def ensure_dirs(dry_run: bool = False) -> None:
    if dry_run:
        return
    for d in [PLANS_DIR, PENDING_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_handbook() -> str:
    if HANDBOOK_FILE.exists():
        try:
            return HANDBOOK_FILE.read_text(encoding="utf-8").strip()
        except Exception as exc:
            log(f"WARN: Could not read Company Handbook - {exc}. Using defaults.")
    else:
        log(f"WARN: Company Handbook not found at {HANDBOOK_FILE}. Using defaults.")
    return "Always be polite. Flag payments > $500 for approval."


def extract_yaml_field(content: str, field: str) -> str:
    pattern = rf'^{re.escape(field)}:\s*["\']?(.+?)["\']?\s*$'
    m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def contains_payment_flag(text: str) -> bool:
    """Return True if text contains a monetary amount > $500 (Handbook rule)."""
    amounts = re.findall(r'\$\s?(\d[\d,]*\.?\d*)', text)
    for amt in amounts:
        try:
            if float(amt.replace(",", "")) > 500:
                return True
        except ValueError:
            continue
    return False


def first_business_keyword(text: str) -> str:
    """Return the first matched business keyword, or 'sales' as default."""
    text_lower = text.lower()
    for kw in BUSINESS_KEYWORDS:
        if kw in text_lower:
            return kw
    return "sales"


def safe_slug(text: str, max_len: int = 20) -> str:
    """Make a filesystem-safe slug from arbitrary text."""
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', text).strip('_').lower()
    return slug[:max_len] if slug else "item"


def unique_path(directory: Path, filename: str) -> Path:
    """Return a path that does not already exist by appending a counter."""
    target = directory / filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_item(filename: str, content: str, yaml_type: str) -> tuple[str, str]:
    """
    Return (domain, reason) where domain is 'personal' or 'business'.

    Priority:
      1. File prefix   (most reliable - set by watchers)
      2. YAML type field
      3. Keyword scoring (fallback)
      4. Default -> 'personal' (safer for HITL)
    """
    fname_lower = filename.lower()

    # 1. File prefix
    for prefix in PERSONAL_PREFIXES:
        if fname_lower.startswith(prefix):
            return "personal", f"prefix match ({prefix}*)"
    for prefix in BUSINESS_PREFIXES:
        if fname_lower.startswith(prefix):
            return "business", f"prefix match ({prefix}*)"

    # 2. YAML type field
    t = yaml_type.lower().strip()
    if t in PERSONAL_TYPES:
        return "personal", f"YAML type={t}"
    if t in BUSINESS_TYPES:
        return "business", f"YAML type={t}"

    # 3. Keyword scoring
    content_lower = content.lower()
    personal_score = sum(1 for kw in PERSONAL_KEYWORDS if kw in content_lower)
    business_score = sum(1 for kw in BUSINESS_KEYWORDS if kw in content_lower)

    if business_score > personal_score:
        return "business", f"keyword score (biz={business_score} > personal={personal_score})"
    if personal_score > business_score:
        return "personal", f"keyword score (personal={personal_score} > biz={business_score})"

    # 4. Default - safer to HITL
    return "personal", "no clear signal -> defaulted to personal (HITL)"


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan_needs_action(specific_file: Path | None = None) -> list[dict]:
    """Return list of item dicts from /Needs Action."""
    items = []

    if specific_file:
        files = [specific_file] if specific_file.exists() else []
        if not files:
            log(f"ERROR: Specified file not found: {specific_file}")
            return []
    else:
        if not NEEDS_ACTION_DIR.exists():
            log(f"ERROR: /Needs Action not found: {NEEDS_ACTION_DIR}")
            return []
        files = [
            f for f in NEEDS_ACTION_DIR.glob("*.md")
            # Skip system/metadata files written by filesystem_watcher
            if not f.name.startswith("FILE_") and not f.name.endswith("_metadata.md")
        ]

    if not files:
        return []

    log(f"Scanning {len(files)} file(s) in /Needs Action...")

    for f in sorted(files):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            yaml_type = extract_yaml_field(content, "type")
            sender    = extract_yaml_field(content, "from") or "unknown sender"
            subject   = extract_yaml_field(content, "subject") or f.stem
            priority  = extract_yaml_field(content, "priority") or "medium"

            domain, reason = classify_item(f.name, content, yaml_type)

            items.append({
                "filename":  f.name,
                "filepath":  f,
                "content":   content,
                "type":      yaml_type,
                "from":      sender,
                "subject":   subject,
                "priority":  priority,
                "domain":    domain,
                "reason":    reason,
                "payment_flag": contains_payment_flag(content),
            })

            log(f"  [CLASSIFY] {f.name} -> {domain.upper()}  ({reason})")

        except Exception as exc:
            log(f"  ERROR reading {f.name}: {exc}")

    return items


# ---------------------------------------------------------------------------
# Router A: Personal -> HITL Pending Approval
# ---------------------------------------------------------------------------

def route_personal_to_hitl(item: dict, dry_run: bool = False) -> dict:
    """
    Write a pending approval request for a personal item.
    Returns a result dict summarising the action.
    """
    ts_str   = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug     = safe_slug(item["subject"])
    filename = f"personal_{ts_str}_{slug}.md"

    # Detect channel from file prefix / YAML type
    fname_lower = item["filename"].lower()
    if fname_lower.startswith("whatsapp_") or "whatsapp" in item["type"].lower():
        channel = "whatsapp"
    else:
        channel = "email"

    # Escalate priority if payment flag or urgency keywords
    priority = item["priority"]
    urgency_words = ["urgent", "asap", "emergency", "immediately", "critical"]
    if item["payment_flag"] or any(w in item["content"].lower() for w in urgency_words):
        priority = "high"

    details = (
        f"Personal {channel} from {item['from']}: {item['subject']}"
    )[:120]

    content = f"""---
type: generic
action: review
source_skill: Skill5_CrossDomainIntegrator
domain: personal
channel: {channel}
details: "{details}"
target: "{item['from']}"
priority: {priority}
status: pending_approval
created: "{_iso()}"
draft_file: "Needs Action/{item['filename']}"
payment_flag: {"true" if item["payment_flag"] else "false"}
---

# Pending Approval: Personal {channel.title()} Message

| Field       | Value |
|-------------|-------|
| Domain      | personal |
| Channel     | {channel} |
| From        | {item['from']} |
| Subject     | {item['subject']} |
| Priority    | {priority.upper()} |
| Status      | pending_approval |
| Created     | {_iso()} |
| Source File | Needs Action/{item['filename']} |
{"| ⚠ Payment  | Amount > $500 detected - review carefully |" if item["payment_flag"] else ""}

## Message Preview

> {item['content'][:300].strip()}

## HITL Instructions

1. Review the message content above.
2. Decide what action to take (reply, ignore, escalate).
3. **Move this file to `/Approved/`** to confirm you have reviewed it.
4. **Move this file to `/Rejected/`** to dismiss without action.
5. The HITL Handler will log your decision automatically.

## Checklist

- [ ] Read the original message: `Needs Action/{item['filename']}`
- [ ] Check if a reply is needed
- [ ] Verify no sensitive data leaks
- [ ] Move to /Approved or /Rejected
"""

    pending_path = unique_path(PENDING_DIR, filename)

    if not dry_run:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(content, encoding="utf-8")

    log(f"  PERSONAL -> HITL | {'[DRY RUN] ' if dry_run else ''}{pending_path.name}")

    return {
        "domain":        "personal",
        "channel":       channel,
        "source":        item["filename"],
        "sender":        item["from"],
        "priority":      priority,
        "pending_path":  pending_path,
        "dry_run":       dry_run,
    }


# ---------------------------------------------------------------------------
# Router B: Business -> LinkedIn Post Draft
# ---------------------------------------------------------------------------

def _draft_linkedin_post(item: dict) -> str:
    """Compose a LinkedIn post from the business item context."""
    keyword = first_business_keyword(item["content"])
    context = KEYWORD_CONTEXT.get(keyword, DEFAULT_CONTEXT)
    service, benefit, outcome, tag1, tag2 = context

    subject_clean = re.sub(
        r'^(re:|fwd:|linkedin message|email alert|facebook|twitter)[:\s]*',
        '', item["subject"], flags=re.IGNORECASE
    ).strip()
    if subject_clean and len(subject_clean) > 5:
        service = subject_clean

    return POST_TEMPLATE.format(
        service=service, benefit=benefit,
        outcome=outcome, tag1=tag1, tag2=tag2,
    )


def route_business_to_linkedin(item: dict, dry_run: bool = False) -> dict:
    """
    Draft a LinkedIn post for a business item, save to /Plans, copy to /Pending Approval.
    Returns a result dict.
    """
    today  = _date()
    ts     = _iso()
    slug   = safe_slug(item["subject"])

    # Detect channel
    fname_lower = item["filename"].lower()
    if fname_lower.startswith("twitter_") or "twitter" in item["type"].lower():
        channel = "twitter"
    elif fname_lower.startswith("facebook_") or fname_lower.startswith("fb_") or "facebook" in item["type"].lower():
        channel = "facebook"
    else:
        channel = "linkedin"

    keyword    = first_business_keyword(item["content"])
    post_text  = _draft_linkedin_post(item)
    plan_name  = f"linkedin_post_{today}_{slug}.md"
    plan_path  = unique_path(PLANS_DIR, plan_name)

    # Handbook: payment flag -> always escalate (still queues, but priority=high)
    priority = "high" if item["payment_flag"] else item.get("priority", "medium")

    plan_content = f"""---
type: linkedin_post_draft
source_lead: "{item['filename']}"
channel: {channel}
contact: "{item['from']}"
keyword_matched: "{keyword}"
domain: business
content: "{post_text.replace(chr(10), ' ').replace(chr(34), chr(39))}"
priority: {priority}
status: pending_approval
created: "{ts}"
source_skill: Skill5_CrossDomainIntegrator
payment_flag: {"true" if item["payment_flag"] else "false"}
---

# LinkedIn Post Draft - {today}

## Drafted Post

{post_text}

## Source Item

- File    : Needs Action/{item['filename']}
- Channel : {channel}
- From    : {item['from']}
- Subject : {item['subject']}
- Keyword : `{keyword}`
{"- ⚠ **Payment flag**: Amount > $500 detected - review before posting." if item["payment_flag"] else ""}

## Checklist

- [ ] Review tone against Company Handbook (always polite)
- [ ] Confirm no payment amounts > $500 referenced in post
- [ ] Approve and move to /Approved
- [ ] LinkedIn Watcher or HITL Handler will publish on approval
"""

    pending_name = plan_path.name  # same filename in /Pending Approval
    pending_path = unique_path(PENDING_DIR, pending_name)

    if not dry_run:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan_content, encoding="utf-8")
        shutil.copy2(plan_path, pending_path)

    log(f"  BUSINESS -> LINKEDIN POSTER | {'[DRY RUN] ' if dry_run else ''}Draft: {plan_path.name}")

    return {
        "domain":        "business",
        "channel":       channel,
        "source":        item["filename"],
        "keyword":       keyword,
        "plan_path":     plan_path,
        "pending_path":  pending_path,
        "dry_run":       dry_run,
    }


# ---------------------------------------------------------------------------
# Unified Log Writer
# ---------------------------------------------------------------------------

def write_cross_domain_log(
    personal_results: list[dict],
    business_results: list[dict],
    unclassified: list[dict],
    dry_run: bool = False,
) -> Path:
    """Write the unified cross-domain summary to /Logs/cross_domain_[date].md."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_name = f"cross_domain_{_date()}.md"
    log_path = unique_path(LOGS_DIR, log_name)

    now = _iso()
    p_count = len(personal_results)
    b_count = len(business_results)
    total   = p_count + b_count + len(unclassified)

    lines = [
        f"---",
        f'type: cross_domain_log',
        f'date: "{_date()}"',
        f"personal_count: {p_count}",
        f"business_count: {b_count}",
        f"total_processed: {total}",
        f'generated: "{now}"',
        f"dry_run: {'true' if dry_run else 'false'}",
        f"---",
        f"",
        f"# Cross Domain Integrator - {now}",
        f"",
        f"## Summary",
        f"",
        f"| Domain       | Items | Routed To                        |",
        f"|--------------|-------|----------------------------------|",
        f"| Personal     | {p_count:<5} | HITL -> /Pending Approval/        |",
        f"| Business     | {b_count:<5} | Auto LinkedIn Poster -> /Plans/   |",
        f"| Unclassified | {len(unclassified):<5} | Defaulted to HITL (personal)     |",
        f"| **Total**    | {total:<5} |                                  |",
        f"",
    ]

    # Personal section
    lines += [
        "## Personal Items (-> HITL /Pending Approval/)",
        "",
        "| File | Channel | From | Priority | Pending Path |",
        "|------|---------|------|----------|--------------|",
    ]
    for r in personal_results:
        pending_rel = str(r["pending_path"]).replace(str(BASE_DIR), "").lstrip("/\\")
        dry_tag = " [DRY RUN]" if r.get("dry_run") else ""
        lines.append(
            f"| {r['source'][:35]} | {r['channel']} | {r['sender'][:20]} "
            f"| {r['priority']} | {pending_rel[:45]}{dry_tag} |"
        )
    if not personal_results:
        lines.append("| *(none)* | - | - | - | - |")
    lines.append("")

    # Business section
    lines += [
        "## Business Items (-> Auto LinkedIn Poster / /Plans/)",
        "",
        "| File | Channel | Keyword | Draft Path | Pending Path |",
        "|------|---------|---------|------------|--------------|",
    ]
    for r in business_results:
        plan_rel    = str(r["plan_path"]).replace(str(BASE_DIR), "").lstrip("/\\")
        pending_rel = str(r["pending_path"]).replace(str(BASE_DIR), "").lstrip("/\\")
        dry_tag = " [DRY RUN]" if r.get("dry_run") else ""
        lines.append(
            f"| {r['source'][:30]} | {r['channel']} | {r['keyword']} "
            f"| {plan_rel[:35]} | {pending_rel[:35]}{dry_tag} |"
        )
    if not business_results:
        lines.append("| *(none)* | - | - | - | - |")
    lines.append("")

    # Unclassified section
    if unclassified:
        lines += [
            "## Unclassified Items (defaulted to HITL)",
            "",
            "| File | Reason | Action Taken |",
            "|------|--------|--------------|",
        ]
        for item in unclassified:
            lines.append(
                f"| {item['filename'][:40]} | {item['reason'][:40]} | Routed to HITL as personal |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        f"*Generated by Skill 5: Cross Domain Integrator - Gold Tier AI Employee*",
        f"*Run at: {now}{'  (DRY RUN - no files written)' if dry_run else ''}*",
    ]

    content = "\n".join(lines) + "\n"

    if not dry_run:
        log_path.write_text(content, encoding="utf-8")
        log(f"LOG written: {log_path}")
    else:
        log(f"[DRY RUN] Log would be written to: {log_path}")

    return log_path


# ---------------------------------------------------------------------------
# Core Skill Runner
# ---------------------------------------------------------------------------

def run_skill(specific_file: Path | None = None, dry_run: bool = False) -> None:
    """Main entry point: classify -> route -> log. Wraps inner with error recovery."""
    try:
        _run_skill_inner(specific_file, dry_run)
    except Exception as exc:
        tb = traceback.format_exc()
        log(f"SKILL ERROR: {exc}")
        log_action("skill_error", "Skill5_CrossDomainIntegrator",
                   target=str(specific_file or ""),
                   parameters={"exception": type(exc).__name__, "dry_run": dry_run},
                   result=f"failed: {exc}")
        err_path = log_skill_error(
            "Skill5_CrossDomainIntegrator", exc,
            context=f"specific_file={specific_file}, dry_run={dry_run}", tb=tb,
        )
        log(f"  Error logged: {err_path}")
        if not dry_run:
            plan_path = write_manual_action_plan(
                skill_name="Skill5_CrossDomainIntegrator",
                action_description=(
                    "Manually classify items in /Needs Action/ as personal or business. "
                    "Route personal items to /Pending Approval/ and business items to /Plans/."
                ),
                context=f"Error: {exc}",
                error=str(exc),
            )
            log(f"  Manual action plan: {plan_path}")


def _run_skill_inner(specific_file: Path | None = None, dry_run: bool = False) -> None:
    """Inner implementation of Cross Domain Integrator — wrapped by run_skill."""
    log_action("skill_start", "Skill5_CrossDomainIntegrator",
               target=str(specific_file or ""),
               parameters={"dry_run": dry_run})

    print("=" * 60)
    print("  Skill 5: Cross Domain Integrator")
    print("=" * 60)
    if dry_run:
        print("  MODE: DRY RUN - no files will be written")
        print("=" * 60)
    print()
    log(f"BASE DIR         : {BASE_DIR}")
    log(f"NEEDS ACTION     : {NEEDS_ACTION_DIR}")
    log(f"PLANS            : {PLANS_DIR}")
    log(f"PENDING APPROVAL : {PENDING_DIR}")
    log(f"LOGS             : {LOGS_DIR}")
    log(f"HANDBOOK         : {HANDBOOK_FILE}")
    print()

    ensure_dirs(dry_run)

    # Step 1 - Load handbook
    log("Step 1: Loading Company Handbook rules...")
    handbook_text = load_handbook()
    log(f"  Rules: {handbook_text[:80]}...")
    print()

    # Step 2 - Scan
    log("Step 2: Scanning /Needs Action for items...")
    items = scan_needs_action(specific_file)

    if not items:
        print()
        print("NO ITEMS: /Needs Action is empty or contains no processable .md files.")
        print("=" * 60)
        return

    log(f"  {len(items)} item(s) found.")
    print()

    # Step 3 - Route
    log("Step 3: Routing items by domain...")
    personal_results: list[dict] = []
    business_results: list[dict] = []
    unclassified:     list[dict] = []

    for item in items:
        print("-" * 40)
        log(f"Processing: {item['filename']}  [{item['domain'].upper()}]")

        if item["domain"] == "personal":
            result = route_personal_to_hitl(item, dry_run=dry_run)
            personal_results.append(result)
            log_action("item_routed", "Skill5_CrossDomainIntegrator",
                       target=item["filename"],
                       parameters={"domain": "personal", "channel": result.get("channel", ""),
                                    "priority": result.get("priority", "medium"),
                                    "dry_run": dry_run},
                       approval_status="pending",
                       result="routed to HITL")

            # Track items that had no strong signal (defaulted)
            if "defaulted" in item["reason"]:
                unclassified.append(item)

        else:  # business
            result = route_business_to_linkedin(item, dry_run=dry_run)
            business_results.append(result)
            log_action("item_routed", "Skill5_CrossDomainIntegrator",
                       target=item["filename"],
                       parameters={"domain": "business", "channel": result.get("channel", ""),
                                    "keyword": result.get("keyword", ""), "dry_run": dry_run},
                       approval_status="pending",
                       result="routed to LinkedIn poster")

    print()

    # Step 4 - Write unified log
    log("Step 4: Writing unified cross-domain log...")
    log_path = write_cross_domain_log(
        personal_results, business_results, unclassified, dry_run=dry_run
    )

    # Step 5 - Output success
    p_count = len(personal_results)
    b_count = len(business_results)
    total   = p_count + b_count

    log_action("skill_complete", "Skill5_CrossDomainIntegrator",
               parameters={"personal": p_count, "business": b_count,
                            "total": total, "dry_run": dry_run},
               result=f"success — {total} items routed")

    print()
    print("=" * 60)
    print("  CROSS DOMAIN INTEGRATOR COMPLETE")
    print("=" * 60)
    print(f"  Personal Items : {p_count} -> /Pending Approval/ (HITL)")
    print(f"  Business Items : {b_count} -> /Plans/ + /Pending Approval/ (Auto LinkedIn Poster)")
    print(f"  Total Processed: {total}")
    print(f"  Log Path       : {log_path.relative_to(BASE_DIR)}")
    if dry_run:
        print("  [DRY RUN - no files were written]")
    print("=" * 60)
    print()

    if p_count:
        print("Next Steps - Personal:")
        print("  Review /Pending Approval/ -> move to /Approved or /Rejected")
        print("  HITL Handler (Skill 4) will log and execute on approval.")
        print()
    if b_count:
        print("Next Steps - Business:")
        print("  HITL Handler will auto-execute approved LinkedIn post drafts.")
        print("  Or run: python Skills/hitl_approval_handler.py --monitor")
    print()


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cross-domain-integrator",
        description="Skill 5: Cross Domain Integrator - Gold Tier AI Employee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python Skills/cross_domain_integrator.py
  python Skills/cross_domain_integrator.py --dry-run
  python Skills/cross_domain_integrator.py --file "Needs Action/LINKEDIN_lead_2026-02-19.md"
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Classify and report only - no files are written.",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        metavar="PATH",
        help="Process a single specific .md file instead of all of /Needs Action.",
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
