from io import StringIO
from pathlib import Path
from urllib.request import Request

import pytest

from box.errors import RuntimeError
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


def test_download_retries_and_resumes_a_reset_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    requests: list[Request] = []
    updates: list[tuple[int, int | None]] = []
    responses = iter(
        (
            FakeResponse(200, {"Content-Length": "4"}, [b"ab", ConnectionResetError("reset")]),
            FakeResponse(206, {"Content-Length": "2", "Content-Range": "bytes 2-3/4"}, [b"cd"]),
        )
    )

    def open_request(request: Request, timeout: int) -> FakeResponse:
        assert timeout == 60
        requests.append(request)
        return next(responses)

    def no_sleep(_: int) -> None:
        return None

    monkeypatch.setattr("box.runtime.downloader.urlopen", open_request)
    monkeypatch.setattr("box.runtime.downloader.time.sleep", no_sleep)

    download_archive(
        "https://dl.nwjs.io/v0.90.0/runtime.tar.gz",
        destination,
        progress=lambda completed, total: updates.append((completed, total)),
    )

    assert destination.read_bytes() == b"abcd"
    assert not destination.with_suffix(".gz.part").exists()
    assert requests[1].get_header("Range") == "bytes=2-"
    assert updates == [(0, 4), (2, 4), (2, 4), (4, 4)]


def test_download_restarts_when_a_server_ignores_a_resume_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    partial = destination.with_suffix(".gz.part")
    partial.write_bytes(b"old")
    response = FakeResponse(200, {"Content-Length": "5"}, [b"fresh"])

    def ignored_range_response(_: Request, timeout: int) -> FakeResponse:
        assert timeout == 60
        return response

    monkeypatch.setattr("box.runtime.downloader.urlopen", ignored_range_response)

    download_archive("https://dl.nwjs.io/v0.90.0/runtime.tar.gz", destination)

    assert destination.read_bytes() == b"fresh"


def test_download_renders_a_progress_bar_for_an_interactive_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    response = FakeResponse(200, {"Content-Length": "2"}, [b"ok"])
    terminal = FakeTerminal()

    def open_request(_: Request, timeout: int) -> FakeResponse:
        assert timeout == 60
        return response

    monkeypatch.setattr("box.runtime.downloader.urlopen", open_request)
    monkeypatch.setattr("box.runtime.downloader.sys.stderr", terminal)

    download_archive("https://dl.nwjs.io/v0.90.0/runtime.tar.gz", destination)

    assert "Downloading NW.js: [##############################] 100%\n" in terminal.getvalue()


def test_download_keeps_partial_archive_after_exhausting_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "runtime.tar.gz"
    partial = destination.with_suffix(".gz.part")
    partial.write_bytes(b"partial")

    def reset_connection(_: Request, timeout: int) -> FakeResponse:
        assert timeout == 60
        raise ConnectionResetError("reset")

    def no_sleep(_: int) -> None:
        return None

    monkeypatch.setattr("box.runtime.downloader.urlopen", reset_connection)
    monkeypatch.setattr("box.runtime.downloader.time.sleep", no_sleep)

    with pytest.raises(RuntimeError, match="after 4 attempts"):
        download_archive("https://dl.nwjs.io/v0.90.0/runtime.tar.gz", destination)

    assert partial.read_bytes() == b"partial"
