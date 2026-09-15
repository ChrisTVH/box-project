from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.request import BaseHandler, OpenerDirector, ProxyHandler, Request, build_opener
from urllib.response import addinfourl

import pytest

from box.errors import RuntimeError
from box.models import RuntimeSpec
from box.runtime import available, downloader, easyrpg
from box.runtime.http import open_official

HOSTS = frozenset({"dl.nwjs.io", "dl.node-webkit.org"})
INITIAL = "https://dl.nwjs.io/start"
FORBIDDEN = (
    "https://evil.test/archive",
    "http://dl.nwjs.io/archive",
    "https://dl.nwjs.io:444/archive",
    "https://user:password@dl.nwjs.io/archive",
    "https://dl.nwjs.io:invalid/archive",
    "file:///etc/passwd",
)


class TransportResponse(addinfourl):
    msg = "test response"


class UnreadableRedirectBody(BytesIO):
    def read(self, size: int | None = -1, /) -> bytes:
        raise AssertionError("redirect bodies must be closed, not drained")


class RecordingTransport(BaseHandler):
    handler_order = 100

    def __init__(self, redirects: dict[str, str]) -> None:
        self.redirects = redirects
        self.visited: list[str] = []

    def https_open(self, req: Request) -> addinfourl:
        self.visited.append(req.full_url)
        headers = Message()
        destination = self.redirects.get(req.full_url)
        if destination:
            headers["Location"] = destination
        body = UnreadableRedirectBody() if destination else BytesIO(b"ok")
        return TransportResponse(body, headers, req.full_url, 302 if destination else 200)


def install_transport(monkeypatch: pytest.MonkeyPatch, transport: RecordingTransport) -> None:
    def build(*handlers: BaseHandler) -> OpenerDirector:
        return build_opener(ProxyHandler({}), transport, *handlers)

    monkeypatch.setattr("box.runtime.http.build_opener", build)


@pytest.mark.parametrize("url", FORBIDDEN)
def test_initial_url_is_rejected_without_contact(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = RecordingTransport({})
    install_transport(monkeypatch, transport)
    with pytest.raises(RuntimeError):
        open_official(Request(url), 1, HOSTS)
    assert transport.visited == []


@pytest.mark.parametrize("url", FORBIDDEN)
def test_each_redirect_is_rejected_before_contact(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = "https://dl.node-webkit.org/second"
    transport = RecordingTransport({INITIAL: mirror, mirror: url})
    install_transport(monkeypatch, transport)
    with pytest.raises(RuntimeError):
        open_official(Request(INITIAL), 1, HOSTS)
    assert transport.visited == [INITIAL, mirror]


def test_relative_and_standard_port_redirects_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = RecordingTransport(
        {INITIAL: "/second", "https://dl.nwjs.io/second": "https://dl.node-webkit.org:443/end"}
    )
    install_transport(monkeypatch, transport)
    with open_official(Request(INITIAL), 1, HOSTS) as response:
        assert response.read() == b"ok"
    assert transport.visited == [
        INITIAL,
        "https://dl.nwjs.io/second",
        "https://dl.node-webkit.org:443/end",
    ]


@pytest.mark.parametrize(
    "consumer", ("nw-index", "nw-probe", "easy-index", "nw-download", "easy-download")
)
def test_all_http_consumers_use_precontact_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: str
) -> None:
    initial = easyrpg.VERSIONS_INDEX if consumer.startswith("easy") else available.VERSIONS_INDEX
    if consumer == "nw-probe":
        initial = downloader.download_url(RuntimeSpec("v0.90.0", "x64", False))
    transport = RecordingTransport({initial: "https://evil.test/never"})
    install_transport(monkeypatch, transport)
    with pytest.raises(RuntimeError, match="official HTTPS"):
        if consumer == "nw-index":
            available.fetch_available_versions(1, "x64", False)
        elif consumer == "nw-probe":
            available.runtime_archive_available.cache_clear()
            available.runtime_archive_available(RuntimeSpec("v0.90.0", "x64", False))
        elif consumer == "easy-index":
            easyrpg.fetch_available_versions(1)
        else:
            hosts = easyrpg.OFFICIAL_DOWNLOAD_HOSTS if consumer == "easy-download" else HOSTS
            downloader.download_archive(initial, tmp_path / "runtime.tar.gz", allowed_hosts=hosts)
    assert transport.visited == [initial]
