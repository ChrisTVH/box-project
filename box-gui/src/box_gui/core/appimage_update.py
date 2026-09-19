"""AppImage self-update helpers: locate, download, verify, replace, restart.

Toolkit-free: nothing here imports Gtk/Adw. Callers run the download on a
worker thread and marshal progress back to the main loop. The running
AppImage is replaced atomically with ``os.replace`` and the process is
re-executed so the fresh payload owns the backend gate on next startup.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from box_gui.core.updates import UpdatesError, is_due, should_prompt

__all__ = [
    "download_and_verify",
    "locate_self",
    "replace_self",
    "restart_into",
    "should_offer_appimage_update",
    "update_check_due",
]

_USER_AGENT = "box-rpg-maker"
_CHUNK_SIZE = 65536
_DEFAULT_FILENAME = "box-rpg-maker.appimage"
_DEFAULT_TIMEOUT_S = 60.0


def locate_self() -> Path:
    """Return the running AppImage path, preferring $APPIMAGE.

    Order: ``$APPIMAGE`` when it names a regular file, then
    ``readlink /proc/self/exe``, then ``sys.argv[0]`` (resolved against
    the current directory when relative). Anything else raises
    UpdatesError: outside an AppImage there is nothing to replace.
    """
    candidates: list[Path] = []
    try:
        env_value = os.environ.get("APPIMAGE", "").strip()
    except Exception:
        env_value = ""
    if env_value:
        candidates.append(Path(env_value))
    try:
        exe_target = os.readlink("/proc/self/exe")
    except OSError:
        exe_target = ""
    if exe_target:
        candidates.append(Path(exe_target))
    try:
        argv_zero = sys.argv[0] if sys.argv else ""
    except Exception:
        argv_zero = ""
    if argv_zero:
        argv_path = Path(argv_zero)
        try:
            if not argv_path.is_absolute():
                argv_path = Path.cwd() / argv_path
        except OSError:
            pass
        candidates.append(argv_path)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    raise UpdatesError(
        "cannot locate the running AppImage (no $APPIMAGE, /proc/self/exe, or argv file)"
    )


def _request(url: str) -> urllib.request.Request:
    """Build a download request with the stable User-Agent."""
    return urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})


def _fetch_text(url: str, timeout: float) -> str:
    """Fetch a small text document (the .sha256 sidecar)."""
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response:
            data = response.read()
    except OSError as exc:
        raise UpdatesError(f"cannot download {url}: {exc}") from exc
    except ValueError as exc:
        raise UpdatesError(f"cannot download {url}: {exc}") from exc
    except http.client.HTTPException as exc:
        raise UpdatesError(f"cannot download {url}: {exc}") from exc
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdatesError(f"cannot decode {url}: {exc}") from exc
    return text


def _parse_expected_hex(text: str, source_url: str) -> str:
    """Return the hex digest from a .sha256 document (hash or hash + name)."""
    token = text.strip().split()[0] if text.strip() else ""
    digest = token.strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise UpdatesError(f"invalid checksum document from {source_url}")
    return digest


def _content_length(response: object) -> int | None:
    """Best-effort Content-Length from a urlopen response, if present."""
    try:
        headers = getattr(response, "headers", None)
        if headers is not None:
            getter = getattr(headers, "get", None)
            if callable(getter):
                try:
                    value = getter("Content-Length")
                except Exception:
                    value = None
                if value is not None:
                    return int(str(value).strip())
        getheader = getattr(response, "getheader", None)
        if callable(getheader):
            try:
                value = getheader("Content-Length")
            except Exception:
                value = None
            if value is not None:
                return int(str(value).strip())
        info = getattr(response, "info", None)
        if callable(info):
            try:
                message = info()
            except Exception:
                message = None
            getter = getattr(message, "get", None) if message is not None else None
            if callable(getter):
                try:
                    value = getter("Content-Length")
                except Exception:
                    value = None
                if value is not None:
                    return int(str(value).strip())
    except ValueError, TypeError:
        return None
    except Exception:
        return None
    return None


def _basename_from_url(url: str) -> str:
    """Return the URL basename, defaulting to the AppImage file name."""
    try:
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].strip()
    except Exception:
        name = ""
    return name or _DEFAULT_FILENAME


def download_and_verify(
    appimage_url: str,
    sha256_url: str,
    dest_dir: Path,
    *,
    progress: Callable[[int, int | None], None] | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> Path:
    """Download an AppImage plus its .sha256 sidecar and verify the hash.

    The expected hex is fetched first so a corrupt binary never reaches
    disk under its final name. The binary streams to ``<name>.part`` with
    ``progress(received, total)`` callbacks, then the received byte count
    is checked against Content-Length when the server sent one, and the
    sha256 hex is compared BEFORE any chmod. Returns the verified ``.part``
    path for ``replace_self``; the caller decides when to swap it in.
    Network, length, and hash failures raise UpdatesError.
    """
    if not appimage_url or not sha256_url:
        raise UpdatesError("missing AppImage or checksum URL")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdatesError(f"cannot create download directory {dest_dir}: {exc}") from exc
    expected_hex = _parse_expected_hex(_fetch_text(sha256_url, timeout), sha256_url)
    filename = _basename_from_url(appimage_url)
    part_path = dest_dir / f"{filename}.part"
    # Best-effort unlink of a pre-existing .part so a stale file or a
    # planted symlink never survives under the fresh download.
    _unlink_best_effort(part_path)
    digest = hashlib.sha256()
    received = 0
    total: int | None = None
    try:
        with urllib.request.urlopen(_request(appimage_url), timeout=timeout) as response:
            total = _content_length(response)
            with open(part_path, "wb") as target:
                while True:
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    chunk = bytes(chunk)
                    target.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress is not None:
                        with contextlib.suppress(Exception):
                            progress(received, total)
                with contextlib.suppress(OSError):
                    target.flush()
                    os.fsync(target.fileno())
    except UpdatesError:
        raise
    except OSError as exc:
        _unlink_best_effort(part_path)
        raise UpdatesError(f"cannot download {appimage_url}: {exc}") from exc
    except ValueError as exc:
        _unlink_best_effort(part_path)
        raise UpdatesError(f"cannot download {appimage_url}: {exc}") from exc
    except http.client.HTTPException as exc:
        _unlink_best_effort(part_path)
        raise UpdatesError(f"cannot download {appimage_url}: {exc}") from exc
    if total is not None and received != total:
        _unlink_best_effort(part_path)
        raise UpdatesError(
            f"incomplete download for {appimage_url}: got {received} of {total} bytes"
        )
    actual_hex = digest.hexdigest().lower()
    if actual_hex != expected_hex:
        _unlink_best_effort(part_path)
        raise UpdatesError(
            f"checksum mismatch for {appimage_url}: expected {expected_hex}, got {actual_hex}"
        )
    return part_path


def _unlink_best_effort(path: Path) -> None:
    """Remove a partial download without masking the original failure."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def replace_self(new_file: Path) -> None:
    """Atomically replace the running AppImage with a verified download.

    The new file must already live in the same directory as the running
    AppImage (same filesystem by construction); anything else is refused
    as cross-filesystem. The target directory must be writable. Steps:
    chmod 0o755 the new file, fsync it, ``os.replace`` onto the running
    path, then fsync the directory. Failures raise UpdatesError.
    """
    try:
        is_file = new_file.is_file()
    except OSError as exc:
        raise UpdatesError(f"cannot replace AppImage with {new_file}: {exc}") from exc
    if not is_file:
        raise UpdatesError(f"cannot replace AppImage with {new_file}: not a regular file")
    # Lstat-reject a symlinked staged file: is_symlink does not follow, so a
    # symlink to a regular file still fails closed instead of swapping a link.
    try:
        if new_file.is_symlink():
            raise UpdatesError(f"cannot replace AppImage with {new_file}: staged file is a symlink")
    except OSError as exc:
        raise UpdatesError(f"cannot replace AppImage with {new_file}: {exc}") from exc
    target = locate_self()
    # Lstat-reject a symlinked running path: replacing a link would swap the
    # link itself instead of the AppImage bytes.
    try:
        if target.is_symlink():
            raise UpdatesError(f"cannot replace symlinked AppImage path {target}")
    except OSError as exc:
        raise UpdatesError(f"cannot replace {target} with {new_file}: {exc}") from exc
    try:
        target_dir = target.parent
        new_dir = new_file.parent
        try:
            same_dir = new_dir.resolve() == target_dir.resolve()
        except OSError:
            same_dir = new_dir.absolute() == target_dir.absolute()
        if not same_dir:
            raise UpdatesError(
                f"cannot replace {target} with {new_file}: not in the same directory"
            )
        if not os.access(target_dir, os.W_OK | os.X_OK):
            raise UpdatesError(f"cannot replace {target}: directory {target_dir} is not writable")
        try:
            if os.stat(new_file).st_dev != os.stat(target_dir).st_dev:
                raise UpdatesError(
                    f"cannot replace {target} with {new_file}: cross-filesystem move refused"
                )
        except OSError as exc:
            raise UpdatesError(f"cannot replace {target} with {new_file}: {exc}") from exc
    except UpdatesError:
        raise
    except OSError as exc:
        raise UpdatesError(f"cannot replace {target} with {new_file}: {exc}") from exc
    try:
        os.chmod(new_file, 0o755)
    except OSError as exc:
        raise UpdatesError(f"cannot chmod {new_file}: {exc}") from exc
    try:
        with open(new_file, "rb") as handle:
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                raise UpdatesError(f"cannot fsync {new_file}: {exc}") from exc
        os.replace(new_file, target)
    except OSError as exc:
        raise UpdatesError(f"cannot replace {target} with {new_file}: {exc}") from exc
    try:
        descriptor = os.open(target_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restart_into(path: Path | str) -> NoReturn:
    """Re-execute the (fresh) AppImage, preserving CLI arguments."""
    target = str(path)
    os.execv(target, [target, *sys.argv[1:]])


def update_check_due(
    now: float,
    interval: str,
    last_appimage_at: float | None,
) -> bool:
    """Return True when the AppImage update check is due for the cadence.

    Unknown intervals behave as off (never due) through ``is_due``. The
    backend has no independent release channel (its expectation derives
    from the embedded AppImage tag), so only the AppImage timestamp gates
    the check.
    """
    return is_due(now, interval, last_appimage_at)


def should_offer_appimage_update(current: str | None, latest: str, skipped: str | None) -> bool:
    """Return True when the AppImage update prompt should be shown.

    Thin ``should_prompt`` wrapper that degrades to False on invalid
    tags instead of crashing startup.
    """
    try:
        return should_prompt(latest, current, skipped)
    except ValueError:
        return False
