"""
Master Orchestrator
===================
Monitors the /Approved folder for POST_*.md files using watchdog and
dispatches each file to social_media_executor_v2.py as a subprocess.

State machine per file:
  DETECTED → RUNNING → DONE (moved by executor)
                     ↘ RETRY (up to 3×) → COOLDOWN (5 min) → RETRY again

Usage:
    python scripts/master_orchestrator.py

Install deps:
    pip install watchdog

Log output:
    Logs/orchestrator_YYYY-MM-DD.log   (new file each day, appended)
"""

import os
import sys
import time
import subprocess
import threading
import logging
from pathlib import Path
from datetime import datetime, timedelta
from enum import Enum, auto
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent
except ImportError:
    print("[FATAL] watchdog is not installed. Run: pip install watchdog")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

APPROVED_DIR = PROJECT_ROOT / "Approved"
DONE_DIR     = PROJECT_ROOT / "Done"
LOGS_DIR     = PROJECT_ROOT / "Logs"

EXECUTOR     = SCRIPT_DIR / "social_media_executor_v2.py"
PYTHON_EXE   = sys.executable   # same interpreter that's running this script

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
POLL_INTERVAL_S  = 5       # seconds between directory scans
MAX_RETRIES      = 3
COOLDOWN_MINUTES = 5
FILE_PATTERN     = "POST_"  # only process files whose name starts with this

# ---------------------------------------------------------------------------
# Logger setup — writes to console AND a daily log file
# ---------------------------------------------------------------------------
def build_logger() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"orchestrator_{datetime.now().strftime('%Y-%m-%d')}.log"

    fmt = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    logger = logging.getLogger("orchestrator")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

log = build_logger()

# ---------------------------------------------------------------------------
# File state tracking
# ---------------------------------------------------------------------------
class FileState(Enum):
    QUEUED   = auto()
    RUNNING  = auto()
    COOLDOWN = auto()
    DONE     = auto()
    FAILED   = auto()   # exhausted all retries

@dataclass
class FileRecord:
    path:         Path
    retries:      int       = 0
    state:        FileState = FileState.QUEUED
    cooldown_until: datetime | None = None

# ---------------------------------------------------------------------------
# Orchestrator core
# ---------------------------------------------------------------------------
class Orchestrator:
    """
    Thread-safe registry of known files + dispatcher loop.
    """

    def __init__(self) -> None:
        self._lock:    threading.Lock        = threading.Lock()
        self._records: dict[str, FileRecord] = {}  # key = filename stem

    # -----------------------------------------------------------------------
    # Public: called from watchdog thread or the poll loop
    # -----------------------------------------------------------------------
    def enqueue(self, file_path: Path) -> None:
        key = file_path.name
        with self._lock:
            if key in self._records:
                rec = self._records[key]
                # Only re-enqueue if it somehow came back (edge case)
                if rec.state in (FileState.DONE, FileState.FAILED):
                    log.debug(f"[QUEUE] Ignoring already-terminal file: {key}")
                    return
                log.debug(f"[QUEUE] Already tracking {key} (state={rec.state.name}) — skip.")
                return
            rec = FileRecord(path=file_path)
            self._records[key] = rec
            log.info(f"[QUEUE] Enqueued: {key}")

    # -----------------------------------------------------------------------
    # Main dispatch loop — runs on the main thread
    # -----------------------------------------------------------------------
    def run_forever(self) -> None:
        log.info("[ORCH] Orchestrator started. Watching: %s", APPROVED_DIR)
        log.info("[ORCH] Poll interval: %ss | Max retries: %s | Cooldown: %s min",
                 POLL_INTERVAL_S, MAX_RETRIES, COOLDOWN_MINUTES)
        try:
            while True:
                self._scan_approved_dir()
                self._dispatch_pending()
                time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            log.info("[ORCH] Shutdown requested (KeyboardInterrupt). Bye.")

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------
    def _scan_approved_dir(self) -> None:
        """Pick up any POST_*.md files sitting in Approved/ that aren't tracked yet."""
        if not APPROVED_DIR.exists():
            return
        for f in APPROVED_DIR.iterdir():
            if f.is_file() and f.suffix == ".md" and f.name.startswith(FILE_PATTERN):
                self.enqueue(f)

    def _dispatch_pending(self) -> None:
        """Process all QUEUED / COOLDOWN-expired records sequentially."""
        with self._lock:
            candidates = list(self._records.values())

        for rec in candidates:
            with self._lock:
                if rec.state == FileState.RUNNING:
                    continue
                if rec.state in (FileState.DONE, FileState.FAILED):
                    continue
                if rec.state == FileState.COOLDOWN:
                    if rec.cooldown_until and datetime.now() < rec.cooldown_until:
                        remaining = (rec.cooldown_until - datetime.now()).seconds
                        log.debug(f"[COOL] {rec.path.name} — {remaining}s cooldown remaining.")
                        continue
                    # Cooldown expired — back to QUEUED for another attempt
                    rec.state = FileState.QUEUED
                    log.info(f"[COOL] Cooldown expired for {rec.path.name} — re-queuing.")

                # File must still physically exist
                if not rec.path.exists():
                    log.info(f"[SKIP] {rec.path.name} no longer in Approved/ — marking DONE.")
                    rec.state = FileState.DONE
                    continue

                rec.state = FileState.RUNNING

            # Run outside the lock so we don't block watchdog callbacks
            self._execute(rec)

    def _execute(self, rec: FileRecord) -> None:
        attempt = rec.retries + 1
        log.info(f"[RUN ] {rec.path.name} — attempt {attempt}/{MAX_RETRIES}")

        try:
            result = subprocess.run(
                [PYTHON_EXE, str(EXECUTOR), str(rec.path)],
                capture_output=False,   # let executor print to console
                timeout=300,            # 5 min hard cap per post attempt
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            log.error(f"[TIMEOUT] {rec.path.name} — executor timed out after 300s.")
            exit_code = -1
        except Exception as exc:
            log.error(f"[ERROR] {rec.path.name} — failed to launch executor: {exc}")
            exit_code = -1

        with self._lock:
            if exit_code == 0:
                # Executor moves file to Done/ itself on success
                rec.state = FileState.DONE
                log.info(f"[DONE] {rec.path.name} — posted successfully.")
                self._append_summary(rec.path.name, success=True, attempt=attempt)
            else:
                rec.retries += 1
                if rec.retries >= MAX_RETRIES:
                    # Enter cooldown instead of hard-failing forever
                    rec.state = FileState.COOLDOWN
                    rec.cooldown_until = datetime.now() + timedelta(minutes=COOLDOWN_MINUTES)
                    log.warning(
                        f"[FAIL] {rec.path.name} — all {MAX_RETRIES} retries exhausted. "
                        f"Cooling down until {rec.cooldown_until.strftime('%H:%M:%S')}."
                    )
                    self._append_summary(rec.path.name, success=False, attempt=attempt)
                else:
                    rec.state = FileState.QUEUED
                    log.warning(
                        f"[RETRY] {rec.path.name} — attempt {attempt} failed (exit={exit_code}). "
                        f"Will retry ({rec.retries}/{MAX_RETRIES} used)."
                    )

    def _append_summary(self, filename: str, success: bool, attempt: int) -> None:
        """Append a one-liner summary row to today's log."""
        status = "SUCCESS" if success else f"FAILED (after {MAX_RETRIES} retries)"
        line = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"SUMMARY | {filename} | {status} | attempts={attempt}\n"
        )
        try:
            log_file = LOGS_DIR / f"orchestrator_{datetime.now().strftime('%Y-%m-%d')}.log"
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass  # non-fatal

# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------
class ApprovedFolderHandler(FileSystemEventHandler):
    """
    Listens for new .md files dropped into the Approved/ folder
    and immediately notifies the orchestrator.
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        super().__init__()
        self._orch = orchestrator

    def _maybe_enqueue(self, path_str: str) -> None:
        p = Path(path_str)
        if p.suffix == ".md" and p.name.startswith(FILE_PATTERN):
            log.info(f"[WATCH] Detected new file: {p.name}")
            self._orch.enqueue(p)

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._maybe_enqueue(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        # Handle files moved/renamed INTO Approved/
        if not event.is_directory:
            dest = Path(event.dest_path)
            if dest.parent.resolve() == APPROVED_DIR.resolve():
                self._maybe_enqueue(event.dest_path)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    # Sanity checks
    if not EXECUTOR.exists():
        log.error(f"[FATAL] Executor script not found: {EXECUTOR}")
        log.error("        Make sure social_media_executor_v2.py is in scripts/")
        sys.exit(1)

    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    orch    = Orchestrator()
    handler = ApprovedFolderHandler(orch)

    observer = Observer()
    observer.schedule(handler, str(APPROVED_DIR), recursive=False)
    observer.start()
    log.info(f"[WATCH] watchdog observer started on: {APPROVED_DIR}")

    try:
        orch.run_forever()          # blocks; handles KeyboardInterrupt internally
    finally:
        observer.stop()
        observer.join()
        log.info("[WATCH] watchdog observer stopped.")


if __name__ == "__main__":
    main()
