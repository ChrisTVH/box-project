import shlex
import subprocess
from pathlib import Path

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


def test_dry_run_commands_quote_completion_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_override() -> list[str]:
        return []

    source = Path("/source directory/box-rpg.bash")
    target = Path("/target directory/completions/box-rpg")
    monkeypatch.setattr(install, "COMPLETION_TARGETS", [(source, target)])
    monkeypatch.setattr(install, "_break_system_packages_args", no_override)

    assert install.install_commands()[1:] == [
        shlex.join(["mkdir", "-p", str(target.parent)]),
        shlex.join(["cp", str(source), str(target)]),
    ]
    assert install.uninstall_commands()[1:] == [shlex.join(["rm", "-f", str(target)])]


@pytest.mark.parametrize(
    ("arguments", "installed"),
    [(["--install"], None), (["--uninstall"], "1.0.0")],
)
def test_interactive_actions_abort_cleanly_on_end_of_input(
    arguments: list[str],
    installed: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def end_of_input(_: str) -> str:
        raise EOFError

    def no_commands() -> list[str]:
        return []

    monkeypatch.setattr(install.sys, "argv", ["install.py", *arguments])
    monkeypatch.setattr(install, "is_linux", lambda: True)
    monkeypatch.setattr(install, "_check_python_version", lambda: True)
    monkeypatch.setattr(install, "has_pip", lambda: True)
    monkeypatch.setattr(install, "installed_version", lambda: installed)
    monkeypatch.setattr(install, "install_commands", no_commands)
    monkeypatch.setattr(install, "uninstall_commands", no_commands)
    monkeypatch.setattr(install, "run_install", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(install, "run_uninstall", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr("builtins.input", end_of_input)

    assert install.main() == 0
    assert "Aborted." in capsys.readouterr().out


def test_help_uses_the_configured_translation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LANGUAGE", "es")
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--help"])

    with pytest.raises(SystemExit) as error:
        install.main()

    assert error.value.code == 0
    assert "Instala box-rpg en el sistema." in capsys.readouterr().out


def test_has_pip_handles_a_missing_pip_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_pip(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("pip unavailable")

    monkeypatch.setattr(install.subprocess, "run", missing_pip)

    assert not install.has_pip()
