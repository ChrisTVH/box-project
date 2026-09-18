#!/bin/bash
# Thin wrapper around tools/versioning.py -- keep all logic in Python.
# Usage: ./get_version.sh [path-to-repo]
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m tools.versioning "$@"
