#!/usr/bin/env bash
# Ralph Wiggum Loop Runner — Unix/Mac/Linux Launcher
# Usage: ./ralph-loop.sh "Process Needs_Action" --max-iterations 10
#        ./ralph-loop.sh --dry-run
#        ./ralph-loop.sh --max-iterations 5 --quiet
#
# Setup: chmod +x ralph-loop.sh
# Requires: ANTHROPIC_API_KEY environment variable set.

set -euo pipefail

# Resolve the directory of this script (project root)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check Python
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "ERROR: Python not found. Please install Python 3.10+"
    exit 1
fi

PYTHON=$(command -v python3 || command -v python)

# Check anthropic SDK
if ! "$PYTHON" -c "import anthropic" &>/dev/null; then
    echo "ERROR: anthropic SDK not installed."
    echo "Run: pip install anthropic"
    exit 1
fi

# Check ANTHROPIC_API_KEY
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set."
    echo 'Set it with: export ANTHROPIC_API_KEY="sk-ant-..."'
    exit 1
fi

# Run the loop, passing all arguments through
"$PYTHON" "$PROJECT_ROOT/tools/ralph_loop_runner.py" "$@"
