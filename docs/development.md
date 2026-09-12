# Locked development and builds

Use CPython 3.14 on Linux x86_64 with glibc >= 2.17. These locks were
resolved and checked with Python 3.14.7; other Python versions, architectures
and musl need a separate resolution and validation. Extra artifact hashes do
not imply support for other platforms. Runtime `dependencies` remain empty.

## Prepare a local environment

Start with a fresh `.venv` for an environment without unrelated packages.
Python's bundled `ensurepip` is the initial trust anchor; never install build
tools into the base interpreter. Run from a trusted checkout:

```sh
python3.14 -m venv .venv
PIP_CONFIG_FILE=/dev/null .venv/bin/python -I -m pip --isolated install \
  --require-hashes --only-binary=:all: --force-reinstall \
  --index-url https://pypi.org/simple \
  -r res/requirements/bootstrap.txt \
  -r res/requirements/build.txt -r res/requirements/dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest tests/test_install.py tests/test_locks.py
.venv/bin/ruff check install.py tests/test_install.py tests/test_locks.py
.venv/bin/ruff format --check install.py tests/test_install.py tests/test_locks.py
```

`bootstrap.txt` pins uv, `build.txt` pins pip and the backend, and `dev.txt`
pins the development tools and their transitive Python dependencies. Install
with dependency checking enabled: do not add `--no-deps` to lock installs.
`--force-reinstall` also checks artifacts for previously installed versions.
Hashes protect artifact integrity, not the trustworthiness of package code.

Pyright additionally needs an independently provisioned Node.js runtime.
Node.js is outside the Python locks. To avoid the Python wrapper's automatic
Node.js download fallback, invoke the bundled checker directly with an
existing `node` on PATH (do not install Node automatically):

```sh
node "$(.venv/bin/python -c 'from pathlib import Path; import pyright; print(Path(pyright.__file__).parent / "dist/index.js")')"
```

## Regenerate and review

Use the bootstrapped uv version. Sources are the `dev` extra and `build`
dependency group in `pyproject.toml`, plus `res/requirements/bootstrap.in`.
Keep the exact setuptools pin in `[build-system]` and the build group equal.

```sh
compile() {
  .venv/bin/uv --no-config pip compile "$@" \
    --python .venv/bin/python --python-platform x86_64-manylinux_2_17 \
    --generate-hashes --only-binary :all: --default-index https://pypi.org/simple \
    --no-header --no-annotate
}
compile --group build -o res/requirements/build.txt
compile pyproject.toml --extra dev -o res/requirements/dev.txt
compile res/requirements/bootstrap.in -o res/requirements/bootstrap.txt
git diff -- res/requirements pyproject.toml
```

Existing output pins are preserved. Add `--upgrade` or `--upgrade-package NAME`
for deliberate updates; change exact input pins when updating pip, setuptools
or uv. When updating uv, generate and verify its bootstrap lock with the old
tool first, install it with hashes, then regenerate with the new tool.
For a non-mutating resolution check, pass `-c res/requirements/NAME.txt` and
write `-o /tmp/NAME.txt`, then compare with `diff -u`. Review version and SHA256
changes, reinstall all locks with the command above, and rerun checks. PyPI's
`https://pypi.org/pypi/NAME/VERSION/json` lists artifact SHA256 digests for review;
never insert guessed hashes. Tests check input agreement and installed
transitive closure offline; pip verifies actual downloaded wheel hashes.

## Install the launcher safely

`python3.14 install.py` previews the staged build and user installation.
`python3.14 install.py --install` requests confirmation before executing it:

1. Copy the trusted checkout into a private temporary directory, excluding
   prior build artifacts and the development environment.
2. Create a private venv using Python's bundled pip; force-install `build.txt`
   with `--require-hashes --only-binary=:all:`. No build tools enter base Python.
3. Build one wheel with `pip wheel --no-build-isolation --no-deps --no-index`.
   A missing backend requirement fails instead of triggering another download.
4. Install only that local wheel with base Python's
   `pip install --user --no-deps --no-index`, then publish safe completions.
   Any build/install failure stops before completion changes. Temporary files
   are cleaned on success and failure.

User installation and removal run through an isolated bootstrap: it imports
base Python's pip first, validates that user-site is owned by the user beneath
their home without symlink ancestors, then exposes that directory for metadata
discovery only. A path-entry finder blocks imports from user-site; no `.pth`
files are processed. This lets pip remove the previous wheel's recorded files
and metadata on upgrade without loading user-provided Python modules.

Completion upgrades/removal accept the current source or the exact reviewed
historical SHA256 entries in `install.py`. Customized copies remain untouched;
raced replacements are never overwritten. When releasing changed completions,
add only hashes of reviewed previous release artifacts to that registry. Do
not trust arbitrary local files or discover ownership from Git at install time.

The installer ignores external pip/Python configuration for these commands.
The base Python and its existing pip remain trust anchors for user installation.
It refuses root; `--yes` never grants a PEP 668 override. Only the separate
`--break-system-packages` option permits that override, and only for user
installation/removal, never for the private build. Do not use `pip install .`
or `pip install '.[dev]'` as a substitute: their default isolated builds are
not hash-locked. Locks reproduce dependencies, not necessarily byte-identical
wheels or the interpreter/OS; build only a trusted, reviewed checkout.

References: [uv compile](https://docs.astral.sh/uv/pip/compile/),
[pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/).
