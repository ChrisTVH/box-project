# box-rpg security and compatibility limits

`box-rpg` runs untrusted games and runtimes as your own user. The launcher
confines what it can, but it cannot turn hostile software safe. This document
is the single reference for the trust model and its limits; the
[manual](manual.md) covers daily use.

## Trust model

- Games and downloaded runtimes are untrusted software. Never run the launcher
  with elevated privileges; it needs none and refuses root where it matters.
  Keep authorized roots limited to directories you control.
- Game paths are never trusted merely because they contain `package.json`.
  The launcher validates the game structure, resolves paths (including
  symlinks) before use, and only launches games below configured
  `allowed_game_roots`. Add a library root explicitly to authorize more than
  one game below it. On the first interactive launch of a game outside your
  configured libraries, the launcher asks before storing its exact validated
  root.
- Keep game directories in place during authorization and launch. The launcher
  rejects observed identity or location changes and opens inspected files
  without following symlinks.
- An isolated session separates launcher files and profiles; only the
  Bubblewrap sandbox below confines processes.

## Launch sandbox

Games and runtime version probes execute inside a mandatory Bubblewrap
sandbox (`/usr/bin/bwrap` with enabled user namespaces; no unsandboxed
fallback). It exposes read-only system libraries and selected game/runtime
files, with private process, IPC, UTS, and `/tmp` namespaces. The host HOME,
D-Bus, and SSH agent sockets are never exposed. The game and runtime trees
are read-only; only game saves, the selected profile, and private temporary
storage are writable.

- Networking is disabled unless `launch --allow-network` is passed for that
  run. It shares the host network, including Internet, LAN, loopback services,
  and abstract Unix sockets.
- GPU devices under `/dev/dri` plus read-only `/sys` are exposed after strict
  validation so games can render with acceleration; this enables hardware
  fingerprinting and driver attack surface.
- Your PipeWire and PulseAudio sockets are exposed after strict validation so
  games play sound. The audio socket does not distinguish speakers from the
  microphone; a missing PulseAudio cookie runs without one rather than failing.
- Any X11 socket exposure works only after an explicit per-launch
  confirmation (or `--x11`): X11 sessions, and XWayland for runtimes without
  Wayland support on Wayland sessions (such as the EasyRPG static build).
  Non-interactive launches refuse X11 without `--x11`; with `--x11` an
  unusable display fails instead of falling back. The X protocol lets any
  client observe input and screen contents, so a game under X11 is inherently
  less isolated than under Wayland. Only the local display socket and its
  validated authority cookie are exposed; remote displays are rejected.
- `--allow-game-writes` mounts the game directory writable for that launch so
  self-updating games can patch their own files. New files created directly in
  the NW.js game view may not persist (only the save directory is guaranteed
  writable); EasyRPG writes persist. Do not combine it with untrusted games.
- Save directories are pinned without following symlinks and must be
  user-owned without group/other write permission. Host sockets, special
  files, escaping links, and hardlinks in exposed trees are rejected.
- Runtime processes receive a small explicit environment, not exported host
  secrets or loader injection variables. Terminal output escapes control
  characters from game metadata and authorization prompts without changing the
  actual paths.
- Validation is not an immutable snapshot and cannot defend against arbitrary
  concurrent host processes with the same permissions. Game code can still
  damage its own writable saves/profile, exhaust resources, or attack exposed
  interfaces. `diagnose` executes the runtime, but never outside the sandbox.

## Runtimes: network, quotas, authenticity

- Runtime requests validate HTTPS origins before each redirect. Each download
  has a 1 GiB transfer budget across retries. Extraction allows at most 4 GiB
  of expanded tar data, 1 GiB per member, 50,000 entries/paths, and depth 32.
  Download and extraction operations each have a cooperative 15-minute
  deadline; it does not interrupt a blocked system call. Sparse members,
  archive hardlinks, and symlinks escaping the installed runtime are rejected.
- Retry busy runtime operations after the other operation finishes. Persistent
  `.lock` files coordinate cooperating processes and are not stale downloads.
  Failed `.part` downloads may remain for cleanup but are never resumed.
- NW.js installations authenticate `SHASUMS256.txt` using its official
  detached GPG signature and bundled primary key fingerprint
  `1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A`, then hash the same archive used
  for extraction. Cached archives are checked again. Only an HTTP 404 for the
  signature permits HTTPS-only installation, with a `NOT VERIFIED` warning.
  Invalid signatures, hash mismatches, and other fetch failures reject
  installation. This requires `/usr/bin/gpg`; the user's keyring is not used.
  Key rotation and newly published revocations require updating the bundled
  key. Already installed runtime trees are not reauthenticated on launch.
- No official detached signatures for the distributed EasyRPG Linux tar
  archives were found; these continue to use the official HTTPS sources
  without a project checksum catalog. Neither transport nor signatures certify
  that software is benign.

## Configuration and managed data

- Use an absolute `HOME`. Empty or relative XDG variables fall back to
  `$HOME/.config` and `$HOME/.cache`; they never select the working directory.
  Configuration must be a regular user-owned file, not a symlink, without
  group/other write permission, and at most 1 MiB.
- Managed cache paths (runtimes, profiles, downloads) use lower-case
  components. Runtime removal is limited to runtimes managed inside the
  configured cache; it must not remove arbitrary paths.
- Inspection reads at most 16 MiB per metadata file. Launch manifests are
  limited to 1 MiB and diagnostic core files to 4 MiB. Runtime version probes
  have a 10-second timeout and a combined 64 KiB output limit.
- The game manifest never forwards `chromium-args` or `js-flags`. Only
  supported, correctly typed window settings are copied; games relying on
  custom environment variables may no longer work unchanged.
- Cleanup never deletes game files or saves. It leaves `config.toml`,
  sessions, and diagnostic reports intact, although root cleanup updates its
  authorized-root entries. Cache cleanup only removes launcher-owned data
  under `$XDG_CACHE_HOME/box-rpg` (or `~/.cache/box-rpg`), including
  individual or all game profiles.

## Compatibility limits

- Wayland sessions are recommended. Graphical compatibility has not been
  validated against real games; some exports may fail to run.
- The managed EasyRPG Player build has no Wayland video driver, so it runs
  through XWayland (see the X11 rule above).
