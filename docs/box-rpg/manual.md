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

## Packed single-executable games

A folder holding exactly one packed `.exe` file (any name) with no unpacked game files is unpacked automatically on launch. Unpacking runs offline inside the launcher: the embedded files are extracted and the executable is restored next to them, then the game starts from that copy. Consent still covers the folder you pointed at. The unpacked tree lives in your game profile at `$XDG_CACHE_HOME/box-rpg/profiles/<16-hex-game-id>/game/`, next to the persistent `sandbox/` profile; sessions, saves, and profiles follow that source-keyed identity, so replacing the executable keeps them. A fingerprint (content hash, size, modification time) decides staleness: an unchanged source reuses the tree, while a changed source re-unpacks over it with save folders carried forward. For EasyRPG games saves live in the source folder (see below); the unpacked `save/` goes stale after the first migration and removing the profile does not delete source saves. For NW.js, removing the profile deletes the unpacked tree and its saves with it. `inspect` unpacks on demand on first use and reports the source folder afterwards.

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

Projects are detected from `RPG_RT.ini`, `RPG_RT.ldb`, and `RPG_RT.lmt`. The game starts fullscreen. Saves are stored in `<game>/save/` as files such as `Save01.lsd`. For packed single-executable games `<game>` is the source folder you pointed at, not the unpacked cache copy; the first launch moves saves from the old unpacked `save/` into the source `save/` when the source is empty, then the unpacked copy goes stale. To reuse old saves, close the game and copy the `Save*.lsd` files from the game root into `<game>/save/`.

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

Some NW.js exports reference asset files with the wrong letter case (for example `image.png` on disk as `Image.PNG`). Windows ignores the difference, but Linux does not, so those assets fail to load. Mount the game through a case-insensitive view for one launch with:

```sh
box-rpg launch /path/to/game --ci-mount
```

The view is read-only and only affects filename lookups; nothing in the game folder is changed. It needs `libfuse3` and `/dev/fuse` on the host, and it is NW.js only: EasyRPG Player already resolves filename case itself since 0.8, so combining it with a 2000/2003 project is rejected.

If a `--ci-mount` launch fails while validating the game tree (for example `cannot validate sandbox tree at ./www/example/...: [Errno 2] ...`), capture what the mount layer actually saw:

```sh
BOX_CIMOUNT_DEBUG_LOG=/tmp/cimount.log box-rpg launch . --ci-mount
```

The log file is created with private permissions and records one line per lookup and directory listing (names, inode numbers, and directory timestamps only — never file contents). A line ending in `open_failed step=open errno=24` means the mount ran out of file descriptors (`EMFILE`); lines ending in `opened ino=<number>` are successful lookups. This exact procedure once diagnosed descriptor exhaustion behind `ENOENT` errors on files that existed: the log showed every lookup resolving correctly until the ~1021st distinct file, where opens started failing with `errno=24` against the shell's 1024 file-descriptor limit.

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
