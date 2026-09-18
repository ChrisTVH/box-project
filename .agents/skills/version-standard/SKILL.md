---
name: version-standard
description: Computes the project's version number using the year.month.commit-count scheme (e.g. 26.8.5 = year 2026, month 8, 5th commit of that month). Use this whenever the user asks for the current version, wants to tag a release, or mentions "versioning", "version", "release version", or "what version is this". Always run this instead of guessing or hand-counting commits.
---

# Version Standard

Format: `year.month.commit`

- **year**: last 2 digits of the year the commit was made.
- **month**: month of the commit, no leading zero (1-12).
- **commit**: how many commits have landed in that month so far (1st commit of the month = 1, 3rd = 3).

Example: `26.8.5` -> 2026, August, 5th commit of August.

## How to compute it

Run the module from the monorepo (or the thin wrapper, which forwards to it):

```bash
python3 -m tools.versioning [path-to-repo]
```

```bash
bash .agents/skills/version-standard/scripts/get_version.sh [path-to-repo]
```

If no path is given, it uses the current directory and walks up until it finds `.git`, so passing the monorepo root, `box-rpg/`, or `box-gui/` all yields the same number. The command counts commits reachable from `HEAD` from the 1st of the current month until now and prints the version — don't count commits by hand.

Both packages always share the same number because it is computed from the monorepo root, never from a subdirectory.

## Notes

- The version is NEVER written into source files. It is computed at build/CI time from git history and pinned into the artifact (e.g. a generated `_version.py` via `write_version_file()`); the source tree stays version-free.
- The count is by calendar month, not the last 30 days.
- If the repo has multiple branches, the count covers commits reachable from `HEAD` (the active branch). If the user wants a different branch, `git checkout` it first.
- If the user asks for "the version of commit X", use `git log --since=... --until=<date of that commit> --oneline | wc -l` with that commit's date instead of "now".
