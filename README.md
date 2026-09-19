# box

`box` is a launcher for RPG Maker games on Linux. Each game runs isolated in its own sandbox. It ships as two distributions sharing one backend:

- `box-rpg`: command-line launcher (command `box-rpg`).
- `box-rpg-maker`: GTK4/libadwaita front end (command `box-rpg-maker`, App ID `io.gitlab.christvh.BoxRpgApp`).

Supported games, and nothing else by decision:

- RPG Maker MV/MZ exports, played with NW.js.
- RPG Maker 2000/2003 projects, played with EasyRPG Player.

## Requirements

You need Linux with Python 3.14+, pip, and GnuPG (`/usr/bin/gpg`) for NW.js signature checks, plus Bubblewrap (`/usr/bin/bwrap`) with working user namespaces; there is no unsandboxed mode. The GUI additionally needs GTK 4 and libadwaita typelibs; without the optional `icoextract` package the GUI falls back to engine icons. Wayland is recommended; X11 and XWayland sessions work with per-launch consent, and on Wayland EasyRPG still asks once per launch when it runs through XWayland. Running the AppImage additionally needs `libfuse3` with `/dev/fuse`, also used for `--ci-mount` runs; `libfuse2` is never required. The installer checks these tools but does not install them. Background and limits are in [the manual](docs/box-rpg/manual.md) and [security and compatibility limits](docs/box-rpg/security.md).

## Install and uninstall

The installer shows what it would do without changing anything; add `--yes` to skip confirmation:

```sh
./install.py --install
./install.py --install --yes
./install.py --install --target gui
./install.py --uninstall
```

| `--target` | Installs | Applies to |
|---|---|---|
| `cli` | `box-rpg` backend | `--install` and `--uninstall` |
| `gui` | `box-rpg-maker` GUI, desktop entry, and icons | `--install` and `--uninstall` |
| `all` (default) | both distributions | `--install` and `--uninstall` |

The installer refuses root execution. On externally managed Pythons, user-site installation needs the separate `--break-system-packages` consent; `--yes` does not grant it. Installing overwrites the managed shell completions with the current copies. Use `box-rpg --help` after installation.

### AppImage

The AppImage ships only the `box-rpg-maker` GUI; the `box-rpg` backend stays on the host so the sandbox keeps working. Install the backend first, then run the artifact:

```sh
./install.py --install --target cli
chmod +x box-rpg-maker.appimage
./box-rpg-maker.appimage
```

It needs Linux with Python 3.14+, GTK 4 with libadwaita 1.5+, and `libfuse3`. The AppImage starts on `/usr/bin/python3` and, when it is older than 3.14, re-executes the first Python 3.14+ with GTK bindings from `$BOX_RPG_MAKER_PYTHON`, `python3` on `PATH`, then `/usr/local/bin/python3`, `/usr/local/bin/python3.14`, `/usr/bin/python3.14`. It resolves `box.api` from your user install; it never bundles Python or the backend. Artifacts are built manually from tagged releases (see the frontend notes); each one carries its build tag.

## Quick start

Launch a game from its directory with the CLI:

```sh
box-rpg
```

Use `box-rpg inspect /path/to/game` to check detection without launching, and `box-rpg launch /path/to/game` to launch from elsewhere. If no compatible runtime is installed (the NW.js or EasyRPG engine version a game needs), pick one first:

```sh
box-rpg runtime nwjs available --interactive
box-rpg runtime easyrpg available --interactive
```

The first launch outside your allowed game roots asks for authorization, and network access, game-file writes, and X11 each need per-launch consent; see [the manual](docs/box-rpg/manual.md).

With the GUI, run `box-rpg-maker`, add a game folder with `+`, open it, and press launch. Runtime choice and sandbox permissions live on the game detail page.

## Development

Follow [locked development and builds](docs/box-rpg/development.md) to create `.venv` and install the pinned requirements with hashes; the frontend setup is in [the frontend notes](docs/box-gui/development.md). Plain `pip install .` does not provide that guarantee.

Preview cleanup of ignored tool caches, environments, build outputs, and bytecode:

```sh
./cleaner.py
```

Apply it after reviewing the listed paths:

```sh
./cleaner.py --apply
./cleaner.py --yes
```

`--yes` applies without asking. Tracked files and nested projects are preserved.

## Documentation

| Guide | What it covers |
|---|---|
| [User guide](docs/box-rpg/manual.md) | Launch games, install runtimes, manage stored data. |
| [Security and compatibility limits](docs/box-rpg/security.md) | Trust model and sandbox limits. |
| [Public API](docs/box-rpg/api.md) | Stable backend surface shared by the CLI and the GUI. |
| [Backend development](docs/box-rpg/development.md) | Locked environments, reproducible builds, safe installs. |
| [Frontend development](docs/box-gui/development.md) | Frontend layout, backend boundary, AppImage distribution. |
| [Backend translations](docs/box-rpg/translations.md) and [frontend translations](docs/box-gui/translations.md) | Contributing translations; shared [guidelines](guidelines.md) and [glossary](glossary.md). |
| [Example configuration](box-rpg/res/config/box-rpg.toml.example) | Annotated `config.toml` sample. |

## Safety

Games and runtimes are untrusted; `box-rpg` always confines them in the Bubblewrap sandbox. See [security and compatibility limits](docs/box-rpg/security.md) for the trust model and its limits.
