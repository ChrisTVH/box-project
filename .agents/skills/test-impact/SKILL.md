---
name: test-impact
description: Selects which pytest groups to run based on what changed, instead of the full suite. Use this whenever the user asks to run tests, wants to know what tests a change needs, or asks for smart/selective/incremental test execution in this monorepo.
---

# Test Impact

box-rpg and box-gui already mirror `src/<pkg>/<group>/` with `tests/<group>/`
1:1 (`runtime` <-> `runtime`, `pages` <-> `pages`, ...). This skill uses that
convention to map changed files to test groups, so you don't run 68+ test
files for a one-line fix.

## How to run it

```bash
bash .agents/skills/test-impact/scripts/select_tests.sh          # plan only
bash .agents/skills/test-impact/scripts/select_tests.sh --run    # plan + execute
bash .agents/skills/test-impact/scripts/select_tests.sh --base origin/main --run
```

Or directly: `python3 -m tools.test_selector [path] [--base <ref>] [--run] [--quiet]`.

With no `--base`, it looks at the working tree (staged + unstaged + untracked)
against `HEAD` -- "what am I about to test right now". `--base <ref>` diffs
a ref against the working tree instead, for reviewing a whole branch.

## Selection rules (in `tools/test_selector.py`)

1. `<pkg>/src/<toplevel>/<group>/...` changed and `<pkg>/tests/<group>/`
   exists -> run only that group.
2. A src subdir with **no** matching `tests/` dir (currently: `engines`,
   `diagnostics`, `locale` in box-rpg) -> can't scope it, run the full
   package suite.
3. A top-level module file (no subdir, e.g. `box_gui/app.py`) -> full
   package suite.
4. `<pkg>/tests/<group>/...` or a test file itself changed -> run that
   group/file directly.
5. `<pkg>/tests/conftest.py` or `<pkg>/pyproject.toml` changed -> full
   package suite (fixtures/config affect everything).
6. Root `install.py`, `cleaner.py`, or `tools/` changed -> `box-rpg/tests/tools`
   (that's what exercises them).
7. `box-rpg/src/box/api/**` changed -> also run box-gui's full suite (it's
   the stable contract box-gui is built on, per `box-gui/AGENTS.md`).

Anything else (docs, `.md` files, CI config, `res/`) has no pytest coverage
and is skipped.

## Maintaining this as the repo grows

- New src subdir with a matching `tests/<name>/` dir -> picked up
  automatically, nothing to edit.
- New src subdir with **no** test dir yet -> also automatic, falls back to
  a full run until you add the matching `tests/` dir.
- New cross-package contract boundary (like `box.api`) -> add an entry to
  `_CROSS_PACKAGE_FULL` in `tools/test_selector.py`.
- Before a release or in CI, ignore this skill and run each package's full
  suite -- it's a local/dev speedup, not a substitute for the full gate.
