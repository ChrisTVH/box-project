"""Stable surface smoke tests without requiring a display."""

from __future__ import annotations

import ast
from pathlib import Path

from box.api import AppPaths, ConfigRepository
from box.api.inspect import inspect
from box.errors import BoxError
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import EasyRPGCatalog


def test_stable_inspect_importable() -> None:
    assert callable(inspect)
    assert issubclass(BoxError, Exception)


def test_stable_front_door_imports() -> None:
    """AppPaths and ConfigRepository resolve through the box.api front door."""
    assert isinstance(AppPaths.from_environment, object)
    assert isinstance(ConfigRepository, type)


def test_runtime_catalogs_importable() -> None:
    """The two allowed catalog classes stay importable for the frontend."""
    assert isinstance(RuntimeCatalog, type)
    assert isinstance(EasyRPGCatalog, type)


def test_box_gui_imports_stable_surface_only() -> None:
    """box_gui imports only the stable surface plus blessed extras."""
    allowed_prefixes = (
        "box.api",
        "box.models",
        "box.errors",
        "box.paths",
        "box.config.models",
    )
    allowed_modules = ("box.runtime.catalog", "box.runtime.easyrpg", "box.games.identity")
    forbidden_parts = ("downloader", "sandbox", "runtime.platform", "box.cli")
    root = Path(__file__).resolve().parent.parent / "src" / "box_gui"
    assert root.is_dir()
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        is_library = path.name == "library.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if not module.startswith("box."):
                        continue
                    found.append(module)
                    if is_library:
                        raise AssertionError(
                            f"library.py must only import AppPaths via from-import, "
                            f"got import {module} in {path.name}"
                        )
                    if module == "box.utils.i18n" or module.startswith("box.utils.i18n."):
                        raise AssertionError(
                            f"only configure may be imported from box.utils.i18n, "
                            f"got import {module} in {path.name}"
                        )
                    if module == "box.utils" or module.startswith("box.utils."):
                        raise AssertionError(f"unstable import {module} in {path.name}")
            elif (
                isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("box.")
            ):
                module = node.module
                names = {alias.name for alias in node.names}
                found.append(module)
                if is_library:
                    assert module in ("box.api", "box.paths"), (
                        f"library.py unstable import {module} in {path.name}"
                    )
                    assert names == {"AppPaths"}, (
                        f"library.py may only import AppPaths, got {names} in {path.name}"
                    )
                    continue
                if module == "box.utils.i18n":
                    assert names == {"configure"}, (
                        f"only configure may be imported from box.utils.i18n, "
                        f"got {names} in {path.name}"
                    )
                    continue
                if module == "box.utils" or module.startswith("box.utils."):
                    raise AssertionError(f"unstable import {module} in {path.name}")
    assert found, "expected box imports in box_gui"
    for module in found:
        if module == "box.utils.i18n":
            continue
        for part in forbidden_parts:
            assert part not in module, f"forbidden import {module}"
        allowed = module in allowed_modules or module.startswith(
            tuple(f"{name}." for name in allowed_modules)
        )
        if not allowed:
            allowed = any(
                module == prefix or module.startswith(f"{prefix}.") for prefix in allowed_prefixes
            )
        assert allowed, f"unstable import {module}"


def test_core_subpackage_has_no_widget_imports() -> None:
    """Nothing under core/ touches Gtk/Adw (GdkPixbuf decoding stays allowed)."""
    root = Path(__file__).resolve().parent.parent / "src" / "box_gui" / "core"
    assert root.is_dir()
    checked: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        checked.append(path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "gi.repository":
                for alias in node.names:
                    assert alias.name not in ("Gtk", "Adw"), (
                        f"{path.name} imports widget toolkit {alias.name}"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "require_version"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in ("Gtk", "Adw")
            ):
                raise AssertionError(f"{path.name} requires widget toolkit {node.args[0].value}")
    assert checked, "expected modules in box_gui/core/"


def test_bundled_icon_names_resolve_to_repo_svgs() -> None:
    """Every box-rpg-* icon referenced in src ships in res/icons/."""
    import re

    root = Path(__file__).resolve().parent.parent
    names: set[str] = set()
    for path in sorted((root / "src" / "box_gui").rglob("*.py")):
        names.update(re.findall(r'"(box-rpg-[a-z-]+)"', path.read_text(encoding="utf-8")))
    assert names, "expected vendored icon references in src"
    for name in sorted(names):
        assert (root / "res" / "icons" / f"{name}.svg").is_file(), f"missing {name}.svg"
