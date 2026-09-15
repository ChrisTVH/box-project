# box-rpg public API

This reference describes the stable API shared by the CLI and the graphical front end.

`box.api` is the orchestration layer without terminal I/O. `box.cli` is a thin terminal client around it: commands delegate work to `box.api` and keep only printing, prompting, and formatting locally.

## Stable surface

Stable, safe to import from a front end:

- `box.api`, `box.models`, `box.errors`, `box.config.models`, `box.paths`.

Internal, do not import from a front end:

- `box.launch.sandbox`, `box.runtime.downloader`, `box.launch.session`, and everything else under `box.cli`.

The only blessed extras are `box.runtime.catalog`, `box.runtime.easyrpg`, and `box.games.identity`, used to build per-page runtime catalogs as `box.cli.runtime` does.

`box.api` re-exports `Interaction`, `ConsoleInteraction`, `Inspection`, `DiagnoseResult`, `AppConfig`, `AppPaths`, `CleanupCatalog`, `CleanupItem`, `ConfigRepository`, `GameInfo`, `RemovalResult`, `RuntimeInfo`, and `RuntimeSpec` for convenience.

## Modules

- `box.api.interaction`: the `Interaction` protocol with `confirm_x11`, `confirm_add_root`, and `choose_runtime(kind, candidates, title)`. `title` is a ready-made header the front end shows verbatim. `ConsoleInteraction` is the terminal implementation and owns terminal formatting. Pass `None` for non-interactive use: X11 consent and unregistered roots then fail, while ambiguous runtimes resolve to the newest. Return `False` or `None` on denial. `EOFError` counts as cancellation.
- `box.api.inspect`: `inspect(path, registry=None) -> Inspection`. Performs reads but no writes.
- `box.api.diagnose`: `diagnose(paths, repository, game_path, version, sdk)` returns a frozen `DiagnoseResult(environment, versions)`. Format it with `box.diagnostics.report.render_report` only in the CLI.
- `box.api.runtime`: `default_architecture`, `list_nwjs`, `install_nwjs`, `remove_nwjs`, `fetch_nwjs_available`, `list_easyrpg`, `install_easyrpg`, `remove_easyrpg`, `fetch_easyrpg_available`. Install functions accept an optional `ProgressReporter(completed, total | None)`. Pass a real reporter in a GUI to stay silent; `None` enables the terminal progress bar. No prompting.
- `box.api.cleanup`: `CleanupCatalog.list` and `CleanupCatalog.remove`, plus `CleanupItem`, `RemovalResult`, and `CATEGORIES`. Same roots, runtimes, downloads, and profiles enumeration as the CLI, without printing or prompting.
- `box.api.launch`: `launch(paths, repository, game_path, version, sdk, copy_root_files, allow_network, allow_game_writes, x11, interaction)` returns the exit code. Same validation, sandbox, and session order as the CLI, without terminal I/O. Also exposes `authorize_game` and `list_root_files(game)`.

Configuration needs no wrapper: call `ConfigRepository.load`, `add_allowed_root`, or `set_preferred_runtime` directly.

## Blocking contract

`box.api` stays synchronous and dependency-free. These calls block, so run them on a worker thread in a GUI and send results back to the main loop:

- `launch` blocks until the game exits.
- `install_nwjs` and `install_easyrpg` block on network and extraction.
- `fetch_*_available` blocks on network.
- `diagnose` blocks on a sandboxed `--version` probe with a 10-second timeout.

`ProgressReporter` receives byte counts from the download loop and is safe to forward to a progress bar through the GUI event loop. Cancellation is cooperative: deny or return `None` from `Interaction` to abort before the blocking call starts.

## Safety

The GUI uses the same code paths as the CLI: allowed roots, mandatory Bubblewrap sandbox, no unsandboxed fallback, and explicit consent for X11, `--allow-network`, and `--allow-game-writes`. See `security.md`. Never reimplement path validation, sandbox flags, or download logic in the front end.
