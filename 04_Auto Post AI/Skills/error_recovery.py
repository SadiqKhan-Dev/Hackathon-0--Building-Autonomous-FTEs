"""
Gold Tier Error Recovery — Shared Utility Module
=================================================

Provides:
  with_retry()               — exponential backoff (max 3 retries, 1–60s delay)
  log_watcher_error()        — append to /Logs/error_{watcher}_{date}.log
  log_skill_error()          — append to /Errors/skill_error_{date}.md
  write_manual_action_plan() — MCP/API fail degradation → /Plans/manual_{skill}_{date}.md

IMPORT IN WATCHERS:
  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Skills"))
  from error_recovery import with_retry, log_watcher_error

IMPORT IN SKILLS:
  from error_recovery import log_skill_error, write_manual_action_plan
"""

import time
import traceback
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent.parent
LOGS_DIR   = BASE_DIR / "Logs"
ERRORS_DIR = BASE_DIR / "Errors"
PLANS_DIR  = BASE_DIR / "Plans"

# Ensure UTF-8 output on Windows
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Exponential Backoff Retry
# max_retries=3, delays: 1s → 2s → 4s … capped at max_delay (60s)
# ---------------------------------------------------------------------------

def with_retry(fn, *args, max_retries: int = 3, base_delay: float = 1.0,
               max_delay: float = 60.0, label: str = "", **kwargs):
    """
    Call fn(*args, **kwargs) with exponential backoff on any exception.

    - max_retries : total attempts (default 3)
    - base_delay  : first retry wait in seconds (default 1)
    - max_delay   : cap on retry delay in seconds (default 60)
    - label       : human-readable name shown in retry log lines

    Returns fn's return value on success.
    Raises the last exception if all retries are exhausted.
    """
    label     = label or getattr(fn, "__name__", "operation")
    delay     = base_delay
    last_exc  = None

    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = min(delay, max_delay)
                print(
                    f"[{_iso()}] RETRY [{label}] attempt {attempt}/{max_retries} "
                    f"failed — {type(exc).__name__}: {exc}. "
                    f"Retrying in {wait:.0f}s...",
                    flush=True,
                )
                time.sleep(wait)
                delay = min(delay * 2, max_delay)
            else:
                print(
                    f"[{_iso()}] RETRY [{label}] all {max_retries} attempts "
                    f"exhausted — {type(exc).__name__}: {exc}",
                    flush=True,
                )

    raise last_exc


# ---------------------------------------------------------------------------
# Watcher Error Logger → /Logs/error_{watcher}_{date}.log
# ---------------------------------------------------------------------------

def log_watcher_error(
    watcher_name: str,
    error: Exception,
    context: str = "",
    tb: str = "",
) -> Path:
    """
    Append a structured error entry to /Logs/error_{watcher}_{date}.log.

    Args:
        watcher_name : short watcher identifier, e.g. "gmail", "linkedin"
        error        : the caught exception
        context      : optional human-readable description of what was happening
        tb           : optional pre-formatted traceback string (uses current
                       traceback if omitted)

    Returns: path to the log file.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    safe_name = watcher_name.lower().replace(" ", "_")
    log_path  = LOGS_DIR / f"error_{safe_name}_{_date()}.log"
    tb_text   = tb or traceback.format_exc()

    entry = (
        f"\n{'=' * 60}\n"
        f"TIMESTAMP : {_iso()}\n"
        f"WATCHER   : {watcher_name}\n"
        f"ERROR     : {type(error).__name__}: {error}\n"
        f"CONTEXT   : {context or 'No additional context'}\n"
        f"TRACEBACK :\n{tb_text.strip()}\n"
        f"{'=' * 60}\n"
    )

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        pass  # Never crash the watcher just because error logging failed

    return log_path


# ---------------------------------------------------------------------------
# Skill Error Logger → /Errors/skill_error_{date}.md
# ---------------------------------------------------------------------------

def log_skill_error(
    skill_name: str,
    error: Exception,
    context: str = "",
    tb: str = "",
) -> Path:
    """
    Append a structured error report to /Errors/skill_error_{date}.md.

    Multiple skills writing on the same day append to the same file.
    Returns the error file path.
    """
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)

    date_str = _date()
    log_path = ERRORS_DIR / f"skill_error_{date_str}.md"
    tb_text  = tb or traceback.format_exc()

    # Write a day-header if this is the first entry of the day
    is_new = not log_path.exists() or log_path.stat().st_size == 0
    header  = f"# Skill Error Log — {date_str}\n\n" if is_new else ""

    entry = (
        f"{header}"
        f"---\n"
        f"skill: {skill_name}\n"
        f'error_type: {type(error).__name__}\n'
        f'timestamp: "{_iso()}"\n'
        f"---\n\n"
        f"## [{_iso()}] {skill_name} — {type(error).__name__}\n\n"
        f"**Error:** `{type(error).__name__}: {error}`\n\n"
        f"**Context:** {context or 'No additional context'}\n\n"
        f"**Traceback:**\n"
        f"```\n{tb_text.strip()}\n```\n\n"
    )

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        pass

    return log_path


# ---------------------------------------------------------------------------
# MCP / API Degradation → /Plans/manual_{skill}_{date}.md
# ---------------------------------------------------------------------------

def write_manual_action_plan(
    skill_name: str,
    action_description: str,
    context: str = "",
    error: str = "",
) -> Path:
    """
    When an MCP tool or external API fails, write a human-readable manual
    action plan to /Plans/ so the task is not lost.

    Args:
        skill_name         : e.g. "Skill3_AutoLinkedInPoster"
        action_description : what the human needs to do to complete the action
        context            : additional details (input data, target, etc.)
        error              : short error string for reference

    Returns: path to the written plan file.
    """
    PLANS_DIR.mkdir(parents=True, exist_ok=True)

    date_str  = _date()
    ts        = _iso()
    safe_name = skill_name.lower().replace(" ", "_").replace(":", "")
    filename  = f"manual_{safe_name}_{date_str}.md"

    plan_path = PLANS_DIR / filename
    counter   = 1
    while plan_path.exists():
        plan_path = PLANS_DIR / f"manual_{safe_name}_{date_str}_{counter}.md"
        counter  += 1

    content = f"""---
type: manual_action
skill: "{skill_name}"
status: pending_approval
priority: high
created: "{ts}"
reason: "Automated skill failed — manual action required"
---

# Manual Action Required: {skill_name}

> **The automated skill encountered an error and could not complete this action.**
> A human must complete it manually or re-run the skill after fixing the issue.

## Action Required

{action_description}

---

## Failure Details

| Field      | Value |
|------------|-------|
| Skill      | {skill_name} |
| Failed At  | {ts} |
| Error      | `{error or f"See /Errors/skill_error_{date_str}.md"}` |

## Additional Context

{context or 'No additional context provided.'}

## Recovery Steps

1. Review the error in `/Errors/skill_error_{date_str}.md`
2. Complete the action manually as described above
3. Move this file to `/Done/` when complete
4. Investigate and resolve the root cause before re-running the skill

---

*Auto-generated by Gold Tier Error Recovery*
*Skill: {skill_name} | Created: {ts}*
"""

    try:
        plan_path.write_text(content, encoding="utf-8")
    except OSError:
        pass

    return plan_path
