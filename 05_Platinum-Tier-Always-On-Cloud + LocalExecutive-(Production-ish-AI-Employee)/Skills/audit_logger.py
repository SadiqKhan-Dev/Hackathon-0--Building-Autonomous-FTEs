"""
Gold Tier — Audit Logger
========================

Append-only structured audit trail for every skill action in the system.

LOG FILE  : /Logs/audit_YYYY-MM-DD.json
FORMAT    : JSON Lines — one JSON object per line for atomic, streaming appends.
RETENTION : 90 days — auto-purged by purge_old_logs() (called from weekly briefer).

ENTRY SCHEMA:
  timestamp       : "YYYY-MM-DD HH:MM:SS"
  action_type     : skill_start | skill_complete | skill_error |
                    item_drafted | item_routed | item_queued |
                    hitl_queued | hitl_executed | hitl_rejected |
                    briefing_written | log_purged
  actor           : skill name  e.g. "Skill3_AutoLinkedInPoster"
  target          : file path, email address, or "" if not applicable
  parameters      : dict — relevant key/values (keyword, platform, lead_score …)
  approval_status : "n/a" | "pending" | "approved" | "rejected" | "executed"
  result          : "started" | "success" | "failed: <msg>" | short summary

IMPORT IN SKILLS:
  from audit_logger import log_action, purge_old_logs, get_weekly_summary

DESIGN RULES:
  - log_action() NEVER raises — audit failures are silently swallowed.
  - All other functions also catch and suppress their own exceptions so that a
    broken log directory never blocks a skill from running.
  - JSON Lines lets any tool (jq, Python, Excel) read partial logs without
    loading the whole file into memory.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR           = Path(__file__).resolve().parent.parent
LOGS_DIR           = BASE_DIR / "Logs"
LOG_RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _audit_path(date_str: str | None = None) -> Path:
    return LOGS_DIR / f"audit_{date_str or _date()}.json"


# ---------------------------------------------------------------------------
# Primary API: log_action
# ---------------------------------------------------------------------------

def log_action(
    action_type:     str,
    actor:           str,
    target:          str = "",
    parameters:      dict | None = None,
    approval_status: str = "n/a",
    result:          str = "",
) -> None:
    """
    Append one structured JSON entry to today's audit log file.

    Args:
        action_type     : Event category (skill_start, hitl_executed, …).
        actor           : Skill or module name writing the entry.
        target          : Affected file path, email, or empty string.
        parameters      : Arbitrary key/value context dict.
        approval_status : Human-review state ("n/a", "pending", "approved", …).
        result          : Short outcome string ("started", "success", "failed: …").

    Never raises — all errors are silently ignored so that audit logging
    never disrupts the calling skill.
    """
    entry = {
        "timestamp":       _iso(),
        "action_type":     action_type,
        "actor":           actor,
        "target":          target,
        "parameters":      parameters or {},
        "approval_status": approval_status,
        "result":          result,
    }
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(_audit_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Retention: purge_old_logs
# ---------------------------------------------------------------------------

def purge_old_logs(days: int = LOG_RETENTION_DAYS) -> list[str]:
    """
    Delete audit_*.json files whose date is older than `days` days.

    Returns the list of deleted filenames (may be empty).
    Writes a 'log_purged' audit entry after deletion.
    Safe to call on every weekly briefer run.
    """
    deleted: list[str] = []

    try:
        if not LOGS_DIR.exists():
            return deleted

        cutoff = datetime.now() - timedelta(days=days)

        for f in sorted(LOGS_DIR.glob("audit_*.json")):
            try:
                date_str  = f.stem.replace("audit_", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
                    deleted.append(f.name)
            except (ValueError, OSError):
                continue

        if deleted:
            log_action(
                action_type="log_purged",
                actor="AuditLogger",
                target=str(LOGS_DIR),
                parameters={
                    "deleted_count":   len(deleted),
                    "retention_days":  days,
                    "deleted_files":   deleted[:10],
                },
                approval_status="n/a",
                result=f"Deleted {len(deleted)} file(s) older than {days} days",
            )

    except Exception:
        pass

    return deleted


# ---------------------------------------------------------------------------
# Analytics: get_weekly_summary
# ---------------------------------------------------------------------------

def get_weekly_summary(days: int = 7) -> dict:
    """
    Aggregate the last `days` audit log files into summary stats.

    Returns a dict:
      {
        "total_actions" : int,
        "by_type"       : {action_type: count, …},
        "by_actor"      : {actor: count, …},
        "errors"        : int,          # entries with action_type containing "error"
                                        # or result starting with "failed"
        "files_read"    : int,
        "period_days"   : int,
        "top_targets"   : [(target, count), …],   # top 5 most-hit targets
      }

    Never raises.
    """
    empty: dict = {
        "total_actions": 0,
        "by_type":       {},
        "by_actor":      {},
        "errors":        0,
        "files_read":    0,
        "period_days":   days,
        "top_targets":   [],
    }

    try:
        if not LOGS_DIR.exists():
            return empty

        cutoff    = datetime.now() - timedelta(days=days)
        total     = 0
        by_type:   dict[str, int] = {}
        by_actor:  dict[str, int] = {}
        by_target: dict[str, int] = {}
        errors     = 0
        files_read = 0

        for f in sorted(LOGS_DIR.glob("audit_*.json")):
            try:
                date_str  = f.stem.replace("audit_", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    continue
            except ValueError:
                continue

            try:
                for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    total += 1
                    atype  = entry.get("action_type", "unknown")
                    actor  = entry.get("actor",       "unknown")
                    target = entry.get("target",      "")
                    res    = entry.get("result",      "")

                    by_type[atype]  = by_type.get(atype, 0)  + 1
                    by_actor[actor] = by_actor.get(actor, 0) + 1

                    if target:
                        by_target[target] = by_target.get(target, 0) + 1

                    if "error" in atype or res.lower().startswith("failed"):
                        errors += 1

                files_read += 1

            except Exception:
                continue

        top_targets = sorted(by_target.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "total_actions": total,
            "by_type":       by_type,
            "by_actor":      by_actor,
            "errors":        errors,
            "files_read":    files_read,
            "period_days":   days,
            "top_targets":   top_targets,
        }

    except Exception:
        return empty


# ---------------------------------------------------------------------------
# Convenience: build_markdown_section  (used by weekly_audit_briefer)
# ---------------------------------------------------------------------------

def build_markdown_section(summary: dict) -> str:
    """
    Convert a get_weekly_summary() dict into a Markdown section for the CEO briefing.
    """
    total      = summary["total_actions"]
    errors     = summary["errors"]
    files_read = summary["files_read"]
    days       = summary["period_days"]
    by_type    = summary["by_type"]
    by_actor   = summary["by_actor"]
    top_targets = summary["top_targets"]

    if total == 0:
        return (
            f"*No audit entries found for the past {days} days.*\n\n"
            "> Ensure skills are importing `audit_logger` and calling `log_action()`."
        )

    error_rate = f"{errors / total * 100:.1f}%" if total else "0%"

    # Action type breakdown table
    type_rows = "\n".join(
        f"| {atype:<28} | {count:>6} |"
        for atype, count in sorted(by_type.items(), key=lambda x: x[1], reverse=True)
    )

    # Actor breakdown table
    actor_rows = "\n".join(
        f"| {actor:<35} | {count:>6} |"
        for actor, count in sorted(by_actor.items(), key=lambda x: x[1], reverse=True)
    )

    # Top targets
    target_section = ""
    if top_targets:
        target_rows = "\n".join(
            f"| {t[:50]:<50} | {c:>5} |"
            for t, c in top_targets
        )
        target_section = (
            "\n### Top Targets\n\n"
            "| Target | Hits |\n"
            "|--------|------|\n"
            f"{target_rows}\n"
        )

    return f"""### Overview

| Metric | Value |
|--------|-------|
| Period | Last {days} days |
| Log files read | {files_read} |
| Total audit entries | {total} |
| Errors / failures | {errors} ({error_rate}) |

### Actions by Type

| Action Type | Count |
|-------------|-------|
{type_rows}

### Actions by Actor (Skill)

| Actor | Count |
|-------|-------|
{actor_rows}
{target_section}
{"**⚠ Error rate above 5% — review `/Errors/skill_error_*.md` for details.**" if total and errors / total > 0.05 else ""}
"""
