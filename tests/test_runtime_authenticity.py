# pyright: reportPrivateUsage=false
import hashlib
import subprocess
import tempfile
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from box.errors import RuntimeError
from box.paths import AppPaths
from box.runtime import authenticity, downloader

FILENAME = "nwjs-v0.90.0-linux-x64.tar.gz"
URL = "https://dl.nwjs.io/v0.90.0/" + FILENAME
ARCHIVE = b"offline archive fixture"
MANIFEST = f"{hashlib.sha256(ARCHIVE).hexdigest()}  {FILENAME}\n".encode()


@pytest.fixture(scope="module")
def signed_fixture() -> Iterator[tuple[Path, str, bytes]]:
    # Secret test material exists only in a temporary directory, never the repo.
    with tempfile.TemporaryDirectory(prefix="box-test-gpg-", dir="/tmp") as directory:
        root = Path(directory)
        command = [
            "/usr/bin/gpg",
            "--no-options",
            "--homedir",
            directory,
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
        ]

        def gpg(*arguments: str, data: bytes | None = None) -> bytes:
            return subprocess.run(
                command + list(arguments), input=data, capture_output=True, check=True, timeout=30
            ).stdout

        gpg(
            "--quick-generate-key",
            "Offline Test <offline@example.invalid>",
            "ed25519",
            "cert",
            "1d",
        )
        listing = gpg("--with-colons", "--list-keys").decode()
        fingerprint = next(
            line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
        )
        gpg("--quick-add-key", fingerprint, "ed25519", "sign", "1d")
        (root / "keys").mkdir()
        (root / "keys/nwjs.asc").write_bytes(gpg("--armor", "--export"))
        signature = gpg("--detach-sign", "--digest-algo", "SHA256", data=MANIFEST)
        yield root, fingerprint, signature


@pytest.fixture
def trusted_fixture(
    signed_fixture: tuple[Path, str, bytes], monkeypatch: pytest.MonkeyPatch
) -> bytes:
    root, fingerprint, signature = signed_fixture

    def resources(package: str) -> Path:
        return root

    monkeypatch.setattr(authenticity, "files", resources)
    monkeypatch.setattr(authenticity, "PRIMARY_FINGERPRINT", fingerprint)
    return signature


def test_real_signature_and_subkey_binding(trusted_fixture: bytes) -> None:
    authenticity.verify_manifest(MANIFEST, trusted_fixture)


def test_real_signature_rejects_modified_manifest(trusted_fixture: bytes) -> None:
    with pytest.raises(RuntimeError, match="GPG verification failed"):
        authenticity.verify_manifest(MANIFEST + b"tampered", trusted_fixture)


def test_real_signature_rejects_invalid_signature(trusted_fixture: bytes) -> None:
    with pytest.raises(RuntimeError, match="GPG verification failed"):
        authenticity.verify_manifest(MANIFEST, trusted_fixture[:20])


def test_incorrect_primary_key(
    signed_fixture: tuple[Path, str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, signature = signed_fixture

    def resources(package: str) -> Path:
        return root

    monkeypatch.setattr(authenticity, "files", resources)
    with pytest.raises(RuntimeError, match="incorrect"):
        authenticity.verify_manifest(MANIFEST, signature)


def test_bundled_key_does_not_accept_test_signer(signed_fixture: tuple[Path, str, bytes]) -> None:
    with pytest.raises(RuntimeError, match="GPG verification failed"):
        authenticity.verify_manifest(MANIFEST, signed_fixture[2])


def test_bundled_key_pins_expected_primary_fingerprint(tmp_path: Path) -> None:
    """Pin the shipped public key offline: exactly one key, expected fingerprint."""
    if not Path("/usr/bin/gpg").is_file():
        pytest.skip("Bundled key pinning needs /usr/bin/gpg")
    from importlib.resources import files

    key = files("box.runtime").joinpath("keys/nwjs.asc").read_bytes()
    home = tmp_path / "keyring"
    home.mkdir()
    subprocess.run(
        ["/usr/bin/gpg", "--no-options", "--homedir", str(home), "--batch", "--import"],
        input=key,
        capture_output=True,
        check=True,
        timeout=30,
    )
    listing = subprocess.run(
        ["/usr/bin/gpg", "--no-options", "--homedir", str(home), "--with-colons", "--list-keys"],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.decode("ascii")
    rows = [line.split(":") for line in listing.splitlines()]
    primaries = [index for index, row in enumerate(rows) if row[0] == "pub"]
    assert len(primaries) == 1
    index = primaries[0]
    assert rows[index][1] not in {"r", "e", "d", "i"}
    assert rows[index + 1][0] == "fpr"
    assert rows[index + 1][9] == authenticity.PRIMARY_FINGERPRINT


def _metadata(monkeypatch: pytest.MonkeyPatch, signature: bytes) -> list[str]:
    calls: list[str] = []

    def fetch(url: str, maximum: int, *, missing_ok: bool = False) -> bytes:
        calls.append(url)
        return signature if url.endswith(".asc") else MANIFEST

    monkeypatch.setattr(authenticity, "_fetch", fetch)
    return calls


def test_archive_and_modified_cache_are_hashed_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted_fixture: bytes
) -> None:
    calls = _metadata(monkeypatch, trusted_fixture)
    archive = tmp_path / "cache.tar.gz"
    archive.write_bytes(ARCHIVE)
    with archive.open("rb") as source:
        authenticity.verify_archive(source.fileno(), URL)
        assert source.tell() == 0
        authenticity.verify_archive(source.fileno(), URL)
        archive.write_bytes(b"modified cache")
        with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
            authenticity.verify_archive(source.fileno(), URL)
    assert len(calls) == 6


@pytest.mark.parametrize("cached", [False, True])
def test_install_rejects_hash_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted_fixture: bytes, cached: bool
) -> None:
    _metadata(monkeypatch, trusted_fixture)
    paths = AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    paths.ensure()
    archive = paths.downloads_root / "standard-v0.90.0-linux-x64.tar.gz"
    if cached:
        archive.write_bytes(b"modified cache")
    else:

        def download(url: str, name: str, descriptor: int) -> None:
            archive.write_bytes(b"new but incorrect archive")

        monkeypatch.setattr(downloader, "download_archive_at", download)

    def extract(source: int, destination: int) -> str:
        pytest.fail("unverified archive reached extraction")

    monkeypatch.setattr(downloader, "extract_runtime_at", extract)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        downloader.install_runtime(paths, "0.90.0", "x64")
    assert not (paths.runtimes_root / "linux-x64/standard-v0.90.0").exists()


def test_signature_404_warns_without_fetching_manifest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def missing(request: Request, timeout: float, allowed_hosts: frozenset[str]) -> None:
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 404, "missing", Message(), None)

    monkeypatch.setattr(authenticity, "open_official", missing)
    authenticity.verify_archive(-1, URL)
    assert calls == [URL.rsplit("/", 1)[0] + "/SHASUMS256.txt.asc"]
    assert "NOT VERIFIED" in capsys.readouterr().err


@pytest.mark.parametrize("code", [403, 429, 500, 503])
def test_signature_http_failure_never_downgrades(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    def failed(request: Request, timeout: float, allowed_hosts: frozenset[str]) -> None:
        raise HTTPError(request.full_url, code, "failed", Message(), None)

    monkeypatch.setattr(authenticity, "open_official", failed)
    with pytest.raises(RuntimeError, match="metadata"):
        authenticity.verify_archive(-1, URL)
    assert not capsys.readouterr().err


def test_network_failure_never_downgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(request: Request, timeout: float, allowed_hosts: frozenset[str]) -> None:
        raise URLError("offline")

    monkeypatch.setattr(authenticity, "open_official", failed)
    with pytest.raises(RuntimeError, match="metadata"):
        authenticity.verify_archive(-1, URL)


def test_manifest_404_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(request: Request, timeout: float, allowed_hosts: frozenset[str]) -> None:
        raise HTTPError(request.full_url, 404, "missing", Message(), None)

    monkeypatch.setattr(authenticity, "open_official", failed)
    with pytest.raises(RuntimeError, match="metadata"):
        authenticity._fetch(URL.rsplit("/", 1)[0] + "/SHASUMS256.txt", 100)


@pytest.mark.parametrize(
    "manifest", [b"", MANIFEST + MANIFEST, MANIFEST.replace(FILENAME.encode(), b"other.tar.gz")]
)
def test_checksum_requires_unique_exact_filename(manifest: bytes) -> None:
    with pytest.raises(RuntimeError, match="exactly one"):
        authenticity._checksum(manifest, FILENAME)


@pytest.mark.parametrize(
    "failure", ["BADSIG", "EXPKEYSIG", "REVKEYSIG", "EXPSIG", "KEYEXPIRED", "NO_PUBKEY"]
)
def test_status_rejects_bad_expired_revoked_signatures(failure: str) -> None:
    status = f"[GNUPG:] VALIDSIG {'A' * 40} 2026-01-01 1 0 4 0 1 10 00 {authenticity.PRIMARY_FINGERPRINT}\n[GNUPG:] {failure} anything\n"
    with pytest.raises(RuntimeError, match="invalid, expired or revoked"):
        authenticity._validate_status(status.encode())


def test_status_pins_primary_not_subkey() -> None:
    status = f"[GNUPG:] VALIDSIG {authenticity.PRIMARY_FINGERPRINT} 2026-01-01 1 0 4 0 1 10 00 {'A' * 40}\n"
    with pytest.raises(RuntimeError, match="not authorized"):
        authenticity._validate_status(status.encode())


def test_signature_byte_limit() -> None:
    with pytest.raises(RuntimeError, match="byte limit"):
        authenticity.verify_manifest(MANIFEST, b"x" * (authenticity.MAX_SIGNATURE_BYTES + 1))


class MetadataResponse:
    status = 200

    def __init__(self, data: bytes, headers: dict[str, str], url: str = URL) -> None:
        self.data = data
        self.headers = headers
        self.url = url

    def __enter__(self) -> MetadataResponse:
        return self

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        pass

    def geturl(self) -> str:
        return self.url

    def read(self, amount: int) -> bytes:
        return self.data[:amount]


@pytest.mark.parametrize(
    ("data", "headers", "url"),
    [
        (b"12345", {}, URL),
        (b"x", {"Content-Length": "5"}, URL),
        (b"x", {"Content-Length": "-1"}, URL),
        (b"x", {"Content-Length": "broken"}, URL),
        (b"x", {"Content-Length": "2"}, URL),
        (b"x", {"Content-Encoding": "gzip"}, URL),
        (b"x", {}, "https://untrusted.example/manifest"),
    ],
)
def test_metadata_rejects_oversize_truncation_encoding_and_redirects(
    monkeypatch: pytest.MonkeyPatch, data: bytes, headers: dict[str, str], url: str
) -> None:
    def response(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> MetadataResponse:
        assert allowed_hosts == authenticity.OFFICIAL_HOSTS
        return MetadataResponse(data, headers, url)

    monkeypatch.setattr(authenticity, "open_official", response)
    with pytest.raises(RuntimeError):
        authenticity._fetch(URL, 4)


def test_present_signature_missing_manifest_never_downgrades(
    monkeypatch: pytest.MonkeyPatch, trusted_fixture: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    def response(
        request: Request, timeout: float, allowed_hosts: frozenset[str]
    ) -> MetadataResponse:
        if request.full_url.endswith(".asc"):
            return MetadataResponse(trusted_fixture, {})
        raise HTTPError(request.full_url, 404, "missing", Message(), None)

    monkeypatch.setattr(authenticity, "open_official", response)
    with pytest.raises(RuntimeError, match="metadata"):
        authenticity.verify_archive(-1, URL)
    assert not capsys.readouterr().err


def test_user_gpg_configuration_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted_fixture: bytes
) -> None:
    (tmp_path / "gpg.conf").write_text("invalid-option-that-must-not-be-read\n")
    monkeypatch.setenv("GNUPGHOME", str(tmp_path))
    authenticity.verify_manifest(MANIFEST, trusted_fixture)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["gpg.conf"]


def test_missing_gpg_is_fatal(monkeypatch: pytest.MonkeyPatch, trusted_fixture: bytes) -> None:
    def unavailable(home: str, *arguments: str, data: bytes | None = None) -> bytes:
        raise FileNotFoundError("gpg unavailable")

    monkeypatch.setattr(authenticity, "_gpg", unavailable)
    with pytest.raises(RuntimeError, match="cannot authenticate"):
        authenticity.verify_manifest(MANIFEST, trusted_fixture)
