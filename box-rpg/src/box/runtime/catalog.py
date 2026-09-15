"""Discovery and deletion of launcher-owned NW.js runtimes."""

from __future__ import annotations

import os
import shutil
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from box.errors import ConfigurationError, RuntimeError
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.platform import normalize_architecture
from box.runtime.security import cache_lock
from box.runtime.validator import normalize_version, runtime_executable
from box.utils.i18n import _


@dataclass(frozen=True, slots=True)
class ManagedRuntime:
    """A structurally valid launcher-owned runtime directory.

    Unlike :class:`box.models.RuntimeInfo`, this representation does not
    require an executable and is suitable for cleaning up interrupted installs.
    """

    spec: RuntimeSpec
    root: Path


class RuntimeCatalog:
    """Inspect and remove only runtimes owned by the launcher cache."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def list(self) -> tuple[RuntimeInfo, ...]:
        """Return valid local runtimes ordered by platform and version directory."""
        runtimes: list[RuntimeInfo] = []
        for runtime in self.list_managed():
            try:
                runtimes.append(
                    RuntimeInfo(runtime.spec, runtime.root, runtime_executable(runtime.root))
                )
            except RuntimeError:
                continue
        return tuple(runtimes)

    def list_managed(self) -> tuple[ManagedRuntime, ...]:
        """Return structurally valid runtime directories, including incomplete installs."""
        self._paths.ensure()
        runtimes: list[ManagedRuntime] = []
        for platform_directory in sorted(
            path
            for path in self._paths.runtimes_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        ):
            architecture = _platform_architecture(platform_directory)
            if architecture is None:
                continue
            for runtime_directory in sorted(
                path
                for path in platform_directory.iterdir()
                if path.is_dir() and not path.is_symlink()
            ):
                runtime = _managed_runtime(runtime_directory, architecture)
                if runtime is None:
                    continue
                try:
                    root = self._validate_managed_runtime(runtime)
                except ConfigurationError:
                    continue
                runtimes.append(ManagedRuntime(runtime.spec, root))
        return tuple(runtimes)

    def get(self, version: str, architecture: str, sdk: bool = False) -> RuntimeInfo:
        """Return a requested installed runtime or raise a clear error."""
        spec = RuntimeSpec(normalize_version(version), normalize_architecture(architecture), sdk)
        root = self._paths.runtimes_root / f"linux-{spec.architecture}" / spec.directory_name
        self._paths.ensure_managed_runtime_path(root)
        return RuntimeInfo(spec, root, runtime_executable(root))

    def remove(self, version: str, architecture: str, sdk: bool = False) -> None:
        """Delete one launcher-owned runtime, including an incomplete install."""
        spec = RuntimeSpec(normalize_version(version), normalize_architecture(architecture), sdk)
        root = self._paths.runtimes_root / f"linux-{spec.architecture}" / spec.directory_name
        self.remove_managed(ManagedRuntime(spec, root))

    def remove_managed(self, runtime: ManagedRuntime) -> None:
        """Delete an enumerated managed runtime after revalidating its directory."""
        managed = self._validate_managed_runtime(runtime)
        descriptor = _open_runtime_platform(self._paths, runtime.spec.architecture)
        locks = ExitStack()
        try:
            locks.enter_context(cache_lock(descriptor, managed.name))
            entry = os.stat(managed.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode):
                raise ConfigurationError(
                    _("managed runtime directory is missing or unsafe: {path}").format(path=managed)
                )
            shutil.rmtree(managed.name, dir_fd=descriptor)
        finally:
            locks.close()
            os.close(descriptor)

    def _validate_managed_runtime(self, runtime: ManagedRuntime) -> Path:
        """Validate an exact managed layout immediately before using its directory."""
        try:
            spec = RuntimeSpec(
                normalize_version(runtime.spec.version),
                normalize_architecture(runtime.spec.architecture),
                runtime.spec.sdk,
            )
        except RuntimeError as exc:
            raise ConfigurationError(
                _("invalid managed runtime specification: {specification}").format(
                    specification=runtime.spec
                )
            ) from exc
        if spec != runtime.spec:
            raise ConfigurationError(
                _("invalid managed runtime specification: {specification}").format(
                    specification=runtime.spec
                )
            )
        expected = self._paths.runtimes_root / f"linux-{spec.architecture}" / spec.directory_name
        if runtime.root.absolute() != expected.absolute():
            raise ConfigurationError(
                _("refusing to manage unexpected runtime path: {path}").format(path=runtime.root)
            )
        managed = self._paths.ensure_managed_runtime_path(expected)
        if not managed.is_dir() or managed.is_symlink():
            raise ConfigurationError(
                _("managed runtime directory is missing or unsafe: {path}").format(path=expected)
            )
        return managed


def _platform_architecture(directory: Path) -> str | None:
    """Return the architecture only for an exact managed platform directory name."""
    if not directory.name.startswith("linux-"):
        return None
    try:
        architecture = normalize_architecture(directory.name.removeprefix("linux-"))
    except RuntimeError:
        return None
    return architecture if directory.name == f"linux-{architecture}" else None


def _managed_runtime(directory: Path, architecture: str) -> ManagedRuntime | None:
    """Build a managed runtime only when the directory name is canonical."""
    prefix, separator, version = directory.name.partition("-")
    if separator != "-" or prefix not in {"sdk", "standard"}:
        return None
    try:
        spec = RuntimeSpec(normalize_version(version), architecture, prefix == "sdk")
        if directory.name != spec.directory_name:
            return None
        return ManagedRuntime(spec, directory)
    except RuntimeError:
        return None


def _open_runtime_platform(paths: AppPaths, architecture: str) -> int:
    """Open a managed runtime platform directory without following symlinks."""
    try:
        return paths.open_managed_cache_directory("runtimes", "nwjs", f"linux-{architecture}")
    except OSError as exc:
        raise ConfigurationError(
            _("cannot securely open managed runtime directory: {error}").format(error=exc)
        ) from exc
