"""HTTPS requests whose initial URL and redirects are checked before contact."""

from __future__ import annotations

from collections.abc import Callable
from http.client import HTTPResponse
from io import BytesIO
from typing import Any, cast
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from box.errors import RuntimeError
from box.runtime import limits
from box.runtime.limits import Budget


def validate_source(url: str, allowed_hosts: frozenset[str]) -> None:
    """Allow only credential-free HTTPS on the official hosts' standard port."""
    try:
        destination = urlsplit(url)
        valid = (
            destination.scheme == "https"
            and destination.hostname in allowed_hosts
            and destination.port in {None, 443}
            and destination.username is None
            and destination.password is None
            and not any(ord(character) <= 32 for character in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("archive redirected outside an official HTTPS mirror or host")


class _OfficialRedirects(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str], budget: Budget) -> None:
        self.allowed_hosts = allowed_hosts
        self.budget = budget

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        validate_source(newurl, self.allowed_hosts)
        self.budget.check()
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req: Request, fp: Any, code: int, msg: str, headers: Any) -> Any:
        # urllib normally drains redirect bodies without a byte limit. Close them
        # instead; redirect loop detection and URL resolution remain in urllib.
        fp.close()
        location = headers.get("Location", headers.get("URI"))
        if location is not None:
            validate_source(urljoin(req.full_url, location), self.allowed_hosts)
        remaining = self.budget.check()
        req.timeout = min(req.timeout if req.timeout is not None else remaining, remaining)
        return super().http_error_302(req, BytesIO(), code, msg, headers)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


class OfficialResponse:
    """Check elapsed time between single buffered reads, including index bodies."""

    def __init__(self, response: HTTPResponse, budget: Budget) -> None:
        self.response = response
        self.budget = budget
        self.status = response.status
        self.headers = response.headers

    def __enter__(self) -> OfficialResponse:
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.response.close()

    def geturl(self) -> str:
        return self.response.url

    def read1(self, amount: int) -> bytes:
        self.budget.check()
        read = cast(Callable[[int], bytes], getattr(self.response, "read1", self.response.read))
        chunk = read(min(amount, 64 * 1024, self.budget.remaining + 1))
        self.budget.check(len(chunk))
        return chunk

    def read(self, amount: int = -1) -> bytes:
        maximum = self.budget.remaining + 1 if amount < 0 else amount
        chunks: list[bytes] = []
        received = 0
        while received < maximum:
            chunk = self.read1(maximum - received)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)


def open_official(
    request: Request, timeout: float, allowed_hosts: frozenset[str]
) -> OfficialResponse:
    """Build a dedicated opener; global urllib openers cannot bypass the policy."""
    validate_source(request.full_url, allowed_hosts)
    budget = Budget(limits.MAX_TRANSFER_BYTES)
    response = cast(
        HTTPResponse,
        build_opener(_OfficialRedirects(allowed_hosts, budget)).open(
            request, timeout=min(timeout, budget.check())
        ),
    )
    try:
        budget.check()
        return OfficialResponse(response, budget)
    except Exception:
        response.close()
        raise
