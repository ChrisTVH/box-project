"""Discovery and deletion of launcher-owned NW.js runtimes."""

from __future__ import annotations

import shutil
from pathlib import Path

from box.errors import ConfigurationError, RuntimeError
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.platform import normalize_architecture
from box.runtime.validator import normalize_version, runtime_executable


class RuntimeCatalog:
    """Inspect and remove only runtimes owned by the launcher cache."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def list(self) -> tuple[RuntimeInfo, ...]:
        """Return valid local runtimes ordered by platform and version directory."""
        self._paths.ensure()
        root = self._paths.runtimes_root
        runtimes: list[RuntimeInfo] = []
        for platform_directory in sorted(
            path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
        ):
            architecture = platform_directory.name.removeprefix("linux-")
            try:
                normalize_architecture(architecture)
            except RuntimeError:
                continue
            for runtime_directory in sorted(
                path
                for path in platform_directory.iterdir()
                if path.is_dir() and not path.is_symlink()
            ):
                try:
                    self._paths.ensure_managed_runtime_path(runtime_directory)
                except ConfigurationError:
                    continue
                info = _runtime_info(runtime_directory, architecture)
                if info is not None:
                    runtimes.append(info)
        return tuple(runtimes)

    def get(self, version: str, architecture: str, sdk: bool = False) -> RuntimeInfo:
        """Return a requested installed runtime or raise a clear error."""
        spec = RuntimeSpec(normalize_version(version), normalize_architecture(architecture), sdk)
        root = self._paths.runtimes_root / f"linux-{spec.architecture}" / spec.directory_name
        self._paths.ensure_managed_runtime_path(root)
        return RuntimeInfo(spec, root, runtime_executable(root))

    def remove(self, version: str, architecture: str, sdk: bool = False) -> None:
        """Delete one launcher-owned runtime after validating its managed path."""
        runtime = self.get(version, architecture, sdk)
        managed = self._paths.ensure_managed_runtime_path(runtime.root)
        shutil.rmtree(managed)


def _runtime_info(directory: Path, architecture: str) -> RuntimeInfo | None:
    prefix, separator, version = directory.name.partition("-")
    if separator != "-" or prefix not in {"sdk", "standard"}:
        return None
    try:
        spec = RuntimeSpec(normalize_version(version), architecture, prefix == "sdk")
        return RuntimeInfo(spec, directory, runtime_executable(directory))
    except RuntimeError:
        return None
