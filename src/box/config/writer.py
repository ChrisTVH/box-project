"""Atomic TOML configuration writing."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from box.config.models import AppConfig
from box.config.validator import encode_config


def write_config(path: Path, config: AppConfig) -> None:
    """Write configuration atomically with user-only file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = encode_config(config)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
