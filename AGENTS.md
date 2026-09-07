# Repository guide

## Scope

`box-rpg` is a Python 3.14+ command-line launcher for RPG Maker MV/MZ exports
using NW.js. Keep the supported-engine boundary explicit; do not add support
for other RPG Maker generations or runtimes without a documented decision.

## Development

- Keep production code under `src/` and tests under `tests/`.
- Use Python 3.14+ and the checks configured in `pyproject.toml`.
- Keep user-facing documentation concise and in English.
- Keep code comments and variable names in English.
- Add or update focused tests with behavior changes.

## Safety invariants

- Resolve and validate game paths before launch; allow only configured roots.
- Reject path traversal and symlink escapes.
- Treat runtimes and games as untrusted inputs; never require elevated rights.
- Delete only runtimes owned by the launcher inside its XDG cache directory.
- Keep managed path components lower-case. Do not weaken this case-sensitive
  path restriction without updating the documentation and tests.

## Project data

Configuration examples belong in `res/config/`; shell completions belong in
`res/completions/`; longer user guidance belongs in `docs/`.
