import subprocess

import install
import pytest


def test_is_linux_accepts_linux_without_a_distribution_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install.sys, "platform", "linux")

    assert install.is_linux()


def test_is_linux_rejects_non_linux_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.sys, "platform", "darwin")

    assert not install.is_linux()


def test_pip_arguments_only_use_pep_668_override_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_override() -> list[str]:
        return []

    monkeypatch.setattr(install, "system_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(install, "_break_system_packages_args", no_override)

    commands = [*install.install_commands(), *install.uninstall_commands()]

    assert all("--break-system-packages" not in command for command in commands)


def test_has_pip_handles_a_missing_pip_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_pip(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("pip unavailable")

    monkeypatch.setattr(install.subprocess, "run", missing_pip)

    assert not install.has_pip()
