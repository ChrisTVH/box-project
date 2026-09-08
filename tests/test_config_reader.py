import os
from pathlib import Path

import pytest

from box.config.reader import read_config
from box.errors import ConfigurationError


@pytest.mark.parametrize("kind", ["symlink", "dangling", "fifo", "directory", "writable"])
def test_reader_rejects_unsafe_config(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "config.toml"
    if kind in {"symlink", "dangling"}:
        target = tmp_path / "target"
        if kind == "symlink":
            target.write_text("", encoding="utf-8")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("", encoding="utf-8")
        path.chmod(0o666)
    with pytest.raises(ConfigurationError):
        read_config(path)


def test_reader_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "config.toml").write_text("", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ConfigurationError):
        read_config(alias / "config.toml")


def test_reader_checks_permissions_of_opened_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.write_text("", encoding="utf-8")
    replacement.chmod(0o666)
    real_open = os.open

    def racing_open(
        name: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if name == "config.toml":
            replacement.replace(path)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(ConfigurationError, match="permissions"):
        read_config(path)


def test_reader_rejects_oversized_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with path.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)
    with pytest.raises(ConfigurationError, match="byte limit"):
        read_config(path)


def test_reader_checks_open_file_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    other_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: other_uid)
    with pytest.raises(ConfigurationError, match="ownership"):
        read_config(path)


def test_reader_limits_growth_after_fstat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    real_fstat = os.fstat

    def growing_fstat(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        with path.open("wb") as stream:
            stream.truncate(1024 * 1024 + 1)
        return metadata

    monkeypatch.setattr(os, "fstat", growing_fstat)
    with pytest.raises(ConfigurationError, match="byte limit"):
        read_config(path)
