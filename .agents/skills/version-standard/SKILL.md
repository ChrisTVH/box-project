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

Run the script against the repo:

```bash
bash scripts/get_version.sh /path/to/repo
```

If no path is given, it uses the current directory. The script counts commits reachable from `HEAD` from the 1st of the current month until now and builds the string automatically — don't count commits by hand.

## Notes

- The count is by calendar month, not the last 30 days.
- If the repo has multiple branches, the script counts commits reachable from `HEAD` (the active branch). If the user wants a different branch, `git checkout` it first, or adjust the script's `--since`/`--until`.
- If the user asks for "the version of commit X", use `git log --since=... --until=<date of that commit> --oneline | wc -l` with that commit's date instead of "now".
