"""
AI Employee Silver Tier — Ralph Wiggum Loop Runner
=====================================================

Implements a Claude API agentic reasoning loop that autonomously:
  1. Scans /Needs Action for all unprocessed task files
  2. Calls Claude with tool-use to analyze, plan, and act on each file
  3. Writes a detailed Plan.md per task with checkboxes and priorities
  4. Routes HITL-required files to /Pending Approval (payments, leads, posts)
  5. Moves completed files to /Done
  6. Loops until Claude emits TASK_COMPLETE or max iterations reached

Loop Pattern (Ralph Wiggum):
  LOOP START
    → Read /Needs Action state
    → Call Claude: "What is the next step?"
    → Claude calls tools: list_files, read_file, write_file, move_file
    → Execute tool calls
    → Check for task_complete signal
    → If complete or max_iter: EXIT LOOP
    → Else: CONTINUE
  LOOP END

INSTALL:
    pip install anthropic

SETUP:
    Set environment variable:
      Windows : setx ANTHROPIC_API_KEY "your-key-here"
      Linux   : export ANTHROPIC_API_KEY="your-key-here"

RUN:
    python tools/ralph_loop_runner.py
    python tools/ralph_loop_runner.py --max-iterations 10
    python tools/ralph_loop_runner.py --prompt "Process all files in Needs_Action" --max-iterations 5
    python tools/ralph_loop_runner.py --dry-run   # Simulate without moving files

SHORTCUT (Windows):
    ralph-loop.bat "Process Needs_Action" --max-iterations 10

SHORTCUT (Unix/Mac):
    ./ralph-loop.sh "Process Needs_Action" --max-iterations 10

TEST:
    1. Drop a test file in /Needs Action:
         echo "type: file_drop\\nfrom: Test\\nsubject: Urgent invoice $200" > "Needs Action/test_invoice.md"
    2. Run:
         python tools/ralph_loop_runner.py
    3. Watch the loop:
         - Iteration 1: Claude reads and classifies the file
         - Iteration 2: Claude creates Plan.md with checkboxes
         - Iteration 3: Claude moves file to /Done (or /Pending Approval if HITL needed)
         - Claude emits TASK_COMPLETE
    4. Check output:
         /Plans/Plan_[timestamp].md
         /Done/test_invoice.md   (or /Pending Approval/ if HITL)
         /Logs/ralph_loop_[timestamp].md

"""

import os
import sys
import json
import shutil
import argparse
import textwrap
from datetime import datetime
from pathlib import Path

# --- Dependency check ---
try:
    import anthropic
except ImportError:
    print("ERROR: anthropic SDK not installed.")
    print("Run: pip install anthropic")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

NEEDS_ACTION_DIR  = BASE_DIR / "Needs Action"
PLANS_DIR         = BASE_DIR / "Plans"
DONE_DIR          = BASE_DIR / "Done"
PENDING_DIR       = BASE_DIR / "Pending Approval"
REJECTED_DIR      = BASE_DIR / "Rejected"
APPROVED_DIR      = BASE_DIR / "Approved"
LOGS_DIR          = BASE_DIR / "Logs"
HANDBOOK_FILE     = BASE_DIR / "Company Handbook.md"
SKILL2_FILE       = BASE_DIR / "Skills" / "Skill2_TaskAnalyzer.md"
SKILL3_FILE       = BASE_DIR / "Skills" / "Skill3_AutoLinkedInPoster.md"

MODEL             = "claude-sonnet-4-5-20250929"
DEFAULT_MAX_ITER  = 10
TASK_COMPLETE_SIG = "TASK_COMPLETE"

SALES_KEYWORDS    = ["urgent", "invoice", "payment", "sales", "client", "project"]


# ---------------------------------------------------------------------------
# Tool Definitions (Claude tool-use schema)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "list_files",
        "description": (
            "List all .md files in a project directory. "
            "Use this to check what files are in /Needs Action, /Plans, /Done, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": (
                        "Directory to list, relative to project root. "
                        "Examples: 'Needs Action', 'Plans', 'Done', 'Pending Approval'"
                    ),
                }
            },
            "required": ["directory"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the full text content of a file. Use to inspect task files before deciding actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to project root. Example: 'Needs Action/FILE_task.md'",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file. Creates the file if it does not exist, "
            "overwrites if it does. Use to create Plan.md files, update checklists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to project root. Example: 'Plans/Plan_2024-02-19.md'",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write, including YAML frontmatter if applicable.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "move_file",
        "description": (
            "Move a file to a destination folder. "
            "Use to move processed files to /Done, or HITL-required files to /Pending Approval."
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
                        "Destination folder name (relative). "
                        "Options: 'Done', 'Pending Approval', 'Approved', 'Rejected'"
                    ),
                },
            },
            "required": ["source_path", "destination_folder"],
        },
    },
    {
        "name": "task_complete",
        "description": (
            f"Signal '{TASK_COMPLETE_SIG}'. Call this when ALL files in /Needs Action "
            "have been processed, or when no more actionable work remains. "
            "Provide a summary of all completed work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise summary of everything processed in this loop run.",
                },
                "files_processed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of filenames that were fully processed.",
                },
                "files_pending_approval": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of filenames routed to /Pending Approval for HITL review.",
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


def tool_move_file(source_path: str, destination_folder: str, dry_run: bool = False) -> str:
    src = BASE_DIR / source_path
    dst_dir = BASE_DIR / destination_folder

    if not src.exists():
        return f"Source file not found: {source_path}"

    if dry_run:
        return f"[DRY RUN] Would move '{source_path}' → '{destination_folder}/'"

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name

        # Avoid overwrite: suffix counter
        counter = 1
        while dst.exists():
            dst = dst_dir / f"{src.stem}_{counter}{src.suffix}"
            counter += 1

        shutil.move(str(src), str(dst))
        return f"Moved: {source_path} → {destination_folder}/{dst.name}"
    except Exception as e:
        return f"Error moving file: {e}"


def dispatch_tool(name: str, inputs: dict, dry_run: bool = False) -> str:
    """Route a tool call to its executor and return result string."""
    if name == "list_files":
        return tool_list_files(inputs["directory"], dry_run)
    elif name == "read_file":
        return tool_read_file(inputs["path"], dry_run)
    elif name == "write_file":
        return tool_write_file(inputs["path"], inputs["content"], dry_run)
    elif name == "move_file":
        return tool_move_file(inputs["source_path"], inputs["destination_folder"], dry_run)
    elif name == "task_complete":
        return TASK_COMPLETE_SIG  # Handled specially by the loop
    else:
        return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# System Prompt Builder
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    handbook = ""
    if HANDBOOK_FILE.exists():
        handbook = HANDBOOK_FILE.read_text(encoding="utf-8", errors="replace").strip()

    return textwrap.dedent(f"""
        You are the Silver Tier AI Employee running the Ralph Wiggum Reasoning Loop.

        ## Your Mission
        Process every file in /Needs Action using the following pattern:

        LOOP START
          → list_files("Needs Action")                   — see what needs doing
          → read_file(each file)                          — understand the task
          → classify task type                            — File Drop | Payment | Lead | Communication
          → write_file("Plans/Plan_[date].md", ...)       — create detailed Plan with checkboxes + priority
          → decide routing:
              - Standard task  → move_file to "Done"
              - Payment > $500 → move_file to "Pending Approval"  (HITL required)
              - Sales/lead     → draft LinkedIn post stub, move to "Pending Approval"  (HITL required)
              - Multi-step     → iterate until all sub-steps complete
          → When ALL files processed: call task_complete(summary)
        LOOP END

        ## Task Classification Rules
        | Type | Trigger | Action |
        |------|---------|--------|
        | File Drop | type: file_drop | Summarize, create plan, move to Done |
        | Payment Request | payment, invoice, amount | Check handbook; if >$500 → Pending Approval |
        | Sales Lead | sales, client, project, lead | Draft LinkedIn post stub → Pending Approval |
        | Communication | from, message, email | Draft reply plan → move to Done |
        | Multi-Step | complex, multiple tasks | Iterate sub-steps, check off each |

        ## Plan.md Format
        Every Plan.md MUST have:
        ```yaml
        ---
        type: action_plan
        source_file: "[filename]"
        priority: high|medium|low
        status: in_progress|complete|pending_approval
        iteration: [current iteration number]
        created: "[YYYY-MM-DD HH:MM:SS]"
        ---
        ```
        Followed by:
        - ## Summary (1-2 sentences)
        - ## Priority: HIGH / MEDIUM / LOW (with justification)
        - ## Action Steps with checkboxes [ ] / [x]
        - ## Routing Decision (Done / Pending Approval / reason)

        ## Company Handbook Rules
        {handbook}

        ## Completion Signal
        When you have processed all files — or when /Needs Action contains only
        files you have already handled — call the `task_complete` tool with a
        clear summary. This ends the loop.

        ## Iteration Budget
        You have a fixed iteration budget. Do not waste iterations re-reading
        files you have already processed. Track state in your responses.

        ## Output Style
        Be concise in text responses. Let tool calls do the work.
        Do not apologise or over-explain. Act decisively.
    """).strip()


# ---------------------------------------------------------------------------
# Loop Logger
# ---------------------------------------------------------------------------

class LoopLogger:
    def __init__(self, timestamp: str):
        self.timestamp = timestamp
        self.log_path = LOGS_DIR / f"ralph_loop_{timestamp}.md"
        self.lines: list[str] = []
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        self.lines.append(f"# Ralph Loop Run — {timestamp}\n")
        self.lines.append(f"- Model: {MODEL}")
        self.lines.append(f"- Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    def iteration(self, n: int, max_n: int) -> None:
        self.lines.append(f"\n## Iteration {n}/{max_n}")

    def tool_call(self, name: str, inputs: dict, result: str) -> None:
        self.lines.append(f"\n### Tool: `{name}`")
        self.lines.append(f"- Input: `{json.dumps(inputs, ensure_ascii=False)[:200]}`")
        self.lines.append(f"- Result: {result[:300]}")

    def assistant_text(self, text: str) -> None:
        if text.strip():
            self.lines.append(f"\n**Claude:** {text.strip()[:400]}")

    def complete(self, summary: str, reason: str) -> None:
        self.lines.append(f"\n## Loop Complete")
        self.lines.append(f"- Reason: {reason}")
        self.lines.append(f"- Summary: {summary}")
        self.lines.append(f"- Ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.flush()

    def flush(self) -> None:
        try:
            self.log_path.write_text("\n".join(self.lines), encoding="utf-8")
        except Exception:
            pass  # Non-fatal


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
    Run the Ralph Wiggum agentic loop.

    Returns a result dict:
        {
          "status": "complete" | "max_iterations" | "error",
          "iterations": int,
          "summary": str,
          "log_path": Path,
        }
    """

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("Set it with:  setx ANTHROPIC_API_KEY \"sk-ant-...\"  (Windows)")
        print("          or: export ANTHROPIC_API_KEY=\"sk-ant-...\"  (Unix)")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = LoopLogger(ts)

    def log(msg: str) -> None:
        if verbose:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"[{now}] {msg}", flush=True)

    # Ensure all required folders exist
    for d in [NEEDS_ACTION_DIR, PLANS_DIR, DONE_DIR, PENDING_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    system_prompt = build_system_prompt()
    messages: list[dict] = [{"role": "user", "content": prompt}]

    print("=" * 65)
    print("  Ralph Wiggum Reasoning Loop — Silver Tier AI Employee")
    print("=" * 65)
    log(f"Model       : {MODEL}")
    log(f"Max iter    : {max_iterations}")
    log(f"Dry run     : {dry_run}")
    log(f"Prompt      : {prompt}")
    log(f"Log         : {logger.log_path}")
    if dry_run:
        print("  [DRY RUN MODE — no files will actually be moved or written]")
    print("-" * 65)

    status = "max_iterations"
    final_summary = "(no summary)"
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        log(f"\n{'='*20} ITERATION {iteration}/{max_iterations} {'='*20}")
        logger.iteration(iteration, max_iterations)

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
            status = "error"
            final_summary = str(e)
            break
        except anthropic.AuthenticationError:
            log("FATAL: Invalid ANTHROPIC_API_KEY.")
            status = "error"
            final_summary = "Authentication failed."
            break
        except Exception as e:
            log(f"FATAL: Unexpected API error — {e}")
            status = "error"
            final_summary = str(e)
            break

        # Collect assistant message
        assistant_message: dict = {"role": "assistant", "content": response.content}
        messages.append(assistant_message)

        tool_results = []
        complete_called = False

        # Process each content block
        for block in response.content:
            if block.type == "text":
                log(f"Claude: {block.text.strip()[:300]}")
                logger.assistant_text(block.text)

            elif block.type == "tool_use":
                tool_name = block.name
                tool_inputs = block.input
                tool_id = block.id

                log(f"Tool call: {tool_name}({json.dumps(tool_inputs)[:150]})")

                # Execute tool
                tool_result = dispatch_tool(tool_name, tool_inputs, dry_run)
                logger.tool_call(tool_name, tool_inputs, tool_result)

                # Check for completion signal
                if tool_name == "task_complete":
                    final_summary = tool_inputs.get("summary", "(no summary)")
                    files_done = tool_inputs.get("files_processed", [])
                    files_hitl = tool_inputs.get("files_pending_approval", [])
                    complete_called = True
                    log(f"\n{TASK_COMPLETE_SIG} received.")
                    log(f"Summary: {final_summary}")
                    if files_done:
                        log(f"Processed: {', '.join(files_done)}")
                    if files_hitl:
                        log(f"Pending HITL: {', '.join(files_hitl)}")
                    tool_result = TASK_COMPLETE_SIG
                else:
                    log(f"Result: {tool_result[:200]}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": tool_result,
                })

        # Append tool results to messages
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        # Check stop conditions
        if complete_called:
            status = "complete"
            break

        if response.stop_reason == "end_turn" and not tool_results:
            log("Claude stopped without calling task_complete. Ending loop.")
            status = "complete"
            final_summary = "Claude finished reasoning without explicit task_complete call."
            break

    # ----- Loop finished -----

    reason = {
        "complete": f"{TASK_COMPLETE_SIG} received",
        "max_iterations": f"Reached max {max_iterations} iterations",
        "error": "Fatal error occurred",
    }.get(status, status)

    logger.complete(final_summary, reason)

    print()
    print("=" * 65)
    print(f"  LOOP FINISHED — {reason}")
    print("=" * 65)
    print(f"  Iterations used : {iteration}/{max_iterations}")
    print(f"  Status          : {status.upper()}")
    print(f"  Summary         : {final_summary}")
    print(f"  Loop log saved  : {logger.log_path}")
    print("=" * 65)

    return {
        "status": status,
        "iterations": iteration,
        "summary": final_summary,
        "log_path": logger.log_path,
    }


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="ralph-loop",
        description="Ralph Wiggum Reasoning Loop — Silver Tier AI Employee",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python tools/ralph_loop_runner.py
              python tools/ralph_loop_runner.py --max-iterations 5
              python tools/ralph_loop_runner.py --prompt "Process all files in Needs_Action"
              python tools/ralph_loop_runner.py --dry-run
              python tools/ralph_loop_runner.py "Process Needs_Action" --max-iterations 10

            Shortcut (Windows):
              ralph-loop.bat "Process Needs_Action" --max-iterations 10

            Shortcut (Unix):
              ./ralph-loop.sh "Process Needs_Action" --max-iterations 10
        """),
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        default="Process all files in /Needs_Action. Analyze each with Task Analyzer logic, "
                "create a detailed Plan.md in /Plans with checkbox steps and priorities, "
                "route HITL items to /Pending Approval, move completed tasks to /Done.",
        help="Natural language instruction for the loop (default: process all Needs Action files)",
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
        help="Simulate the loop without actually moving or writing files",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-iteration console output (log file still written)",
    )

    args = parser.parse_args()

    ralph_loop(
        prompt=args.prompt,
        max_iterations=args.max_iterations,
        dry_run=args.dry_run,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
