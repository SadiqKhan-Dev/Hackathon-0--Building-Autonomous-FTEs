"""
AI Employee Bronze Tier - File System Watcher
==============================================

Monitors the /Inbox folder and processes new files automatically.

INSTALL:
    pip install watchdog

RUN:
    python watchers/filesystem_watcher.py

TEST:
    Drop any file in /Inbox folder and watch the console output.
    Check /Needs Action folder for processed files.

"""

import os
import sys
import time
import shutil
import traceback
from datetime import datetime
from pathlib import Path

# --- Gold Tier Error Recovery ---
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "Skills"
sys.path.insert(0, str(_SKILLS_DIR))
from error_recovery import with_retry, log_watcher_error  # noqa: E402

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("ERROR: watchdog library not installed.")
    print("Please run: pip install watchdog")
    sys.exit(1)


# Configuration
BASE_DIR = Path(__file__).resolve().parent.parent
INBOX_DIR = BASE_DIR / "Inbox"
NEEDS_ACTION_DIR = BASE_DIR / "Needs Action"
CHECK_INTERVAL = 5  # seconds


def log(message: str) -> None:
    """Print timestamped log message to console."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def get_file_size(filepath: Path) -> int:
    """Get file size in bytes."""
    try:
        return filepath.stat().st_size
    except OSError:
        return 0


def create_metadata_file(original_name: str, size: int, dest_dir: Path) -> Path:
    """Create YAML metadata file for the processed file."""
    metadata_filename = f"FILE_{Path(original_name).stem}_metadata.md"
    metadata_path = dest_dir / metadata_filename

    metadata_content = f"""---
type: file_drop
original_name: {original_name}
size: {size}
status: pending
processed_at: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
---

# File Drop: {original_name}

A new file has been dropped in the Inbox and requires action.

## Details
- **Original Name:** {original_name}
- **Size:** {size} bytes
- **Status:** Pending

## Next Steps
- [ ] Review file contents
- [ ] Determine required action
- [ ] Process and move to Done
"""

    try:
        with open(metadata_path, 'w', encoding='utf-8') as f:
            f.write(metadata_content)
        return metadata_path
    except Exception as e:
        log(f"ERROR: Failed to create metadata file: {e}")
        return None


def process_file(filepath: Path) -> bool:
    """
    Process a new file from Inbox.
    - Copy to /Needs Action with FILE_ prefix
    - Create metadata file
    """
    try:
        # Skip if not a file or if it's a temp file
        if not filepath.is_file():
            return False

        if filepath.name.startswith('.') or filepath.name.startswith('~'):
            log(f"SKIP: Temporary file ignored - {filepath.name}")
            return False

        original_name = filepath.name
        file_size = get_file_size(filepath)

        # Create destination filename with FILE_ prefix
        dest_filename = f"FILE_{original_name}"
        dest_path = NEEDS_ACTION_DIR / dest_filename

        # Copy file to Needs Action (with retry for locked/in-use files)
        with_retry(
            shutil.copy2, filepath, dest_path,
            label="fs_copy_to_needs_action",
            max_retries=3, base_delay=1, max_delay=10,
        )
        log(f"COPIED: {original_name} -> {dest_path}")

        # Create metadata file
        metadata_path = create_metadata_file(original_name, file_size, NEEDS_ACTION_DIR)
        if metadata_path:
            log(f"METADATA: Created {metadata_path.name}")

        log(f"SUCCESS: File processed - {original_name} ({file_size} bytes)")
        return True

    except PermissionError as e:
        log(f"ERROR: Permission denied - {filepath.name}")
        log_watcher_error("filesystem", e, context=f"PermissionError copying {filepath.name}")
        return False
    except FileNotFoundError as e:
        log(f"ERROR: File not found - {filepath.name}")
        log_watcher_error("filesystem", e, context=f"FileNotFoundError for {filepath.name}")
        return False
    except Exception as e:
        tb = traceback.format_exc()
        log(f"ERROR: Failed to process {filepath.name} - {e}")
        log_watcher_error("filesystem", e, context=f"process_file failed for {filepath.name}", tb=tb)
        return False


class InboxHandler(FileSystemEventHandler):
    """Handle file system events in the Inbox folder."""

    def __init__(self):
        super().__init__()
        self.processed_files = set()

    def on_created(self, event):
        """Called when a file is created in the watched directory."""
        if event.is_directory:
            return

        filepath = Path(event.src_path)

        # Avoid processing the same file multiple times
        if filepath.name in self.processed_files:
            return

        # Small delay to ensure file is fully written
        time.sleep(0.5)

        log(f"DETECTED: New file - {filepath.name}")

        if process_file(filepath):
            self.processed_files.add(filepath.name)

    def on_modified(self, event):
        """Called when a file is modified (sometimes triggered on creation)."""
        if event.is_directory:
            return

        filepath = Path(event.src_path)

        # Only process if not already processed
        if filepath.name not in self.processed_files:
            # Small delay to ensure file is fully written
            time.sleep(0.5)

            if filepath.exists() and filepath.is_file():
                log(f"DETECTED: Modified file - {filepath.name}")
                if process_file(filepath):
                    self.processed_files.add(filepath.name)


def ensure_directories() -> bool:
    """Ensure required directories exist."""
    try:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        NEEDS_ACTION_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except Exception as e:
        log(f"ERROR: Failed to create directories - {e}")
        return False


def main():
    """Main entry point for the file system watcher."""
    print("=" * 60)
    print("AI Employee Bronze Tier - File System Watcher")
    print("=" * 60)
    print()

    # Ensure directories exist
    if not ensure_directories():
        sys.exit(1)

    log(f"BASE DIR: {BASE_DIR}")
    log(f"WATCHING: {INBOX_DIR}")
    log(f"OUTPUT: {NEEDS_ACTION_DIR}")
    log(f"CHECK INTERVAL: {CHECK_INTERVAL} seconds")
    print()
    log("Watcher started. Press Ctrl+C to stop.")
    print("-" * 60)

    # Set up the observer
    event_handler = InboxHandler()
    observer = Observer()
    observer.schedule(event_handler, str(INBOX_DIR), recursive=False)

    try:
        observer.start()
        while True:
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print()
        log("Shutdown requested...")
        observer.stop()
    except Exception as e:
        tb = traceback.format_exc()
        log(f"ERROR: Watcher crashed - {e}")
        log_watcher_error("filesystem", e, context="observer crashed in main loop", tb=tb)
        observer.stop()

    observer.join()
    log("Watcher stopped.")
    print("=" * 60)


if __name__ == "__main__":
    main()
