import os
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from urllib.request import Request

import pytest

from box.errors import RuntimeError
from box.runtime import limits
from box.runtime.downloader import download_archive, validate_download_source


class FakeResponse:
    def __init__(
        self, status: int, headers: dict[str, str], chunks: list[bytes | BaseException]
    ) -> None:
        self.status = status
        self.headers = headers
        self._chunks = iter(chunks)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        return None

    def geturl(self) -> str:
        return "https://dl.nwjs.io/v0.90.0/runtime.tar.gz"

    def read(self, _: int) -> bytes:
        chunk = next(self._chunks, b"")
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


class FakeTerminal(StringIO):
    def isatty(self) -> bool:
        return True


def test_validate_download_source_accepts_official_https_mirrors() -> None:
    validate_download_source("https://dl.nwjs.io/v0.90.0/runtime.tar.gz")
    validate_download_source("https://dl.node-webkit.org/v0.90.0/runtime.tar.gz")


@pytest.mark.parametrize(
    "url",
    (
        "http://dl.nwjs.io/v0.90.0/runtime.tar.gz",
        "https://downloads.example.test/v0.90.0/runtime.tar.gz",
    ),
)
def test_validate_download_source_rejects_non_official_urls(url: str) -> None:
    with pytest.raises(RuntimeError, match="official HTTPS mirror"):
        validate_download_source(url)


def test_download_retries_with_a_fresh_representation_after_a_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    requests: list[Request] = []
    updates: list[tuple[int, int | None]] = []
    responses = iter(
        (
            FakeResponse(200, {"Content-Length": "4"}, [b"ab", ConnectionResetError("reset")]),
            FakeResponse(200, {"Content-Length": "4"}, [b"NEW!"]),
        )
    )

    def open_request(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        assert timeout == 60
        requests.append(request)
        return next(responses)

    def no_sleep(_: int) -> None:
        return None

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    monkeypatch.setattr("box.runtime.downloader.time.sleep", no_sleep)

    download_archive(
        "https://dl.nwjs.io/v0.90.0/runtime.tar.gz",
        destination,
        progress=lambda completed, total: updates.append((completed, total)),
    )

    assert destination.read_bytes() == b"NEW!"
    assert not destination.with_suffix(".gz.part").exists()
    assert requests[1].get_header("Range") is None
    assert updates == [(0, 4), (2, 4), (0, 4), (4, 4)]


def test_download_restarts_when_a_server_ignores_a_resume_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    partial = destination.with_suffix(".gz.part")
    partial.write_bytes(b"old")
    response = FakeResponse(200, {"Content-Length": "5"}, [b"fresh"])

    def ignored_range_response(
        _: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        assert timeout == 60
        return response

    monkeypatch.setattr("box.runtime.downloader.open_official", ignored_range_response)

    download_archive("https://dl.nwjs.io/v0.90.0/runtime.tar.gz", destination)

    assert destination.read_bytes() == b"fresh"


def test_download_renders_a_progress_bar_for_an_interactive_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    response = FakeResponse(200, {"Content-Length": "2"}, [b"ok"])
    terminal = FakeTerminal()

    def open_request(_: Request, timeout: float, allowed_hosts: frozenset[str]) -> FakeResponse:
        assert timeout == 60
        return response

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    monkeypatch.setattr("box.runtime.downloader.sys.stderr", terminal)

    download_archive("https://dl.nwjs.io/v0.90.0/runtime.tar.gz", destination)

    assert "Downloading NW.js: [##############################] 100%\n" in terminal.getvalue()


def test_download_keeps_partial_archive_after_exhausting_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    partial = destination.with_suffix(".gz.part")
    partial.write_bytes(b"partial")

    def reset_connection(_: Request, timeout: float, allowed_hosts: frozenset[str]) -> FakeResponse:
        assert timeout == 60
        raise ConnectionResetError("reset")

    def no_sleep(_: int) -> None:
        return None

    monkeypatch.setattr("box.runtime.downloader.open_official", reset_connection)
    monkeypatch.setattr("box.runtime.downloader.time.sleep", no_sleep)

    with pytest.raises(RuntimeError, match="after 4 attempts"):
        download_archive("https://dl.nwjs.io/v0.90.0/runtime.tar.gz", destination)

    assert partial.read_bytes() == b"partial"


def test_download_rejects_a_destination_symlink_without_touching_its_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"outside")
    destination = tmp_path / "runtime.tar.gz"
    destination.symlink_to(outside)

    with pytest.raises(RuntimeError, match="refusing unsafe download path"):
        download_archive("https://dl.nwjs.io/v0.90.0/runtime.tar.gz", destination)

    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("name", ("runtime.tar.gz", "runtime.tar.gz.part"))
def test_download_rejects_hardlinks_before_any_write(tmp_path: Path, name: str) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not truncate")
    os.link(outside, tmp_path / name)
    with pytest.raises(RuntimeError, match="unsafe download"):
        download_archive("https://dl.nwjs.io/archive", tmp_path / "runtime.tar.gz")
    assert outside.read_bytes() == b"do not truncate"


def test_partial_replaced_with_hardlink_after_precheck_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not truncate")
    partial = tmp_path / "runtime.tar.gz.part"

    def open_request(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        os.link(outside, partial)
        return FakeResponse(200, {}, [b"bad"])

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    with pytest.raises(RuntimeError, match="unsafe download"):
        download_archive("https://dl.nwjs.io/archive", tmp_path / "runtime.tar.gz")
    assert outside.read_bytes() == b"do not truncate"


@pytest.mark.parametrize("headers", ({}, {"Content-Length": "9"}))
def test_transfer_quota_rejects_declared_and_streamed_excess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    monkeypatch.setattr(limits, "MAX_TRANSFER_BYTES", 4)
    response = FakeResponse(200, headers, [b"1234", b"5"])

    def open_request(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        return response

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    destination = tmp_path / "runtime.tar.gz"
    with pytest.raises(RuntimeError, match="byte limit"):
        download_archive("https://dl.nwjs.io/archive", destination)
    assert not destination.exists()
    partial = tmp_path / "runtime.tar.gz.part"
    assert not partial.exists() or partial.stat().st_size <= 4


def test_transfer_budget_is_shared_across_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_TRANSFER_BYTES", 5)
    monkeypatch.setattr("box.runtime.downloader.DOWNLOAD_RETRY_DELAYS", (0,))
    responses = iter(
        (FakeResponse(200, {}, [b"1234", ConnectionResetError()]), FakeResponse(200, {}, [b"ab"]))
    )

    def open_request(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        return next(responses)

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    with pytest.raises(RuntimeError, match="byte limit"):
        download_archive("https://dl.nwjs.io/archive", tmp_path / "runtime.tar.gz")
    assert not (tmp_path / "runtime.tar.gz").exists()


def test_transfer_deadline_is_checked_after_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(limits.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(limits, "TOTAL_SECONDS", 1)

    class SlowResponse(FakeResponse):
        def read(self, _: int) -> bytes:
            now[0] = 2.0
            return b"late"

    def open_request(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        assert timeout <= 1
        return SlowResponse(200, {}, [])

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    with pytest.raises(RuntimeError, match="deadline"):
        download_archive("https://dl.nwjs.io/archive", tmp_path / "runtime.tar.gz")
    assert (tmp_path / "runtime.tar.gz.part").read_bytes() == b""


def test_two_downloaders_cannot_write_the_same_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    requests: list[str] = []

    def open_request(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        requests.append(request.full_url)
        return FakeResponse(200, {}, [b"one body"])

    def progress(completed: int, total: int | None) -> None:
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            pytest.raises(RuntimeError, match="busy"),
        ):
            executor.submit(download_archive, "https://dl.nwjs.io/archive", destination).result(
                timeout=5
            )

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    download_archive("https://dl.nwjs.io/archive", destination, progress)
    assert requests == ["https://dl.nwjs.io/archive"]
    assert destination.read_bytes() == b"one body"


def test_partial_descriptor_owner_is_checked_before_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = tmp_path / "runtime.tar.gz.part"
    partial.write_bytes(b"preserved")
    inode = partial.stat().st_ino
    fstat = os.fstat

    def wrong_owner(descriptor: int) -> os.stat_result:
        entry = fstat(descriptor)
        if entry.st_ino != inode:
            return entry
        fields = list(entry)
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)

    def open_request(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> FakeResponse:
        return FakeResponse(200, {}, [b"bad"])

    monkeypatch.setattr("box.runtime.downloader.open_official", open_request)
    monkeypatch.setattr("box.runtime.security.os.fstat", wrong_owner)
    with pytest.raises(RuntimeError, match="unsafe download"):
        download_archive("https://dl.nwjs.io/archive", tmp_path / "runtime.tar.gz")
    assert partial.read_bytes() == b"preserved"
