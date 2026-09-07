"""Official NW.js archive download and atomic runtime installation."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from box.errors import RuntimeError
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.archive import extract_runtime
from box.runtime.platform import normalize_architecture
from box.runtime.validator import normalize_version, runtime_executable


def download_url(spec: RuntimeSpec) -> str:
    """Return the official HTTPS archive URL for a requested runtime."""
    prefix = "nwjs-sdk" if spec.sdk else "nwjs"
    filename = f"{prefix}-{spec.version}-linux-{spec.architecture}.tar.gz"
    return f"https://dl.nwjs.io/{spec.version}/{filename}"


def install_runtime(
    paths: AppPaths, version: str, architecture: str, sdk: bool = False
) -> RuntimeInfo:
    """Download, extract and atomically install an official NW.js runtime."""
    spec = RuntimeSpec(normalize_version(version), normalize_architecture(architecture), sdk)
    paths.ensure()
    target = paths.runtimes_root / f"linux-{spec.architecture}" / spec.directory_name
    paths.ensure_managed_runtime_path(target)
    if target.exists():
        executable = runtime_executable(target)
        return RuntimeInfo(spec, target, executable)
    archive = paths.downloads_root / f"{spec.directory_name}-linux-{spec.architecture}.tar.gz"
    _download(download_url(spec), archive)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as temporary_name:
        temporary = Path(temporary_name)
        extracted_root = extract_runtime(archive, temporary)
        runtime_executable(extracted_root)
        os.replace(extracted_root, target)
    executable = runtime_executable(target)
    return RuntimeInfo(spec, target, executable)


def _download(url: str, destination: Path) -> None:
    """Stream an HTTPS archive to a private temporary file before replacement."""
    if destination.exists():
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with urlopen(url, timeout=30) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target)
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    except (OSError, URLError) as exc:
        raise RuntimeError(f"cannot download NW.js from {url}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()
