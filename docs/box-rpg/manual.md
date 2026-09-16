# box-rpg user guide

This guide shows how to launch your games with `box-rpg`.

`box-rpg` supports two kinds of games:

- RPG Maker MV/MZ exports, played with NW.js.
- RPG Maker 2000/2003 projects, played with EasyRPG Player.

Other RPG Maker generations are not supported. You need Python 3.14 or newer.

## Before you start

Games always run inside an isolated sandbox. This requires Linux with Bubblewrap at `/usr/bin/bwrap` and working user namespaces. Wayland is recommended. X11 and XWayland sessions work with per-launch consent (`--x11` or interactive confirmation).

`--gamemode` additionally requires `/usr/bin/gamemoderun`, `/usr/bin/xdg-dbus-proxy`, and `/usr/bin/busctl` plus a host `gamemoded`, which D-Bus-activates on demand with no manual enable step. Unlike the mandatory Bubblewrap dependency, these are only needed for GameMode launches.

There is no unsandboxed mode. If Bubblewrap or user namespaces are missing, games will not start. See [security and compatibility limits](security.md) for details.

## Launch a game

Go to the game directory in a terminal and run:

```sh
box-rpg
```

The launcher detects the game type and starts it. You can also give the path explicitly:

```sh
box-rpg launch /path/to/game
box-rpg inspect /path/to/game
```

Use `inspect` when you only want to check detection without starting the game.

The first time you launch a game outside your allowed game roots, the launcher asks you to authorize that exact directory. This remembers one game folder. To authorize a whole collection at once, see [Configuration](#configuration).

NW.js games open as their manifest requests. Window size and fullscreen follow the supported manifest window settings. Game plugins that control the window behave as in a normal export.

## Permissions per launch

By default a game has no network access and cannot change its own files. Saves still work in their usual places. Extra permissions apply to one launch only and are never saved.

Grant network access:

```sh
box-rpg launch /path/to/game --allow-network
```

Only use this with games you trust. It shares the host network, including internet, local network, and local services.

Let a self-updating game change its own files:

```sh
box-rpg launch /path/to/game --allow-game-writes
```

Only use this with games you trust not to damage their own files.

On an X11 session the launcher shows a warning and asks for confirmation before continuing, because X11 programs can observe input and screen contents. Wayland is recommended. To give X11 consent directly for one launch:

```sh
box-rpg launch /path/to/game --x11
```

Without a terminal, X11 launches require `--x11`. `diagnose` never uses X11.

## Install a runtime

If no compatible runtime is installed, install one and then launch the game again.

For MV/MZ games you need NW.js:

```sh
box-rpg runtime nwjs available --interactive
```

Browse pages with `n` and `p`, enter a number to choose a version, and confirm. The architecture is detected automatically.

For 2000/2003 projects you need EasyRPG Player:

```sh
box-rpg runtime easyrpg available --interactive
```

Projects are detected from `RPG_RT.ini`, `RPG_RT.ldb`, and `RPG_RT.lmt`. The game starts fullscreen. Saves are stored in `<game>/save/` as files such as `Save01.lsd`. To reuse old saves, close the game and copy the `Save*.lsd` files from the game root into `<game>/save/`.

When several matching runtimes are installed, an interactive launch asks which one to use. Press Enter to keep the newest. Non-interactive launches always use the newest. You can also fix a version. NW.js versions use the form `vX.Y.Z`, while EasyRPG versions use two to four numeric components:

```sh
box-rpg launch /path/to/game --runtime v0.90.0
box-rpg launch /path/to/game --runtime 0.8.1.1
```

## Game files and saves

Some NW.js exports need a helper file from the game root, for example `game_messages.csv`. Copy it into the isolated session with:

```sh
box-rpg launch --copy-root-file game_messages.csv
```

Repeat the option for more files. When `GAME_PATH` is omitted the current directory is used. Only direct regular files from the game root are accepted, up to 16 MiB each. Folders, links, and special files are rejected. The reserved names `game` and `package.json` are also rejected.

Each NW.js game keeps a private profile at `$XDG_CACHE_HOME/box-rpg/profiles/<16-hex-game-id>/sandbox` (usually under `~/.cache`). It stores browser preferences, web storage, and cache. RPG Maker saves stay beside the game, usually in `<game>/www/save/` or `<game>/save/`.

EasyRPG Player has no Wayland video driver, so it runs through XWayland. On Wayland this still asks for one-time X11 consent per launch. Without XWayland the game cannot open a window.

## Manage stored data

List launcher-owned cleanup items, one JSON object per line:

```sh
box-rpg cleanup list
box-rpg cleanup list runtimes
```

Replace `runtimes` with `roots`, `downloads`, or `profiles` to list one category. Remove one item or a whole category. `SELECTOR` and `--all` cannot be combined, and `--interactive` cannot be combined with `--yes`:

```sh
box-rpg cleanup remove CATEGORY SELECTOR
box-rpg cleanup remove CATEGORY --all
box-rpg cleanup all
box-rpg cleanup --interactive
```

Removal asks for confirmation. Add `--yes` for non-interactive deletion. Single-item removal asks for `DELETE`, while category and global removal ask for `DELETE ALL`. Cleanup never deletes game files or saves.

## Configuration

Settings live in `$XDG_CONFIG_HOME/box-rpg/config.toml` (usually `~/.config/box-rpg/config.toml`). The CLI option `allowed-game-root` adds to the `allowed_game_roots` list in that file. To authorize a whole game library:

```sh
box-rpg config set allowed-game-root /home/alice/games
box-rpg config show
```

A game must be below an authorized root. The launcher cleans up roots that no longer exist.
