"""Check the shipped key and sandbox from a real, offline-built wheel."""

import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

from install import copy_build_source


def test_wheel_contains_and_loads_the_public_key_outside_checkout(tmp_path: Path) -> None:
    source = tmp_path / "source"
    copy_build_source(source)
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--no-index",
            "--disable-pip-version-check",
            "--wheel-dir",
            str(tmp_path / "wheels"),
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )
    wheels = list((tmp_path / "wheels").glob("box_rpg-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as archive:
        assert "box/launch/sandbox.py" in archive.namelist()
        assert "box/runtime/keys/README.md" in archive.namelist()
        key = archive.read("box/runtime/keys/nwjs.asc")
        assert b"BEGIN PGP PUBLIC KEY BLOCK" in key
        assert b"PRIVATE KEY" not in key
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from importlib.resources import files; "
            "from box.runtime.authenticity import PRIMARY_FINGERPRINT; "
            "from box.launch.sandbox import Sandbox; "
            "assert files('box.runtime').joinpath('keys/nwjs.asc').read_bytes()"
            ".startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'); "
            "print(PRIMARY_FINGERPRINT)",
            str(wheels[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=15,
    )
    assert result.stdout.strip() == "1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A"
