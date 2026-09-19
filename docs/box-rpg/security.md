# box-rpg security and compatibility limits

This guide explains what `box-rpg` protects against and where its limits are. Read it once before granting extra permissions. Daily steps are in the [manual](manual.md).

`box-rpg` runs games and runtimes as your own user. It confines what it can, but it cannot make hostile software safe.

## Trust model

- Games and downloaded runtimes are untrusted. Never run the launcher with elevated privileges. It needs none.
- A game is only launched below an allowed game root (`allowed_game_roots` in `config.toml`). Keep these roots limited to folders you control. To authorize a root, see [Configuration](manual.md#configuration); to authorize one folder, see [Launch a game](manual.md#launch-a-game).
- Game paths are resolved, including links, before use. Path traversal and escaping links are rejected.
- Keep the game directory in place during authorization and launch. The launcher rejects observed location changes.
- An isolated session separates launcher files and profiles. Only the Bubblewrap sandbox confines running processes.
- One session per game entry is firm: a second launch while one runs fails with "game already running".
- Packed single-executable directories unpack into the source-keyed game profile at `$XDG_CACHE_HOME/box-rpg/profiles/<game-id>/game/` before detection. For the launch steps, see [Packed single-executable games](manual.md#packed-single-executable-games). Consent covers the source folder you pointed at; the session identifier, saves, and profiles follow the source too. Only trees detected as a supported engine are accepted. A changed source fingerprint (content hash, size, modification time) re-unpacks over the same tree with save folders carried forward, and removing the profile deletes the unpacked tree and its saves with it, except EasyRPG source saves which live in the source folder and survive. Inspection unpacks on first use, so it writes to the profile cache: same trust class as launch unpacking, no extra privileges.

## Launch sandbox

Games and runtime version probes always run inside a mandatory Bubblewrap sandbox at `/usr/bin/bwrap` with user namespaces. There is no fallback without it. `--die-with-parent` is kept: the launcher double-forks a detached supervisor (setsid, orphaned to init) that becomes the Bubblewrap parent, so the guarantee moves to the supervisor instead of disappearing when the launcher exits. Only the `pass_fds` allowlist crosses into Bubblewrap; supervisor descriptors use `O_CLOEXEC`, there is no shell, and extra descriptors are closed.

The sandbox gives the game read-only system libraries and selected game and runtime files. Process, IPC, UTS, and `/tmp` namespaces are private. Your home directory and SSH agent are never shared. D-Bus is never shared except for the opt-in filtered GameMode proxy described below. Only saves, the selected profile, and private temporary storage are writable.

Supervised sessions use `sessions_root/<game_id>/<session>` depth-2 directories with `0700` permissions and lower-case names under launcher containment. Liveness is an exclusive non-blocking flock on `session.lock` (PID-reuse safe, never a bare PID). `status.json` is launcher-owned, atomically written (`mkstemp`/`fsync`/`chmod 0600`/`os.replace`), never writable from the sandbox, and read with bounded typed checks (`None` while running, exit code when exited).

Validation is not an immutable snapshot. It cannot defend against other local processes with the same permissions. Game code can still damage its own writable saves or profile, exhaust resources, or attack shared interfaces. `diagnose` runs the runtime too, but always inside the sandbox.

## Opt-in access

Extra access is always opt-in for one launch. For how to request it, see [Permissions per launch](manual.md#permissions-per-launch).

| Access | Flag / Socket | Risk in one line |
| --- | --- | --- |
| Host network | `--allow-network` | shares host network, including internet, local network, and shared local networking such as loopback |
| Graphics acceleration | `/dev/dri` and read-only `/sys` | enables hardware fingerprinting and driver attack surface |
| Sound | PipeWire and PulseAudio sockets | audio socket does not separate speakers from microphone input |
| X11 display | `--x11` | X11 programs can observe input and screen contents, so Wayland stays better isolated |
| GameMode boost | `--gamemode` via `/usr/bin/busctl` and `/run/user/gamemode-proxy` | shares only `com.feralinteractive.GameMode` over a filtered `xdg-dbus-proxy` socket, failure fails the launch |
| Game self-writes | `--allow-game-writes` | mounts the game directory writable so self-updating games can patch themselves, never combine with untrusted games |

- `--gamemode` registers the host Bubblewrap PID with `com.feralinteractive.GameMode` via `/usr/bin/busctl` (host, timeout-bounded) and shares only that bus name over a filtered `xdg-dbus-proxy` socket at `/run/user/gamemode-proxy` (no see/own/broadcasts). Host registration is authoritative because the PID namespace hides in-sandbox PIDs; the in-sandbox `gamemoderun` prefix stays as fallback. A missing bus client, proxy, bus address, socket, or failed host registration fails the launch instead of running unboosted.
- `--allow-game-writes` mounts the game directory writable so self-updating games can patch themselves. Never combine it with untrusted games. For NW.js only the save directory is guaranteed writable. For EasyRPG, writes persist.
- EasyRPG saves are bound writable from the consented source folder's `save/` (`0700`, user-owned, no group or other write, no symlinks). The directory is opened with `O_NOFOLLOW`, pinned by device and inode plus `readlink` checks, and validated with `validate_tree`; the unpacked game tree stays read-only unless `--allow-game-writes`. First-run migration best-effort moves only regular files and directories from the old unpacked `save/` into a user-owned source `save/` without group or other write permission that does not exist or is empty (at most 5000 entries, 16 MiB per file); symlinks, FIFOs, sockets, and other specials never travel, existing destinations are never truncated (`O_EXCL`), and over-budget or unreadable files stay behind without failing the launch. Cleanup never deletes outside the managed cache, so source saves survive profile removal. For save locations, see [Game files and saves](manual.md#game-files-and-saves).
- The runtime receives a small explicit environment. Host secrets and loader injection variables are not passed. Terminal output escapes control characters from game metadata. Only the local display socket and its authority cookie are shared for X11. This covers X11 sessions and XWayland for runtimes without Wayland support, such as the EasyRPG build.

## Runtimes, network, and authenticity

For how to install and pin a runtime, see [Install a runtime](manual.md#install-a-runtime).

- Runtime requests validate HTTPS origins on every redirect. Each download has a 1 GiB transfer budget across retries. Failed `.part` files are never resumed.
- Extraction allows at most 4 GiB of expanded data, 1 GiB per member, 50,000 entries, and depth 32. Sparse members, archive hard links, and links escaping the runtime directory are rejected.
- Packed-executable unpacking enforces the same budgets (4 GiB total, 1 GiB per file, 50,000 entries, depth 32) plus a 1 GiB source size cap. Every run shares a cooperative 15-minute deadline, checked between files and decompression chunks. Over-budget runs fail naming the budget.
- Unpacking confines every write to its output directory: absolute paths and parent escapes are refused, only regular files and directories are created, and symbolic links are never created. Publication has two levels: per-attempt trial writes stage into a private temporary directory below the output and only the winning profile is moved into place, while profile game trees stage into a separate private directory inside the profile and publish under the cooperative per-profile cache lock by renaming the previous tree to a sibling backup, moving staging into place with `os.replace`, then removing the backup (restoring the backup when the final name is missing after a failure); a busy entry fails fast so the caller can retry. Save folders are carried forward across re-unpacks with only regular files and directories; symlinks, FIFOs, and other specials are never carried. For EasyRPG the unpacked `save/` simply goes stale after source migration (it is still carried, but never bound). A denied game still leaves its published profile game tree; removing the profile deletes it with its saves, except EasyRPG source saves which live outside the cache.
- Download and extraction each have a cooperative 15-minute deadline, checked between reads. It does not kill a stuck system call.
- Concurrent operations on the same runtime fail with a busy error. Retry after the other operation finishes.
- NW.js installs verify `SHASUMS256.txt` with its official detached GPG signature and bundled key fingerprint `1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A`, then hash the same archive used for extraction. Cached archives are checked again. Only a missing signature file (HTTP 404) permits an HTTPS-only install with a `NOT VERIFIED` warning. Other failures reject the install. This needs `/usr/bin/gpg`. Installed runtimes are not re-verified on every launch.
- The distributed EasyRPG archives have no official detached signatures, so they use the official HTTPS sources without a checksum catalog. Neither transport nor signatures prove that software is benign.

## Budgets and limits

All enforced numbers in one place. Rationale stays in the sections above.

| Area | Limit |
| --- | --- |
| Runtime download transfer | 1 GiB transfer budget across retries |
| Runtime extraction expanded data | at most 4 GiB of expanded data |
| Runtime extraction single member | 1 GiB per member |
| Runtime extraction entries | 50,000 entries |
| Runtime extraction depth | depth 32 |
| Packed unpack total | 4 GiB total |
| Packed unpack single file | 1 GiB per file |
| Packed unpack entries | 50,000 entries |
| Packed unpack depth | depth 32 |
| Packed source size | 1 GiB source size cap |
| Download deadline | cooperative 15-minute deadline, checked between reads |
| Extraction deadline | cooperative 15-minute deadline, checked between reads |
| Unpack deadline | cooperative 15-minute deadline, checked between files and decompression chunks |
| Configuration file | at most 1 MiB |
| Inspection metadata | at most 16 MiB per metadata file |
| Launch manifest | limited to 1 MiB |
| Diagnostic core | limited to 4 MiB |
| Version probe timeout | 10-second timeout |
| Version probe output | 64 KiB output limit |
| Save migration entries | at most 5000 entries |
| Save migration file size | 16 MiB per file |
| Session directories | `sessions_root/<game_id>/<session>` depth-2 directories with `0700` permissions |
| EasyRPG save directory | `0700`, user-owned, no group or other write, no symlinks |
| Status file | atomically written (`mkstemp`/`fsync`/`chmod 0600`/`os.replace`) |
| Configuration ownership | regular user-owned file without group or other write permission |

## Configuration and stored data

- Configuration must be a regular user-owned file without group or other write permission, at most 1 MiB. Empty or relative XDG variables fall back to `$HOME/.config` and `$HOME/.cache`.
- Managed cache paths use lower-case components. Runtime removal only affects runtimes inside the configured cache.
- Inspection reads at most 16 MiB per metadata file. Launch manifests are limited to 1 MiB and diagnostic core files to 4 MiB. Version probes have a 10-second timeout and a 64 KiB output limit.
- The game manifest never forwards `chromium-args` or `js-flags`. Only supported, correctly typed window settings are copied.
- Cleanup only removes launcher-owned data under `$XDG_CACHE_HOME/box-rpg`. It never deletes game files or saves, and it leaves `config.toml`, sessions, and reports intact. For the cleanup steps, see [Manage stored data](manual.md#manage-stored-data).

## Compatibility limits

Wayland sessions are recommended. Graphical compatibility has not been validated against real games, so some exports may fail. The managed EasyRPG Player build has no Wayland video driver and runs through XWayland.
