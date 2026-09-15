# Repository guide

## Scope

`box-rpg` is a Python 3.14+ command-line launcher for RPG Maker MV/MZ exports
using NW.js and RPG Maker 2000/2003 projects using EasyRPG Player. Keep the
supported-engine boundary explicit; do not add further generations or runtimes
without a documented decision.

## Development

- Keep production code under `src/box/` and tests under `tests/`.
- Keep orchestration functions free of direct terminal I/O; route prompts through `Interaction` and progress through `ProgressReporter`. Only `ConsoleInteraction` and a `None` reporter touch the terminal.
- Use Python 3.14+ and the checks configured in `pyproject.toml`: `pytest`, `ruff check`, `ruff format --check`, and strict `pyright`.
- Keep user-facing documentation concise and in English.
- Keep code comments and variable names in English.
- Add or update focused tests with behavior changes.

## Safety invariants

- Resolve and validate game paths before launch; launch only below allowed game roots.
- Reject path traversal and symlink escapes.
- Treat runtimes and games as untrusted inputs; never require elevated rights.
- Delete only launcher-owned items enumerated by `CleanupCatalog` (runtimes, downloads, and profiles under the configured XDG cache directory; allowed roots are config removals, not file deletions). Never delete game files or saves.
- Keep managed path components lower-case. Do not weaken this case-sensitive
  path restriction without updating the documentation and tests.

## Project data

Configuration examples belong in `res/config/`; shell completions belong in
`res/completions/`; longer user guidance belongs in `../docs/box-rpg/`.
The installer overwrites managed completions with the current copies on
install and removes them on uninstall, never following symlinks.

## Internationalization

- Runtime UI strings use English msgids via `_()` (and `ngettext()` for
  plurals); the Spanish catalog (`box.po`/`box.mo`) lives in `src/box/locale/es/LC_MESSAGES/`.
- Preserve placeholders, commands, options, selectors, JSON/config keys,
  product and file names, paths, menu shortcuts, and literal confirmation
  tokens, including but not limited to `box-rpg`, `box-rpg-maker`, RPG Maker,
  NW.js, EasyRPG Player, Chromium, X11, and SDK. Do not translate
  documentation, shell completions, or TOML comments.
- Workflow: regenerate the template (`xgettext`), merge into `box.po`
  (`msgmerge`), translate (`translator-es` subagent), review (`spell-checker-es`
  subagent), compile (`msgfmt --check`), then run the suite. Full commands live in
  `../docs/box-rpg/translations.md`; terms in `../glossary.md`; translator rules in
  `../guidelines.md`; track the work in `../task.md`.
- The catalog must compile cleanly with zero untranslated or fuzzy entries
  (`msgattrib --untranslated` and `msgattrib --only-fuzzy` print nothing).
