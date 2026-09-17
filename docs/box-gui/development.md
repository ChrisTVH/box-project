# box-gui development notes

This guide explains how the `box-gui` front end is organized. Run instructions live in the root `README.md` (Graphical interface section). User-visible behavior comes from the backend through `box.api`; this file records frontend decisions and conventions.

`box-gui` is the GTK4/libadwaita front end for `box-rpg`, shipped as distribution `box-rpg-maker` with command `box-rpg-maker` and App ID `io.gitlab.christvh.BoxRpgApp`.

## Layout

New modules have an obvious home by dependency direction:

- `core/`: plain logic with no widget toolkit. No `Gtk` or `Adw` imports. `GdkPixbuf` decoding is allowed. Enforced by `test_stable_surface.py`.
- `gtk/`: toolkit plumbing such as threading, the interaction bridge, and themed icon names.
- `widgets/`: reusable GTK widgets shared between pages.
- `pages/`: navigation pages and dialogs.

`app.py` and `i18n.py` stay top-level. `res/` stays at the project root and `locale/` stays inside the package.

Key files:

- `src/box_gui/app.py`: `Adw.Application` entry point. Owns `AppPaths`, `ConfigRepository`, `LibraryRepository`, `DefaultsRepository`, and `GtkInteraction`. Holds a single `Adw.NavigationView` and presents the Settings dialog on demand. Loads frontend and backend catalogs at startup.
- `src/box_gui/core/library.py`: game library persistence. `LibraryEntry` dataclass and `LibraryRepository` with `load`, `save`, `add`, `remove`, `reorder`, and `update` over `library.json`. No GTK dependency.
- `src/box_gui/core/defaults.py`: global defaults over `defaults.json`, currently the preferred EasyRPG Player version. Same atomic-write and user-only-permission pattern as `library.py`. No GTK dependency.
- `src/box_gui/pages/library_page.py`: root page with tag `library`. Folder picker, inspection-to-add flow, rows with runtime pill and reorder/remove menu, Settings entry point.
- `src/box_gui/pages/game_detail_page.py`: detail page with tag `game-detail`. Re-inspects on push, free-text display name, persisted runtime, SDK, GameMode, case-insensitive mount, extra-files, and sandbox permission switches, header launch icon, footer `Diagnose` button opening `DiagnoseDialog`.
- `src/box_gui/pages/settings_dialog.py`: modal `Adw.PreferencesDialog` with search disabled by design, hosting `GeneralPage`, `NwjsPage`, `EasyrpgPage`, and `CleanupPage`.
- `src/box_gui/pages/diagnose_dialog.py`: `Adw.Dialog` with environment and version groups. Requires libadwaita 1.5 or newer.
- `src/box_gui/gtk/workers.py`: threading only. `run_in_thread` with daemon thread plus `GLib.idle_add` marshaling, `ProgressReporter` forwarding `(completed, total)` to the main loop, and thin `run_inspect`, `run_launch`, and `run_diagnose` wrappers.
- `src/box_gui/gtk/interaction.py`: `GtkInteraction`, the `Interaction` bridge for launch-time prompts. Uses a nested `MainLoop` on the main thread with `idle_add` plus `threading.Event` on workers. Not reused by the runtime manager dialogs.
- `src/box_gui/i18n.py`: gettext loading for domain `box-rpg-maker` from `src/box_gui/locale`, exposing `configure`, `_`, and `ngettext`.
- `src/box_gui/gtk/icons.py`: themed icon names. The app icon is full-color, while NW.js and EasyRPG tab icons are `-symbolic` so GTK recolors them for light and dark themes.
- `src/box_gui/core/game_icon.py`: game icon discovery without `Gtk`/`Adw` (`GdkPixbuf` decoding is allowed). Lists `.exe` files through `list_root_files`, converts `.ico` to PNG with `icoextract` and `GdkPixbuf`, and builds the app-owned cache path. Executables are only read, never run. Failures return `False` instead of raising.
- `src/box_gui/widgets/exe_picker.py`: single-choice executable dialog in the `choose_runtime` shape with `Adw.AlertDialog` plus `Gtk.DropDown`.
- `src/box_gui/widgets/icon_widget.py`: shared icon order: cached `icon_path` first, then engine icon, then box icon.
- `res/`: desktop entry named after the App ID with matching `StartupWMClass`, plus app and engine artwork under `res/icons/`.

## Backend boundary

The front end is presentation-only. All behavior lives in `box-rpg` behind `box.api`: `inspect`, `launch` with `list_root_files`, `diagnose`, per-engine runtime `list`, `install`, `remove`, and `fetch` with `default_architecture`, and `CleanupCatalog` with `CATEGORIES`. Catalogs are built per page from `AppPaths`, as `box.cli.runtime` does.

Import only `box.api`, `box.models`, `box.errors`, `box.paths`, `box.config.models`, and the three blessed extras `box.runtime.catalog`, `box.runtime.easyrpg`, and `box.games.identity`. Never import `box.runtime.downloader`, `box.launch.sandbox`, `box.runtime.platform`, or `box.cli`. The single exception is `from box.utils.i18n import configure`, only `configure` and never `_`, used once at startup to load the backend catalog next to the frontend one. `test_stable_surface.py` enforces this by parsing imports.

The live app resolves `box-rpg` from the installed package, not the checkout. A missing API keeps the `x64` initial value silently, while a resolution error still shows a modal. Tests stub `box.api.runtime` either way.

## Navigation

`Adw.NavigationView` is the window content. The root page is `LibraryPage` with tag `library`. Activating a row pushes `GameDetailPage` with tag `game-detail`. The back button comes from `Adw.NavigationView`.

Settings is not a navigation page. The gear button lazily imports `SettingsDialog` and calls `present(parent)` as a modal dialog over the navigation view. `DiagnoseDialog` is presented from the game detail footer for the current root.

The `+` button on the library opens a `Gtk.FileDialog` folder picker, runs `run_inspect` off the main loop, calls `LibraryRepository.add` with the detected title as display name, and pushes the detail page for that game.

## Library persistence

Extra frontend data lives under `~/.config/box-rpg/` without touching the CLI. `AppPaths.config_root` is public through `box.api`, so `box_gui` manages its own file there.

- File: `paths.config_root / "library.json"`. JSON, not TOML: this file is app-managed, not hand-edited like `config.toml`, and `json` needs no new dependency.
- Schema version 6 with entries holding `path`, `display_name`, `order`, `preferred_runtime`, `preferred_sdk`, `copy_root_files`, `engine`, `allow_network`, `allow_game_writes`, `allow_x11`, `use_gamemode`, `use_ci_mount`, `icon_path`, and `missing_streak`. `order` is an explicit integer, not array position. `load()` returns entries sorted by `order`. A missing file yields an empty tuple. A corrupt file raises `LibraryError`, shown as a normal `Adw.AlertDialog` error, never a crash. Older versions migrate forward with safe defaults.
- Writes are atomic: write to a temporary file in the same directory, `fsync`, `chmod 0o600`, then `os.replace`. The config root is created with `mkdir(parents=True, exist_ok=True, mode=0o700)` before the first write.
- `LibraryRepository` imports nothing from `box` except `AppPaths`.

## Runtime manager

Each engine has one page: `NwjsPage` named `nwjs` and `EasyrpgPage` named `easyrpg`. A new engine means a new page, not a restructured dialog.

Each page shows installed runtimes as rows with a remove button, plus a paged available-versions browser with 10 entries per page and clamped Prev/Next controls. The clicked Install button swaps in a `Gtk.Spinner` until the worker finishes. A real progress reporter is still passed so installs never fall back to terminal output.

The install flow mirrors the CLI: pick a version, confirm in a separate `AlertDialog` with suggested Install, then install on a worker. Confirmation is never folded into the picker, and `GtkInteraction.choose_runtime` is never reused for browsing. Confirm before install, never before remove, except cleanup removals which always confirm per item.

A page-level `_busy` flag blocks option, pager, and concurrent remove changes while an install or remove runs. The default architecture is resolved before the view is built so the initial selector value cannot trigger a duplicate fetch.

Every blocking operation shows a spinner plus status label during work and a single-close `Adw.AlertDialog` on error: `"<Op> Failed"` for `BoxError`, `"Unexpected Error"` otherwise. One exception: a launch failure carrying `box.errors.RuntimeError` adds a suggested `Open Runtimes` response that opens Settings. Matching on the error type keeps this working under translated locales.

Dynamic text such as paths and display names always passes through `GLib.markup_escape_text`, because `Adw.ActionRow` titles render as Pango markup. Filesystem paths render abbreviated with the same ladder as the CLI prompt abbreviation, keeping the full path in the tooltip and absolute paths in storage. Dialog search stays disabled because libadwaita matches every titled row dialog-wide. Data rows use empty titles with plain child labels in case search ever returns. Long lists stay compact with collapsible summaries.

## Internationalization and icons

Every user-facing string uses `_()` or `ngettext` from day one. English msgids are the source and fallback. Error-type matching never matches on message text, so headings keep working under locales. Product documentation stays in English. JSON keys and file formats stay untranslated. See `translations.md` for the extractor workflow.

Artwork uses the Microsoft Fluent UI color set for the application icon and Tabler Icons for symbolic interface and engine-tab icons. Attribution lives in `gtk/icons.py` and as XML comments in `res/icons/`. `app.py` registers `res/icons/` on the icon theme search path so checkouts resolve without installing. Sizes follow pack guidance: 24px outline sources as scalable symbolic icons, 48px source for the app icon. Symbolic icons recolor automatically, so no manual theme detection exists.

Game icons are opt-in through the detail Change action. Adding a game never extracts. The first icon of a game `.exe` is extracted into an app-owned cache file under `paths.config_root / "icons" / "<slug>.png"`, so game folders are never written to. Any executable count opens the picker, whose first row reverts to the engine default. With no executable an image file picker is offered instead. Executable listing goes through `box.api.launch.list_root_files`, the same trust boundary as the extra-root-files picker.

## AppImage distribution

`tools/build_appimage.py` (with the `tools/appimage_tag.py` fallback reader) builds `box-rpg-maker.appimage` as a Python-pure, CI-reusable step with no shell scripts. The artifact packs only the frontend: the `box_gui` sources with the in-package `locale/`, plus the `res/` icons, the desktop entry, and the app icon. The `box-rpg` backend and the Python interpreter are never bundled; the generated `AppRun` starts the staged frontend unconditionally on `/usr/bin/python3`, so install the backend first (`./install.py --install --target cli`) for the direct-to-library path. A missing or stale backend is owned by `BackendSetupPage`, never by this launcher: the first run without one opens the in-app setup instead of refusing to start. On externally managed Pythons the one-click setup retries once automatically with `--break-system-packages` after pip refuses with an externally-managed-environment error, streaming both attempts verbatim; `--yes` alone never implies that consent.

The payload mirrors the checkout (`payload/src/box_gui` beside `payload/res`), which keeps `_register_bundled_icons` and `i18n` working with no GUI changes. Conversion uses a recent `appimagetool` (continuous build) with a modern type 2 runtime: hosts run the artifact with `libfuse3`, never `libfuse2` (already required for `--ci-mount`). Extraction is the default tool mode so FUSE-less CI containers can still build; only running the artifact needs `/dev/fuse`.

Releases are tag-driven. The builder computes the `year.month.commit-count` tag from git history (same count as the version-standard script, aligned with the `chore(release)` commits), creates the annotated tag at HEAD, and then verifies `git describe --exact-match` plus the tag/HEAD SHAs before staging anything; any mismatch fails closed. The tag is baked into `AppRun` (`BOX_RPG_MAKER_APPIMAGE_TAG`) and a generated `box_gui/appimage_tag.txt` inside the payload (never committed).

Reader contract for the release screen: implement `box_gui.appimage_tag.get_appimage_tag() -> str | None`, checking the environment variable first and the sibling `appimage_tag.txt` second.

Builds trigger manually only: `workflow_dispatch` in `.github/workflows/appimage.yml`, `when: manual` in `.gitlab-ci.yml`. Each manual build also publishes a Release on both platforms with a generic title, notes, and the AppImage (attached binary on GitHub, artifact link on GitLab). Inspect locally without creating tags or downloading tools:

```sh
python3 -m tools.build_appimage --check
python3 -m tools.build_appimage --yes --appdir-only --output dist/AppDir
```

## Tests and checks

- `tests/conftest.py` provides `_install_auto_answer` for `AlertDialog.present` and `_run_from_worker` as a `MainLoop` stand-in with safeguards. Display tests gate on a display check. Import-surface, icon, and gettext coverage checks stay headless-safe.
- Backend calls are stubbed with no network: pager and cancel paths, confirm accept, cancel, and close, remove-then-refresh, main-loop progress delivery, architecture-error fallback, and missing-helper fallback with zero dialogs.
- `LibraryRepository` is tested standalone with `tmp_path`: load and save round-trip, missing file as empty tuple, corrupt JSON as `LibraryError`, reorder semantics, and no partial file on failure.
- Checks are `pytest`, `ruff check`, `ruff format --check` with line-length 100, and strict `pyright`. Code, identifiers, and comments stay in English. No `print()` or `input()` in `box_gui`. Dialogs are exercised headless or with stubs only; flag the missing visual pass in pull requests.

Strict `pyright` runs from `box-gui/` with the project interpreter, mirroring the backend form in `../box-rpg/development.md` (Node.js on `PATH`, no automatic downloads):

```sh
../.venv/bin/python -m pyright --pythonpath ../.venv/bin/python
```

The `.venv` interpreter carries the `gi`/`cairo` bindings plus pytest, and `extraPaths` in `pyproject.toml` covers both `src` trees. A `box`/`box_gui` copy installed in user site shadows the checkouts if it takes precedence; the checkout roots above must win — if stale-version errors (`No parameter named ...` on fresh APIs) ever appear, check `python -c "import box; print(box.__file__)"` first.
