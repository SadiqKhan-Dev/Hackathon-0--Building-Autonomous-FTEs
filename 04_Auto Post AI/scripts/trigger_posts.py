"""
trigger_posts.py — Draft a social media post for human review.

Usage:
    python scripts/trigger_posts.py --platform linkedin --content "Excited to share!"
    python scripts/trigger_posts.py --platform facebook
    python scripts/trigger_posts.py  (uses all defaults)

Output: Pending_Approval/POST_YYYY-MM-DD_HHMMSS.md
"""

import argparse
import os
from datetime import datetime

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PLATFORM = "linkedin"
DEFAULT_CONTENT = (
    "Thrilled to announce something exciting is coming! "
    "Stay tuned for updates — big things are on the way. #AI #Innovation"
)

VALID_PLATFORMS = {"linkedin", "facebook", "twitter", "instagram"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PENDING_DIR = os.path.join(BASE_DIR, "Pending_Approval")


def build_markdown(platform: str, content: str, created: str) -> str:
    """Return a Markdown post with YAML frontmatter."""
    return f"""---
platform: {platform}
content: "{content}"
status: pending_approval
priority: medium
created: "{created}"
type: social_post_draft
---

# Post Draft — {platform.capitalize()}

**Platform:** {platform.capitalize()}
**Created:** {created}
**Status:** Pending Approval

## Content

{content}

---
*Review this file, then move it to `Approved/` to publish or `Rejected/` to discard.*
"""


def save_post(platform: str, content: str) -> str:
    """Generate the draft file and return its path."""
    os.makedirs(PENDING_DIR, exist_ok=True)

    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H%M%S")
    created_str = now.strftime("%Y-%m-%d %H:%M:%S")

    filename = f"POST_{timestamp}.md"
    filepath = os.path.join(PENDING_DIR, filename)

    markdown = build_markdown(platform, content, created_str)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(markdown)

    return filepath


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draft a social media post and save it for HITL approval."
    )
    parser.add_argument(
        "--platform",
        default=DEFAULT_PLATFORM,
        choices=sorted(VALID_PLATFORMS),
        help=f"Target social platform (default: {DEFAULT_PLATFORM})",
    )
    parser.add_argument(
        "--content",
        default=DEFAULT_CONTENT,
        help="Post body text (default: a sample placeholder post)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    platform = args.platform.lower()
    content = args.content.strip()

    if not content:
        print("[ERROR] --content cannot be empty. Using default content.")
        content = DEFAULT_CONTENT

    print(f"[trigger_posts] Platform : {platform}")
    print(f"[trigger_posts] Content  : {content[:80]}{'...' if len(content) > 80 else ''}")

    filepath = save_post(platform, content)

    print(f"[trigger_posts] Draft saved -> {filepath}")
    print()
    print("Next steps:")
    print(f"  1. Open: {filepath}")
    print("  2. Review the content.")
    print("  3. Move to Approved/ to publish, or Rejected/ to discard.")


if __name__ == "__main__":
    main()
