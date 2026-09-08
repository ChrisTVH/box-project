import os
import subprocess
from pathlib import Path

import install
import pytest

# These tests deliberately exercise the standalone script's safety helpers.
# pyright: reportPrivateUsage=false


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

    assert "'/source directory/box-rpg.bash'" in install.install_commands()[1]
    assert "'/target directory/completions/box-rpg'" in install.uninstall_commands()[1]
    assert not any(command.startswith(("cp ", "rm ")) for command in install.install_commands())


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


@pytest.fixture
def completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", lambda: home)
    source = tmp_path / "source"
    source.write_bytes(b"completion content")
    return source, home / "completions" / "box-rpg"


def test_completion_round_trip(completion: tuple[Path, Path]) -> None:
    source, target = completion
    install.update_completion(source, target)
    assert target.read_bytes() == source.read_bytes()
    install.update_completion(source, target)
    install.update_completion(source, target, uninstall=True)
    assert not target.exists()
    install.update_completion(source, target, uninstall=True)


@pytest.mark.parametrize("uninstall", [False, True])
@pytest.mark.parametrize("ancestor", [False, True])
def test_completion_rejects_symlinks(
    completion: tuple[Path, Path],
    tmp_path: Path,
    ancestor: bool,
    uninstall: bool,
) -> None:
    source, target = completion
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / target.name
    victim.write_bytes(source.read_bytes())
    if ancestor:
        target.parent.symlink_to(outside, target_is_directory=True)
    else:
        target.parent.mkdir()
        target.symlink_to(victim)
    with pytest.raises(OSError):
        install.update_completion(source, target, uninstall=uninstall)
    assert victim.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("uninstall", [False, True])
def test_completion_preserves_modified_files(
    completion: tuple[Path, Path],
    uninstall: bool,
) -> None:
    source, target = completion
    target.parent.mkdir()
    target.write_bytes(b"custom content")
    with pytest.raises(PermissionError):
        install.update_completion(source, target, uninstall=uninstall)
    assert target.read_bytes() == b"custom content"


def test_completion_confines_home(completion: tuple[Path, Path], tmp_path: Path) -> None:
    source, target = completion
    for destination in [tmp_path / "outside", target.parent / ".." / ".." / "outside"]:
        with pytest.raises(PermissionError):
            install.update_completion(source, destination)
    assert not (tmp_path / "outside").exists()


def test_completion_publication_does_not_replace_raced_file(
    completion: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = completion
    original = os.link

    def raced_link(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"foreign")
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "link", raced_link)
    with pytest.raises(FileExistsError):
        install.update_completion(source, target)
    assert target.read_bytes() == b"foreign"


def test_completion_removal_restores_raced_foreign_file(
    completion: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = completion
    install.update_completion(source, target)
    original = os.rename

    def raced_rename(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"foreign")
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "rename", raced_rename)
    with pytest.raises(PermissionError):
        install.update_completion(source, target, uninstall=True)
    assert target.read_bytes() == b"foreign"


@pytest.mark.parametrize("root", [False, True])
def test_uninstall_refuses_unverified_or_root(
    monkeypatch: pytest.MonkeyPatch,
    root: bool,
) -> None:
    monkeypatch.setattr(install.os, "geteuid", lambda: 0 if root else 1000)
    monkeypatch.setattr(install, "_user_distribution_verified", lambda: root)

    def forbidden_run(_: list[str]) -> int:
        pytest.fail("pip must not execute")

    monkeypatch.setattr(install, "run", forbidden_run)
    assert not install.run_uninstall()


@pytest.mark.parametrize(
    ("locations", "accepted"),
    [
        ('["/home/test/site", "/home/test/site"]', True),
        ('["/usr/lib/python/site-packages", "/home/test/site"]', False),
        ('["/home/test/site/subdir", "/home/test/site"]', False),
        ('["/outside/site", "/outside/site"]', False),
        ("null", False),
        ("invalid", False),
    ],
)
def test_base_interpreter_distribution_location(
    monkeypatch: pytest.MonkeyPatch,
    locations: str,
    accepted: bool,
) -> None:
    monkeypatch.setattr(install.Path, "home", lambda: Path("/home/test"))

    def probe(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:2] == [install.system_python(), "-c"]
        return subprocess.CompletedProcess(args, 0, stdout=locations)

    monkeypatch.setattr(install.subprocess, "run", probe)
    assert install._user_distribution_verified() is accepted


def test_pep668_requires_separate_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "allow_system_packages", False)
    assert "--break-system-packages" not in install._pip_install_args()
    assert "--break-system-packages" not in install._pip_uninstall_args()
    monkeypatch.setattr(install, "allow_system_packages", True)
    assert "--break-system-packages" in install._pip_install_args()
    assert "--break-system-packages" in install._pip_uninstall_args()


def test_removal_conflict_keeps_both_foreign_files(
    completion: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = completion
    install.update_completion(source, target)
    original = os.rename

    def raced_rename(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"first foreign file")
        original(src, dst, **kwargs)  # type: ignore[arg-type]
        target.write_bytes(b"second foreign file")

    monkeypatch.setattr(install.os, "rename", raced_rename)
    with pytest.raises(PermissionError, match="preserved"):
        install.update_completion(source, target, uninstall=True)
    assert target.read_bytes() == b"second foreign file"
    recovered = list(target.parent.glob(".box-rpg-recovery-*/entry"))
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == b"first foreign file"


def test_install_refuses_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.os, "geteuid", lambda: 0)

    def forbidden_run(_: list[str]) -> int:
        pytest.fail("pip must not execute")

    monkeypatch.setattr(install, "run", forbidden_run)
    assert not install.run_install()


def test_verified_uninstall_uses_pip_and_safe_completion_removal(
    completion: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = completion
    install.update_completion(source, target)
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(install, "_user_distribution_verified", lambda: True)
    monkeypatch.setattr(install, "COMPLETION_TARGETS", [(source, target)])
    commands: list[list[str]] = []

    def record(args: list[str]) -> int:
        commands.append(args)
        return 0

    monkeypatch.setattr(install, "run", record)
    assert install.run_uninstall()
    assert commands == [install._pip_uninstall_args()]
    assert not target.exists()
