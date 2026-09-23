# Mono-repository architecture

The version is computed dynamically at build/CI time from git history (see `version-standard`); there are no alignment commits.
Based on the `commit-standard`, `version-standard` and `documentation-writer` skills.

## Documentation for both projects

### box-gui
- docs/box-gui/development.md
- docs/box-gui/translations.md

### box-rpg
- docs/box-rpg/api.md
- docs/box-rpg/development.md
- docs/box-rpg/manual.md
- docs/box-rpg/security.md
- docs/box-rpg/translations.md

## Specific context of each project
- box-gui/AGENTS.md
- box-rpg/AGENTS.md

## Test AppImage builds

Build a test artifact from a dirty tree without touching the repo:

```sh
python3 -m tools.build_appimage --test-build --yes
```

This snapshots the tree to `/tmp`, builds there with `--no-create-tag`, and writes the git-ignored `tools/target/box-rpg-maker-test.appimage`. Add `--force` to overwrite a previous test artifact, or `--appdir-only` for an offline AppDir-only smoke test that skips the appimagetool download. The artifact packs only the frontend; install the backend first (`./install.py --install --target cli`). Full details live in `docs/box-gui/development.md` under "AppImage distribution".
