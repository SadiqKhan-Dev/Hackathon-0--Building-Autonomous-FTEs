"""
AI Employee Gold Tier - Skill 8: Weekly Audit Briefer
======================================================

Runs every Sunday (via daily scheduler) to audit the previous week's activity.
Reads /Done/, /Logs/, Company_Handbook.md, Business_Goals.md, and bottleneck
folders (/Needs Action/, /Rejected/, /Pending Approval/) to produce a
CEO-ready briefing at /Briefings/ceo_briefing_[YYYY-MM-DD].md.

PATTERN MATCHING:
  Revenue  : dollar amounts near keywords (invoice, payment, sale, earned, revenue)
  Expenses : dollar amounts near keywords (subscription, fee, cost, expense, renewal)
  Bottlenecks : stale files, rejections, pending backlogs

RUN:
  python Skills/weekly_audit_briefer.py
  python Skills/weekly_audit_briefer.py --dry-run
  python Skills/weekly_audit_briefer.py --date 2026-02-16

PM2:
  pm2 start Skills/weekly_audit_briefer.py --interpreter python --name weekly-audit
  pm2 save

SCHEDULER:
  Called by daily_scheduler.sh / daily_scheduler.ps1 when day-of-week == Sunday.
"""

import re
import sys
import argparse
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# Ensure UTF-8 output on Windows (handles special chars like warning symbols)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --- Gold Tier Error Recovery ---
from error_recovery import log_skill_error, write_manual_action_plan  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR         = Path(__file__).resolve().parent.parent
DONE_DIR         = BASE_DIR / "Done"
LOGS_DIR         = BASE_DIR / "Logs"
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
REJECTED_DIR     = BASE_DIR / "Rejected"
PENDING_DIR      = BASE_DIR / "Pending Approval"
BRIEFINGS_DIR    = BASE_DIR / "Briefings"
HANDBOOK_FILE    = BASE_DIR / "Company Handbook.md"
GOALS_FILE       = BASE_DIR / "Business_Goals.md"

PAYMENT_THRESHOLD = 500.0  # from Company Handbook

# Revenue-adjacent keywords (case-insensitive)
REVENUE_KEYWORDS = [
    "invoice", "payment received", "sale", "earned", "revenue",
    "income", "paid", "billing", "commission", "profit",
]

# Expense-adjacent keywords (case-insensitive)
EXPENSE_KEYWORDS = [
    "subscription", "renewal", "monthly fee", "annual plan", "cost",
    "expense", "fee", "charge", "bill", "spend", "purchase",
]

# Pattern: dollar amounts like $1,200.50 or $99 or $ 350
DOLLAR_RE = re.compile(r'\$\s?([\d,]+\.?\d*)')

# Bottleneck: files older than these thresholds
STALE_NEEDS_ACTION_HOURS = 48
STALE_PENDING_HOURS      = 24


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _date_str(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d")


def log(msg: str) -> None:
    print(f"[{_now().strftime('%H:%M:%S')}] {msg}", flush=True)


def unique_path(directory: Path, filename: str) -> Path:
    """Return a path that does not already exist by appending _N counter."""
    target = directory / filename
    stem   = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def extract_yaml_field(content: str, field: str) -> str:
    """Extract a single YAML frontmatter field value (simple regex, no YAML parser needed)."""
    pattern = rf'^{re.escape(field)}:\s*["\']?(.+?)["\']?\s*$'
    m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_amounts(text: str) -> list[float]:
    """Return all dollar amounts found in text as floats."""
    amounts = []
    for raw in DOLLAR_RE.findall(text):
        try:
            amounts.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return amounts


def has_keyword_near_amount(text: str, keywords: list[str]) -> list[tuple[float, str]]:
    """
    Return list of (amount, keyword) tuples where a dollar amount appears
    within 120 characters of any keyword in the list.
    """
    results = []
    text_lower = text.lower()
    for m in DOLLAR_RE.finditer(text):
        start = max(0, m.start() - 120)
        end   = min(len(text), m.end() + 120)
        window = text_lower[start:end]
        for kw in keywords:
            if kw in window:
                try:
                    amount = float(m.group(1).replace(",", ""))
                    results.append((amount, kw))
                except ValueError:
                    pass
                break  # first matching keyword wins per amount
    return results


def file_age_hours(path: Path) -> float:
    """Return file age in hours based on mtime."""
    try:
        mtime = path.stat().st_mtime
        return (_now().timestamp() - mtime) / 3600
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Context Loaders
# ---------------------------------------------------------------------------

def load_handbook() -> str:
    if HANDBOOK_FILE.exists():
        try:
            return HANDBOOK_FILE.read_text(encoding="utf-8").strip()
        except Exception as exc:
            log(f"WARN: Could not read Company Handbook — {exc}. Using defaults.")
    else:
        log(f"WARN: Company Handbook not found at {HANDBOOK_FILE}. Using defaults.")
    return "Always be polite. Flag payments > $500 for approval."


def load_goals() -> str:
    if GOALS_FILE.exists():
        try:
            return GOALS_FILE.read_text(encoding="utf-8").strip()
        except Exception as exc:
            log(f"WARN: Could not read Business_Goals.md — {exc}. Using generic suggestions.")
            return ""
    log(f"WARN: Business_Goals.md not found at {GOALS_FILE}. Using generic suggestions.")
    return ""


# ---------------------------------------------------------------------------
# Step 2: Scan /Done/
# ---------------------------------------------------------------------------

def scan_done(week_start: datetime, week_end: datetime) -> list[dict]:
    """Return list of task dicts from /Done/ directory."""
    if not DONE_DIR.exists():
        log(f"WARN: /Done/ not found at {DONE_DIR}")
        return []

    tasks = []
    for f in sorted(DONE_DIR.glob("*.md")):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            item = {
                "filename": f.name,
                "type":     extract_yaml_field(content, "type") or "generic",
                "status":   extract_yaml_field(content, "status") or "unknown",
                "priority": extract_yaml_field(content, "priority") or "medium",
                "channel":  extract_yaml_field(content, "channel") or "-",
                "created":  extract_yaml_field(content, "created") or "-",
                "content":  content,
                "amounts":  parse_amounts(content),
            }
            tasks.append(item)
        except Exception as exc:
            log(f"  WARN: Could not read Done/{f.name} — {exc}")

    log(f"  /Done/: {len(tasks)} file(s) found")
    return tasks


# ---------------------------------------------------------------------------
# Step 3: Scan /Logs/ for Revenue & Expenses
# ---------------------------------------------------------------------------

def scan_logs() -> dict:
    """
    Scan /Logs/ for revenue and expense patterns.
    Returns:
      {
        "revenue":  [(amount, keyword, filename), ...],
        "expenses": [(amount, keyword, filename), ...],
        "log_files": N,
      }
    """
    result = {"revenue": [], "expenses": [], "log_files": 0}

    if not LOGS_DIR.exists():
        log(f"WARN: /Logs/ not found at {LOGS_DIR}")
        return result

    log_files = list(LOGS_DIR.glob("*.md"))
    result["log_files"] = len(log_files)

    for f in sorted(log_files):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")

            for amount, kw in has_keyword_near_amount(content, REVENUE_KEYWORDS):
                result["revenue"].append((amount, kw, f.name))

            for amount, kw in has_keyword_near_amount(content, EXPENSE_KEYWORDS):
                result["expenses"].append((amount, kw, f.name))

        except Exception as exc:
            log(f"  WARN: Could not read Logs/{f.name} — {exc}")

    # Also check Done files for revenue signals
    if DONE_DIR.exists():
        for f in DONE_DIR.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                for amount, kw in has_keyword_near_amount(content, REVENUE_KEYWORDS):
                    result["revenue"].append((amount, kw, f"Done/{f.name}"))
                for amount, kw in has_keyword_near_amount(content, EXPENSE_KEYWORDS):
                    result["expenses"].append((amount, kw, f"Done/{f.name}"))
            except Exception:
                pass

    log(f"  Revenue signals: {len(result['revenue'])}, Expense signals: {len(result['expenses'])}")
    return result


# ---------------------------------------------------------------------------
# Step 4: Scan Bottleneck Sources
# ---------------------------------------------------------------------------

def scan_bottlenecks() -> dict:
    """
    Detect bottlenecks:
      - Stale /Needs Action/ files (> 48h)
      - Files in /Rejected/ this week
      - Stale /Pending Approval/ files (> 24h)
    """
    bottlenecks = {
        "stale_needs_action": [],
        "rejected_this_week": [],
        "stale_pending":      [],
    }

    # Stale Needs Action
    if NEEDS_ACTION_DIR.exists():
        for f in NEEDS_ACTION_DIR.glob("*.md"):
            if f.name.startswith("FILE_") or f.name.endswith("_metadata.md"):
                continue
            age = file_age_hours(f)
            if age >= STALE_NEEDS_ACTION_HOURS:
                try:
                    content  = f.read_text(encoding="utf-8", errors="replace")
                    priority = extract_yaml_field(content, "priority") or "medium"
                    bottlenecks["stale_needs_action"].append({
                        "filename": f.name,
                        "age_hours": round(age, 1),
                        "priority":  priority,
                    })
                except Exception:
                    bottlenecks["stale_needs_action"].append({
                        "filename": f.name,
                        "age_hours": round(age, 1),
                        "priority":  "unknown",
                    })

    # Rejected this week (any .md file present)
    if REJECTED_DIR.exists():
        for f in REJECTED_DIR.glob("*.md"):
            age = file_age_hours(f)
            if age <= 7 * 24:  # within 7 days
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    reason  = extract_yaml_field(content, "details") or "-"
                    bottlenecks["rejected_this_week"].append({
                        "filename": f.name,
                        "reason":   reason[:60],
                    })
                except Exception:
                    bottlenecks["rejected_this_week"].append({
                        "filename": f.name,
                        "reason":   "-",
                    })

    # Stale Pending Approval
    if PENDING_DIR.exists():
        for f in PENDING_DIR.glob("*.md"):
            age = file_age_hours(f)
            if age >= STALE_PENDING_HOURS:
                try:
                    content  = f.read_text(encoding="utf-8", errors="replace")
                    priority = extract_yaml_field(content, "priority") or "medium"
                    bottlenecks["stale_pending"].append({
                        "filename": f.name,
                        "age_hours": round(age, 1),
                        "priority":  priority,
                    })
                except Exception:
                    bottlenecks["stale_pending"].append({
                        "filename": f.name,
                        "age_hours": round(age, 1),
                        "priority":  "unknown",
                    })

    log(
        f"  Bottlenecks — stale_needs_action: {len(bottlenecks['stale_needs_action'])}, "
        f"rejected: {len(bottlenecks['rejected_this_week'])}, "
        f"stale_pending: {len(bottlenecks['stale_pending'])}"
    )
    return bottlenecks


# ---------------------------------------------------------------------------
# Step 5: Synthesise Briefing
# ---------------------------------------------------------------------------

def build_executive_summary(
    tasks: list[dict],
    financials: dict,
    bottlenecks: dict,
    week_start: datetime,
    week_end: datetime,
    goals_text: str,
) -> str:
    done_count      = len(tasks)
    revenue_total   = sum(a for a, _, _ in financials["revenue"])
    expense_total   = sum(a for a, _, _ in financials["expenses"])
    bottleneck_count = (
        len(bottlenecks["stale_needs_action"])
        + len(bottlenecks["rejected_this_week"])
        + len(bottlenecks["stale_pending"])
    )
    period = f"{_date_str(week_start)} to {_date_str(week_end)}"

    lines = [
        f"This briefing covers the period **{period}**. "
        f"The AI Employee completed **{done_count} task(s)** during this period."
    ]

    if revenue_total > 0:
        lines.append(
            f"Revenue signals totalling **${revenue_total:,.2f}** were detected in logs and completed tasks."
        )
    else:
        lines.append("No direct revenue signals were detected this week.")

    if expense_total > 0:
        lines.append(f"Expense/subscription signals totalling **${expense_total:,.2f}** were recorded.")

    if bottleneck_count > 0:
        lines.append(
            f"**{bottleneck_count} bottleneck(s)** were identified — see the Bottlenecks section for details."
        )
    else:
        lines.append("No significant bottlenecks were detected.")

    if goals_text:
        lines.append(
            "Strategic suggestions below are aligned to the Business Goals on file."
        )

    return " ".join(lines)


def build_revenue_section(financials: dict) -> str:
    revenue  = financials["revenue"]
    expenses = financials["expenses"]

    lines = []

    if not revenue and not expenses:
        lines.append("*No financial signals detected this week.*")
        lines.append("")
        lines.append(
            "> To enable revenue tracking, include dollar amounts (e.g. `$1,200`) "
            "near keywords like `invoice`, `payment received`, `sale` in your log files."
        )
        return "\n".join(lines)

    if revenue:
        total = sum(a for a, _, _ in revenue)
        lines += [
            "### Revenue Signals",
            "",
            f"**Total Detected Revenue: ${total:,.2f}**",
            "",
            "| Amount | Keyword | Source File |",
            "|--------|---------|-------------|",
        ]
        for amount, kw, fname in revenue:
            flag = " ⚠" if amount > PAYMENT_THRESHOLD else ""
            lines.append(f"| ${amount:,.2f}{flag} | {kw} | {fname} |")
        lines.append("")

    if expenses:
        total = sum(a for a, _, _ in expenses)
        lines += [
            "### Subscription & Expense Signals",
            "",
            f"**Total Detected Expenses: ${total:,.2f}**",
            "",
            "| Amount | Keyword | Source File |",
            "|--------|---------|-------------|",
        ]
        for amount, kw, fname in expenses:
            flag = " ⚠" if amount > PAYMENT_THRESHOLD else ""
            lines.append(f"| ${amount:,.2f}{flag} | {kw} | {fname} |")
        lines.append("")

    if any(a > PAYMENT_THRESHOLD for a, _, _ in (revenue + expenses)):
        lines.append(
            "> **⚠ Handbook Rule:** One or more amounts exceed $500 — review per Company Handbook."
        )

    return "\n".join(lines)


def build_tasks_section(tasks: list[dict]) -> str:
    if not tasks:
        return "*No completed tasks found in /Done/ this week.*"

    lines = [
        "| Task File | Type | Channel | Priority | Created |",
        "|-----------|------|---------|----------|---------|",
    ]
    for t in tasks:
        fname   = t["filename"][:40]
        t_type  = t["type"][:20]
        channel = t["channel"][:15]
        prio    = t["priority"]
        created = t["created"][:10]
        lines.append(f"| {fname} | {t_type} | {channel} | {prio} | {created} |")

    return "\n".join(lines)


def build_bottlenecks_section(bottlenecks: dict) -> str:
    stale_na  = bottlenecks["stale_needs_action"]
    rejected  = bottlenecks["rejected_this_week"]
    stale_pen = bottlenecks["stale_pending"]

    if not stale_na and not rejected and not stale_pen:
        return "*No bottlenecks detected. Pipeline is clear.*"

    lines = []

    if stale_na:
        lines += [
            f"### Stale Items in /Needs Action/ (> {STALE_NEEDS_ACTION_HOURS}h)",
            "",
            "| File | Age (hours) | Priority |",
            "|------|-------------|----------|",
        ]
        for item in stale_na:
            lines.append(
                f"| {item['filename'][:45]} | {item['age_hours']} | {item['priority']} |"
            )
        lines.append("")

    if rejected:
        lines += [
            "### Rejected Tasks This Week",
            "",
            "| File | Details |",
            "|------|---------|",
        ]
        for item in rejected:
            lines.append(f"| {item['filename'][:45]} | {item['reason']} |")
        lines.append("")

    if stale_pen:
        lines += [
            f"### Stale Pending Approvals (> {STALE_PENDING_HOURS}h)",
            "",
            "| File | Age (hours) | Priority |",
            "|------|-------------|----------|",
        ]
        for item in stale_pen:
            lines.append(
                f"| {item['filename'][:45]} | {item['age_hours']} | {item['priority']} |"
            )
        lines.append("")

    return "\n".join(lines)


def build_suggestions(
    tasks: list[dict],
    financials: dict,
    bottlenecks: dict,
    goals_text: str,
) -> str:
    suggestions = []

    rejected_count   = len(bottlenecks["rejected_this_week"])
    pending_count    = len(bottlenecks["stale_pending"])
    stale_na_count   = len(bottlenecks["stale_needs_action"])
    done_count       = len(tasks)
    revenue_total    = sum(a for a, _, _ in financials["revenue"])
    expense_total    = sum(a for a, _, _ in financials["expenses"])
    linkedin_count   = sum(1 for t in tasks if "linkedin" in t["type"].lower() or "linkedin" in t["channel"].lower())
    high_prio_stale  = sum(1 for i in bottlenecks["stale_needs_action"] if i["priority"] == "high")

    if done_count == 0:
        suggestions.append(
            "**No tasks completed this week** — verify that all watchers (Gmail, LinkedIn, WhatsApp, Twitter) "
            "are running and reachable."
        )

    if rejected_count > 2:
        suggestions.append(
            f"**{rejected_count} tasks were rejected** — review the /Rejected/ folder and adjust "
            "content strategy or approval thresholds to reduce rejection rate."
        )

    if pending_count > 5:
        suggestions.append(
            f"**Approval backlog: {pending_count} items** await review in /Pending Approval/. "
            "Clear the queue to unblock the pipeline."
        )

    if revenue_total == 0 and done_count > 0:
        suggestions.append(
            "Revenue signals are absent despite completed tasks. "
            "Ensure that outcome files include monetary values (e.g. invoice amounts) "
            "to enable automated revenue tracking."
        )

    if expense_total > revenue_total and revenue_total > 0:
        suggestions.append(
            f"**Expenses (${expense_total:,.2f}) exceed detected revenue (${revenue_total:,.2f}).** "
            "Review subscription costs and evaluate ROI against Business Goals."
        )
    elif expense_total > 0:
        suggestions.append(
            f"Subscription/expense signals totalling ${expense_total:,.2f} detected. "
            "Review these costs against Business_Goals.md to confirm alignment."
        )

    if linkedin_count >= 3:
        suggestions.append(
            f"**{linkedin_count} LinkedIn posts** completed this week — strong social activity. "
            "Consider integrating engagement metrics (likes, comments, DMs) into the audit pipeline."
        )

    if high_prio_stale > 3:
        suggestions.append(
            f"**{high_prio_stale} high-priority items** in /Needs Action/ are stale (> 48h). "
            "Escalate immediately — these may represent urgent leads or time-sensitive tasks."
        )

    if stale_na_count > 0 and high_prio_stale == 0:
        suggestions.append(
            f"{stale_na_count} item(s) in /Needs Action/ are older than 48 hours. "
            "Run the Cross Domain Integrator (Skill 5) to clear the queue."
        )

    if goals_text:
        suggestions.append(
            "Review completed tasks against `Business_Goals.md` to confirm alignment. "
            "Update goals if the business priorities have shifted since last quarter."
        )

    if not suggestions:
        suggestions.append(
            "Pipeline health looks good. Continue current cadence and monitor for new inbound leads."
        )
        suggestions.append(
            "Consider expanding automation coverage to additional channels (e.g. Instagram, Telegram) "
            "to capture a wider lead surface area."
        )

    return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(suggestions))


# ---------------------------------------------------------------------------
# Step 6: Write Briefing File
# ---------------------------------------------------------------------------

def write_briefing(
    week_start:   datetime,
    week_end:     datetime,
    summary:      str,
    revenue_md:   str,
    tasks_md:     str,
    bottleneck_md: str,
    suggestions_md: str,
    dry_run:      bool = False,
) -> Path:
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)

    date_str     = _date_str(week_end)
    period_str   = f"{_date_str(week_start)} to {date_str}"
    generated_at = _iso()
    filename     = f"ceo_briefing_{date_str}.md"
    output_path  = unique_path(BRIEFINGS_DIR, filename)

    content = f"""---
type: ceo_briefing
period: "{period_str}"
generated: "{generated_at}"
skill: Skill8_WeeklyAuditBriefer
---

# CEO Briefing — Week of {date_str}

## Executive Summary

{summary}

---

## Revenue & Financials

{revenue_md}

---

## Completed Tasks

{tasks_md}

---

## Bottlenecks

{bottleneck_md}

---

## Suggestions

{suggestions_md}

---

*Auto-generated by Skill 8: Weekly Audit Briefer — Gold Tier AI Employee*
*Period: {period_str} | Generated: {generated_at}*
"""

    if not dry_run:
        output_path.write_text(content, encoding="utf-8")
        log(f"Briefing written: {output_path}")
    else:
        log(f"[DRY RUN] Briefing would be written to: {output_path}")
        print()
        print("=" * 60)
        print("  BRIEFING PREVIEW (dry run — not saved)")
        print("=" * 60)
        print(content)

    return output_path


# ---------------------------------------------------------------------------
# Core Skill Runner
# ---------------------------------------------------------------------------

def run_skill(reference_date: datetime | None = None, dry_run: bool = False) -> str:
    """Main entry point with error recovery wrapper."""
    try:
        return _run_skill_inner(reference_date, dry_run)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[ERROR] Skill 8 (Weekly Audit Briefer) failed: {exc}", flush=True)
        err_path = log_skill_error(
            "Skill8_WeeklyAuditBriefer", exc,
            context=f"reference_date={reference_date}, dry_run={dry_run}", tb=tb,
        )
        print(f"  Error logged: {err_path}", flush=True)
        if not dry_run:
            plan_path = write_manual_action_plan(
                skill_name="Skill8_WeeklyAuditBriefer",
                action_description=(
                    "Manually generate the weekly CEO briefing by reviewing:\n"
                    "  - /Done/ folder for completed tasks\n"
                    "  - /Logs/ folder for revenue/expense signals\n"
                    "  - /Needs Action/ and /Rejected/ for bottlenecks\n"
                    "Write the briefing to /Briefings/ceo_briefing_[date].md"
                ),
                context=f"Error: {exc}",
                error=str(exc),
            )
            print(f"  Manual action plan: {plan_path}", flush=True)
        return ""


def _run_skill_inner(reference_date: datetime | None = None, dry_run: bool = False) -> str:
    """
    Inner implementation. Returns the path to the generated briefing file.
    """
    today      = reference_date or _now()
    # Week window: last 7 days ending today
    week_end   = today
    week_start = today - timedelta(days=6)

    print("=" * 60)
    print("  Skill 8: Weekly Audit Briefer")
    print("=" * 60)
    if dry_run:
        print("  MODE: DRY RUN — no files will be written")
        print("=" * 60)
    print()

    log(f"BASE DIR     : {BASE_DIR}")
    log(f"Week Period  : {_date_str(week_start)} to {_date_str(week_end)}")
    log(f"BRIEFINGS    : {BRIEFINGS_DIR}")
    print()

    # Step 1: Load context files
    log("Step 1: Loading context files...")
    handbook_text = load_handbook()
    goals_text    = load_goals()
    log(f"  Handbook rules: {handbook_text[:80]}{'...' if len(handbook_text) > 80 else ''}")
    log(f"  Business Goals: {'loaded' if goals_text else 'not found — using generic suggestions'}")
    print()

    # Step 2: Scan /Done/
    log("Step 2: Scanning /Done/ for completed tasks...")
    tasks = scan_done(week_start, week_end)
    print()

    # Step 3: Scan /Logs/ for financials
    log("Step 3: Scanning /Logs/ for revenue and expense patterns...")
    financials = scan_logs()
    print()

    # Step 4: Detect bottlenecks
    log("Step 4: Scanning bottleneck sources...")
    bottlenecks = scan_bottlenecks()
    print()

    # Step 5: Synthesise sections
    log("Step 5: Synthesising CEO briefing...")
    summary        = build_executive_summary(tasks, financials, bottlenecks, week_start, week_end, goals_text)
    revenue_md     = build_revenue_section(financials)
    tasks_md       = build_tasks_section(tasks)
    bottleneck_md  = build_bottlenecks_section(bottlenecks)
    suggestions_md = build_suggestions(tasks, financials, bottlenecks, goals_text)
    print()

    # Step 6: Write briefing
    log("Step 6: Writing briefing file...")
    output_path = write_briefing(
        week_start, week_end,
        summary, revenue_md, tasks_md, bottleneck_md, suggestions_md,
        dry_run=dry_run,
    )

    # Step 7: Summary output
    revenue_total  = sum(a for a, _, _ in financials["revenue"])
    expense_total  = sum(a for a, _, _ in financials["expenses"])
    bottleneck_count = (
        len(bottlenecks["stale_needs_action"])
        + len(bottlenecks["rejected_this_week"])
        + len(bottlenecks["stale_pending"])
    )

    print()
    print("=" * 60)
    print("  WEEKLY AUDIT BRIEFER COMPLETE")
    print("=" * 60)
    print(f"  Week Period   : {_date_str(week_start)} to {_date_str(week_end)}")
    print(f"  Done Items    : {len(tasks)}")
    print(f"  Revenue Found : ${revenue_total:,.2f}")
    print(f"  Expenses Found: ${expense_total:,.2f}")
    print(f"  Bottlenecks   : {bottleneck_count}")
    rel_path = output_path.relative_to(BASE_DIR)
    print(f"  Briefing Path : {rel_path}")
    if dry_run:
        print("  [DRY RUN — no files written]")
    print("=" * 60)
    print()

    return str(output_path)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="weekly-audit-briefer",
        description="Skill 8: Weekly Audit Briefer — Gold Tier AI Employee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python Skills/weekly_audit_briefer.py
  python Skills/weekly_audit_briefer.py --dry-run
  python Skills/weekly_audit_briefer.py --date 2026-02-16
""",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Analyse and print the briefing — no files written.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Reference date for the week window (default: today).",
    )
    args = parser.parse_args()

    reference_date = None
    if args.date:
        try:
            reference_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid date format '{args.date}'. Use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)

    run_skill(reference_date=reference_date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
