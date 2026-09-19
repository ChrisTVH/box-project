import json
import time
from pathlib import Path
from typing import ClassVar
from urllib.error import URLError

import pytest

from box.errors import RuntimeError
from box.models import RuntimeSpec
from box.paths import AppPaths
from box.runtime import available
from box.runtime.available import AvailableVersions, available_url, parse_versions


def test_available_url_uses_pages_of_ten_versions() -> None:
    assert available_url(2) == "https://dl.nwjs.io/"


def test_available_url_rejects_invalid_pages() -> None:
    with pytest.raises(RuntimeError, match="at least 1"):
        available_url(0)


def test_parse_versions_filters_unstable_and_invalid_directories() -> None:
    content = """
    <a href="v0.90.0/">v0.90.0/</a>
    <a href="v0.91.0-beta.1/">v0.91.0-beta.1/</a>
    <a href="v0.89.0/">v0.89.0/</a>
    <a href="not-a-version/">not-a-version/</a>
    """

    assert parse_versions(content) == ("v0.90.0", "v0.89.0")


def test_parse_versions_ignores_an_index_without_version_directories() -> None:
    assert parse_versions("<html><body>empty</body></html>") == ()


def test_interactive_selection_installs_the_chosen_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    installed: list[tuple[str, str, bool]] = []
    output: list[str] = []
    choices = iter(("2", "yes"))

    def fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        assert architecture == "x64"
        assert not sdk
        return AvailableVersions(page=page, versions=("v0.90.0", "v0.89.0"))

    def install_version(_: AppPaths, version: str, architecture: str, sdk: bool) -> int:
        installed.append((version, architecture, sdk))
        return 0

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", fetch)
    monkeypatch.setattr("box.cli.runtime.install", install_version)

    def _fake_read(prompt: str) -> str:
        return next(choices)

    result = runtime.select_interactively(
        paths,
        1,
        "x64",
        False,
        read=_fake_read,
        write=output.append,
    )

    assert result == 0
    assert installed == [("v0.89.0", "x64", False)]
    assert any("page 1, x64, standard" in line for line in output)


def test_interactive_selection_uses_ten_item_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    pages: list[int] = []
    choices = iter(("n", "q"))

    def fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        assert architecture == "x64"
        assert not sdk
        pages.append(page)
        return AvailableVersions(page=page, versions=("v0.90.0",))

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", fetch)

    def _fake_read(prompt: str) -> str:
        return next(choices)

    assert runtime.select_interactively(paths, 1, "x64", False, read=_fake_read) == 0
    assert pages == [1, 2]


def test_interactive_selection_handles_end_of_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    output: list[str] = []

    def fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        assert architecture == "x64"
        assert not sdk
        return AvailableVersions(page=page, versions=("v0.90.0",))

    def end_of_input(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", fetch)

    assert (
        runtime.select_interactively(paths, 1, "x64", False, read=end_of_input, write=output.append)
        == 0
    )
    assert output[-1] == "Selection cancelled."


def test_interactive_selection_retries_a_failed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    output: list[str] = []
    installed: list[str] = []
    attempts = iter((False, True))
    choices = iter(("r", "1", "yes"))

    def flaky_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        if next(attempts):
            return AvailableVersions(page=page, versions=("v0.90.0",))
        raise RuntimeError("connection reset")

    def install_version(_: AppPaths, version: str, architecture: str, sdk: bool) -> int:
        installed.append(version)
        return 0

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", flaky_fetch)
    monkeypatch.setattr("box.cli.runtime.install", install_version)

    def _fake_read(prompt: str) -> str:
        return next(choices)

    result = runtime.select_interactively(
        paths, 1, "x64", False, read=_fake_read, write=output.append
    )

    assert result == 0
    assert installed == ["v0.90.0"]
    assert any("Could not load the version list" in line for line in output)


def test_interactive_selection_quits_after_a_failed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    output: list[str] = []
    choices = iter(("q",))

    def failing_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        raise RuntimeError("connection reset")

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", failing_fetch)

    def _fake_read(prompt: str) -> str:
        return next(choices)

    result = runtime.select_interactively(
        paths, 1, "x64", False, read=_fake_read, write=output.append
    )

    assert result == 0
    assert output[-1] == "Selection cancelled."


def test_interactive_selection_handles_end_of_input_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from box.cli import runtime

    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    output: list[str] = []

    def failing_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        raise RuntimeError("connection reset")

    def end_of_input(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("box.cli.runtime.fetch_available_versions", failing_fetch)

    assert (
        runtime.select_interactively(paths, 1, "x64", False, read=end_of_input, write=output.append)
        == 0
    )
    assert output[-1] == "Selection cancelled."


class _ProbeResponse:
    """Minimal Range-probe response with configurable headers and status."""

    def __init__(
        self,
        headers: dict[str, str],
        status: int = 206,
        url: str = "https://dl.nwjs.io/v0.90.0/nwjs-v0.90.0-linux-x64.tar.gz",
    ) -> None:
        self.headers = headers
        self.status = status
        self._url = url
        self.read_calls = 0

    def __enter__(self) -> _ProbeResponse:
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int = -1) -> bytes:
        self.read_calls += 1
        if amount < 0:
            return b"x"
        return b"x"[:amount]


class _IndexResponse:
    """Minimal index response for version-listing fetches."""

    headers: ClassVar[dict[str, str]] = {}
    status: ClassVar[int] = 200

    def __init__(self, content: bytes, url: str = "https://dl.nwjs.io/") -> None:
        self._content = content
        self._url = url

    def __enter__(self) -> _IndexResponse:
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return self._content
        return self._content[:amount]


def _stub_probe(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], status: int = 206
) -> _ProbeResponse:
    response = _ProbeResponse(headers, status=status)

    def _fake_open(request: object) -> _ProbeResponse:
        return response

    monkeypatch.setattr(available, "_open_official", _fake_open)
    available.runtime_archive_available.cache_clear()
    return response


def _stub_index(monkeypatch: pytest.MonkeyPatch, html: str) -> list[str]:
    visited: list[str] = []
    payload = html.encode("utf-8")

    def fake_open(request: object) -> _IndexResponse:
        visited.append(getattr(request, "full_url", ""))
        return _IndexResponse(payload)

    monkeypatch.setattr(available, "_open_official", fake_open)
    return visited


def test_available_versions_sizes_default_to_empty() -> None:
    assert AvailableVersions(page=1, versions=()).sizes == {}
    assert AvailableVersions(page=1, versions=("v0.90.0",)).sizes == {}
    assert AvailableVersions(page=2, versions=("v0.90.0",), sizes={"v0.90.0": 10}).sizes == {
        "v0.90.0": 10
    }


def test_probe_prefers_content_range_over_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_probe(monkeypatch, {"Content-Range": "bytes 0-0/12345", "Content-Length": "1"})
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.0", "x64", False))
    assert (exists, total) == (True, 12345)


def test_probe_returns_none_for_206_without_content_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 206 response without Content-Range never falls back to Content-Length."""
    _stub_probe(monkeypatch, {"Content-Length": "6789"})
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.1", "x64", False))
    assert (exists, total) == (True, None)


def test_probe_returns_none_for_non_zero_start_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only bytes 0-0/N counts as a valid 206 size, other ranges are ignored."""
    _stub_probe(monkeypatch, {"Content-Range": "bytes 1-1/12345"})
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.6", "x64", False))
    assert (exists, total) == (True, None)


def test_probe_ignores_content_range_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response uses Content-Length even when Content-Range is present."""
    _stub_probe(
        monkeypatch,
        {"Content-Range": "bytes 0-0/999", "Content-Length": "777"},
        status=200,
    )
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.7", "x64", False))
    assert (exists, total) == (True, 777)


def test_probe_returns_none_for_200_with_only_content_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response without Content-Length reports an unknown size."""
    _stub_probe(monkeypatch, {"Content-Range": "bytes 0-0/999"}, status=200)
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.8", "x64", False))
    assert (exists, total) == (True, None)


def test_probe_returns_missing_on_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect validation failures surface as a missing archive, not an abort."""

    def _bad_redirect(request: object) -> _ProbeResponse:
        raise RuntimeError("redirect to untrusted host")

    monkeypatch.setattr(available, "_open_official", _bad_redirect)
    available.runtime_archive_available.cache_clear()
    assert available.runtime_archive_available(RuntimeSpec("v0.90.9", "x64", False)) == (
        False,
        None,
    )


def test_probe_returns_none_when_size_headers_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, {})
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.2", "x64", False))
    assert (exists, total) == (True, None)


def test_probe_returns_none_for_malformed_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, {"Content-Range": "bytes 0-0/*", "Content-Length": "oops"})
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.3", "x64", False))
    assert (exists, total) == (True, None)


def test_probe_handles_range_ignored_200_with_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_probe(monkeypatch, {"Content-Length": "9999"}, status=200)
    exists, total = available.runtime_archive_available(RuntimeSpec("v0.90.4", "x64", False))
    assert (exists, total) == (True, 9999)


def test_probe_reports_missing_archive_without_size(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(request: object) -> _ProbeResponse:
        raise URLError("not found")

    monkeypatch.setattr(available, "_open_official", missing)
    available.runtime_archive_available.cache_clear()
    assert available.runtime_archive_available(RuntimeSpec("v0.90.5", "x64", False)) == (
        False,
        None,
    )


def test_fetch_includes_per_page_sizes_without_network_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = '<a href="v0.90.0/">v0.90.0/</a><a href="v0.89.0/">v0.89.0/</a>'
    _stub_index(monkeypatch, html)

    def _fake_archive_available(spec: RuntimeSpec) -> tuple[bool, int | None]:
        return (True, 111 if spec.version == "v0.90.0" else None)

    monkeypatch.setattr(
        available,
        "runtime_archive_available",
        _fake_archive_available,
    )
    result = available.fetch_available_versions(1, "x64", False)
    assert result.versions == ("v0.90.0", "v0.89.0")
    assert result.sizes == {"v0.90.0": 111, "v0.89.0": None}


def test_fetch_skips_bad_version_without_aborting_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One RuntimeError probe is treated as missing instead of failing the page."""
    html = '<a href="v0.90.0/">v0.90.0/</a><a href="v0.89.0/">v0.89.0/</a>'
    payload = html.encode("utf-8")
    available.runtime_archive_available.cache_clear()

    def _fake_open(request: object) -> _ProbeResponse | _IndexResponse:
        url = getattr(request, "full_url", "")
        if url == available.VERSIONS_INDEX:
            return _IndexResponse(payload)
        if "v0.90.0" in url:
            raise RuntimeError("redirect to untrusted host")
        return _ProbeResponse({"Content-Range": "bytes 0-0/42"}, status=206)

    monkeypatch.setattr(available, "_open_official", _fake_open)
    try:
        result = available.fetch_available_versions(1, "x64", False)
    finally:
        available.runtime_archive_available.cache_clear()
    assert result.versions == ("v0.89.0",)
    assert result.sizes == {"v0.89.0": 42}


def test_is_fresh_rejects_far_future_timestamp() -> None:
    """Far-future fetched_at is stale, small clock skew stays fresh."""
    from box.runtime import listings

    now = 1_700_000_000.0
    assert listings.is_fresh(now, now) is True
    assert listings.is_fresh(now + 60.0, now) is True
    assert listings.is_fresh(now + listings.CLOCK_SKEW_SECONDS, now) is True
    assert listings.is_fresh(now + listings.CLOCK_SKEW_SECONDS + 1.0, now) is False
    assert listings.is_fresh(now + 7200.0, now) is False
    assert listings.is_fresh(now - listings.LISTINGS_TTL_SECONDS, now) is True
    assert listings.is_fresh(now - listings.LISTINGS_TTL_SECONDS - 1.0, now) is False


def _prime_nwjs_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, html: str, sizes: dict[str, int | None]
) -> AppPaths:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    _stub_index(monkeypatch, html)

    def _fake_archive_available(spec: RuntimeSpec) -> tuple[bool, int | None]:
        return (True, sizes.get(spec.version))

    monkeypatch.setattr(available, "runtime_archive_available", _fake_archive_available)
    result = available.fetch_available_versions(1, "x64", False, paths=paths)
    assert result.versions
    return paths


def test_nwjs_cache_round_trip_and_fresh_avoids_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prime_nwjs_cache(
        tmp_path,
        monkeypatch,
        '<a href="v0.90.0/">v0.90.0/</a><a href="v0.89.0/">v0.89.0/</a>',
        {"v0.90.0": 100, "v0.89.0": 200},
    )
    target = paths.listings_root / "nwjs" / "nwjs-x64-standard-p1.json"
    assert target.is_file()
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["source"] == "nwjs"
    assert document["arch"] == "x64"
    assert document["flavor"] == "standard"
    assert document["page"] == 1
    assert document["versions"] == ["v0.90.0", "v0.89.0"]
    assert document["sizes"] == {"v0.90.0": 100, "v0.89.0": 200}
    assert (target.stat().st_mode & 0o777) == 0o600

    def forbidden(request: object) -> _IndexResponse:
        raise AssertionError("fresh cache must not use the network")

    monkeypatch.setattr(available, "_open_official", forbidden)
    cached = available.fetch_available_versions(1, "x64", False, paths=paths)
    assert cached.versions == ("v0.90.0", "v0.89.0")
    assert cached.sizes == {"v0.90.0": 100, "v0.89.0": 200}


def test_nwjs_cache_without_paths_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    _stub_index(monkeypatch, '<a href="v0.90.0/">v0.90.0/</a><a href="v0.89.0/">v0.89.0/</a>')

    def _fake_archive_available(spec: RuntimeSpec) -> tuple[bool, int | None]:
        return (True, 42)

    monkeypatch.setattr(available, "runtime_archive_available", _fake_archive_available)
    result = available.fetch_available_versions(1, "x64", False)
    assert result.sizes == {"v0.90.0": 42, "v0.89.0": 42}
    assert not (paths.listings_root / "nwjs" / "nwjs-x64-standard-p1.json").exists()


def test_nwjs_cache_expired_entry_triggers_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prime_nwjs_cache(
        tmp_path,
        monkeypatch,
        '<a href="v0.90.0/">v0.90.0/</a>',
        {"v0.90.0": 10},
    )
    target = paths.listings_root / "nwjs" / "nwjs-x64-standard-p1.json"
    document = json.loads(target.read_text(encoding="utf-8"))
    document["fetched_at"] = time.time() - 7200
    target.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    _stub_index(monkeypatch, '<a href="v0.89.0/">v0.89.0/</a>')

    def _fake_archive_available(spec: RuntimeSpec) -> tuple[bool, int | None]:
        return (True, 55)

    monkeypatch.setattr(available, "runtime_archive_available", _fake_archive_available)
    refreshed = available.fetch_available_versions(1, "x64", False, paths=paths)
    assert refreshed.versions == ("v0.89.0",)
    assert refreshed.sizes == {"v0.89.0": 55}


def test_nwjs_stale_cache_returned_when_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prime_nwjs_cache(
        tmp_path, monkeypatch, '<a href="v0.90.0/">v0.90.0/</a>', {"v0.90.0": 77}
    )
    target = paths.listings_root / "nwjs" / "nwjs-x64-standard-p1.json"
    document = json.loads(target.read_text(encoding="utf-8"))
    document["fetched_at"] = time.time() - 7200
    target.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

    def offline(request: object) -> _IndexResponse:
        raise URLError("offline")

    monkeypatch.setattr(available, "_open_official", offline)
    stale = available.fetch_available_versions(1, "x64", False, paths=paths)
    assert stale.versions == ("v0.90.0",)
    assert stale.sizes == {"v0.90.0": 77}


def test_nwjs_corrupt_cache_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    target = paths.listings_root / "nwjs" / "nwjs-x64-standard-p1.json"
    target.write_text("{not-json", encoding="utf-8")
    _stub_index(monkeypatch, '<a href="v0.90.0/">v0.90.0/</a>')

    def _fake_archive_available(spec: RuntimeSpec) -> tuple[bool, int | None]:
        return (True, 5)

    monkeypatch.setattr(available, "runtime_archive_available", _fake_archive_available)
    result = available.fetch_available_versions(1, "x64", False, paths=paths)
    assert result.versions == ("v0.90.0",)
    assert result.sizes == {"v0.90.0": 5}
