# box-rpg public API

`box.api` is the no-I/O orchestration layer shared by the CLI and future
graphical front ends. `box.cli` is a thin terminal client around it:
`launch`, `runtime`, `diagnose` and `inspect` commands delegate orchestration
to `box.api` and keep only printing, prompting (`ConsoleInteraction`) and
formatting locally.

## Public surface

Stable: `box.api`, `box.models`, `box.errors`, `box.config.models`, `box.paths`.
Internal: `box.launch.sandbox`, `box.runtime.downloader`, `box.launch.session`
and everything else under `box.cli`. Do not import internals from a frontend.

`box.api.__init__` re-exports `Interaction`, `ConsoleInteraction`,
`Inspection`, `DiagnoseResult`, `AppConfig`, `AppPaths`, `CleanupCatalog`,
`CleanupItem`, `ConfigRepository`, `GameInfo`, `RemovalResult`, `RuntimeInfo`
and `RuntimeSpec` for convenience.

## Modules

- `box.api.interaction`: `Interaction` protocol
  (`confirm_x11`, `confirm_add_root`, `choose_runtime(kind, candidates, title)`)
  plus `ConsoleInteraction` as the terminal reference. `title` is a
  preformatted header line the frontend shows verbatim (terminal writes it,
  GTK uses it as dialog title). `ConsoleInteraction` owns terminal
  formatting: width, headers. The caller decides whether
  interaction is allowed; pass `None` for non-interactive use, which fails on
  X11 consent and unregistered roots while ambiguous runtimes resolve to
  latest. Custom
  implementations return `False`/`None` on denial; `EOFError` is also treated
  as cancellation.
- `box.api.inspect`: `inspect(path, registry=None) -> Inspection`. Pure.
- `box.api.diagnose`: `diagnose(paths, repository, game_path, version, sdk)`
  returns frozen `DiagnoseResult(environment, versions)`. Use
  `box.diagnostics.report.render_report` only in the CLI to format it.
- `box.api.runtime`: `default_architecture()`, `list_nwjs`, `install_nwjs`, `remove_nwjs`,
  `fetch_nwjs_available`, `list_easyrpg`, `install_easyrpg`,
  `remove_easyrpg`, `fetch_easyrpg_available`. Install functions accept an
  optional `ProgressReporter(completed, total | None)`. No printing or prompting.
- `box.api.cleanup`: `CleanupCatalog.list/remove` plus `CleanupItem`,
  `RemovalResult` and `CATEGORIES`. Same roots, runtimes, downloads and
  profiles enumeration as the CLI, without printing or prompting.
- `box.api.launch`: `launch(paths, repository, game_path, version, sdk,
  copy_root_files, allow_network, allow_game_writes, x11, interaction)`
  returns the exit code. Same validation, sandbox and session order as the
  CLI, without terminal I/O. `authorize_game` is also exposed.
  `list_root_files(game)` lists copyable game-root filenames.

`box.api` performs no terminal I/O except the injectable `read`/`write`
defaults of `ConsoleInteraction`. Configuration
needs no wrapper: call `ConfigRepository.load`, `add_allowed_root` or
`set_preferred_runtime` directly. `box.api.cleanup.CleanupCatalog.list/remove`
is already structured and will adopt the same `Interaction` in a second pass.

## Blocking contract

`box.api` stays synchronous and dependency-free. These calls block and must
run on a worker thread in a GUI, marshalling results back to the main loop:

- `launch`: blocks until the game exits.
- `install_nwjs` / `install_easyrpg`: block on network and extraction.
- `fetch_*_available`: block on network.
- `diagnose`: blocks on a sandboxed `--version` probe (10 s timeout).

`ProgressReporter` is called with byte counts from the download loop and is
safe to forward to a progress bar via the GUI event loop. Cancellation is
cooperative: deny or return `None` from `Interaction` to abort before the
blocking call starts.

## Safety

The GUI goes through the same code paths as the CLI: allowed roots,
mandatory Bubblewrap sandbox, no unsandboxed fallback, explicit consent for
X11, `--allow-network` and `--allow-game-writes`. See `docs/security.md`.
Never reimplement path validation, sandbox flags or download logic in the
frontend.
