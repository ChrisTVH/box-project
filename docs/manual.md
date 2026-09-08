# box-rpg user guide

`box-rpg` launches RPG Maker MV/MZ exports with NW.js and RPG Maker 2000/2003
projects with EasyRPG Player. It requires Python 3.14+. RPG Maker XP, VX, and
VX Ace are not supported.

## Launch a game

Open a terminal in the game directory and run:

```sh
box-rpg
```

The launcher detects the game type. On the first interactive launch of a game
outside your configured libraries, confirm the prompt to authorize that exact
game directory. You can also launch a game from elsewhere:

```sh
box-rpg launch /path/to/game
```

Use `box-rpg inspect /path/to/game` to check detection without launching it.

## Install the required runtime

If no compatible runtime is installed, install one for the detected game type,
then run `box-rpg` again from the game directory.

### RPG Maker MV/MZ

MV/MZ games need NW.js. Choose a stable version from the interactive list; the
architecture is detected automatically.

```sh
box-rpg runtime available --interactive
```

Use `n` and `p` to browse version pages, enter a number to choose a version,
and confirm the installation. NW.js uses Wayland only when the session provides
both `XDG_SESSION_TYPE=wayland` and `WAYLAND_DISPLAY`; otherwise it uses X11.

Some desktop exports load auxiliary files relative to the game root instead of
their web directory. For these special cases, retain the isolated launch session
but set the runtime working directory explicitly:

```sh
box-rpg launch --game-cwd
```

If an export resets its working directory to the isolated session, copy the
required direct game-root file instead:

```sh
box-rpg launch --copy-root-file game_messages.csv
```

`--copy-root-file` can be repeated for multiple files. It accepts only direct,
regular files from the validated game root and rejects paths containing
directories, symlinks, or session-owned names.

Each NW.js game uses a persistent private Chromium/NW.js profile at
`$XDG_CACHE_HOME/box-rpg/profiles/<opaque-game-id>` (or
`~/.cache/box-rpg/profiles/<opaque-game-id>`). The profile stores preferences,
web storage, and cache. RPG Maker saves in `www/save` stay in the game directory.

### RPG Maker 2000/2003

These projects are detected from `RPG_RT.ini`, `RPG_RT.ldb`, and `RPG_RT.lmt`.
Choose an x64 EasyRPG Player version:

```sh
box-rpg runtime easyrpg available --interactive
```

EasyRPG Player launches the project fullscreen with its project path set to the
game directory. Saves remain with the game; `box-rpg` does not move them.

## Manage cached data

Run the interactive cleanup menu to remove launcher-managed data:

```sh
box-rpg cleanup
```

Choose authorized game roots, managed runtimes, downloaded archives, or game
profiles. Each list is paginated; select one item or all items, then confirm the
deletion. The global cleanup option requires entering `DELETE ALL`.

Cleanup never deletes game files or saves. It also leaves `config.toml`,
sessions, and diagnostic reports untouched. It only removes data owned by the
launcher under `$XDG_CACHE_HOME/box-rpg` (or `~/.cache/box-rpg`). This includes
individual or all NW.js game profiles.

## Configuration and safety

Configuration is stored in `$XDG_CONFIG_HOME/box-rpg/config.toml` (or
`~/.config/box-rpg/config.toml`). To authorize an entire game library:

```sh
box-rpg config set allowed-game-root /home/alice/games
```

The launcher removes configured roots that no longer exist when it authorizes a
game. Existing roots are retained because they may be game libraries.

Game paths must stay below an authorized root. Treat games and downloaded
runtimes as untrusted software, keep authorized roots limited to directories
you control, and do not run the launcher with elevated privileges.
