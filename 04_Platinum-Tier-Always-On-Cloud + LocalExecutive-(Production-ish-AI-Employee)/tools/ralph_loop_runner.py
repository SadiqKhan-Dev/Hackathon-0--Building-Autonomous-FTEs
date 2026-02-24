"""
AI Employee Gold Tier — Ralph Wiggum Loop Runner (No-API Edition)
=================================================================

Fully local, rule-based autonomous task loop.
NO Anthropic API key required.  No external dependencies beyond stdlib.

MULTI-STEP PIPELINE (runs locally, zero API calls):
  Step 1  DISCOVER   — scan /Needs Action for unprocessed .md files
  Step 2  CLASSIFY   — parse YAML frontmatter + keyword/amount rules
  Step 3  PLAN       — write Plan.md with YAML frontmatter + checkbox steps
  Step 4  EXECUTE    — run cross_domain_integrator skill for leads/multi-step
  Step 5  ROUTE      — move to Done or Pending Approval (HITL gate)
  Step 6  COMPLETE   — emit TASK_COMPLETE + print summary

CLASSIFICATION RULES (no LLM needed):
  type: payment        + amount > $500   → Pending Approval (HITL)
  type: payment        + amount ≤ $500   → Done
  type: sales_lead                       → run CDI skill → Pending Approval
  type: email_draft                      → Pending Approval
  type: linkedin_post_draft              → Pending Approval
  type: file_drop                        → Done
  type: generic / unknown                → keyword scan → classify + route

AUDIT LOGGING:
  /Logs/audit_YYYY-MM-DD.json    (JSON Lines — via audit_logger)
  /Logs/ralph_loop_TIMESTAMP.md  (human-readable Markdown transcript)

RUN:
  python tools/ralph_loop_runner.py
  python tools/ralph_loop_runner.py --max-iterations 20
  python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
  python tools/ralph_loop_runner.py --dry-run
  python tools/ralph_loop_runner.py --create-test-lead

/ralph-loop shortcut:
  /ralph-loop "Process multi-step task in Needs_Action" --max-iterations 20
  maps to: python tools/ralph_loop_runner.py <same args>

TEST — Multi-Step Sales Lead:
  1. Create a test file:
       python tools/ralph_loop_runner.py --create-test-lead
  2. Run the loop:
       python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
  3. Watch the pipeline:
       Iter 1 : discovers sales_lead_*.md
       Iter 2 : classifies → sales_lead / high / $8,500 > $500 (HITL)
       Iter 3 : writes Plans/Plan_*.md with checkbox steps
       Iter 4 : runs cross_domain_integrator skill
       Iter 5 : moves file → Pending Approval
       Iter 6 : verifies Needs Action empty → TASK_COMPLETE
  4. Check output:
       Plans/Plan_[timestamp].md          ← checkbox plan
       Pending Approval/sales_lead_*.md   ← queued for HITL review
       Logs/ralph_loop_[ts].md            ← Markdown transcript
       Logs/audit_YYYY-MM-DD.json         ← structured audit trail
"""

import re
import sys
import json
import shutil
import argparse
import textwrap
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --- Audit Logger (Gold Tier, optional — falls back silently) ---
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "Skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

try:
    from audit_logger import log_action      # noqa: E402
    _AUDIT_AVAILABLE = True
except ImportError:
    _AUDIT_AVAILABLE = False
    def log_action(*args, **kwargs) -> None:
        pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR          = Path(__file__).resolve().parent.parent

NEEDS_ACTION_DIR  = BASE_DIR / "Needs Action"
PLANS_DIR         = BASE_DIR / "Plans"
DONE_DIR          = BASE_DIR / "Done"
PENDING_DIR       = BASE_DIR / "Pending Approval"
REJECTED_DIR      = BASE_DIR / "Rejected"
APPROVED_DIR      = BASE_DIR / "Approved"
LOGS_DIR          = BASE_DIR / "Logs"
ERRORS_DIR        = BASE_DIR / "Errors"
HANDBOOK_FILE     = BASE_DIR / "Company Handbook.md"

DEFAULT_MAX_ITER  = 20
TASK_COMPLETE_SIG = "TASK_COMPLETE"
PAYMENT_THRESHOLD = 500.0

# Keyword sets for classification when YAML type is missing/generic
SALES_KEYWORDS    = ["sales", "lead", "client", "project", "interested", "proposal",
                     "quotation", "partnership", "opportunity", "inquiry"]
PAYMENT_KEYWORDS  = ["invoice", "payment", "pay", "billing", "charge", "fee",
                     "subscription", "renewal", "purchase", "transaction"]
DOLLAR_RE         = re.compile(r'\$\s?([\d,]+\.?\d*)')

# Gold Tier skill map
SKILL_MAP: dict[str, str] = {
    "cross_domain_integrator":  "Skills/cross_domain_integrator.py",
    "social_summary_generator": "Skills/social_summary_generator.py",
    "twitter_post_generator":   "Skills/twitter_post_generator.py",
    "hitl_approval_handler":    "Skills/hitl_approval_handler.py",
    "weekly_audit_briefer":     "Skills/weekly_audit_briefer.py",
    "auto_linkedin_poster":     "Skills/auto_linkedin_poster.py",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now()


def _ts() -> str:
    return _now().strftime("%Y%m%d_%H%M%S")


def _iso() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _date() -> str:
    return _now().strftime("%Y-%m-%d")


def extract_yaml_field(content: str, field: str) -> str:
    pattern = rf'^{re.escape(field)}:\s*["\']?(.+?)["\']?\s*$'
    m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_amounts(text: str) -> list[float]:
    amounts = []
    for raw in DOLLAR_RE.findall(text):
        try:
            amounts.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return amounts


def unique_path(directory: Path, filename: str) -> Path:
    target  = directory / filename
    stem    = Path(filename).stem
    suffix  = Path(filename).suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


# ---------------------------------------------------------------------------
# File I/O Tools
# ---------------------------------------------------------------------------

def tool_list_files(directory: str) -> list[str]:
    target = BASE_DIR / directory
    if not target.exists():
        return []
    return sorted(f.name for f in target.glob("*.md"))


def tool_read_file(path: str) -> str:
    target = BASE_DIR / path
    if not target.exists():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[read error: {e}]"


def tool_write_file(path: str, content: str, dry_run: bool = False) -> str:
    target = BASE_DIR / path
    if dry_run:
        return f"[DRY RUN] Would write {len(content)} chars → {path}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written: {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def tool_append_file(path: str, content: str, dry_run: bool = False) -> str:
    target = BASE_DIR / path
    if dry_run:
        return f"[DRY RUN] Would append {len(content)} chars → {path}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended to: {path}"
    except Exception as e:
        return f"Error appending {path}: {e}"


def tool_move_file(source_path: str, destination_folder: str, dry_run: bool = False) -> str:
    src     = BASE_DIR / source_path
    dst_dir = BASE_DIR / destination_folder
    if not src.exists():
        return f"Source not found: {source_path}"
    if dry_run:
        return f"[DRY RUN] Would move '{source_path}' → '{destination_folder}/'"
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        counter = 1
        while dst.exists():
            dst = dst_dir / f"{src.stem}_{counter}{src.suffix}"
            counter += 1
        shutil.move(str(src), str(dst))
        return f"Moved → {destination_folder}/{dst.name}"
    except Exception as e:
        return f"Error moving {source_path}: {e}"


def tool_run_skill(skill_name: str, args: list | None = None, dry_run: bool = False) -> str:
    if skill_name not in SKILL_MAP:
        return f"Unknown skill: {skill_name}"
    skill_file = BASE_DIR / SKILL_MAP[skill_name]
    if not skill_file.exists():
        return f"Skill file not found: {SKILL_MAP[skill_name]}"
    if dry_run:
        extra = (" " + " ".join(args)) if args else ""
        return f"[DRY RUN] Would run: {skill_name}{extra}"
    cmd = [sys.executable, str(skill_file)] + (args or [])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            cwd=str(BASE_DIR), encoding="utf-8", errors="replace",
        )
        stdout = (result.stdout or "")[-500:]
        if result.returncode != 0:
            stderr = (result.stderr or "")[-300:]
            return f"Skill '{skill_name}' failed (exit {result.returncode}).\n{stdout}\n{stderr}"
        return f"Skill '{skill_name}' OK.\n{stdout}"
    except subprocess.TimeoutExpired:
        return f"Skill '{skill_name}' timed out (120s)."
    except Exception as e:
        return f"Error running '{skill_name}': {e}"


# ---------------------------------------------------------------------------
# Task Classifier
# ---------------------------------------------------------------------------

def classify_task(content: str, filename: str) -> dict:
    """
    Classify a task file using YAML frontmatter + keyword + amount rules.

    Returns:
        {
          "task_type"  : str,    # payment | sales_lead | email_draft |
                                 #  linkedin_post_draft | file_drop | generic
          "priority"   : str,    # high | medium | low
          "max_amount" : float,
          "needs_hitl" : bool,
          "run_cdi"    : bool,   # run cross_domain_integrator?
          "route"      : str,    # "Pending Approval" | "Done"
        }
    """
    # --- Parse YAML fields ---
    task_type = extract_yaml_field(content, "type").lower()
    priority  = extract_yaml_field(content, "priority").lower() or "medium"

    # --- Dollar amounts ---
    amounts    = parse_amounts(content)
    max_amount = max(amounts) if amounts else 0.0

    # --- Keyword fallback when type is missing or generic ---
    c_lower = content.lower()
    fn_lower = filename.lower()

    if task_type in ("", "generic", "file_drop"):
        if any(kw in c_lower for kw in SALES_KEYWORDS) or "lead" in fn_lower:
            task_type = "sales_lead"
        elif any(kw in c_lower for kw in PAYMENT_KEYWORDS) or max_amount > 0:
            task_type = "payment"
        elif task_type == "":
            task_type = "generic"

    # --- Priority override for high-value items ---
    if max_amount > PAYMENT_THRESHOLD and priority not in ("high",):
        priority = "high"

    # --- Routing rules ---
    HITL_TYPES = {"payment", "sales_lead", "email_draft",
                  "linkedin_post_draft", "multi_step"}
    needs_hitl = task_type in HITL_TYPES or max_amount > PAYMENT_THRESHOLD

    # Cross Domain Integrator: run for leads and multi-step
    run_cdi    = task_type in ("sales_lead", "multi_step")

    route = "Pending Approval" if needs_hitl else "Done"

    return {
        "task_type":  task_type,
        "priority":   priority,
        "max_amount": max_amount,
        "needs_hitl": needs_hitl,
        "run_cdi":    run_cdi,
        "route":      route,
    }


# ---------------------------------------------------------------------------
# Plan Builder
# ---------------------------------------------------------------------------

def build_plan_content(
    source_file: str,
    classification: dict,
    source_content: str,
    iteration: int,
    cdi_result: str = "",
) -> str:
    """Generate a Plan.md with YAML frontmatter and checkbox pipeline steps."""
    c         = classification
    task_type = c["task_type"]
    priority  = c["priority"].upper()
    route     = c["route"]
    amount    = f"${c['max_amount']:,.2f}" if c["max_amount"] > 0 else "N/A"

    # Routing justification
    if c["max_amount"] > PAYMENT_THRESHOLD:
        route_reason = f"Amount {amount} exceeds ${PAYMENT_THRESHOLD:,.0f} threshold (Company Handbook)"
    elif c["needs_hitl"]:
        route_reason = f"Task type '{task_type}' requires human review before action"
    else:
        route_reason = "Standard task — no human approval required"

    # Subject / title from YAML
    subject = (
        extract_yaml_field(source_content, "subject")
        or extract_yaml_field(source_content, "title")
        or source_file
    )
    sender = extract_yaml_field(source_content, "from") or "-"

    cdi_section = ""
    if cdi_result:
        cdi_section = f"\n## Cross Domain Integrator Output\n\n```\n{cdi_result[:500]}\n```\n"

    return textwrap.dedent(f"""\
        ---
        type: action_plan
        source_file: "{source_file}"
        task_type: {task_type}
        priority: {c['priority']}
        status: {"pending_approval" if c['needs_hitl'] else "complete"}
        pipeline_stage: execute
        iteration: {iteration}
        created: "{_iso()}"
        ---

        # Plan: {subject}

        ## Summary

        Processed `{source_file}` — classified as **{task_type}** with **{priority}** priority.
        {"Amount detected: " + amount + "." if c['max_amount'] > 0 else "No dollar amounts detected."}
        Routed to **{route}**.

        ## Priority: {priority}

        - Task type : {task_type}
        - From      : {sender}
        - Amount    : {amount}
        - Reason    : {route_reason}

        ## Pipeline Steps

        - [x] Step 1: Discovered in /Needs Action
        - [x] Step 2: Classified as `{task_type}` — priority `{c['priority']}`
        - [x] Step 3: Plan created (this file)
        - {"[x]" if cdi_result else "[ ]"} Step 4: Cross Domain Integrator {"run" if cdi_result else "— not required"}
        - [x] Step 5: Routed → {route}
        - [ ] Step 6: Complete (awaiting {"HITL approval" if c['needs_hitl'] else "confirmation"})

        ## Routing Decision

        **Destination**: `{route}`
        **Reason**: {route_reason}
        {cdi_section}
        ---
        *Auto-generated by Ralph Wiggum Loop — Gold Tier AI Employee*
        *Generated: {_iso()}*
    """)


# ---------------------------------------------------------------------------
# Loop Logger
# ---------------------------------------------------------------------------

class LoopLogger:
    """Writes a human-readable Markdown transcript of each loop run."""

    def __init__(self, timestamp: str, run_id: str) -> None:
        self.timestamp  = timestamp
        self.run_id     = run_id
        self.log_path   = LOGS_DIR / f"ralph_loop_{timestamp}.md"
        self.lines: list[str] = []
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        self.lines += [
            f"# Ralph Wiggum Loop — Gold Tier — {timestamp}",
            f"- Run ID  : {run_id}",
            f"- Mode    : local rule-based (no API)",
            f"- Started : {_iso()}",
            "",
        ]

    def step(self, label: str) -> None:
        self.lines.append(f"\n### {label}")
        self.flush()

    def info(self, msg: str) -> None:
        self.lines.append(f"- {msg}")

    def complete(self, summary: str, reason: str, iteration: int,
                 done: list[str], hitl: list[str]) -> None:
        self.lines += [
            "",
            "## Loop Complete",
            f"- Reason     : {reason}",
            f"- Iterations : {iteration}",
            f"- Done       : {', '.join(done) or 'none'}",
            f"- HITL Queue : {', '.join(hitl) or 'none'}",
            f"- Summary    : {summary}",
            f"- Ended      : {_iso()}",
        ]
        self.flush()

    def flush(self) -> None:
        try:
            self.log_path.write_text("\n".join(self.lines), encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Single-File Processor
# ---------------------------------------------------------------------------

def process_file(
    filepath: Path,
    iteration: int,
    dry_run: bool,
    logger: LoopLogger,
    verbose_log,
) -> dict:
    """
    Run the full 6-step pipeline for one file.

    Returns:
        {
          "filename" : str,
          "task_type": str,
          "priority" : str,
          "route"    : str,   # "Done" | "Pending Approval"
          "plan_path": str,
          "cdi_run"  : bool,
          "error"    : str,   # "" if success
        }
    """
    rel_path = str(filepath.relative_to(BASE_DIR))
    result   = {
        "filename": filepath.name,
        "task_type": "unknown",
        "priority": "medium",
        "route": "Done",
        "plan_path": "",
        "cdi_run": False,
        "error": "",
    }

    try:
        # Step 1: Read
        logger.step(f"File: {filepath.name}")
        content = filepath.read_text(encoding="utf-8", errors="replace")
        verbose_log(f"  Read: {filepath.name} ({len(content)} chars)")
        logger.info(f"Read: {len(content)} chars")
        log_action("file_read", "RalphLoop", target=rel_path, result="ok")

        # Step 2: Classify
        c = classify_task(content, filepath.name)
        result.update({
            "task_type": c["task_type"],
            "priority":  c["priority"],
            "route":     c["route"],
        })
        verbose_log(
            f"  Classified: type={c['task_type']} | priority={c['priority']} | "
            f"amount=${c['max_amount']:,.2f} | route={c['route']}"
        )
        logger.info(f"Type: {c['task_type']} | Priority: {c['priority']} | "
                    f"Amount: ${c['max_amount']:,.2f} | Route: {c['route']}")
        log_action("item_classified", "RalphLoop", target=rel_path,
                   parameters={"task_type": c["task_type"], "priority": c["priority"],
                                "max_amount": c["max_amount"], "route": c["route"]},
                   result="classified")

        # Step 3: Run Cross Domain Integrator (for leads / multi-step)
        cdi_result = ""
        if c["run_cdi"]:
            verbose_log("  Running: cross_domain_integrator skill...")
            logger.info("Running cross_domain_integrator")
            cdi_result = tool_run_skill("cross_domain_integrator", [], dry_run)
            verbose_log(f"  CDI result: {cdi_result[:120]}")
            logger.info(f"CDI: {cdi_result[:120]}")
            log_action("skill_invoked", "RalphLoop", target="cross_domain_integrator",
                       parameters={"source": filepath.name}, result=cdi_result[:80])
            result["cdi_run"] = True

        # Step 4: Write Plan.md
        plan_slug   = re.sub(r"[^\w]+", "_", filepath.stem)[:30]
        plan_fname  = f"Plan_{_now().strftime('%Y-%m-%d_%H%M%S')}_{plan_slug}.md"
        plan_path   = unique_path(PLANS_DIR, plan_fname)
        plan_rel    = str(plan_path.relative_to(BASE_DIR))
        plan_content = build_plan_content(
            filepath.name, c, content, iteration, cdi_result
        )
        write_result = tool_write_file(plan_rel, plan_content, dry_run)
        verbose_log(f"  Plan: {write_result}")
        logger.info(f"Plan written: {plan_rel}")
        log_action("plan_written", "RalphLoop", target=plan_rel,
                   parameters={"source": filepath.name, "task_type": c["task_type"]},
                   result="written")
        result["plan_path"] = plan_rel

        # Step 5: Route file
        move_result = tool_move_file(rel_path, c["route"], dry_run)
        verbose_log(f"  Route: {move_result}")
        logger.info(f"Route: {move_result}")
        log_action("item_routed", "RalphLoop", target=rel_path,
                   parameters={"destination": c["route"], "task_type": c["task_type"]},
                   approval_status="pending" if c["needs_hitl"] else "n/a",
                   result=f"moved to {c['route']}")

    except Exception as exc:
        tb = traceback.format_exc()
        result["error"] = str(exc)
        verbose_log(f"  ERROR processing {filepath.name}: {exc}")
        logger.info(f"ERROR: {exc}")
        log_action("item_error", "RalphLoop", target=rel_path,
                   parameters={"exception": type(exc).__name__},
                   result=f"failed: {exc}")
        # Write error file
        try:
            ERRORS_DIR.mkdir(parents=True, exist_ok=True)
            err_file = ERRORS_DIR / f"ralph_loop_error_{_ts()}.md"
            err_file.write_text(
                f"# Ralph Loop Error\n\n- File: {filepath.name}\n"
                f"- Error: {exc}\n\n```\n{tb}\n```\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Test File Creator
# ---------------------------------------------------------------------------

def create_test_lead(dry_run: bool = False) -> str:
    """Drop a realistic multi-step $8,500 sales lead into /Needs Action."""
    filename = f"sales_lead_{_ts()}.md"
    content  = textwrap.dedent(f"""\
        ---
        type: sales_lead
        status: pending
        priority: high
        from: john.smith@acme-corp.com
        subject: Interested in your services — $8,500 website project
        created: "{_iso()}"
        channel: email
        ---

        # Sales Lead: Acme Corp — Website Redesign

        John Smith from Acme Corp reached out via email about a full website
        redesign for their e-commerce platform. Budget mentioned: **$8,500**.

        ## Lead Details

        - **Company**: Acme Corp
        - **Contact**: John Smith <john.smith@acme-corp.com>
        - **Budget**: $8,500
        - **Timeline**: Q2 2026
        - **Requirement**: E-commerce redesign, mobile-first, Shopify integration

        ## Next Steps Required

        1. Draft a LinkedIn post showcasing our web-design capabilities
        2. Send a follow-up email with portfolio links and pricing tiers
        3. Schedule a discovery call

        > This lead requires HITL approval before outreach (amount > $500 threshold).
    """)

    if dry_run:
        print(f"[DRY RUN] Would create: Needs Action/{filename}")
        print()
        print(content)
        return f"Needs Action/{filename}"

    NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)
    target = NEEDS_ACTION_DIR / filename
    target.write_text(content, encoding="utf-8")
    print(f"Test lead created: Needs Action/{filename}")
    return f"Needs Action/{filename}"


# ---------------------------------------------------------------------------
# Core Loop (No API)
# ---------------------------------------------------------------------------

def ralph_loop(
    prompt: str = "",
    max_iterations: int = DEFAULT_MAX_ITER,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Run the Ralph Wiggum Gold Tier loop — fully local, no API required.

    Returns:
        {
          "status"     : "complete" | "max_iterations" | "error",
          "iterations" : int,
          "summary"    : str,
          "log_path"   : Path,
        }
    """
    ts     = _ts()
    run_id = f"ralph_{ts}"
    logger = LoopLogger(ts, run_id)

    def log(msg: str) -> None:
        if verbose:
            print(f"[{_now().strftime('%H:%M:%S')}] {msg}", flush=True)

    for d in [NEEDS_ACTION_DIR, PLANS_DIR, DONE_DIR, PENDING_DIR, LOGS_DIR, ERRORS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("  Ralph Wiggum Loop — Gold Tier AI Employee  [No-API Mode]")
    print("=" * 65)
    log(f"Run ID    : {run_id}")
    log(f"Max iter  : {max_iterations}")
    log(f"Dry run   : {dry_run}")
    log(f"Audit log : {'enabled' if _AUDIT_AVAILABLE else 'disabled (audit_logger not found)'}")
    log(f"Loop log  : {logger.log_path}")
    if prompt:
        log(f"Prompt    : {prompt[:100]}")
    if dry_run:
        print("  [DRY RUN — no files will be moved or written]")
    print("-" * 65)

    log_action("loop_start", "RalphLoop",
               parameters={"run_id": run_id, "max_iterations": max_iterations,
                            "dry_run": dry_run, "mode": "local"},
               result="started")

    files_done: list[str] = []
    files_hitl: list[str] = []
    skills_run: list[str] = []
    errors:     list[str] = []
    iteration = 0
    status    = "complete"

    try:
        while iteration < max_iterations:
            iteration += 1
            log(f"\n{'='*20} ITERATION {iteration}/{max_iterations} {'='*20}")
            logger.step(f"Iteration {iteration}/{max_iterations}")

            log_action("loop_iteration", "RalphLoop",
                       parameters={"run_id": run_id, "iteration": iteration},
                       result="in_progress")

            # Discover unprocessed files
            pending_files = [
                NEEDS_ACTION_DIR / fname
                for fname in tool_list_files("Needs Action")
                if not fname.startswith(".")
            ]

            if not pending_files:
                log("  /Needs Action is empty — nothing to process.")
                logger.info("Needs Action: empty")
                break

            log(f"  Found {len(pending_files)} file(s) in /Needs Action:")
            for f in pending_files:
                log(f"    - {f.name}")

            # Process each file in this iteration pass
            for filepath in pending_files:
                log(f"\n  Processing: {filepath.name}")
                result = process_file(filepath, iteration, dry_run, logger, log)

                if result["error"]:
                    errors.append(filepath.name)
                    continue

                if result["route"] == "Done":
                    files_done.append(result["filename"])
                else:
                    files_hitl.append(result["filename"])

                if result["cdi_run"]:
                    skills_run.append("cross_domain_integrator")

                log(f"  Complete: {result['filename']} → {result['route']}")

            # After processing all files this pass, check if Needs Action is clear
            remaining = tool_list_files("Needs Action")
            if not remaining:
                log("\n  /Needs Action is now empty.")
                break
            else:
                log(f"\n  {len(remaining)} file(s) still in /Needs Action — continuing...")

        else:
            status = "max_iterations"

    except Exception as exc:
        tb = traceback.format_exc()
        log(f"FATAL: Unhandled exception — {exc}")
        status = "error"
        try:
            ERRORS_DIR.mkdir(parents=True, exist_ok=True)
            err_file = ERRORS_DIR / f"ralph_loop_fatal_{ts}.md"
            err_file.write_text(
                f"# Ralph Loop Fatal Error — {ts}\n\n"
                f"- Error  : {exc}\n- Run ID : {run_id}\n\n```\n{tb}\n```\n",
                encoding="utf-8",
            )
            log(f"Fatal error logged: {err_file}")
        except Exception:
            pass

    # ----- Build summary -----
    done_count = len(files_done)
    hitl_count = len(files_hitl)
    err_count  = len(errors)

    summary_parts = [f"Processed {done_count + hitl_count} file(s) in {iteration} iteration(s)."]
    if files_done:
        summary_parts.append(f"{done_count} moved to Done: {', '.join(files_done)}.")
    if files_hitl:
        summary_parts.append(f"{hitl_count} queued in Pending Approval (HITL): {', '.join(files_hitl)}.")
    if skills_run:
        summary_parts.append(f"Skills run: {', '.join(sorted(set(skills_run)))}.")
    if err_count:
        summary_parts.append(f"Errors on {err_count} file(s): {', '.join(errors)}.")
    if not files_done and not files_hitl:
        summary_parts = ["No files found in /Needs Action — nothing to process."]

    final_summary = " ".join(summary_parts)

    reason = {
        "complete":       f"{TASK_COMPLETE_SIG} — pipeline finished",
        "max_iterations": f"Reached max {max_iterations} iterations",
        "error":          "Fatal error occurred",
    }.get(status, status)

    logger.complete(final_summary, reason, iteration, files_done, files_hitl)

    log_action("loop_complete", "RalphLoop",
               parameters={"run_id": run_id, "iterations": iteration,
                            "files_done": files_done, "files_hitl": files_hitl,
                            "skills_run": list(set(skills_run)), "dry_run": dry_run},
               result=f"{status}: {final_summary[:80]}")

    # Final banner
    print()
    print("=" * 65)
    print(f"  {TASK_COMPLETE_SIG}" if status == "complete" else f"  LOOP ENDED — {reason}")
    print("=" * 65)
    print(f"  Iterations  : {iteration}/{max_iterations}")
    print(f"  Status      : {status.upper()}")
    if files_done:
        print(f"  Done        : {', '.join(files_done)}")
    if files_hitl:
        print(f"  HITL Queue  : {', '.join(files_hitl)}")
    if skills_run:
        print(f"  Skills Run  : {', '.join(sorted(set(skills_run)))}")
    if errors:
        print(f"  Errors      : {', '.join(errors)}")
    print(f"  Summary     : {final_summary}")
    print(f"  Loop log    : {logger.log_path}")
    print("=" * 65)

    return {
        "status":     status,
        "iterations": iteration,
        "summary":    final_summary,
        "log_path":   logger.log_path,
    }


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ralph-loop",
        description="Ralph Wiggum Loop — Gold Tier AI Employee  [No-API Mode]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""
            No API key required — fully local rule-based processing.

            Examples:
              python tools/ralph_loop_runner.py
              python tools/ralph_loop_runner.py --max-iterations 20
              python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
              python tools/ralph_loop_runner.py --dry-run
              python tools/ralph_loop_runner.py --create-test-lead

            /ralph-loop shortcut:
              /ralph-loop "Process multi-step task in Needs_Action" --max-iterations 20
              maps to: python tools/ralph_loop_runner.py <same args>

            Full pipeline test:
              1. python tools/ralph_loop_runner.py --create-test-lead
              2. python tools/ralph_loop_runner.py --max-iterations 20
              3. Check: Plans/, Pending Approval/, Logs/ralph_loop_*.md, Logs/audit_*.json

            Default max iterations: {DEFAULT_MAX_ITER}
        """),
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Optional label/description for this run (not used for processing logic)",
    )
    parser.add_argument(
        "--max-iterations", "-n",
        type=int,
        default=DEFAULT_MAX_ITER,
        metavar="N",
        help=f"Maximum loop iterations (default: {DEFAULT_MAX_ITER})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate — no files moved or written",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-step console output (log file still written)",
    )
    parser.add_argument(
        "--create-test-lead",
        action="store_true",
        dest="create_test_lead",
        help="Drop a realistic multi-step $8,500 sales lead into /Needs Action, then exit.",
    )

    args = parser.parse_args()

    if args.create_test_lead:
        create_test_lead(dry_run=args.dry_run)
        sys.exit(0)

    ralph_loop(
        prompt=args.prompt,
        max_iterations=args.max_iterations,
        dry_run=args.dry_run,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
