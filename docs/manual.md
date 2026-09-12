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

NW.js games always start fullscreen; there is no windowed mode.

Bubblewrap at `/usr/bin/bwrap`, enabled user namespaces, and a local Wayland
socket are required. The launcher refuses unsandboxed execution. Runtime
version probes also use the sandbox, without desktop
or network access.

On an X11 session the launcher prints a security warning and asks for explicit
confirmation before exposing only the local X display socket; without a
terminal it refuses. X11 clients can observe input and screen contents, so
Wayland remains the recommended session. `diagnose` never uses X11.

To grant network access for one launch:

```sh
box-rpg launch /path/to/game --allow-network
```

The permission is not saved. It shares the host network, including Internet,
LAN, loopback services and abstract Unix sockets. Only grant it to trusted games.

To let a game update its own files for one launch:

```sh
box-rpg launch /path/to/game --allow-game-writes
```

The permission is not saved. The game directory is mounted writable, so only
use it with games you trust not to damage their own assets. Saves keep their
usual locations; the flag only lifts the read-only protection on game files.

## Install the required runtime

If no compatible runtime is installed, install one for the detected game type,
then run `box-rpg` again from the game directory.

### RPG Maker MV/MZ

MV/MZ games need NW.js. Choose a stable version from the interactive list; the
architecture is detected automatically.

```sh
box-rpg runtime nwjs available --interactive
```

Use `n` and `p` to browse version pages, enter a number to choose a version,
and confirm the installation.

When several matching runtimes are installed and no version was requested
(`--runtime`) or preferred (`box-rpg config set preferred-runtime`), an
interactive launch asks which one to use; empty input keeps the newest.
Non-interactive launches always use the newest without asking.

NW.js uses the owned Wayland socket selected by
`WAYLAND_DISPLAY` under `XDG_RUNTIME_DIR`. On X11 the launcher asks first and
then exposes only the local X socket with `DISPLAY` set. GPU devices under
`/dev/dri` and your PipeWire audio socket are exposed after validation so games
can render and play sound; both widen the sandbox (see the limits section).

Some desktop exports need an auxiliary file from the game root. The supported
workaround is to copy the required direct game-root file into the isolated
session:

```sh
box-rpg launch --copy-root-file game_messages.csv
```

`--copy-root-file` can be repeated for multiple files. It accepts only direct,
regular files from the validated game root and rejects paths containing
directories, symlinks, or session-owned names. Each copied file is limited to
16 MiB; FIFOs and other nonregular files are rejected without waiting for input.

Each NW.js game uses a persistent private Chromium/NW.js profile at
`$XDG_CACHE_HOME/box-rpg/profiles/<opaque-game-id>/sandbox` (or the corresponding
path under `~/.cache`). The profile stores preferences, web storage, and cache.
Existing profiles from older layouts are not migrated automatically.
RPG Maker saves remain in `save/` beside the entrypoint, usually `<game>/www/save/`
or `<game>/save/`. Existing saves are used directly, without copying to XDG data.

### RPG Maker 2000/2003

These projects are detected from `RPG_RT.ini`, `RPG_RT.ldb`, and `RPG_RT.lmt`.
Choose an x64 EasyRPG Player version:

```sh
box-rpg runtime easyrpg available --interactive
```

EasyRPG Player launches the project fullscreen with its project path set to the
game directory. The launcher creates `<game>/save/` if needed and passes it as
`--save-path` through the sandbox view. New files such as `Save01.lsd` and
`Save02.lsd` appear there immediately. Only that directory is writable, not the
entire game root.

The managed EasyRPG Player build has no Wayland video driver, so it runs
through XWayland: on a Wayland session the launcher asks once per launch for
explicit X11 consent (as described above) and exposes only the local X socket
next to the Wayland one, letting SDL negotiate the backend. Without XWayland
the game cannot open a window; `diagnose` still works because it never opens
one.

When several player versions are installed, an interactive launch asks which
one to use; empty input keeps the newest. Non-interactive launches always use
the newest without asking. Pass an explicit version with
`box-rpg launch /path/to/game --runtime 0.8.1.1` to skip the question
(`diagnose` accepts `--runtime` the same way).

To continue older saves, close the game, back up the root-level `Save*.lsd` files,
and copy them into `<game>/save/` without overwriting newer slots. There is no
automatic migration. Partially tested older launcher builds may also have saves
under XDG data or cached profiles: recover those manually before cleaning cache.

## Manage cached data

Inspect launcher-managed cleanup selectors as JSON Lines:

```sh
box-rpg cleanup list
```

Use `roots`, `runtimes`, `downloads`, or `profiles` to list one category. Remove
one listed item or every item in a category:

```sh
box-rpg cleanup remove CATEGORY SELECTOR
box-rpg cleanup remove CATEGORY --all
```

Removal prompts for confirmation. Add `--yes` to either command for immediate
noninteractive deletion; without a terminal, `--yes` is required.

`box-rpg cleanup all` shows the global scope and requires `DELETE ALL`. Add
`--yes` for immediate noninteractive deletion. To browse the interactive menu
instead, run `box-rpg cleanup --interactive`; it is incompatible with `--yes`.
Run `box-rpg cleanup -h` to see the available cleanup actions.

Cleanup never deletes game files or saves. It leaves `config.toml`, sessions,
and diagnostic reports intact, although root cleanup updates its authorized-root
entries. Cache cleanup only removes launcher-owned data under
`$XDG_CACHE_HOME/box-rpg` (or `~/.cache/box-rpg`), including individual or all
NW.js game profiles.

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

## Security and compatibility limits

- Use an absolute `HOME`. Empty or relative XDG variables fall back to
  `$HOME/.config` and `$HOME/.cache`; they never select the working directory.
  Configuration must be a regular user-owned file, not a symlink, without
  group/other write permission, and at most 1 MiB.
- Keep game directories in place during authorization and launch. The launcher
  rejects observed identity or location changes and opens inspected files without
  following symlinks. Terminal output escapes control characters in game metadata
  and authorization prompts without changing the actual paths.
- Inspection reads at most 16 MiB per metadata file. Launch manifests are limited
  to 1 MiB and diagnostic core files to 4 MiB. Runtime version probes have a
  10-second timeout and a combined 64 KiB output limit.
- Manifest `chromium-args` and `js-flags` are not forwarded. Only supported,
  correctly typed window settings are copied. Runtime processes receive a small
  explicit environment, not exported host secrets or loader injection variables.
  Games relying on custom environment variables may no longer work unchanged.
- Runtime requests validate HTTPS origins before each redirect. Each download
  has a 1 GiB transfer budget across retries. Extraction allows at most 4 GiB
  of expanded tar data, 1 GiB per member, 50,000 entries/paths, and depth 32.
  Download and extraction operations each have a cooperative 15-minute deadline;
  it does not interrupt a blocked system call. Sparse members, archive hardlinks,
  and symlinks escaping the installed runtime are rejected.
- Retry busy runtime operations after the other operation finishes. Persistent
  `.lock` files coordinate cooperating processes and are not stale downloads.
  Failed `.part` downloads may remain for cleanup but are never resumed.
- NW.js installations authenticate `SHASUMS256.txt` using its official detached
  GPG signature and bundled primary key fingerprint
  `1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A`, then hash the same archive used
  for extraction. Cached archives are checked again. Only an HTTP 404 for the
  signature permits HTTPS-only installation, with a `NOT VERIFIED` warning.
  Invalid signatures, hash mismatches, and other fetch failures reject installation.
  This requires `/usr/bin/gpg`; the user's keyring is not used. Key rotation and
  newly published revocations require updating the bundled key. Already installed
  runtime trees are not reauthenticated on launch.
- No official detached signatures for the distributed EasyRPG Linux tar archives
  were found; these continue to use the official HTTPS sources without a project
  checksum catalog. Neither transport nor signatures certify that software is benign.
- The sandbox exposes read-only system libraries and selected game/runtime files,
  a private process namespace, and private `/tmp`. Host HOME, D-Bus and SSH agent
  sockets are never exposed. GPU devices under `/dev/dri` and your PipeWire audio
  socket are exposed after strict validation so games can render and play sound.
  Both widen the sandbox: GPU access enables hardware fingerprinting and driver
  attack surface, and the audio socket does not distinguish speakers from the
  microphone. PulseAudio is covered through its native socket and cookie with
  the same validation; a missing cookie runs without one rather than failing.
  Graphical compatibility
  has not been validated against real games; some exports may fail to run.
- X11 sessions are supported only after an explicit per-launch confirmation.
  The X protocol lets any client observe input and screen contents, so a game
  running under X11 is inherently less isolated than under Wayland. Only the
  local display socket and its validated authority cookie are exposed; remote
  displays are rejected, and non-interactive launches refuse X11.
- `--allow-game-writes` mounts the game directory writable for that launch so
  self-updating games can patch their own files. New files created directly in
  the NW.js game view may not persist (only the save directory is guaranteed
  writable); EasyRPG writes persist. Do not combine it with untrusted games.
- Save directories are pinned without following symlinks and must be user-owned
  without group/other write permission. Host sockets, special files, escaping
  links and hardlinks in exposed game/runtime trees are rejected. Keep these
  trees under your control: validation is not an immutable snapshot and cannot
  defend against arbitrary concurrent host processes with the same permissions.
  Game code can still damage its own writable saves/profile, exhaust resources,
  or attack exposed interfaces. The sandbox is not a guarantee that hostile
  software is safe. `diagnose` executes the runtime, but never outside the sandbox.
