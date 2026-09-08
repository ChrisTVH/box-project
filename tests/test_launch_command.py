from pathlib import Path

import pytest

from box.launch.command import build_command
from box.launch.platform import ozone_platform
from box.models import RuntimeInfo, RuntimeSpec


def test_ozone_platform_selects_wayland_only_with_a_display_socket() -> None:
    assert (
        ozone_platform({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}) == "wayland"
    )
    assert ozone_platform({"XDG_SESSION_TYPE": "wayland"}) == "x11"
    assert ozone_platform({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}) == "x11"


def test_build_command_sets_the_detected_ozone_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = RuntimeInfo(
        RuntimeSpec("v0.115.0", "x64"),
        Path("/runtime"),
        Path("/runtime/nw"),
    )
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    assert build_command(runtime, Path("/session"), Path("/profile")) == [
        "/runtime/nw",
        "--ozone-platform=wayland",
        "--user-data-dir=/profile/user-data",
        "--disk-cache-dir=/profile/disk-cache",
        "/session",
    ]
