# Diagnosing ci-mount launch failures

This guide covers the `cannot validate sandbox tree ... [Errno 9] Bad file
descriptor` failure seen when launching an RPG Maker MV/MZ game with the
case-insensitive mount enabled (`--ci-mount`, the "Case-insensitive mount"
switch in Game Detail). It is a diagnosis aid, not a fix.

## Symptoms

The launch fails immediately with an error naming a path deep inside the game
assets, for example:

```text
cannot validate sandbox tree at ./www/img/parallaxes/uni.rpgmv: [Errno 9] Bad file descriptor: 71
```

The number at the end is a file descriptor, not a line number. Reports so far
come from the graphical launcher and could not be reproduced from the CLI on
the same folder.

## When it happens, and what changed

Earlier reports described a specific pattern: the failure appeared on the
**first** launch of a freshly added game and cleared on a retry. That pattern
came from the consent prompt. `box-rpg launch` asks for consent before the
first launch of a folder that is not yet an allowed game root, and the GUI
presents that prompt through `Interaction`, which marshals a dialog onto the
GTK main loop and blocks the worker thread running the launch. A retry found
the root already recorded, so no dialog blocked the worker.

The launcher now records a game root as an allowed root at the moment the
game is added to the library, so that prompt no longer appears for freshly
added games and the first-launch asymmetry described above no longer occurs
for that case. A game whose root is still unregistered — because it was
relocated, because the configuration was edited by hand, or because the root
was removed — can still hit the prompt and the failure with it.

So: if you can still reproduce this, note whether the game was just added, and
whether you saw a consent dialog first. That distinction decides which
hypothesis applies.

## Capturing the trace

The ci-mount FUSE daemon already writes a metadata-only trace (lookup and
readdir names, inode numbers, and mtimes; never file contents) when the
`BOX_CIMOUNT_DEBUG_LOG` environment variable names a file.

From the GUI, click the **bug icon in the library footer** (bottom-left,
next to the centered credit). It opens the **Debugging** window, which holds
the **Record ci-mount debug log** switch under *Diagnostics*. Point it at a
file, enable it, then launch the failing game. The option is stored in
`defaults.json` and re-exported at startup, so it survives a restart. The
trace applies to the next launch, never to one already running. Leaving it
off is the default; the daemon logs nothing while the variable is unset.

For the CLI, export the variable yourself:

```sh
BOX_CIMOUNT_DEBUG_LOG=/tmp/cimount.log box-rpg launch . --ci-mount --gamemode
```

## What the trace tells you

A healthy walk over the game root looks like repeated lookups followed by
`readdir` cache hits:

```text
lookup parent=1 name='www' name_resolved='www'
readdir ino=2 miss mtime_ns=1663908530450812300 entries=10
readdir ino=2 hit mtime_ns=1663908530450812300 entries=10
```

Look for `error=` lines, which the daemon emits when it cannot serve a
request:

```text
readdir ino=2 error=scan errno=9
readdir ino=2 error=fstat errno=9
```

An `errno=9` in the trace means the daemon lost a directory descriptor it was
serving from, which matches the `EBADF` the launcher reports. If the trace
ends abruptly partway through the game tree, the daemon died or was torn down
while the walk was in progress. If the trace is complete and shows only hits,
the failure is on the launcher side of the walk, not in the daemon.

Please attach the trace to any bug report, together with the failing game, the
exact error text, and whether a retry succeeded.

## Known open question

The failing walk in `box/launch/sandbox.py::validate_tree` runs
`os.fwalk` over the FUSE mount while the launcher process is multithreaded:
the GUI runs the launch on a worker thread with the GTK main loop and the
library page's session poll still active. `os.fwalk` takes each directory
descriptor from the shared descriptor space, so the walk is exposed to
concurrent descriptor reuse by other threads. This is the leading hypothesis
for the `EBADF`, but it has not been reproduced deterministically outside the
failing launch, so no speculative change has been made to that code. The
trace above is the tool for confirming or refuting it.
