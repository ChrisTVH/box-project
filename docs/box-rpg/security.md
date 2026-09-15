# box-rpg security and compatibility limits

This guide explains what `box-rpg` protects against and where its limits are. Read it once before granting extra permissions. Daily steps are in the [manual](manual.md).

`box-rpg` runs games and runtimes as your own user. It confines what it can, but it cannot make hostile software safe.

## Trust model

- Games and downloaded runtimes are untrusted. Never run the launcher with elevated privileges. It needs none.
- A game is only launched below an allowed game root (`allowed_game_roots` in `config.toml`). Keep these roots limited to folders you control.
- Game paths are resolved, including links, before use. Path traversal and escaping links are rejected.
- Keep the game directory in place during authorization and launch. The launcher rejects observed location changes.
- An isolated session separates launcher files and profiles. Only the Bubblewrap sandbox confines running processes.

## Launch sandbox

Games and runtime version probes always run inside a mandatory Bubblewrap sandbox at `/usr/bin/bwrap` with user namespaces. There is no fallback without it.

The sandbox gives the game read-only system libraries and selected game and runtime files. Process, IPC, UTS, and `/tmp` namespaces are private. Your home directory, D-Bus, and SSH agent are never shared. Only saves, the selected profile, and private temporary storage are writable.

Extra access is always opt-in for one launch:

- `--allow-network` shares the host network, including internet, local network, and shared local networking such as loopback.
- Graphics devices under `/dev/dri` and read-only `/sys` are shared after validation so games can use acceleration. This enables hardware fingerprinting and driver attack surface.
- Your PipeWire and PulseAudio sockets are shared after validation so games play sound. The audio socket does not separate speakers from microphone input.
- X11 access needs explicit confirmation per launch, or `--x11`. This covers X11 sessions and XWayland for runtimes without Wayland support, such as the EasyRPG build. X11 programs can observe input and screen contents, so Wayland stays better isolated. Only the local display socket and its authority cookie are shared.
- `--allow-game-writes` mounts the game directory writable so self-updating games can patch themselves. Never combine it with untrusted games. For NW.js only the save directory is guaranteed writable. For EasyRPG, writes persist.
- The runtime receives a small explicit environment. Host secrets and loader injection variables are not passed. Terminal output escapes control characters from game metadata.

Validation is not an immutable snapshot. It cannot defend against other local processes with the same permissions. Game code can still damage its own writable saves or profile, exhaust resources, or attack shared interfaces. `diagnose` runs the runtime too, but always inside the sandbox.

## Runtimes, network, and authenticity

- Runtime requests validate HTTPS origins on every redirect. Each download has a 1 GiB transfer budget across retries. Failed `.part` files are never resumed.
- Extraction allows at most 4 GiB of expanded data, 1 GiB per member, 50,000 entries, and depth 32. Sparse members, archive hard links, and links escaping the runtime directory are rejected.
- Download and extraction each have a cooperative 15-minute deadline, checked between reads. It does not kill a stuck system call.
- Concurrent operations on the same runtime fail with a busy error. Retry after the other operation finishes.
- NW.js installs verify `SHASUMS256.txt` with its official detached GPG signature and bundled key fingerprint `1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A`, then hash the same archive used for extraction. Cached archives are checked again. Only a missing signature file (HTTP 404) permits an HTTPS-only install with a `NOT VERIFIED` warning. Other failures reject the install. This needs `/usr/bin/gpg`. Installed runtimes are not re-verified on every launch.
- The distributed EasyRPG archives have no official detached signatures, so they use the official HTTPS sources without a checksum catalog. Neither transport nor signatures prove that software is benign.

## Configuration and stored data

- Configuration must be a regular user-owned file without group or other write permission, at most 1 MiB. Empty or relative XDG variables fall back to `$HOME/.config` and `$HOME/.cache`.
- Managed cache paths use lower-case components. Runtime removal only affects runtimes inside the configured cache.
- Inspection reads at most 16 MiB per metadata file. Launch manifests are limited to 1 MiB and diagnostic core files to 4 MiB. Version probes have a 10-second timeout and a 64 KiB output limit.
- The game manifest never forwards `chromium-args` or `js-flags`. Only supported, correctly typed window settings are copied.
- Cleanup only removes launcher-owned data under `$XDG_CACHE_HOME/box-rpg`. It never deletes game files or saves, and it leaves `config.toml`, sessions, and reports intact.

## Compatibility limits

Wayland sessions are recommended. Graphical compatibility has not been validated against real games, so some exports may fail. The managed EasyRPG Player build has no Wayland video driver and runs through XWayland.
