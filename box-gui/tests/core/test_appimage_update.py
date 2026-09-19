"""Unit tests for AppImage self-update helpers (no GTK, no network)."""

from __future__ import annotations

import hashlib
import http.client
from pathlib import Path
from typing import Any

import pytest

import box_gui.core.appimage_update as appimage_module
from box_gui.core.appimage_update import (
    UpdatesError,
    download_and_verify,
    locate_self,
    replace_self,
    restart_into,
    should_offer_appimage_update,
    update_check_due,
)

_BINARY = b"fake-appimage-bytes-1234"
_APPIMAGE_URL = (
    "https://github.com/ChrisTVH/box-project/releases/download/26.9.44/box-rpg-maker.appimage"
)
_SHA256_URL = f"{_APPIMAGE_URL}.sha256"


def _hex_for(payload: bytes) -> str:
    """Return the sha256 hex for one payload."""
    return hashlib.sha256(payload).hexdigest()


class _FakeResponse:
    """Minimal urlopen response with headers and chunked reads."""

    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self._pos = 0
        self.headers = dict(headers or {})

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        """Return the next chunk, honouring an optional size."""
        if size < 0:
            size = len(self._payload) - self._pos
        chunk = self._payload[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def _stub_urlopen(
    monkeypatch: Any,
    bodies: dict[str, tuple[bytes, dict[str, str]]],
    seen: list[tuple[str, str | None, float | None]] | None = None,
) -> None:
    """Serve canned bodies keyed by URL, recording User-Agent and timeout."""

    def _fake_open(request: Any, timeout: Any = None) -> _FakeResponse:
        if isinstance(request, str):
            url = request
            agent: str | None = None
        else:
            url = request.full_url
            try:
                agent = request.get_header("User-agent") or request.get_header("User-Agent")
            except Exception:
                agent = None
            if agent is None:
                try:
                    agent = request.headers.get("User-agent") or request.headers.get("User-Agent")
                except Exception:
                    agent = None
        if seen is not None:
            seen.append((url, agent, timeout))
        if url not in bodies:
            raise OSError(f"unexpected URL {url}")
        payload, headers = bodies[url]
        return _FakeResponse(payload, headers)

    monkeypatch.setattr(appimage_module.urllib.request, "urlopen", _fake_open)


def test_locate_prefers_appimage_env(tmp_path: Path, monkeypatch: Any) -> None:
    """$APPIMAGE wins when it names a regular file."""
    target = tmp_path / "box.AppImage"
    target.write_bytes(b"x")
    other = tmp_path / "other.AppImage"
    other.write_bytes(b"y")
    monkeypatch.setenv("APPIMAGE", str(target))
    monkeypatch.setattr(appimage_module.os, "readlink", lambda _path: str(other))
    monkeypatch.setattr(appimage_module.sys, "argv", ["nope"])

    assert locate_self() == target


def test_locate_falls_back_to_proc_exe(tmp_path: Path, monkeypatch: Any) -> None:
    """A missing $APPIMAGE falls back to /proc/self/exe."""
    target = tmp_path / "proc.AppImage"
    target.write_bytes(b"x")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(appimage_module.os, "readlink", lambda _path: str(target))
    monkeypatch.setattr(appimage_module.sys, "argv", ["nope"])

    assert locate_self() == target


def test_locate_falls_back_to_argv(tmp_path: Path, monkeypatch: Any) -> None:
    """Without env or proc entries, sys.argv[0] locates the AppImage."""
    target = tmp_path / "argv.AppImage"
    target.write_bytes(b"x")
    monkeypatch.delenv("APPIMAGE", raising=False)

    def _boom(_path: str) -> str:
        raise OSError("no proc")

    monkeypatch.setattr(appimage_module.os, "readlink", _boom)
    monkeypatch.setattr(appimage_module.sys, "argv", [str(target)])

    assert locate_self() == target


def test_locate_resolves_relative_argv(tmp_path: Path, monkeypatch: Any) -> None:
    """A relative argv[0] resolves against the current directory."""
    target = tmp_path / "rel.AppImage"
    target.write_bytes(b"x")
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(
        appimage_module.os, "readlink", lambda _path: (_ for _ in ()).throw(OSError("no proc"))
    )
    monkeypatch.setattr(appimage_module.sys, "argv", ["rel.AppImage"])
    monkeypatch.chdir(tmp_path)

    assert locate_self() == tmp_path / "rel.AppImage"


def test_locate_ignores_directories(tmp_path: Path, monkeypatch: Any) -> None:
    """Directories never count; the next candidate wins."""
    directory = tmp_path / "dir.AppImage"
    directory.mkdir()
    target = tmp_path / "real.AppImage"
    target.write_bytes(b"x")
    monkeypatch.setenv("APPIMAGE", str(directory))
    monkeypatch.setattr(appimage_module.os, "readlink", lambda _path: str(target))
    monkeypatch.setattr(appimage_module.sys, "argv", ["nope"])

    assert locate_self() == target


def test_locate_raises_without_a_file(monkeypatch: Any) -> None:
    """No usable candidate raises UpdatesError (a ValueError)."""
    monkeypatch.setenv("APPIMAGE", "/no/such/app.AppImage")
    monkeypatch.setattr(
        appimage_module.os, "readlink", lambda _path: (_ for _ in ()).throw(OSError("no proc"))
    )
    monkeypatch.setattr(appimage_module.sys, "argv", ["/no/such/argv.AppImage"])

    with pytest.raises(UpdatesError):
        locate_self()
    with pytest.raises(ValueError):
        locate_self()


def test_download_happy_path_reports_progress(tmp_path: Path, monkeypatch: Any) -> None:
    """A matching hash returns the .part path with progress callbacks."""
    expected = _hex_for(_BINARY)
    bodies = {
        _SHA256_URL: (f"{expected}  box-rpg-maker.appimage\n".encode(), {}),
        _APPIMAGE_URL: (_BINARY, {"Content-Length": str(len(_BINARY))}),
    }
    seen: list[tuple[str, str | None, float | None]] = []
    _stub_urlopen(monkeypatch, bodies, seen)
    events: list[tuple[int, int | None]] = []

    result = download_and_verify(
        _APPIMAGE_URL,
        _SHA256_URL,
        tmp_path,
        progress=lambda completed, total: events.append((completed, total)),
        timeout=15.0,
    )

    assert result.name == "box-rpg-maker.appimage.part"
    assert result.read_bytes() == _BINARY
    assert events, "no progress reported"
    assert events[-1][0] == len(_BINARY)
    assert events[-1][1] == len(_BINARY)
    urls = [url for url, _agent, _timeout in seen]
    assert urls[0] == _SHA256_URL, "checksum must be fetched before the binary"
    assert urls[1] == _APPIMAGE_URL
    assert all(agent == "box-rpg-maker" for _url, agent, _timeout in seen)
    assert all(timeout == 15.0 for _url, _agent, timeout in seen)


def test_download_accepts_bare_hex(tmp_path: Path, monkeypatch: Any) -> None:
    """A checksum file with only the hex verifies as well."""
    expected = _hex_for(_BINARY)
    _stub_urlopen(
        monkeypatch,
        {
            _SHA256_URL: (f"{expected}\n".encode(), {}),
            _APPIMAGE_URL: (_BINARY, {}),
        },
    )

    result = download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)

    assert result.read_bytes() == _BINARY


def test_download_hash_mismatch_removes_part(tmp_path: Path, monkeypatch: Any) -> None:
    """A wrong digest fails closed and leaves no part file behind."""
    wrong = "0" * 64
    assert wrong != _hex_for(_BINARY)
    _stub_urlopen(
        monkeypatch,
        {
            _SHA256_URL: (f"{wrong}  box-rpg-maker.appimage\n".encode(), {}),
            _APPIMAGE_URL: (_BINARY, {"Content-Length": str(len(_BINARY))}),
        },
    )

    with pytest.raises(UpdatesError, match="checksum mismatch"):
        download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)

    assert list(tmp_path.glob("*.part")) == []


def test_download_length_mismatch_fails_closed(tmp_path: Path, monkeypatch: Any) -> None:
    """A short body against Content-Length raises before hashing."""
    expected = _hex_for(_BINARY)
    _stub_urlopen(
        monkeypatch,
        {
            _SHA256_URL: (f"{expected}\n".encode(), {}),
            _APPIMAGE_URL: (_BINARY, {"Content-Length": str(len(_BINARY) + 10)}),
        },
    )

    with pytest.raises(UpdatesError, match="incomplete download"):
        download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)


def test_download_rejects_bad_checksum_document(tmp_path: Path, monkeypatch: Any) -> None:
    """A non-hex checksum document raises without touching the network twice."""
    calls: list[str] = []

    def _fake_open(request: Any, timeout: Any = None) -> _FakeResponse:
        url = request if isinstance(request, str) else request.full_url
        calls.append(url)
        return _FakeResponse(b"not-a-checksum\n", {})

    monkeypatch.setattr(appimage_module.urllib.request, "urlopen", _fake_open)

    with pytest.raises(UpdatesError, match="invalid checksum"):
        download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)

    assert calls == [_SHA256_URL]


def test_download_wraps_network_errors(tmp_path: Path, monkeypatch: Any) -> None:
    """Transport failures surface as UpdatesError, never raw OSError."""

    def _boom(request: Any, timeout: Any = None) -> _FakeResponse:
        raise OSError("offline")

    monkeypatch.setattr(appimage_module.urllib.request, "urlopen", _boom)

    with pytest.raises(UpdatesError, match="cannot download"):
        download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)


def test_download_wraps_http_exceptions(tmp_path: Path, monkeypatch: Any) -> None:
    """HTTP layer failures surface as UpdatesError, never raw HTTPException."""

    def _boom(request: Any, timeout: Any = None) -> _FakeResponse:
        raise http.client.HTTPException("truncated response")

    monkeypatch.setattr(appimage_module.urllib.request, "urlopen", _boom)

    with pytest.raises(UpdatesError, match="cannot download"):
        download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)


def test_download_unlinks_stale_part_before_writing(tmp_path: Path, monkeypatch: Any) -> None:
    """A stale .part from a previous crash never survives the fresh download."""
    expected = _hex_for(_BINARY)
    _stub_urlopen(
        monkeypatch,
        {
            _SHA256_URL: (f"{expected}\n".encode(), {}),
            _APPIMAGE_URL: (_BINARY, {}),
        },
    )
    stale = tmp_path / "box-rpg-maker.appimage.part"
    stale.write_bytes(b"stale-bytes-from-crash")

    result = download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)

    assert result.read_bytes() == _BINARY


def test_download_replaces_symlinked_part(tmp_path: Path, monkeypatch: Any) -> None:
    """A planted .part symlink is unlinked so the download lands on a file."""
    expected = _hex_for(_BINARY)
    _stub_urlopen(
        monkeypatch,
        {
            _SHA256_URL: (f"{expected}\n".encode(), {}),
            _APPIMAGE_URL: (_BINARY, {}),
        },
    )
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do-not-touch")
    link = tmp_path / "box-rpg-maker.appimage.part"
    link.symlink_to(outside)

    result = download_and_verify(_APPIMAGE_URL, _SHA256_URL, tmp_path)

    assert result.read_bytes() == _BINARY
    assert not result.is_symlink()
    assert outside.read_bytes() == b"do-not-touch"


def test_download_rejects_missing_urls(tmp_path: Path) -> None:
    """Empty URLs fail fast without network access."""
    with pytest.raises(UpdatesError, match="missing"):
        download_and_verify("", _SHA256_URL, tmp_path)


def test_replace_same_dir_with_renamed_file(tmp_path: Path, monkeypatch: Any) -> None:
    """A renamed verified file replaces the running path and gains exec bits."""
    target = tmp_path / "box.AppImage"
    target.write_bytes(b"old")
    target.chmod(0o644)
    new_file = tmp_path / "box-rpg-maker.appimage.part"
    new_file.write_bytes(b"new-payload")
    new_file.chmod(0o644)
    monkeypatch.setattr(appimage_module, "locate_self", lambda: target)

    replace_self(new_file)

    assert target.read_bytes() == b"new-payload"
    assert not new_file.exists()
    mode = (tmp_path / "box.AppImage").stat().st_mode & 0o777
    assert mode == 0o755


def test_replace_same_name_in_place(tmp_path: Path, monkeypatch: Any) -> None:
    """Replacing onto the same basename works like a renamed file."""
    target = tmp_path / "same.AppImage"
    target.write_bytes(b"old")
    staged = tmp_path / "staged.part"
    staged.write_bytes(b"staged")
    monkeypatch.setattr(appimage_module, "locate_self", lambda: target)

    replace_self(staged)

    assert target.read_bytes() == b"staged"


def test_replace_refuses_other_directory(tmp_path: Path, monkeypatch: Any) -> None:
    """A new file outside the AppImage directory is refused."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    target = target_dir / "box.AppImage"
    target.write_bytes(b"old")
    other_dir = tmp_path / "downloads"
    other_dir.mkdir()
    new_file = other_dir / "box.part"
    new_file.write_bytes(b"new")
    monkeypatch.setattr(appimage_module, "locate_self", lambda: target)

    with pytest.raises(UpdatesError, match="same directory"):
        replace_self(new_file)

    assert target.read_bytes() == b"old"


def test_replace_refuses_unwritable_directory(tmp_path: Path, monkeypatch: Any) -> None:
    """A non-writable target directory fails closed before chmod."""
    target = tmp_path / "box.AppImage"
    target.write_bytes(b"old")
    new_file = tmp_path / "new.part"
    new_file.write_bytes(b"new")
    monkeypatch.setattr(appimage_module, "locate_self", lambda: target)
    monkeypatch.setattr(appimage_module.os, "access", lambda _path, _mode: False)

    with pytest.raises(UpdatesError, match="not writable"):
        replace_self(new_file)


def test_replace_refuses_missing_file(tmp_path: Path, monkeypatch: Any) -> None:
    """A missing staged file never touches the running AppImage."""
    target = tmp_path / "box.AppImage"
    target.write_bytes(b"old")
    monkeypatch.setattr(appimage_module, "locate_self", lambda: target)

    with pytest.raises(UpdatesError, match="not a regular file"):
        replace_self(tmp_path / "absent.part")


def test_replace_rejects_symlinked_staged_file(tmp_path: Path, monkeypatch: Any) -> None:
    """A symlinked staged file fails closed even when it points at a file."""
    target = tmp_path / "box.AppImage"
    target.write_bytes(b"old")
    real = tmp_path / "real.part"
    real.write_bytes(b"new")
    link = tmp_path / "link.part"
    link.symlink_to(real)
    monkeypatch.setattr(appimage_module, "locate_self", lambda: target)

    with pytest.raises(UpdatesError, match="symlink"):
        replace_self(link)

    assert target.read_bytes() == b"old"


def test_replace_rejects_symlinked_target(tmp_path: Path, monkeypatch: Any) -> None:
    """A symlinked running path fails closed instead of swapping the link."""
    real_target = tmp_path / "real.AppImage"
    real_target.write_bytes(b"old")
    link_target = tmp_path / "box.AppImage"
    link_target.symlink_to(real_target)
    new_file = tmp_path / "new.part"
    new_file.write_bytes(b"new")
    # locate_self resolves to the link path: replacing it would swap the
    # link itself, so the updater must refuse.
    monkeypatch.setattr(appimage_module, "locate_self", lambda: link_target)

    with pytest.raises(UpdatesError, match="symlink"):
        replace_self(new_file)

    assert real_target.read_bytes() == b"old"


def test_restart_preserves_cli_arguments(monkeypatch: Any) -> None:
    """restart_into re-execs the target with the original extra arguments."""
    calls: list[tuple[str, list[str]]] = []

    def _fake_execv(path: str, args: list[str]) -> None:
        calls.append((path, list(args)))
        raise OSError("should not return")

    monkeypatch.setattr(appimage_module.os, "execv", _fake_execv)
    monkeypatch.setattr(appimage_module.sys, "argv", ["box.AppImage", "--foo", "bar"])

    with pytest.raises(OSError):
        restart_into(Path("/tmp/fresh.AppImage"))

    assert len(calls) == 1
    path, args = calls[0]
    assert path == "/tmp/fresh.AppImage"
    assert args == ["/tmp/fresh.AppImage", "--foo", "bar"]


def test_update_check_due_single_appimage_domain() -> None:
    """Only the AppImage timestamp gates the check; off never does."""
    now = 1_000_000.0
    assert update_check_due(now, "off", None) is False
    assert update_check_due(now, "bogus", None) is False
    assert update_check_due(now, "daily", None) is True
    assert update_check_due(now, "daily", now) is False
    assert update_check_due(now, "daily", now - 86400.0) is True
    assert update_check_due(now, "weekly", now - 100.0) is False


def test_should_offer_wraps_should_prompt() -> None:
    """Newer prompts, equal/older/skipped never do, bad tags degrade."""
    assert should_offer_appimage_update("26.9.43", "26.9.44", None) is True
    assert should_offer_appimage_update("26.9.44", "26.9.44", None) is False
    assert should_offer_appimage_update("26.9.44", "26.9.43", None) is False
    assert should_offer_appimage_update("26.9.43", "26.9.44", "26.9.44") is False
    assert should_offer_appimage_update(None, "26.9.44", None) is True
    assert should_offer_appimage_update("26.9.43", "not-a-tag", None) is False
    assert should_offer_appimage_update("not-a-tag", "26.9.44", None) is False


def test_module_has_no_gtk_dependency() -> None:
    """The updater stays toolkit-free for worker-thread use."""
    source = Path(appimage_module.__file__).read_text(encoding="utf-8")
    assert "gi.repository" not in source
    assert "from gi" not in source
    assert "import Gtk" not in source
    assert "import Adw" not in source
