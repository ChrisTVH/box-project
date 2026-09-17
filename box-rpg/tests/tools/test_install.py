import base64
import csv
import hashlib
import io
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn
from zipfile import ZipFile

import install
import pytest

# These tests deliberately exercise the standalone script's safety helpers.
# pyright: reportPrivateUsage=false


def _fixed_installed_version(version: str | None) -> Callable[[str], str | None]:
    """Return an installed_version fake answering one version for any package."""

    def _version(package: str = "") -> str | None:
        return version

    return _version


def _succeed(*args: object) -> bool:
    """Pretend an installer action succeeded regardless of its target."""
    return True


def _unreachable(*args: object) -> NoReturn:
    """Fail the test when an installer action runs unexpectedly."""
    raise AssertionError("installer action must not run")


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

    def no_commands(*args: object) -> list[str]:
        return []

    monkeypatch.setattr(install.sys, "argv", ["install.py", *arguments])
    monkeypatch.setattr(install, "is_linux", lambda: True)
    monkeypatch.setattr(install, "_check_python_version", lambda: True)
    monkeypatch.setattr(install, "has_pip", lambda: True)
    monkeypatch.setattr(install, "bwrap_problem", lambda: None)
    monkeypatch.setattr(install, "gpg_problem", lambda: None)
    monkeypatch.setattr(install, "fuse3_problem", lambda: None)
    monkeypatch.setattr(install, "gtk_problem", lambda: None)
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(installed))
    monkeypatch.setattr(install, "install_commands", no_commands)
    monkeypatch.setattr(install, "uninstall_commands", no_commands)
    monkeypatch.setattr(install, "run_install", _unreachable)
    monkeypatch.setattr(install, "run_uninstall", _unreachable)
    monkeypatch.setattr("builtins.input", end_of_input)

    assert install.main() == 0
    assert "Aborted." in capsys.readouterr().out


@pytest.mark.parametrize("forced", [False, True])
def test_pip_install_args_force_reinstall_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forced: bool
) -> None:
    monkeypatch.setattr(install, "force_reinstall", forced)
    monkeypatch.setattr(install, "allow_system_packages", False)

    args = install._pip_install_args(tmp_path / "box_rpg-1.0.0-py3-none-any.whl")

    assert ("--force-reinstall" in args) == forced


@pytest.mark.parametrize("flag", [[], ["--force-reinstall"]])
def test_install_same_version_message_reflects_force_flag(
    flag: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(install, "force_reinstall", False)
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--install", "--yes", *flag])
    monkeypatch.setattr(install, "is_linux", lambda: True)
    monkeypatch.setattr(install, "_check_python_version", lambda: True)
    monkeypatch.setattr(install, "has_pip", lambda: True)
    monkeypatch.setattr(install, "bwrap_problem", lambda: None)
    monkeypatch.setattr(install, "gpg_problem", lambda: None)
    monkeypatch.setattr(install, "fuse3_problem", lambda: None)
    monkeypatch.setattr(install, "gtk_problem", lambda: None)
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version("1.0.0"))
    monkeypatch.setattr(install, "repo_version", lambda: "1.0.0")
    calls: list[None] = []

    def succeed(*args: object) -> bool:
        calls.append(None)
        return True

    monkeypatch.setattr(install, "run_install", succeed)

    assert install.main() == 0
    assert install.force_reinstall == bool(flag)
    assert len(calls) == 1
    output = capsys.readouterr().out
    if flag:
        assert "Reinstalling the same version (1.0.0) as requested." in output
    else:
        assert "The same version (1.0.0) is already installed." in output


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


def test_bwrap_problem_reports_missing_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install, "BWRAP", tmp_path / "missing-bwrap")

    assert install.bwrap_problem() == "missing"


@pytest.mark.parametrize(
    ("returncodes", "expected"),
    [([1], "broken"), ([0, 1], "userns"), ([0, 0], None)],
)
def test_bwrap_problem_classifies_version_and_namespace_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncodes: list[int],
    expected: str | None,
) -> None:
    binary = tmp_path / "bwrap"
    binary.write_bytes(b"fixture")
    binary.chmod(0o700)
    monkeypatch.setattr(install, "BWRAP", binary)
    codes: list[int] = [int(code) for code in returncodes]

    def record(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], codes.pop(0))

    monkeypatch.setattr(install.subprocess, "run", record)

    assert install.bwrap_problem() == expected
    assert codes == []


def test_bwrap_smoke_test_uses_namespaces_and_library_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "bwrap"
    binary.write_bytes(b"fixture")
    binary.chmod(0o700)
    monkeypatch.setattr(install, "BWRAP", binary)
    calls: list[list[str]] = []

    def record(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(install.subprocess, "run", record)

    assert install.bwrap_problem() is None
    assert calls[0] == [str(binary), "--version"]
    smoke = calls[1]
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts"):
        assert flag in smoke
    assert "--unshare-net" not in smoke
    for name in ("/usr", "/lib", "/lib64"):
        assert smoke.count(name) >= 2
    assert smoke[-2:] == ["--", "/usr/bin/true"]


def test_gpg_problem_reports_missing_broken_and_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install, "GPG", tmp_path / "missing-gpg")
    assert install.gpg_problem() == "missing"
    binary = tmp_path / "gpg"
    binary.write_bytes(b"fixture")
    binary.chmod(0o700)
    monkeypatch.setattr(install, "GPG", binary)

    def failing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1)

    def passing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(install.subprocess, "run", failing)
    assert install.gpg_problem() == "broken"
    monkeypatch.setattr(install.subprocess, "run", passing)
    assert install.gpg_problem() is None


def test_fuse3_problem_reports_missing_no_device_and_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_name: str) -> str | None:
        return None

    monkeypatch.setattr(install, "find_library", _missing)
    assert install.fuse3_problem() == "missing"

    def _boom(_name: str) -> str:
        raise OSError("find_library unavailable")

    monkeypatch.setattr(install, "find_library", _boom)
    assert install.fuse3_problem() == "missing"

    def _found(_name: str) -> str | None:
        return "libfuse3.so.3"

    def _no_device(self: object) -> bool:
        return False

    def _device(self: object) -> bool:
        return True

    monkeypatch.setattr(install, "find_library", _found)
    monkeypatch.setattr(install.Path, "exists", _no_device)
    assert install.fuse3_problem() == "no-device"

    monkeypatch.setattr(install.Path, "exists", _device)
    assert install.fuse3_problem() is None


def _mock_install_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "is_linux", lambda: True)
    monkeypatch.setattr(install, "_check_python_version", lambda: True)
    monkeypatch.setattr(install, "has_pip", lambda: True)
    monkeypatch.setattr(install, "bwrap_problem", lambda: None)
    monkeypatch.setattr(install, "gpg_problem", lambda: None)
    monkeypatch.setattr(install, "fuse3_problem", lambda: None)
    monkeypatch.setattr(install, "gtk_problem", lambda: None)


def test_main_blocks_install_without_bwrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "bwrap_problem", lambda: "userns")
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--install", "--yes"])
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(None))
    monkeypatch.setattr(install, "repo_version", lambda: "1.0.0")

    def forbidden(*args: object) -> bool:
        pytest.fail("runtime checks must stop the install")

    monkeypatch.setattr(install, "run_install", forbidden)

    assert install.main() == 1
    assert "user namespaces" in capsys.readouterr().err


def test_main_warns_but_installs_without_fuse3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "fuse3_problem", lambda: "missing")
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--install", "--yes"])
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(None))
    monkeypatch.setattr(install, "repo_version", lambda: "1.0.0")
    calls: list[str] = []

    def succeed(target: str = "all") -> bool:
        calls.append(target)
        return True

    monkeypatch.setattr(install, "run_install", succeed)

    assert install.main() == 0
    assert "warning:" in capsys.readouterr().err
    assert calls == ["all"]


def test_main_warns_but_installs_without_fuse_device(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "fuse3_problem", lambda: "no-device")
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--install", "--yes"])
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(None))
    monkeypatch.setattr(install, "repo_version", lambda: "1.0.0")
    calls: list[str] = []

    def succeed(target: str = "all") -> bool:
        calls.append(target)
        return True

    monkeypatch.setattr(install, "run_install", succeed)

    assert install.main() == 0
    assert "warning:" in capsys.readouterr().err
    assert calls == ["all"]


def test_main_uninstall_skips_runtime_tool_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "BWRAP", tmp_path / "missing-bwrap")
    monkeypatch.setattr(install, "GPG", tmp_path / "missing-gpg")
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--uninstall", "--yes"])
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version("1.0.0"))
    monkeypatch.setattr(install, "run_uninstall", _succeed)

    assert install.main() == 0


def test_main_uninstall_skips_fuse3_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_install_prerequisites(monkeypatch)

    def forbidden() -> str | None:
        raise AssertionError("uninstall must not consult fuse3")

    monkeypatch.setattr(install, "fuse3_problem", forbidden)
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--uninstall", "--yes"])
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version("1.0.0"))
    monkeypatch.setattr(install, "run_uninstall", _succeed)

    assert install.main() == 0


@pytest.mark.parametrize("verbose", [False, True])
def test_main_dry_run_plan_verbosity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verbose: bool
) -> None:
    _mock_install_prerequisites(monkeypatch)
    argv = ["install.py"]
    if verbose:
        argv.append("--verbose")
    monkeypatch.setattr(install.sys, "argv", argv)
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(None))
    monkeypatch.setattr(install, "repo_version", lambda: "1.0.0")

    assert install.main() == 0
    output = capsys.readouterr().out
    if verbose:
        assert "MetadataOnlyFinder" in output
    else:
        assert "Install plan:" in output
        assert "3 shell completions" in output
        assert "MetadataOnlyFinder" not in output


def test_run_install_reports_completion_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", lambda: home)
    source = tmp_path / "source"
    source.write_bytes(b"completion")
    target = home / "target"
    monkeypatch.setattr(install, "COMPLETION_TARGETS", [(source, target)])

    def fake_wheel(workspace: Path) -> Path:
        return tmp_path / "wheel"

    def succeed(args: list[str]) -> int:
        return 0

    monkeypatch.setattr(install, "build_wheel", fake_wheel)
    monkeypatch.setattr(install, "run", succeed)

    assert install.run_install("cli")
    assert "installed 1 shell completions" in capsys.readouterr().out


def test_user_bin_without_path_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_packages = tmp_path / ".local/lib/python3.14/site-packages"
    site_packages.mkdir(parents=True)
    monkeypatch.setattr(install.site, "getusersitepackages", lambda: str(site_packages))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert install.user_bin_without_path() == tmp_path / ".local/bin"
    monkeypatch.setenv("PATH", f"/usr/bin:/bin:{tmp_path / '.local/bin'}")
    assert install.user_bin_without_path() is None


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
    install.update_managed_file(source, target)
    assert target.read_bytes() == source.read_bytes()
    install.update_managed_file(source, target)
    install.update_managed_file(source, target, uninstall=True)
    assert not target.exists()
    install.update_managed_file(source, target, uninstall=True)


@pytest.mark.parametrize("failed_step", [0, 1, 2, 3, None])
def test_install_uses_private_locked_build_and_stops_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_step: int | None,
) -> None:
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(install, "allow_system_packages", False)
    monkeypatch.setattr(install, "system_python", lambda: "/base/python")
    source = tmp_path / "completion"
    source.write_bytes(b"completion")
    target = tmp_path / "target"
    monkeypatch.setattr(install, "COMPLETION_TARGETS", [(source, target)])
    completed: list[Path] = []
    staged: list[Path] = []

    def complete(source: Path, target: Path) -> None:
        completed.append(target)

    def stage(destination: Path, project_dir: Path | None = None) -> None:
        staged.append(destination)

    monkeypatch.setattr(install, "update_managed_file", complete)
    monkeypatch.setattr(install, "copy_build_source", stage)
    commands: list[list[str]] = []

    def record(args: list[str]) -> int:
        step = len(commands)
        commands.append(args)
        assert staged[0].parent.is_dir()
        assert staged[0].parent.stat().st_mode & 0o777 == 0o700
        if step == failed_step:
            return 1
        if step == 2:
            wheels = staged[0].parent / "wheels"
            wheels.mkdir()
            (wheels / "box_rpg-1-py3-none-any.whl").write_bytes(b"mock wheel")
        return 0

    monkeypatch.setattr(install, "run", record)
    assert install.run_install("cli") is (failed_step is None)
    assert len(commands) == (4 if failed_step is None else failed_step + 1)
    assert not staged[0].parent.exists()
    assert completed == ([target] if failed_step is None else [])
    if len(commands) > 1:
        assert {"--require-hashes", "--only-binary=:all:", "--force-reinstall"} <= set(commands[1])
        assert commands[1][0] != "/base/python"
        assert "--no-deps" not in commands[1]
    if len(commands) > 2:
        assert {"--no-build-isolation", "--no-deps", "--no-index"} <= set(commands[2])
    if len(commands) > 3:
        assert commands[3][0] == "/base/python"
        assert {"--user", "--no-deps", "--no-index"} <= set(commands[3])
        assert str(install.REPO_ROOT) not in commands[3]
    assert all("--break-system-packages" not in command for command in commands)


@pytest.mark.parametrize("wheel_names", [[], ["box_rpg-1.whl", "box_rpg-2.whl"]])
def test_build_rejects_missing_or_ambiguous_wheels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wheel_names: list[str]
) -> None:
    def stage(destination: Path, project_dir: Path | None = None) -> None:
        pass

    def run(args: list[str]) -> int:
        return 0

    monkeypatch.setattr(install, "copy_build_source", stage)
    monkeypatch.setattr(install, "run", run)
    (tmp_path / "wheels").mkdir()
    for name in wheel_names:
        (tmp_path / "wheels" / name).touch()
    with pytest.raises(RuntimeError, match="exactly one"):
        install.build_wheel(tmp_path)


def test_build_source_excludes_previous_artifacts(tmp_path: Path) -> None:
    destination = tmp_path / "source"
    install.copy_build_source(destination)
    assert (destination / "res/requirements/build.txt").is_file()
    assert (destination / "src/box/__init__.py").is_file()
    assert not list(destination.rglob("*.egg-info"))
    assert not list(destination.rglob("__pycache__"))
    assert not (destination / ".venv").exists()
    assert not (destination / "build").exists()


def test_build_source_creates_missing_workspace_parents(tmp_path: Path) -> None:
    """Nested workspaces (GUI cli/ and gui/ side by side) start nonexistent."""
    destination = tmp_path / "cli" / "source"
    install.copy_build_source(destination)
    assert (destination / "pyproject.toml").is_file()
    assert (destination / "src/box/__init__.py").is_file()
    gui_destination = tmp_path / "gui" / "source"
    install.copy_build_source(gui_destination, install.GUI_ROOT)
    assert (gui_destination / "src/box_gui/__init__.py").is_file()
    assert (gui_destination / "res/requirements/build.txt").is_file()


def test_build_source_refuses_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "source"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        install.copy_build_source(destination)


def test_run_disables_external_pip_and_python_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://untrusted.invalid")
    monkeypatch.setenv("PIP_TARGET", "/outside")
    monkeypatch.setenv("PYTHONPATH", "/outside")
    monkeypatch.setenv("VIRTUAL_ENV", "/outside")
    monkeypatch.setenv("LD_PRELOAD", "/outside/evil.so")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/outside")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/outside/evil.dylib")
    monkeypatch.setenv("PATH", "/outside")

    def record(
        args: list[str], *, check: bool, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert not check
        assert env["PIP_CONFIG_FILE"] == os.devnull
        assert env["PATH"] == "/usr/bin:/bin"
        assert (
            not {
                "PIP_EXTRA_INDEX_URL",
                "PIP_TARGET",
                "PYTHONPATH",
                "VIRTUAL_ENV",
                "LD_PRELOAD",
                "LD_LIBRARY_PATH",
                "DYLD_INSERT_LIBRARIES",
            }
            & env.keys()
        )
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(install.subprocess, "run", record)
    assert install.run(["mock"]) == 0


def test_install_cleans_up_and_stops_on_build_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)
    workspaces: list[Path] = []

    def fail(workspace: Path, *args: object, **kwargs: object) -> Path:
        workspaces.append(workspace)
        raise OSError("missing build input")

    def forbidden(args: list[str]) -> int:
        pytest.fail("pip must not execute after build failure")

    def forbidden_completion(source: Path, target: Path) -> None:
        pytest.fail("completions must not change after build failure")

    monkeypatch.setattr(install, "build_wheel", fail)
    monkeypatch.setattr(install, "run", forbidden)
    monkeypatch.setattr(install, "update_managed_file", forbidden_completion)
    assert not install.run_install("cli")
    assert len(workspaces) == 1
    assert not workspaces[0].exists()


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
        install.update_managed_file(source, target, uninstall=uninstall)
    assert victim.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("uninstall", [False, True])
def test_completion_overwrites_modified_files(
    completion: tuple[Path, Path],
    uninstall: bool,
) -> None:
    source, target = completion
    target.parent.mkdir()
    target.write_bytes(b"custom content")
    install.update_managed_file(source, target, uninstall=uninstall)
    if uninstall:
        assert not target.exists()
    else:
        assert target.read_bytes() == source.read_bytes()


def test_completion_confines_home(completion: tuple[Path, Path], tmp_path: Path) -> None:
    source, target = completion
    for destination in [tmp_path / "outside", target.parent / ".." / ".." / "outside"]:
        with pytest.raises(PermissionError):
            install.update_managed_file(source, destination)
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
        install.update_managed_file(source, target)
    assert target.read_bytes() == b"foreign"


def test_completion_removal_restores_raced_foreign_file(
    completion: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = completion
    install.update_managed_file(source, target)
    original = os.rename

    def raced_rename(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"foreign")
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "rename", raced_rename)
    with pytest.raises(PermissionError):
        install.update_managed_file(source, target, uninstall=True)
    assert target.read_bytes() == b"foreign"


@pytest.mark.parametrize("root", [False, True])
def test_uninstall_refuses_unverified_or_root(
    monkeypatch: pytest.MonkeyPatch,
    root: bool,
) -> None:
    monkeypatch.setattr(install.os, "geteuid", lambda: 0 if root else 1000)

    def _verified(*args: object) -> bool:
        return root

    monkeypatch.setattr(install, "_user_distribution_verified", _verified)
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version("1.0.0"))
    monkeypatch.setattr(install, "COMPLETION_TARGETS", [])
    monkeypatch.setattr(install, "GUI_TARGETS", [])

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
        assert args[:3] == [install.system_python(), "-I", "-c"]
        assert args[-1] == "--verify-user-distribution"
        return subprocess.CompletedProcess(args, 0, stdout=locations)

    monkeypatch.setattr(install.subprocess, "run", probe)
    assert install._user_distribution_verified() is accepted


def test_pep668_requires_separate_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = Path("/private/box_rpg-1-py3-none-any.whl")
    monkeypatch.setattr(install, "allow_system_packages", False)
    assert "--break-system-packages" not in install._pip_install_args(wheel)
    assert "--break-system-packages" not in install._pip_uninstall_args()
    monkeypatch.setattr(install, "allow_system_packages", True)
    assert "--break-system-packages" in install._pip_install_args(wheel)
    assert "--break-system-packages" in install._pip_uninstall_args()


def test_removal_conflict_keeps_both_foreign_files(
    completion: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = completion
    install.update_managed_file(source, target)
    original = os.rename

    def raced_rename(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"first foreign file")
        original(src, dst, **kwargs)  # type: ignore[arg-type]
        target.write_bytes(b"second foreign file")

    monkeypatch.setattr(install.os, "rename", raced_rename)
    with pytest.raises(PermissionError, match="preserved"):
        install.update_managed_file(source, target, uninstall=True)
    assert target.read_bytes() == b"second foreign file"
    recovered = list(target.parent.glob(".box-rpg-recovery-*/entry"))
    assert len(recovered) == 1
    assert recovered[0].read_bytes() == b"first foreign file"


def test_install_refuses_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.os, "geteuid", lambda: 0)

    def forbidden_run(_: list[str]) -> int:
        pytest.fail("pip must not execute")

    monkeypatch.setattr(install, "run", forbidden_run)
    assert not install.run_install("cli")


def test_verified_uninstall_uses_pip_and_safe_completion_removal(
    completion: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = completion
    install.update_managed_file(source, target)
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(install, "_user_distribution_verified", _succeed)

    def _cli_installed(package: str = "") -> str | None:
        return "1.0.0" if package == install.PACKAGE else None

    monkeypatch.setattr(install, "installed_version", _cli_installed)
    monkeypatch.setattr(install, "COMPLETION_TARGETS", [(source, target)])
    monkeypatch.setattr(install, "GUI_TARGETS", [])
    commands: list[list[str]] = []

    def record(args: list[str]) -> int:
        commands.append(args)
        return 0

    monkeypatch.setattr(install, "run", record)
    assert install.run_uninstall()
    assert commands == [install._pip_uninstall_args()]
    assert not target.exists()


@pytest.fixture(params=["box-rpg.bash", "box-rpg.fish", "_box-rpg"])
def stale_completion(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    name = str(request.param)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", lambda: home)
    source = tmp_path / name
    source.write_bytes((install.CLI_ROOT / "res/completions" / name).read_bytes())
    target = home / name
    target.write_bytes(b"outdated completion")
    return source, target


@pytest.mark.parametrize("uninstall", [False, True])
def test_stale_completion_is_overwritten_or_removed(
    stale_completion: tuple[Path, Path], uninstall: bool
) -> None:
    source, target = stale_completion
    install.update_managed_file(source, target, uninstall=uninstall)
    if uninstall:
        assert not target.exists()
    else:
        assert target.read_bytes() == source.read_bytes()
        install.update_managed_file(source, target, uninstall=True)
        assert not target.exists()


def test_previous_completion_upgrade_preserves_raced_replacement(
    stale_completion: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = stale_completion
    original = os.rename

    def raced_rename(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"foreign replacement")
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "rename", raced_rename)
    with pytest.raises(PermissionError):
        install.update_managed_file(source, target)
    assert target.read_bytes() == b"foreign replacement"


def test_previous_completion_upgrade_never_overwrites_raced_publication(
    stale_completion: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = stale_completion
    original = os.link

    def raced_link(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"foreign publication")
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "link", raced_link)
    with pytest.raises(FileExistsError):
        install.update_managed_file(source, target)
    assert target.read_bytes() == b"foreign publication"


@pytest.mark.parametrize("uninstall", [False, True])
def test_previous_official_completion_symlink_is_never_followed(
    stale_completion: tuple[Path, Path], tmp_path: Path, uninstall: bool
) -> None:
    source, target = stale_completion
    outside = tmp_path / "outside-completion"
    outside.write_bytes(b"outdated completion")
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(OSError):
        install.update_managed_file(source, target, uninstall=uninstall)
    assert outside.read_bytes() == b"outdated completion"
    assert target.is_symlink()


def test_installed_version_does_not_expose_user_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = tmp_path / "box_rpg-1.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.4\nName: box-rpg\nVersion: 1.0.0\n")
    monkeypatch.setattr(install.site, "getusersitepackages", lambda: str(tmp_path))
    original_path = sys.path[:]
    assert install.installed_version() == "1.0.0"
    assert sys.path == original_path


def make_install_wheel(directory: Path, version: str, module: str) -> Path:
    """Create a valid local wheel with RECORD hashes; never download/build code."""
    info = f"box_rpg-{version}.dist-info"
    files = {
        "box/__init__.py": f'__version__ = "{version}"\n'.encode(),
        "box/cli.py": b"def main():\n    return 0\n",
        f"box/{module}.py": b"VALUE = 1\n",
        f"{info}/METADATA": f"Metadata-Version: 2.4\nName: box-rpg\nVersion: {version}\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": b"[console_scripts]\nbox-rpg = box.cli:main\n",
    }
    record = io.StringIO()
    writer = csv.writer(record)
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(content)))
    writer.writerow((f"{info}/RECORD", "", ""))
    files[f"{info}/RECORD"] = record.getvalue().encode()
    wheel = directory / f"box_rpg-{version}-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel


def test_real_user_wheel_upgrade_and_uninstall_are_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real pip only against a disposable HOME, never the actual user."""
    if not install.has_pip():
        pytest.skip("The base interpreter needs pip for the isolated user-install test")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYTHONUSERBASE", str(home / ".local"))
    # Explicit consent applies only to this temporary --user destination.
    monkeypatch.setattr(install, "allow_system_packages", True)
    user_site = (
        home / f".local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    user_site.mkdir(parents=True)
    marker = home / "executed-user-code"
    payload = f"from pathlib import Path; Path({str(marker)!r}).touch(); raise RuntimeError('untrusted')\n"
    (user_site / "danger.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n"
    )
    for name in ("pip", "fractions", "box_user_tripwire", "sitecustomize"):
        (user_site / f"{name}.py").write_text(payload)
    probe = install.USER_PIP_BOOTSTRAP.replace(
        "raise SystemExit(main(['--isolated', *sys.argv[1:]]))",
        "import fractions, importlib.util\n"
        "importlib.invalidate_caches()\n"
        "assert importlib.util.find_spec('box_user_tripwire') is None\n"
        "assert not Path(fractions.__file__).is_relative_to(user_site)\n",
    )
    assert install.run([install.system_python(), "-I", "-c", probe, "probe"]) == 0
    first = make_install_wheel(tmp_path, "1.0.0", "obsolete")
    second = make_install_wheel(tmp_path, "2.0.0", "replacement")
    assert install.run(install._pip_install_args(first)) == 0
    assert (user_site / "box/obsolete.py").is_file()
    assert install.run(install._pip_install_args(second)) == 0
    assert not (user_site / "box/obsolete.py").exists()
    assert (user_site / "box/replacement.py").is_file()
    assert [path.name for path in user_site.glob("box_rpg-*.dist-info")] == [
        "box_rpg-2.0.0.dist-info"
    ]
    assert (home / ".local/bin/box-rpg").is_file()
    assert install._user_distribution_verified()
    assert install.run(install._pip_uninstall_args()) == 0
    assert not list(user_site.glob("box_rpg-*.dist-info"))
    assert not (user_site / "box").exists()
    assert not (home / ".local/bin/box-rpg").exists()
    assert not marker.exists()


def test_user_pip_rejects_symlinked_user_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not install.has_pip():
        pytest.skip("The base interpreter needs pip for the isolated bootstrap test")
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    assert install.run([*install._user_pip_args(), "--version"]) != 0
    assert not list(outside.iterdir())


def test_user_pip_accepts_symlinked_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not install.has_pip():
        pytest.skip("The base interpreter needs pip for the isolated bootstrap test")
    real = tmp_path / "real-home"
    real.mkdir()
    home = tmp_path / "home"
    home.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    assert install.run([*install._user_pip_args(), "--version"]) == 0


def test_user_pip_rejects_group_writable_home_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    local = home / ".local"
    local.mkdir(parents=True)
    local.chmod(0o775)
    monkeypatch.setenv("HOME", str(home))
    assert install.run([*install._user_pip_args(), "--version"]) != 0


def test_has_pip_uses_the_sanitized_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, dict[str, str]] = {}

    def record(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["env"] = kwargs["env"]  # type: ignore[assignment]
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(install.subprocess, "run", record)

    assert install.has_pip()
    assert seen["env"] == install._command_environment()


def test_target_selection_filters_install_and_uninstall_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        install, "GUI_TARGETS", [(Path("/gui/app.desktop"), Path("/home/gui/app.desktop"))]
    )

    cli_install = install.install_commands("cli")
    assert any("safe completion install" in command for command in cli_install)
    assert not any("GUI" in command for command in cli_install)
    gui_install = install.install_commands("gui")
    assert any("safe GUI file install" in command for command in gui_install)
    assert not any("completion" in command for command in gui_install)
    assert any("box_rpg_maker" in command for command in gui_install)
    assert len(install.install_commands("all")) == len(cli_install) + len(gui_install)

    cli_uninstall = install.uninstall_commands("cli")
    assert not any("GUI" in command or "box-rpg-maker" in command for command in cli_uninstall)
    assert any("box-rpg-maker" in command for command in install.uninstall_commands("gui"))


def test_gui_targets_cover_desktop_entry_and_current_icons() -> None:
    icons = sorted((install.GUI_ROOT / "res/icons").glob("*.svg"))
    assert icons, "expected bundled GUI icons in the checkout"
    desktop, *icon_targets = install.GUI_TARGETS
    assert desktop[0].name == install.DESKTOP_FILE_NAME
    assert desktop[1].name == install.DESKTOP_FILE_NAME
    assert desktop[1].parent.name == "applications"
    assert [source.name for source, _ in icon_targets] == [icon.name for icon in icons]
    for source, destination in icon_targets:
        assert source.suffix == ".svg"
        assert destination.name == source.name
        assert destination.parent.name == "apps"


def test_gui_install_and_uninstall_use_only_gui_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", lambda: home)
    sources = tmp_path / "sources"
    sources.mkdir()
    desktop_source = sources / install.DESKTOP_FILE_NAME
    desktop_source.write_bytes(b"[Desktop Entry]\n")
    icon_source = sources / "example-symbolic.svg"
    icon_source.write_bytes(b"<svg></svg>")
    gui_targets = [
        (desktop_source, home / ".local/share/applications" / install.DESKTOP_FILE_NAME),
        (icon_source, home / ".local/share/icons/hicolor/scalable/apps" / icon_source.name),
    ]
    monkeypatch.setattr(install, "GUI_TARGETS", gui_targets)
    untouched = tmp_path / "untouched-completion"
    monkeypatch.setattr(
        install, "COMPLETION_TARGETS", [(sources / "missing-completion", untouched)]
    )
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(None))

    seen: list[dict[str, object]] = []

    def fake_wheel(workspace: Path, *args: object, **kwargs: object) -> Path:
        seen.append(
            {
                "project_dir": args[0] if args else None,
                "wheel_glob": kwargs.get("wheel_glob", "box_rpg-*.whl"),
            }
        )
        return tmp_path / f"wheel-{len(seen)}"

    calls: list[list[str]] = []

    def record(args: list[str]) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(install, "build_wheel", fake_wheel)
    monkeypatch.setattr(install, "run", record)

    assert install.run_install("gui")
    # The backend wheel is built first so the GUI pip install pins box-rpg
    # to the local checkout instead of resolving it from PyPI.
    assert [build["project_dir"] for build in seen] == [None, install.GUI_ROOT]
    assert [build["wheel_glob"] for build in seen] == ["box_rpg-*.whl", "box_rpg_maker-*.whl"]
    assert len(calls) == 1
    assert "--no-deps" not in calls[0]
    assert "--no-index" not in calls[0]
    assert str(tmp_path / "wheel-1") in calls[0]
    assert str(tmp_path / "wheel-2") in calls[0]
    assert gui_targets[0][1].read_bytes() == b"[Desktop Entry]\n"
    assert gui_targets[1][1].read_bytes() == b"<svg></svg>"
    assert not untouched.exists()
    assert "GUI files" in capsys.readouterr().out

    assert install.run_uninstall("gui")
    assert not gui_targets[0][1].exists()
    assert not gui_targets[1][1].exists()


def test_dual_distribution_versions_are_detected_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_site = tmp_path / "site"
    user_site.mkdir()
    for name, version in (("box-rpg", "26.9.30"), ("box-rpg-maker", "0.1.0")):
        info = user_site / f"{name.replace('-', '_')}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n")
    monkeypatch.setattr(install.site, "getusersitepackages", lambda: str(user_site))

    assert install.installed_version() == "26.9.30"
    assert install.installed_version(install.PACKAGE) == "26.9.30"
    assert install.installed_version(install.GUI_PACKAGE) == "0.1.0"
    # Both distributions share one version-standard stamp from the monorepo root.
    assert install.gui_repo_version() == install.repo_version()
    assert install.selected_packages("cli") == (install.PACKAGE,)
    assert install.selected_packages("gui") == (install.GUI_PACKAGE,)
    assert install.selected_packages("all") == (install.PACKAGE, install.GUI_PACKAGE)


def test_gtk_problem_probes_gtk_and_adwaita_typelibs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "python3"
    binary.write_bytes(b"fixture")
    binary.chmod(0o700)
    monkeypatch.setattr(install, "system_python", lambda: str(binary))
    calls: list[list[str]] = []

    def record(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(install.subprocess, "run", record)

    assert install.gtk_problem() is None
    assert calls[0][:3] == [str(binary), "-I", "-c"]
    assert "Gtk" in calls[0][3]
    assert "Adw" in calls[0][3]

    def failing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1)

    monkeypatch.setattr(install.subprocess, "run", failing)
    assert install.gtk_problem() == "missing"


def test_gtk_prerequisite_blocks_gui_install_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "gtk_problem", lambda: "missing")
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(None))

    def forbidden(*args: object) -> bool:
        pytest.fail("the GTK gate must stop the install")

    monkeypatch.setattr(install, "run_install", forbidden)
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--install", "--yes"])

    assert install.main() == 1
    assert "GTK" in capsys.readouterr().err

    monkeypatch.setattr(install, "run_install", _succeed)
    monkeypatch.setattr(
        install.sys, "argv", ["install.py", "--install", "--yes", "--target", "cli"]
    )

    assert install.main() == 0

    monkeypatch.setattr(install, "run_uninstall", _succeed)
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version("1.0.0"))
    monkeypatch.setattr(
        install.sys, "argv", ["install.py", "--uninstall", "--yes", "--target", "gui"]
    )

    assert install.main() == 0


def test_gui_pip_install_uses_real_dependencies(tmp_path: Path) -> None:
    wheel = tmp_path / "box_rpg_maker-0.1.0-py3-none-any.whl"
    cli_wheel = tmp_path / "box_rpg-26.9.30-py3-none-any.whl"
    args = install._pip_install_args(wheel, extra_wheels=(cli_wheel,), with_dependencies=True)

    assert "--user" in args
    assert str(wheel) in args
    assert str(cli_wheel) in args
    assert args.index(str(wheel)) < args.index(str(cli_wheel))
    assert "--no-deps" not in args
    assert "--no-index" not in args

    cli_args = install._pip_install_args(tmp_path / "box_rpg-1.0.0-py3-none-any.whl")

    assert "--no-deps" in cli_args
    assert "--no-index" in cli_args


def test_gui_build_stages_gui_project_and_shared_build_lock(tmp_path: Path) -> None:
    destination = tmp_path / "source"
    install.copy_build_source(destination, install.GUI_ROOT)

    assert 'name = "box-rpg-maker"' in (destination / "pyproject.toml").read_text()
    assert (destination / "src/box_gui/__init__.py").is_file()
    assert (destination / "LICENSE").is_file()
    assert (destination / "res/requirements/build.txt").is_file()
    assert not (destination / "docs").exists()
    assert not list(destination.rglob("*.egg-info"))
    assert not list(destination.rglob("__pycache__"))


def test_cli_build_stages_monorepo_docs_for_data_files(tmp_path: Path) -> None:
    destination = tmp_path / "source"
    install.copy_build_source(destination)

    assert (destination / "docs/manual.md").is_file()
    assert (destination / "LICENSE").is_file()
    assert (destination / "src/box/__init__.py").is_file()


def test_gui_uninstall_removes_only_gui_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", lambda: home)
    gui_source = tmp_path / install.DESKTOP_FILE_NAME
    gui_source.write_bytes(b"[Desktop Entry]\n")
    gui_target = home / install.DESKTOP_FILE_NAME
    cli_source = tmp_path / "box-rpg.bash"
    cli_source.write_bytes(b"completion")
    cli_target = home / "box-rpg"
    install.update_managed_file(gui_source, gui_target)
    install.update_managed_file(cli_source, cli_target)
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)

    def _gui_installed(package: str = "") -> str | None:
        return "0.1.0" if package == install.GUI_PACKAGE else None

    monkeypatch.setattr(install, "installed_version", _gui_installed)
    monkeypatch.setattr(install, "_user_distribution_verified", _succeed)
    monkeypatch.setattr(install, "GUI_TARGETS", [(gui_source, gui_target)])
    monkeypatch.setattr(install, "COMPLETION_TARGETS", [(cli_source, cli_target)])
    commands: list[list[str]] = []

    def record(args: list[str]) -> int:
        commands.append(args)
        return 0

    monkeypatch.setattr(install, "run", record)

    assert install.run_uninstall("gui")
    assert commands == [install._pip_uninstall_args(install.GUI_PACKAGE)]
    assert not gui_target.exists()
    assert cli_target.read_bytes() == b"completion"


def test_main_dry_run_plan_reflects_selected_target(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "installed_version", _fixed_installed_version(None))

    monkeypatch.setattr(install.sys, "argv", ["install.py", "--target", "gui"])
    assert install.main() == 0
    gui_output = capsys.readouterr().out
    assert "box-rpg-maker" in gui_output
    assert "GUI files" in gui_output
    assert "shell completions" not in gui_output

    monkeypatch.setattr(install.sys, "argv", ["install.py", "--target", "cli"])
    assert install.main() == 0
    cli_output = capsys.readouterr().out
    assert "shell completions" in cli_output
    assert "GUI files" not in cli_output
