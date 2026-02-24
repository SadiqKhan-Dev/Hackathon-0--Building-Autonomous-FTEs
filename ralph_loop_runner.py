"""
AI Employee Gold Tier — Ralph Wiggum Loop Runner (Gold Edition)
===============================================================

Multi-step autonomous task loop that processes every file in /Needs Action
through the full Gold Tier pipeline — no constant human oversight required.

MULTI-STEP PIPELINE:
  Step 1  DISCOVER  — list_files / read each unprocessed file
  Step 2  CLASSIFY  — determine type, priority, route
  Step 3  PLAN      — write Plan.md with YAML frontmatter + checkbox steps
  Step 4  EXECUTE   — run_skill(cross_domain_integrator) + route to Done / HITL
  Step 5  VERIFY    — confirm /Needs Action is empty
  Step 6  COMPLETE  — task_complete → emit TASK_COMPLETE + move files

MULTI-STEP EXAMPLE (sales lead → draft post → HITL → MCP):
  Iter 1  list_files("Needs Action")
  Iter 2  read_file(lead.md) → classify as sales_lead / high priority
  Iter 3  write_file(Plans/Plan_...) with checkbox pipeline steps
  Iter 4  run_skill("cross_domain_integrator") → routes lead to LinkedIn draft
  Iter 5  move_file → "Pending Approval" (HITL gate, amount > $500)
  Iter 6  list_files verify → Needs Action empty → task_complete

AUDIT LOGGING:
  Every tool call and loop boundary is logged to:
    /Logs/audit_YYYY-MM-DD.json  (via audit_logger — structured JSON Lines)
    /Logs/ralph_loop_TIMESTAMP.md (human-readable markdown transcript)

INSTALL:
    pip install anthropic

SETUP:
    export ANTHROPIC_API_KEY="sk-ant-..."   (Unix / Mac)
    setx  ANTHROPIC_API_KEY "sk-ant-..."    (Windows)

RUN:
    python tools/ralph_loop_runner.py
    python tools/ralph_loop_runner.py --max-iterations 20
    python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
    python tools/ralph_loop_runner.py --dry-run
    python tools/ralph_loop_runner.py --create-test-lead

SHORTCUT (/ralph-loop):
    /ralph-loop "Process multi-step task in Needs_Action" --max-iterations 20
    maps to: python tools/ralph_loop_runner.py <args>

TEST — Multi-Step Sales Lead:
    1. Create a test file:
       python tools/ralph_loop_runner.py --create-test-lead
    2. Run the loop:
       python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
    3. Watch the pipeline:
       Iter 1 : list_files → sees lead file
       Iter 2 : read_file → classifies as sales_lead / high
       Iter 3 : write_file Plans/Plan_... → checkbox plan with pipeline stages
       Iter 4 : run_skill(cross_domain_integrator) → routes item
       Iter 5 : move_file → Pending Approval (HITL gate: amount > $500)
       Iter 6 : list_files verify → task_complete emitted
    4. Check output:
       Plans/Plan_[timestamp].md          ← checkbox plan
       Pending Approval/[lead].md         ← queued for human HITL review
       Logs/ralph_loop_[ts].md            ← full loop transcript
       Logs/audit_YYYY-MM-DD.json         ← structured audit trail (JSON Lines)
"""

import os
import sys
import json
import shutil
import argparse
import textwrap
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output on Windows (handles special chars in file names)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --- Dependency check ---
try:
    import anthropic
except ImportError:
    print("ERROR: anthropic SDK not installed.")
    print("Run: pip install anthropic")
    sys.exit(1)

# --- Audit Logger (Gold Tier) ---
# Add Skills/ to path so audit_logger can be imported without installation
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "Skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

try:
    from audit_logger import log_action          # noqa: E402
    _AUDIT_AVAILABLE = True
except ImportError:
    _AUDIT_AVAILABLE = False
    def log_action(*args, **kwargs) -> None:     # silent no-op fallback
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

MODEL             = "claude-sonnet-4-6"
DEFAULT_MAX_ITER  = 20
TASK_COMPLETE_SIG = "TASK_COMPLETE"

# Gold Tier skills available via run_skill tool
SKILL_MAP: dict[str, str] = {
    "cross_domain_integrator":  "Skills/cross_domain_integrator.py",
    "social_summary_generator": "Skills/social_summary_generator.py",
    "twitter_post_generator":   "Skills/twitter_post_generator.py",
    "hitl_approval_handler":    "Skills/hitl_approval_handler.py",
    "weekly_audit_briefer":     "Skills/weekly_audit_briefer.py",
    "auto_linkedin_poster":     "Skills/auto_linkedin_poster.py",
}


# ---------------------------------------------------------------------------
# Tool Definitions (Claude tool-use schema)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "list_files",
        "description": (
            "List all .md files in a project directory. "
            "Use this to check what files are in /Needs Action, /Plans, /Done, "
            "/Pending Approval, /Approved, /Rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": (
                        "Directory to list, relative to project root. "
                        "Examples: 'Needs Action', 'Plans', 'Done', 'Pending Approval', 'Approved'"
                    ),
                }
            },
            "required": ["directory"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full text content of a file. "
            "Use to inspect task files before deciding what action to take."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to project root. Example: 'Needs Action/lead_task.md'",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file, creating it if it does not exist (overwrites if it does). "
            "Use to create Plan.md files, LinkedIn post drafts, email drafts, checklists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to project root. Example: 'Plans/Plan_2026-02-23_1015.md'",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content including YAML frontmatter if applicable.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "append_file",
        "description": (
            "Append content to an existing file without overwriting it. "
            "Use to mark checkbox steps as complete in a Plan.md, or add progress notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to project root.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to append at the end of the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "move_file",
        "description": (
            "Move a file to a destination folder. "
            "Use this to route processed files through the pipeline: "
            "Done / Pending Approval / Approved / Rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Source file path relative to project root.",
                },
                "destination_folder": {
                    "type": "string",
                    "description": (
                        "Destination folder (relative to project root). "
                        "Options: 'Done', 'Pending Approval', 'Approved', 'Rejected', 'Plans'"
                    ),
                },
            },
            "required": ["source_path", "destination_folder"],
        },
    },
    {
        "name": "run_skill",
        "description": (
            "Execute a Gold Tier skill to process pipeline items. "
            "Use cross_domain_integrator to route Needs Action items "
            "(personal messages → HITL queue, business leads → LinkedIn draft). "
            "Use hitl_approval_handler --check to inspect the approval queue. "
            f"Available: {', '.join(SKILL_MAP.keys())}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": (
                        "Skill to run. One of: "
                        "cross_domain_integrator, social_summary_generator, "
                        "twitter_post_generator, hitl_approval_handler, "
                        "weekly_audit_briefer, auto_linkedin_poster"
                    ),
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional CLI arguments. "
                        "Example: ['--check'] for hitl_approval_handler. "
                        "Example: ['--dry-run'] for weekly_audit_briefer."
                    ),
                },
            },
            "required": ["skill_name"],
        },
    },
    {
        "name": "task_complete",
        "description": (
            f"Signal '{TASK_COMPLETE_SIG}'. Call this ONLY when ALL files in /Needs Action "
            "have been fully processed through the pipeline. "
            "Provide a comprehensive summary of everything accomplished this run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise summary of all work completed in this loop run.",
                },
                "files_processed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filenames fully completed and moved to /Done.",
                },
                "files_pending_approval": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filenames queued in /Pending Approval for HITL review.",
                },
                "skills_run": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of skills executed during this loop run.",
                },
            },
            "required": ["summary"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Executors
# ---------------------------------------------------------------------------

def tool_list_files(directory: str, dry_run: bool = False) -> str:
    target = BASE_DIR / directory
    if not target.exists():
        return f"Directory not found: {directory}"
    files = sorted(target.glob("*.md"))
    if not files:
        return f"No .md files found in '{directory}'."
    return "\n".join(f.name for f in files)


def tool_read_file(path: str, dry_run: bool = False) -> str:
    target = BASE_DIR / path
    if not target.exists():
        return f"File not found: {path}"
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"


def tool_write_file(path: str, content: str, dry_run: bool = False) -> str:
    target = BASE_DIR / path
    if dry_run:
        return f"[DRY RUN] Would write {len(content)} chars to: {path}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written: {path} ({len(content)} chars)"
    except Exception as e:
        return f"Error writing file: {e}"


def tool_append_file(path: str, content: str, dry_run: bool = False) -> str:
    target = BASE_DIR / path
    if dry_run:
        return f"[DRY RUN] Would append {len(content)} chars to: {path}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} chars to: {path}"
    except Exception as e:
        return f"Error appending to file: {e}"


def tool_move_file(source_path: str, destination_folder: str, dry_run: bool = False) -> str:
    src     = BASE_DIR / source_path
    dst_dir = BASE_DIR / destination_folder

    if not src.exists():
        return f"Source file not found: {source_path}"

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
        return f"Moved: {source_path} → {destination_folder}/{dst.name}"
    except Exception as e:
        return f"Error moving file: {e}"


def tool_run_skill(skill_name: str, args: list | None = None, dry_run: bool = False) -> str:
    """Execute a Gold Tier skill as a subprocess and return its output."""
    if skill_name not in SKILL_MAP:
        available = ", ".join(SKILL_MAP.keys())
        return f"Unknown skill '{skill_name}'. Available: {available}"

    skill_file = BASE_DIR / SKILL_MAP[skill_name]
    if not skill_file.exists():
        return f"Skill file not found: {SKILL_MAP[skill_name]}"

    if dry_run:
        extra = (" " + " ".join(args)) if args else ""
        return f"[DRY RUN] Would run skill: {skill_name}{extra}"

    cmd = [sys.executable, str(skill_file)] + (args or [])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(BASE_DIR),
            encoding="utf-8",
            errors="replace",
        )
        stdout = (result.stdout or "")[-600:]
        if result.returncode != 0:
            stderr = (result.stderr or "")[-300:]
            return (
                f"Skill '{skill_name}' exited with code {result.returncode}.\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        return f"Skill '{skill_name}' completed (exit 0).\nOutput:\n{stdout}"
    except subprocess.TimeoutExpired:
        return f"Skill '{skill_name}' timed out after 120s."
    except Exception as e:
        return f"Error running skill '{skill_name}': {e}"


def dispatch_tool(name: str, inputs: dict, dry_run: bool = False) -> str:
    """Route a tool call to its executor and return the result string."""
    if name == "list_files":
        return tool_list_files(inputs["directory"], dry_run)
    elif name == "read_file":
        return tool_read_file(inputs["path"], dry_run)
    elif name == "write_file":
        return tool_write_file(inputs["path"], inputs["content"], dry_run)
    elif name == "append_file":
        return tool_append_file(inputs["path"], inputs["content"], dry_run)
    elif name == "move_file":
        return tool_move_file(inputs["source_path"], inputs["destination_folder"], dry_run)
    elif name == "run_skill":
        return tool_run_skill(inputs["skill_name"], inputs.get("args", []), dry_run)
    elif name == "task_complete":
        return TASK_COMPLETE_SIG      # Handled specially by the loop
    else:
        return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# System Prompt Builder
# ---------------------------------------------------------------------------

def build_system_prompt(max_iterations: int = DEFAULT_MAX_ITER) -> str:
    handbook = "Always be polite. Flag payments > $500 for approval."
    if HANDBOOK_FILE.exists():
        try:
            handbook = HANDBOOK_FILE.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            pass

    return textwrap.dedent(f"""
        You are the Gold Tier AI Employee running the Ralph Wiggum Autonomous Loop.

        ## Your Mission
        Fully process every file in /Needs Action through the Gold Tier multi-step pipeline.
        You have a budget of {max_iterations} iterations. Use them efficiently.

        ## Gold Tier Multi-Step Pipeline

        LOOP START

          Step 1 — DISCOVER
            list_files("Needs Action")         — see every pending file
            read_file(each unprocessed file)   — understand the full task

          Step 2 — CLASSIFY & PLAN
            For each file determine:
              task_type : file_drop | payment | sales_lead | communication | multi_step
              priority  : high | medium | low
              route     : Done | Pending Approval | run_skill first

            write_file("Plans/Plan_[YYYY-MM-DD_HHMM]_[slug].md", plan_content)
            → YAML frontmatter + checkbox pipeline steps (see format below)

          Step 3 — EXECUTE (multi-step for leads and complex tasks)

            Sales Lead flow (multi-step: lead → post → HITL → MCP):
              1. run_skill("cross_domain_integrator")
                 → Routes the item (personal → HITL queue, business → LinkedIn draft)
              2. append_file(Plan.md, "- [x] Step 3: cross_domain_integrator run\\n")
              3. move_file(source, "Pending Approval")   — HITL gate before outreach
              4. append_file(Plan.md, "- [x] Step 4: moved to Pending Approval\\n")

            Payment flow:
              1. Check dollar amount against $500 threshold (Company Handbook)
              2. > $500 → move_file to "Pending Approval" (HITL required)
              3. ≤ $500 → move_file to "Done"

            File Drop flow:
              1. write Plan.md → move_file to "Done"

            Communication flow:
              1. Write reply draft in Plans/ → move_file to "Pending Approval" or "Done"

          Step 4 — VERIFY
            list_files("Needs Action")    — MUST be empty (or only already-processed files)
            list_files("Pending Approval") — report what awaits human HITL review

          Step 5 — COMPLETE
            task_complete(
              summary               = "...",
              files_processed       = ["file1.md", ...],
              files_pending_approval= ["lead.md", ...],
              skills_run            = ["cross_domain_integrator", ...],
            )

        LOOP END

        ## Plan.md YAML Frontmatter (required for EVERY task)
        ```yaml
        ---
        type: action_plan
        source_file: "[original filename]"
        task_type: file_drop|payment|sales_lead|communication|multi_step
        priority: high|medium|low
        status: in_progress|complete|pending_approval
        pipeline_stage: discover|classify|execute|verify|complete
        iteration: [iteration number when created]
        created: "[YYYY-MM-DD HH:MM:SS]"
        ---
        ```
        Body MUST include:
        - ## Summary (1–2 sentences)
        - ## Priority + Justification
        - ## Pipeline Steps (checkboxes)
          - [ ] Step 1: Classify
          - [ ] Step 2: Plan
          - [ ] Step 3: Execute / Route
          - [ ] Step 4: Verify
          - [ ] Step 5: Complete
        - ## Routing Decision (with reason)
        - ## Cross Domain Notes (if run_skill was called)

        ## Company Handbook Rules
        {handbook}

        ## Skills Available via run_skill
        | Skill | When to use |
        |-------|-------------|
        | cross_domain_integrator | Route Needs Action items — personal→HITL, business→LinkedIn |
        | social_summary_generator | Summarise social activity and draft a response |
        | twitter_post_generator | Generate tweet drafts from Needs Action items |
        | hitl_approval_handler | Inspect HITL queue (use args: ["--check"]) |
        | auto_linkedin_poster | Post queued LinkedIn drafts |
        | weekly_audit_briefer | Generate weekly CEO briefing (use args: ["--dry-run"] to preview) |

        ## Iteration Budget Rules
        - Never re-read a file you already classified this session
        - Each file should take ≤ 4 iterations: read → plan → execute/route → verify
        - If /Needs Action is already empty on iter 1: call task_complete immediately
        - If a file was already moved to /Plans or /Done, skip it
        - Emit task_complete as soon as /Needs Action is empty and verified

        ## Audit Trail
        Every tool call you make is automatically logged to /Logs/audit_YYYY-MM-DD.json.
        You do NOT need to call any logging tool yourself.

        ## Output Style
        Be concise in text blocks. Let tool calls do the work. No apologies. Act decisively.
    """).strip()


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
        self._tools_run: list[str] = []
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        self.lines.append(f"# Ralph Wiggum Loop — Gold Tier — {timestamp}\n")
        self.lines.append(f"- Model   : {MODEL}")
        self.lines.append(f"- Run ID  : {run_id}")
        self.lines.append(f"- Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    def iteration(self, n: int, max_n: int) -> None:
        self.lines.append(f"\n## Iteration {n}/{max_n}")
        self.flush()

    def tool_call(self, name: str, inputs: dict, result: str) -> None:
        self._tools_run.append(name)
        self.lines.append(f"\n### Tool: `{name}`")
        self.lines.append(f"- Input : `{json.dumps(inputs, ensure_ascii=False)[:200]}`")
        self.lines.append(f"- Result: {result[:300]}")
        # Audit every tool execution
        log_action(
            "tool_executed", "RalphLoop",
            target=inputs.get(
                "path",
                inputs.get("source_path",
                inputs.get("directory",
                inputs.get("skill_name", ""))),
            ),
            parameters={"tool": name, "run_id": self.run_id,
                        "inputs": json.dumps(inputs)[:150]},
            result=result[:120],
        )

    def assistant_text(self, text: str) -> None:
        if text.strip():
            self.lines.append(f"\n**Claude:** {text.strip()[:400]}")

    def complete(self, summary: str, reason: str, iteration: int) -> None:
        self.lines.append(f"\n## Loop Complete")
        self.lines.append(f"- Reason     : {reason}")
        self.lines.append(f"- Iterations : {iteration}")
        self.lines.append(f"- Summary    : {summary}")
        self.lines.append(f"- Tools run  : {', '.join(sorted(set(self._tools_run))) or 'none'}")
        self.lines.append(f"- Ended      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.flush()

    def flush(self) -> None:
        try:
            self.log_path.write_text("\n".join(self.lines), encoding="utf-8")
        except Exception:
            pass  # Non-fatal


# ---------------------------------------------------------------------------
# Test File Creator
# ---------------------------------------------------------------------------

def create_test_lead(dry_run: bool = False) -> str:
    """
    Drop a realistic multi-step sales lead into /Needs Action.
    Designed to exercise the full pipeline: classify → plan → cross_domain_integrator
    → HITL gate (amount > $500) → Pending Approval.
    """
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"sales_lead_{ts}.md"
    content  = textwrap.dedent(f"""\
        ---
        type: sales_lead
        status: pending
        priority: high
        from: john.smith@acme-corp.com
        subject: Interested in your services — $8,500 website project
        created: "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
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
# Core Loop
# ---------------------------------------------------------------------------

def ralph_loop(
    prompt: str,
    max_iterations: int = DEFAULT_MAX_ITER,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Run the Ralph Wiggum Gold Tier agentic loop.

    Returns:
        {
          "status"     : "complete" | "max_iterations" | "error",
          "iterations" : int,
          "summary"    : str,
          "log_path"   : Path,
        }
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("  Windows : setx ANTHROPIC_API_KEY \"sk-ant-...\"")
        print("  Unix    : export ANTHROPIC_API_KEY=\"sk-ant-...\"")
        sys.exit(1)

    client    = anthropic.Anthropic(api_key=api_key)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id    = f"ralph_{ts}"
    logger    = LoopLogger(ts, run_id)

    def log(msg: str) -> None:
        if verbose:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] {msg}", flush=True)

    # Ensure all required directories exist
    for d in [NEEDS_ACTION_DIR, PLANS_DIR, DONE_DIR, PENDING_DIR, LOGS_DIR, ERRORS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    system_prompt = build_system_prompt(max_iterations)
    messages: list[dict] = [{"role": "user", "content": prompt}]

    print("=" * 65)
    print("  Ralph Wiggum Loop — Gold Tier AI Employee")
    print("=" * 65)
    log(f"Model       : {MODEL}")
    log(f"Max iter    : {max_iterations}")
    log(f"Dry run     : {dry_run}")
    log(f"Prompt      : {prompt[:120]}")
    log(f"Run ID      : {run_id}")
    log(f"Log         : {logger.log_path}")
    log(f"Audit       : {'enabled' if _AUDIT_AVAILABLE else 'disabled (audit_logger not found)'}")
    if dry_run:
        print("  [DRY RUN MODE — no files will be moved or written]")
    print("-" * 65)

    # Audit: loop start
    log_action(
        "loop_start", "RalphLoop",
        parameters={
            "run_id":         run_id,
            "max_iterations": max_iterations,
            "dry_run":        dry_run,
            "prompt":         prompt[:120],
        },
        result="started",
    )

    status              = "max_iterations"
    final_summary       = "(no summary)"
    final_files_done:  list[str] = []
    final_files_hitl:  list[str] = []
    final_skills_run:  list[str] = []
    iteration           = 0

    try:
        while iteration < max_iterations:
            iteration += 1
            log(f"\n{'='*20} ITERATION {iteration}/{max_iterations} {'='*20}")
            logger.iteration(iteration, max_iterations)

            log_action(
                "loop_iteration", "RalphLoop",
                parameters={"run_id": run_id, "iteration": iteration},
                result="in_progress",
            )

            # Call Claude
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=TOOLS,
                    messages=messages,
                )
            except anthropic.APIConnectionError as e:
                log(f"FATAL: API connection error — {e}")
                status        = "error"
                final_summary = f"API connection error: {e}"
                break
            except anthropic.AuthenticationError:
                log("FATAL: Invalid ANTHROPIC_API_KEY.")
                status        = "error"
                final_summary = "Authentication failed."
                break
            except anthropic.RateLimitError as e:
                log(f"WARN: Rate limited — {e}. Ending loop.")
                status        = "error"
                final_summary = f"Rate limit: {e}"
                break
            except Exception as e:
                log(f"FATAL: Unexpected API error — {e}")
                status        = "error"
                final_summary = str(e)
                break

            # Collect assistant message
            assistant_message: dict = {"role": "assistant", "content": response.content}
            messages.append(assistant_message)

            tool_results    = []
            complete_called = False

            # Process each content block
            for block in response.content:
                if block.type == "text":
                    log(f"Claude: {block.text.strip()[:300]}")
                    logger.assistant_text(block.text)

                elif block.type == "tool_use":
                    tool_name   = block.name
                    tool_inputs = block.input
                    tool_id     = block.id

                    log(f"Tool call: {tool_name}({json.dumps(tool_inputs)[:150]})")

                    # Execute the tool
                    tool_result = dispatch_tool(tool_name, tool_inputs, dry_run)
                    logger.tool_call(tool_name, tool_inputs, tool_result)

                    # Handle completion signal
                    if tool_name == "task_complete":
                        final_summary    = tool_inputs.get("summary", "(no summary)")
                        final_files_done = tool_inputs.get("files_processed", [])
                        final_files_hitl = tool_inputs.get("files_pending_approval", [])
                        final_skills_run = tool_inputs.get("skills_run", [])
                        complete_called  = True
                        log(f"\n{TASK_COMPLETE_SIG} received.")
                        log(f"Summary : {final_summary}")
                        if final_files_done:
                            log(f"Done    : {', '.join(final_files_done)}")
                        if final_files_hitl:
                            log(f"HITL    : {', '.join(final_files_hitl)}")
                        if final_skills_run:
                            log(f"Skills  : {', '.join(final_skills_run)}")
                        tool_result = TASK_COMPLETE_SIG
                    else:
                        log(f"Result  : {tool_result[:200]}")

                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": tool_id,
                        "content":     tool_result,
                    })

            # Append tool results for next iteration
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            # Stop conditions
            if complete_called:
                status = "complete"
                break

            if response.stop_reason == "end_turn" and not tool_results:
                log("Claude stopped without calling task_complete. Ending loop.")
                status        = "complete"
                final_summary = "Claude finished reasoning (no explicit task_complete call)."
                break

    except Exception as exc:
        tb = traceback.format_exc()
        log(f"FATAL: Unhandled exception — {exc}")
        status        = "error"
        final_summary = str(exc)
        # Write error log
        try:
            ERRORS_DIR.mkdir(parents=True, exist_ok=True)
            err_file = ERRORS_DIR / f"ralph_loop_error_{ts}.md"
            err_file.write_text(
                f"# Ralph Loop Error — {ts}\n\n"
                f"- Error     : {exc}\n"
                f"- Iteration : {iteration}\n"
                f"- Run ID    : {run_id}\n\n"
                f"```\n{tb}\n```\n",
                encoding="utf-8",
            )
            log(f"Error logged: {err_file}")
        except Exception:
            pass

    # ----- Loop finished -----

    reason = {
        "complete":       f"{TASK_COMPLETE_SIG} received",
        "max_iterations": f"Reached max {max_iterations} iterations",
        "error":          "Fatal error occurred",
    }.get(status, status)

    logger.complete(final_summary, reason, iteration)

    # Audit: loop completion
    log_action(
        "loop_complete", "RalphLoop",
        parameters={
            "run_id":       run_id,
            "iterations":   iteration,
            "files_done":   final_files_done,
            "files_hitl":   final_files_hitl,
            "skills_run":   final_skills_run,
            "dry_run":      dry_run,
        },
        result=f"{status}: {final_summary[:80]}",
    )

    print()
    print("=" * 65)
    print(f"  LOOP FINISHED — {reason}")
    print("=" * 65)
    print(f"  Iterations  : {iteration}/{max_iterations}")
    print(f"  Status      : {status.upper()}")
    if final_files_done:
        print(f"  Done        : {', '.join(final_files_done)}")
    if final_files_hitl:
        print(f"  HITL Queue  : {', '.join(final_files_hitl)}")
    if final_skills_run:
        print(f"  Skills Run  : {', '.join(final_skills_run)}")
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
        description="Ralph Wiggum Reasoning Loop — Gold Tier AI Employee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""
            Examples:
              python tools/ralph_loop_runner.py
              python tools/ralph_loop_runner.py --max-iterations 20
              python tools/ralph_loop_runner.py "Process multi-step task in Needs_Action" --max-iterations 20
              python tools/ralph_loop_runner.py --dry-run
              python tools/ralph_loop_runner.py --create-test-lead

            /ralph-loop shortcut:
              /ralph-loop "Process multi-step task in Needs_Action" --max-iterations 20
              maps to: python tools/ralph_loop_runner.py <same args>

            Multi-step test (full pipeline):
              Step 1  Create a test sales lead:
                        python tools/ralph_loop_runner.py --create-test-lead
              Step 2  Run the loop:
                        python tools/ralph_loop_runner.py \\
                          "Process multi-step task in Needs_Action" \\
                          --max-iterations 20
              Step 3  Check output:
                        Plans/Plan_*.md                — checkbox plan
                        Pending Approval/sales_lead_*  — queued for HITL
                        Logs/ralph_loop_*.md           — loop transcript
                        Logs/audit_*.json              — structured audit trail

            Default max iterations: {DEFAULT_MAX_ITER}
        """),
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        default=(
            "Process all files in /Needs Action. For each file: classify the task type "
            "(file_drop, payment, sales_lead, communication, or multi_step), create a "
            "Plan.md with YAML frontmatter and checkbox pipeline steps, run "
            "cross_domain_integrator to route items, move payments > $500 and all "
            "sales leads to Pending Approval for HITL review, move standard tasks "
            "to Done. When /Needs Action is empty, call task_complete with a full summary."
        ),
        help="Natural language instruction for the loop (default: full Gold Tier pipeline)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITER,
        metavar="N",
        help=f"Maximum loop iterations before forced stop (default: {DEFAULT_MAX_ITER})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the loop — no files are moved or written",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-iteration console output (log file still written)",
    )
    parser.add_argument(
        "--create-test-lead",
        action="store_true",
        dest="create_test_lead",
        help="Create a realistic multi-step sales lead in /Needs Action, then exit.",
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
