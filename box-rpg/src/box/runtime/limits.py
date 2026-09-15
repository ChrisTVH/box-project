"""Explicit resource budgets for untrusted archive bodies and extraction."""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

from box.errors import RuntimeError

MAX_TRANSFER_BYTES = 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 50_000
MAX_DEPTH = 32
TOTAL_SECONDS = 15 * 60


class Budget:
    """One deadline and byte budget shared by all attempts of an operation."""

    def __init__(self, maximum: int) -> None:
        self.remaining = maximum
        self.deadline = time.monotonic() + TOTAL_SECONDS

    def check(self, amount: int = 0) -> float:
        self.remaining -= amount
        remaining_time = self.deadline - time.monotonic()
        if self.remaining < 0:
            raise RuntimeError("runtime resource byte limit exceeded")
        if remaining_time <= 0:
            raise RuntimeError("runtime operation deadline exceeded")
        return remaining_time


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _LimitedReader(io.RawIOBase):
    def __init__(self, source: _Readable, budget: Budget) -> None:
        self.source = source
        self.budget = budget

    def read(self, size: int = -1) -> bytes:
        self.budget.check()
        chunk = self.source.read(min(size if size >= 0 else 64 * 1024, 64 * 1024))
        self.budget.check(len(chunk))
        return chunk


def _members(archive: tarfile.TarFile, budget: Budget) -> Iterator[tarfile.TarInfo]:
    total = 0
    paths: set[Path] = set()
    for count, member in enumerate(archive, start=1):
        budget.check()
        total += member.size
        if (
            count > MAX_MEMBERS
            or member.size < 0
            or member.size > MAX_MEMBER_BYTES
            or total > MAX_UNPACKED_BYTES
            or len(Path(member.name).parts) > MAX_DEPTH
            or member.issparse()
            or member.islnk()
        ):
            raise RuntimeError("runtime archive member limit exceeded")
        path = Path(member.name)
        paths.update((path, *path.parents))
        if len(paths) > MAX_MEMBERS:
            raise RuntimeError("runtime archive member limit exceeded")
        yield member


def extract_bounded(
    source: BinaryIO, destination: Path, prepare: Callable[[Path], None] | None = None
) -> None:
    """Stream gzip and tar under quotas, retaining data filtering and error cleanup.

    Count decompressed tar bytes too: oversized PAX headers and padding must not
    evade member quotas. Sparse files and hardlinks are deliberately unsupported.
    """
    budget = Budget(MAX_UNPACKED_BYTES)
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=destination) as temporary:
        compressed_budget = Budget(MAX_TRANSFER_BYTES)
        compressed_budget.deadline = budget.deadline
        with gzip.GzipFile(
            fileobj=_LimitedReader(source, compressed_budget), mode="rb"
        ) as decompressed:
            reader = _LimitedReader(decompressed, budget)
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                archive.extractall(temporary, members=_members(archive, budget), filter="data")
            while reader.read(64 * 1024):
                pass
        budget.check()
        if prepare is not None:
            prepare(Path(temporary))
        budget.check()
        for entry in Path(temporary).iterdir():
            if (destination / entry.name).exists() or (destination / entry.name).is_symlink():
                raise RuntimeError("refusing to overwrite an extraction destination")
        for entry in Path(temporary).iterdir():
            os.replace(entry, destination / entry.name)
