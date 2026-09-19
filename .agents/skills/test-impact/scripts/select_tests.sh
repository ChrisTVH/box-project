#!/bin/bash
# Thin wrapper around tools/test/test_selector.py -- keep all logic in Python.
# Usage: ./select_tests.sh [path-to-repo] [--base <ref>] [--run] [--quiet]
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m tools.test.test_selector "$@"
