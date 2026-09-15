# Repository guide

## Scope

`box-gui` is the Python 3.14+ GTK4/libadwaita front end for `box-rpg`, shipped as distribution `box-rpg-maker` with command `box-rpg-maker` and App ID `io.gitlab.christvh.BoxRpgApp`. It inspects RPG Maker MV/MZ and 2000/2003 games, launches them with per-launch permissions, and manages runtimes and launcher-owned cleanup items through the stable `box.api` surface. Keep the presentation-only boundary explicit; do not reimplement backend behavior in the front end without a documented decision.

## Development

- Keep production code under `src/box_gui/` and tests under `tests/`.
- Split new modules by dependency direction: `core/` holds plain logic with no `Gtk`/`Adw` imports, `gtk/` holds toolkit plumbing, `widgets/` holds reusable widgets, and `pages/` holds navigation pages and dialogs. Keep `app.py` and `i18n.py` top-level.
- Use Python 3.14+ and the checks configured in `pyproject.toml`: `pytest`, `ruff check`, `ruff format --check`, and strict `pyright`.
- Keep user-facing documentation concise and in English.
- Keep code comments and variable names in English, with no `print()` or `input()` in `box_gui`.
- Add or update focused tests with behavior changes. Stub backend calls with no network. Flag the missing visual pass in pull requests.
- Degrade gracefully when the installed `box-rpg` predates a needed API, for example with a `getattr` fallback, rather than assuming the checkout.

## Safety invariants

- Resolve and validate game paths only through `box.api`; launch only below allowed game roots.
- Reject path traversal and symlink escapes by delegating to the backend; never add a frontend bypass.
- Treat runtimes and games as untrusted inputs; never require elevated rights.
- Run blocking `box.api` calls on a worker thread and marshal results back to the main loop.
- Ask explicit per-launch consent through `Interaction` for X11, network, and game writes. Do not persist these permissions outside the per-game switches owned by the library.
- Keep managed path components lower-case through backend paths. Do not weaken this case-sensitive path restriction without updating the documentation and tests.

## Project data

Frontend-owned persistence lives under `AppPaths.config_root` without touching the CLI: `library.json` for the game library and `defaults.json` for global frontend defaults such as the preferred EasyRPG Player version. Use atomic writes with user-only permissions. Longer user guidance belongs in `../docs/box-gui/`. Desktop entry and icons belong in `res/`.

## Internationalization

- Runtime UI strings use English msgids via `_()` (and `ngettext()` for plurals) in domain `box-rpg-app`; the Spanish catalog lives in `src/box_gui/locale/es/LC_MESSAGES/`. Load the frontend catalog at startup and then `box.utils.i18n.configure()` for backend strings shown by the UI.
- Preserve placeholders, commands, options, selectors, JSON keys, product and file names, paths, and literal tokens, including but not limited to `box-rpg-maker`, `box-rpg`, RPG Maker, NW.js, EasyRPG Player, Chromium, X11, and SDK. Do not translate documentation or JSON file formats.
- Workflow: regenerate the template (`xgettext`), translate (`translator-es` subagent), review (`spell-checker-es` subagent), compile (`msgfmt --check`), then run the suite. Full commands live in `../docs/box-gui/translations.md`; terms in `../glossary.md`; translator rules in `../guidelines.md`; track the work in `../task.md`.
- The catalog must compile cleanly with zero untranslated or fuzzy entries (`msgattrib --untranslated` prints nothing), except the documented legacy entry covered by `test_i18n_app.py`.
