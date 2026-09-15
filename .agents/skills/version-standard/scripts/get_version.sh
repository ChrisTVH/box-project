#!/bin/bash
# Computes the version using the year.month.commit-count-in-month scheme
# Usage: ./get_version.sh [path-to-repo]
set -e

REPO_PATH="${1:-.}"
cd "$REPO_PATH"

YEAR=$(date +%y)
MONTH=$(date +%-m)
FIRST_DAY=$(date +%Y-%m-01)

COUNT=$(git log --since="$FIRST_DAY 00:00:00" --oneline | wc -l | tr -d ' ')

echo "${YEAR}.${MONTH}.${COUNT}"
