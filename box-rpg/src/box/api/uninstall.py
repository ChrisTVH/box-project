"""Descriptor-safe full wipe of launcher-owned cache and config roots."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from box.errors import BoxError
from box.paths import AppPaths
from box.utils.i18n import _

__all__ = ["full_wipe_data"]


def full_wipe_data(paths: AppPaths) -> None:
    """Validate and delete only the launcher cache and config roots."""
    home = _resolve_home()
    cache = _validate_wipe_root(paths.cache_root, home)
    config = _validate_wipe_root(paths.config_root, home)
    if cache == config:
        _remove_validated_root(cache)
    else:
        _remove_validated_root(cache)
        _remove_validated_root(config)


def _resolve_home() -> Path:
    """Return the resolved absolute HOME, refusing relative or root values."""
    home_value = os.environ.get("HOME", "")
    home = Path(home_value) if home_value else Path.home()
    if not home.is_absolute():
        raise BoxError(_("HOME must be an absolute path"))
    try:
        resolved = home.resolve(strict=False)
    except OSError as exc:
        raise BoxError(_("cannot resolve HOME directory: {path}").format(path=home)) from exc
    if resolved == Path(resolved.anchor):
        raise BoxError(_("refusing full wipe of filesystem root: {path}").format(path=home))
    return resolved


def _validate_wipe_root(candidate: Path, home_resolved: Path) -> Path:
    """Validate one launcher-owned root without following symlinks."""
    # Reject a symlinked candidate itself using lstat without following it.
    try:
        candidate_stat = os.lstat(candidate)
    except FileNotFoundError:
        candidate_stat = None
    except OSError as exc:
        raise BoxError(_("cannot inspect wipe path: {path}").format(path=candidate)) from exc
    if candidate_stat is not None and stat.S_ISLNK(candidate_stat.st_mode):
        raise BoxError(_("refusing full wipe of symlinked path: {path}").format(path=candidate))
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise BoxError(_("cannot inspect wipe path: {path}").format(path=candidate)) from exc
    if resolved.name != "box-rpg":
        raise BoxError(_("refusing full wipe of unexpected path: {path}").format(path=candidate))
    if resolved == home_resolved:
        raise BoxError(_("refusing full wipe of home directory: {path}").format(path=candidate))
    if resolved == Path(resolved.anchor):
        raise BoxError(_("refusing full wipe of filesystem root: {path}").format(path=candidate))
    try:
        relative = resolved.relative_to(home_resolved)
    except ValueError as exc:
        raise BoxError(_("refusing full wipe outside home: {path}").format(path=candidate)) from exc
    # Walk the resolved chain with lstat so symlinked ancestors are refused.
    current = home_resolved
    for component in relative.parts:
        current = current / component
        try:
            component_stat = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BoxError(_("cannot inspect wipe path: {path}").format(path=candidate)) from exc
        if stat.S_ISLNK(component_stat.st_mode):
            raise BoxError(_("refusing full wipe through symlink: {path}").format(path=current))
    if os.path.lexists(resolved) or os.path.lexists(candidate):
        try:
            final_stat = os.stat(resolved, follow_symlinks=False)
        except OSError as exc:
            raise BoxError(_("cannot inspect wipe path: {path}").format(path=candidate)) from exc
        if stat.S_ISLNK(final_stat.st_mode):
            raise BoxError(_("refusing full wipe of symlinked path: {path}").format(path=candidate))
        if final_stat.st_uid != os.getuid():
            raise BoxError(
                _("refusing full wipe of path owned by another user: {path}").format(path=candidate)
            )
    return resolved


def _remove_validated_root(resolved: Path) -> None:
    """Remove one validated root through its parent descriptor without following links."""
    if not os.path.lexists(resolved):
        return
    parent = resolved.parent
    name = resolved.name
    try:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise BoxError(_("cannot safely open wipe path: {path}").format(path=resolved)) from exc
    try:
        try:
            entry_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BoxError(_("cannot inspect wipe path: {path}").format(path=resolved)) from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise BoxError(_("refusing full wipe of symlinked path: {path}").format(path=resolved))
        if entry_stat.st_uid != os.getuid():
            raise BoxError(
                _("refusing full wipe of path owned by another user: {path}").format(path=resolved)
            )
        try:
            if stat.S_ISDIR(entry_stat.st_mode):
                shutil.rmtree(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
        except OSError as exc:
            raise BoxError(_("cannot remove wipe path: {path}").format(path=resolved)) from exc
    finally:
        os.close(descriptor)
