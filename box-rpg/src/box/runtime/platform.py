"""NW.js platform normalization."""

from __future__ import annotations

import platform

from box.errors import RuntimeError
from box.utils.i18n import _

_ARCHITECTURES = {
    "x86_64": "x64",
    "amd64": "x64",
    "i386": "ia32",
    "i686": "ia32",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
}
_SUPPORTED_ARCHITECTURES = frozenset(_ARCHITECTURES.values())


def current_architecture() -> str:
    """Return the NW.js architecture corresponding to the current machine."""
    machine = platform.machine().lower()
    try:
        return _ARCHITECTURES[machine]
    except KeyError as exc:
        raise RuntimeError(
            _("unsupported CPU architecture for NW.js: {architecture}").format(architecture=machine)
        ) from exc


def normalize_architecture(value: str) -> str:
    """Validate an NW.js archive architecture identifier."""
    architecture = value.lower()
    if architecture not in _SUPPORTED_ARCHITECTURES:
        supported = ", ".join(sorted(_SUPPORTED_ARCHITECTURES))
        raise RuntimeError(
            _(
                "unsupported NW.js architecture {architecture!r}; expected one of: {supported}"
            ).format(architecture=value, supported=supported)
        )
    return architecture
