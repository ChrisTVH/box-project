"""Selection policy for installed NW.js runtimes."""

from __future__ import annotations

from box.errors import RuntimeError
from box.models import RuntimeInfo
from box.runtime.catalog import RuntimeCatalog


def select_runtime(
    catalog: RuntimeCatalog,
    architecture: str,
    version: str | None,
    sdk: bool,
) -> RuntimeInfo:
    """Select an explicit runtime, or the latest matching installed runtime."""
    if version is not None:
        return catalog.get(version, architecture, sdk)
    candidates = [
        runtime
        for runtime in catalog.list()
        if runtime.spec.architecture == architecture and runtime.spec.sdk == sdk
    ]
    if not candidates:
        raise RuntimeError(
            "no matching NW.js runtime is installed; run 'box-rpg runtime nwjs available --interactive'"
        )
    return max(candidates, key=lambda runtime: _version_key(runtime.spec.version))


def _version_key(version: str) -> tuple[int, int, int]:
    """Return the numeric core used for deterministic version ordering."""
    numbers = version.removeprefix("v").split("-", maxsplit=1)[0].split(".")
    return int(numbers[0]), int(numbers[1]), int(numbers[2])
