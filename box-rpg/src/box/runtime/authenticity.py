"""Authenticate NW.js archives against the upstream signed checksum manifest.

Only a signature HTTP 404 permits an explicitly unverified HTTPS installation.
Already installed trees are not reauthenticated; no local checksum metadata is trusted.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from http.client import IncompleteRead
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from box.errors import RuntimeError
from box.runtime.http import open_official, validate_source
from box.runtime.limits import MAX_TRANSFER_BYTES, Budget
from box.utils.i18n import _

PRIMARY_FINGERPRINT = "1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A"
OFFICIAL_HOSTS = frozenset({"dl.nwjs.io", "dl.node-webkit.org"})
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
GPG_TIMEOUT_SECONDS = 30


def verify_archive(archive_descriptor: int, archive_url: str) -> None:
    """Verify the same pinned tar descriptor used by extraction, including cache hits."""
    validate_source(archive_url, OFFICIAL_HOSTS)
    base, filename = archive_url.rsplit("/", 1)
    signature = _fetch(base + "/SHASUMS256.txt.asc", MAX_SIGNATURE_BYTES, missing_ok=True)
    if signature is None:
        sys.stderr.write(
            _(
                "Warning: NW.js {url} is NOT VERIFIED: official signature returned HTTP 404; relying on HTTPS only."
            ).format(url=archive_url)
            + "\n"
        )
        return
    manifest = _fetch(base + "/SHASUMS256.txt", MAX_MANIFEST_BYTES)
    if manifest is None:
        raise RuntimeError("NW.js signed checksum manifest is missing")
    verify_manifest(manifest, signature)
    expected = _checksum(manifest, filename)
    digest = hashlib.sha256()
    budget = Budget(MAX_TRANSFER_BYTES)
    offset = 0
    while True:
        budget.check()
        chunk = os.pread(archive_descriptor, min(64 * 1024, budget.remaining + 1), offset)
        budget.check(len(chunk))
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError("NW.js archive SHA-256 mismatch; refusing extraction")


def _fetch(url: str, maximum: int, *, missing_ok: bool = False) -> bytes | None:
    """Fetch bounded identity bytes; never treat a transport error as missing signature."""
    try:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; box-rpg)",
                "Accept-Encoding": "identity",
            },
        )
        with open_official(request, timeout=30, allowed_hosts=OFFICIAL_HOSTS) as response:
            validate_source(response.geturl(), OFFICIAL_HOSTS)
            if response.status == 404 and missing_ok:
                return None
            if response.status != 200:
                raise RuntimeError("NW.js authentication metadata returned unexpected HTTP status")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise RuntimeError("unexpected NW.js authentication metadata encoding")
            length = response.headers.get("Content-Length")
            if length is not None and not 0 <= int(length) <= maximum:
                raise RuntimeError("NW.js authentication metadata byte limit exceeded")
            data = response.read(maximum + 1)
            if len(data) > maximum:
                raise RuntimeError("NW.js authentication metadata byte limit exceeded")
            if length is not None and len(data) != int(length):
                raise RuntimeError("incomplete NW.js authentication metadata")
            return data
    except HTTPError as exc:
        exc.close()
        validate_source(exc.url, OFFICIAL_HOSTS)
        if exc.code == 404 and missing_ok:
            return None
        raise RuntimeError("cannot download NW.js authentication metadata") from exc
    except (OSError, URLError, IncompleteRead, ValueError) as exc:
        raise RuntimeError("cannot download NW.js authentication metadata") from exc


def _checksum(manifest: bytes, filename: str) -> str:
    matches: list[str] = []
    for line in manifest.splitlines():
        match = re.fullmatch(rb"([0-9a-fA-F]{64}) [ *](.+)", line)
        if match is not None and match[2] == filename.encode("ascii"):
            matches.append(match[1].decode("ascii").lower())
    if len(matches) != 1:
        raise RuntimeError("NW.js signed manifest must contain exactly one archive checksum")
    return matches[0]


def _gpg(home: str, *arguments: str, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            [
                "/usr/bin/gpg",
                "--no-options",
                "--homedir",
                home,
                "--batch",
                "--no-tty",
                "--no-auto-key-retrieve",
                "--auto-key-locate",
                "clear",
                "--no-auto-check-trustdb",
                *arguments,
            ],
            input=data,
            capture_output=True,
            timeout=GPG_TIMEOUT_SECONDS,
            check=False,
            env={"PATH": "/usr/bin", "LC_ALL": "C", "GNUPGHOME": home},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("NW.js authentication requires working /usr/bin/gpg") from exc
    if result.returncode != 0:
        raise RuntimeError("NW.js GPG verification failed; refusing extraction")
    return result.stdout


def verify_manifest(manifest: bytes, signature: bytes) -> None:
    """Verify detached bytes with a bundled public key and a fresh isolated keyring."""
    if len(manifest) > MAX_MANIFEST_BYTES or len(signature) > MAX_SIGNATURE_BYTES:
        raise RuntimeError("NW.js authentication metadata byte limit exceeded")
    try:
        key = files("box.runtime").joinpath("keys/nwjs.asc").read_bytes()
        with tempfile.TemporaryDirectory(prefix="box-nwjs-gpg-") as home:
            _gpg(home, "--import", data=key)
            listing = _gpg(home, "--with-colons", "--list-keys").decode("ascii")
            records = [line.split(":") for line in listing.splitlines()]
            primary_keys = [index for index, row in enumerate(records) if row[0] == "pub"]
            if len(primary_keys) != 1:
                raise RuntimeError("unexpected NW.js public key bundle")
            index = primary_keys[0]
            if (
                records[index][1] in {"r", "e", "d", "i"}
                or records[index + 1][0] != "fpr"
                or records[index + 1][9] != PRIMARY_FINGERPRINT
            ):
                raise RuntimeError("NW.js public key is invalid, expired, revoked or incorrect")
            manifest_path = Path(home, "manifest")
            signature_path = Path(home, "signature")
            manifest_path.write_bytes(manifest)
            signature_path.write_bytes(signature)
            status = _gpg(
                home, "--status-fd", "1", "--verify", str(signature_path), str(manifest_path)
            )
            _validate_status(status)
    except (OSError, UnicodeError, IndexError) as exc:
        raise RuntimeError("cannot authenticate NW.js manifest with bundled key") from exc


def _validate_status(status: bytes) -> None:
    records = [
        line[len(b"[GNUPG:] ") :].split()
        for line in status.splitlines()
        if line.startswith(b"[GNUPG:] ")
    ]
    invalid = {
        b"BADSIG",
        b"ERRSIG",
        b"NO_PUBKEY",
        b"EXPSIG",
        b"EXPKEYSIG",
        b"REVKEYSIG",
        b"KEYEXPIRED",
        b"SIGEXPIRED",
        b"KEYREVOKED",
        b"FAILURE",
        b"NODATA",
    }
    valid = [row for row in records if row and row[0] == b"VALIDSIG"]
    if any(row and row[0] in invalid for row in records) or len(valid) != 1:
        raise RuntimeError("NW.js manifest signature is invalid, expired or revoked")
    row = valid[0]
    # GnuPG validates subkey binding/cross-certification. Pin the full primary
    # fingerprint from VALIDSIG, never GOODSIG's short key ID or the signer UID.
    primary = row[10] if len(row) == 11 else row[1] if len(row) == 10 else b""
    if primary != PRIMARY_FINGERPRINT.encode("ascii") or row[8] not in {b"8", b"9", b"10", b"11"}:
        raise RuntimeError("NW.js manifest signer or signature digest is not authorized")
