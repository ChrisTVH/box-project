# Locked development and builds

This guide shows how to set up a reproducible development environment and build `box-rpg` safely.

Use CPython 3.14 on Linux x86_64 with glibc 2.17 or newer. The locks were resolved with Python 3.14.7. Other versions, architectures, and musl need a separate resolution. Runtime `dependencies` stay empty.

## Prepare an environment

Create a fresh `.venv` so unrelated packages cannot leak in. Never install build tools into the base interpreter. Python's bundled `ensurepip` is the initial trust anchor. Run from a trusted checkout:

```sh
python3.14 -m venv .venv
PIP_CONFIG_FILE=/dev/null .venv/bin/python -I -m pip --isolated install \
  --require-hashes --only-binary=:all: --force-reinstall \
  --index-url https://pypi.org/simple \
  -r box-rpg/res/requirements/bootstrap.txt \
  -r box-rpg/res/requirements/build.txt -r box-rpg/res/requirements/dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest box-rpg/tests/tools/test_install.py box-rpg/tests/tools/test_locks.py
.venv/bin/ruff check install.py box-rpg/tests/tools/test_install.py box-rpg/tests/tools/test_locks.py
.venv/bin/ruff format --check install.py box-rpg/tests/tools/test_install.py box-rpg/tests/tools/test_locks.py
```

Each lock has one role: `bootstrap.txt` pins uv, `build.txt` pins pip and the build backend, and `dev.txt` pins development tools and their transitive dependencies. Keep dependency checking on: never add `--no-deps` to lock installs. Hashes protect artifact integrity, not the trust of package code.

Pyright needs its own Node.js runtime, outside the Python locks. Use an existing `node` on PATH and call the bundled checker directly, so no automatic Node.js download happens. Run from the repository root:

```sh
node "$(.venv/bin/python -c 'from pathlib import Path; import pyright; print(Path(pyright.__file__).parent / "dist/index.js")')" --project box-rpg/pyproject.toml --pythonpath .venv/bin/python
```

The equivalent wrapper form from inside `box-rpg/` is `../.venv/bin/python -m pyright --pythonpath ../.venv/bin/python`. Both resolve through the extra paths configured in `box-rpg/pyproject.toml`.

The frontend has its own config. Check it from `box-gui/` as described in `../box-gui/development.md`.

## Regenerate locks

Use the bootstrapped uv version. Inputs are the `dev` extra and `build` group in `box-rpg/pyproject.toml`, plus `box-rpg/res/requirements/bootstrap.in`. Keep the exact setuptools pin in `[build-system]` and the build group equal.

```sh
compile() {
  .venv/bin/uv --no-config pip compile "$@" \
    --python .venv/bin/python --python-platform x86_64-manylinux_2_17 \
    --generate-hashes --only-binary :all: --default-index https://pypi.org/simple \
    --no-header --no-annotate
}
compile --group build -o box-rpg/res/requirements/build.txt
compile box-rpg/pyproject.toml --extra dev -o box-rpg/res/requirements/dev.txt
compile box-rpg/res/requirements/bootstrap.in -o box-rpg/res/requirements/bootstrap.txt
git diff -- box-rpg/res/requirements box-rpg/pyproject.toml
```

Existing pins are preserved. Add `--upgrade` or `--upgrade-package NAME` only for deliberate updates, and change exact input pins when updating pip, setuptools, or uv. When updating uv, lock with the old tool first, install it with hashes, then regenerate with the new tool. For a non-mutating check, resolve with `-c box-rpg/res/requirements/NAME.txt`, write to `/tmp/NAME.txt`, and compare with `diff -u`. Review version and SHA256 changes, reinstall all locks, and rerun checks. Look up artifact hashes at `https://pypi.org/pypi/NAME/VERSION/json`. Never invent hashes. Tests verify input agreement and the installed closure offline, while pip verifies downloaded wheel hashes.

## Install safely

`python3.14 install.py` previews the staged build and user install. `python3.14 install.py --install` asks for confirmation and then:

1. Copies the trusted checkout into a private temporary directory, excluding build artifacts and the development environment.
2. Creates a private environment with bundled pip and force-installs `build.txt` with hashes. No build tools enter base Python.
3. Builds one wheel with `pip wheel --no-build-isolation --no-deps --no-index`. A missing backend requirement fails instead of downloading more.
4. Installs only that local wheel with base Python's `pip install --user --no-deps --no-index`, then publishes safe completions. Any failure stops before completion changes. Temporary files are cleaned on success and failure.

User install and removal use an isolated bootstrap: it imports base Python's pip first, checks that user-site is owned by the user below their home without link ancestors, then exposes that directory for metadata discovery only. A path-entry finder blocks imports from user-site and no `.pth` files run. This lets pip remove the previous wheel on upgrade without loading user-provided modules.

Completion install overwrites existing files with current sources and removal deletes them. Links are never followed. Do not trust arbitrary local files or Git ownership at install time. The installer ignores external pip and Python configuration, refuses root, and never uses `--yes` as consent for a PEP 668 override. Only the separate `--break-system-packages` option permits that override, only for user install and removal, never for the private build. Do not use `pip install .` as a substitute: its default isolated builds are not hash-locked. Locks reproduce dependencies, not byte-identical wheels or the interpreter. Build only a trusted, reviewed checkout.

References: [uv compile](https://docs.astral.sh/uv/pip/compile/), [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/).
