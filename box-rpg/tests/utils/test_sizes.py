"""Tests for base-10 file and directory size helpers."""

from __future__ import annotations

import os
from pathlib import Path

from box.utils.sizes import directory_size, file_size, format_size_decimal


def test_format_size_decimal_boundaries() -> None:
    assert format_size_decimal(0) == "0 B"
    assert format_size_decimal(999) == "999 B"
    assert format_size_decimal(1_000) == "1.0 kB"
    assert format_size_decimal(1_500_000) == "1.5 MB"
    assert format_size_decimal(2_500_000_000) == "2.5 GB"
    assert format_size_decimal(3_000_000_000_000) == "3.0 TB"


def test_file_size_returns_stat_size(tmp_path: Path) -> None:
    target = tmp_path / "archive.bin"
    target.write_bytes(b"x" * 2_048)

    assert file_size(target) == 2_048


def test_file_size_missing_returns_none(tmp_path: Path) -> None:
    assert file_size(tmp_path / "absent.bin") is None


def test_file_size_does_not_follow_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"x" * 1_024)
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    expected = os.lstat(link).st_size
    assert file_size(link) == expected


def test_directory_size_sums_nested_files(tmp_path: Path) -> None:
    root = tmp_path / "profile"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "first.bin").write_bytes(b"x" * 100)
    (nested / "second.bin").write_bytes(b"y" * 250)

    assert directory_size(root) == 350


def test_directory_size_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    assert directory_size(root) == 0


def test_directory_size_skips_symlinked_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "payload.bin").write_bytes(b"x" * 1_024)
    (root / "own.bin").write_bytes(b"y" * 10)
    (root / "linked").symlink_to(outside, target_is_directory=True)

    total = directory_size(root)
    assert total is not None
    assert total < 1_024
    assert total >= 10


def test_directory_size_missing_returns_none(tmp_path: Path) -> None:
    assert directory_size(tmp_path / "absent") is None
