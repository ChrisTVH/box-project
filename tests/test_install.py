import base64
import csv
import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

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
    monkeypatch.setattr(install, "bwrap_problem", lambda: None)
    monkeypatch.setattr(install, "gpg_problem", lambda: None)
    monkeypatch.setattr(install, "installed_version", lambda: installed)
    monkeypatch.setattr(install, "install_commands", no_commands)
    monkeypatch.setattr(install, "uninstall_commands", no_commands)
    monkeypatch.setattr(install, "run_install", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(install, "run_uninstall", lambda: (_ for _ in ()).throw(AssertionError()))
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
    monkeypatch.setattr(install, "installed_version", lambda: "1.0.0")
    monkeypatch.setattr(install, "repo_version", lambda: "1.0.0")
    calls: list[None] = []
    monkeypatch.setattr(install, "run_install", lambda: calls.append(None) or True)

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


def _mock_install_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "is_linux", lambda: True)
    monkeypatch.setattr(install, "_check_python_version", lambda: True)
    monkeypatch.setattr(install, "has_pip", lambda: True)
    monkeypatch.setattr(install, "bwrap_problem", lambda: None)
    monkeypatch.setattr(install, "gpg_problem", lambda: None)


def test_main_blocks_install_without_bwrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "bwrap_problem", lambda: "userns")
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--install", "--yes"])
    monkeypatch.setattr(install, "installed_version", lambda: None)
    monkeypatch.setattr(install, "repo_version", lambda: "1.0.0")

    def forbidden() -> bool:
        pytest.fail("runtime checks must stop the install")

    monkeypatch.setattr(install, "run_install", forbidden)

    assert install.main() == 1
    assert "user namespaces" in capsys.readouterr().err


def test_main_uninstall_skips_runtime_tool_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_install_prerequisites(monkeypatch)
    monkeypatch.setattr(install, "BWRAP", tmp_path / "missing-bwrap")
    monkeypatch.setattr(install, "GPG", tmp_path / "missing-gpg")
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--uninstall", "--yes"])
    monkeypatch.setattr(install, "installed_version", lambda: "1.0.0")
    monkeypatch.setattr(install, "run_uninstall", lambda: True)

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
    monkeypatch.setattr(install, "installed_version", lambda: None)
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

    assert install.run_install()
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
    install.update_completion(source, target)
    assert target.read_bytes() == source.read_bytes()
    install.update_completion(source, target)
    install.update_completion(source, target, uninstall=True)
    assert not target.exists()
    install.update_completion(source, target, uninstall=True)


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

    def stage(destination: Path) -> None:
        staged.append(destination)

    monkeypatch.setattr(install, "update_completion", complete)
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
    assert install.run_install() is (failed_step is None)
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
    def stage(destination: Path) -> None:
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

    def fail(workspace: Path) -> Path:
        workspaces.append(workspace)
        raise OSError("missing build input")

    def forbidden(args: list[str]) -> int:
        pytest.fail("pip must not execute after build failure")

    def forbidden_completion(source: Path, target: Path) -> None:
        pytest.fail("completions must not change after build failure")

    monkeypatch.setattr(install, "build_wheel", fail)
    monkeypatch.setattr(install, "run", forbidden)
    monkeypatch.setattr(install, "update_completion", forbidden_completion)
    assert not install.run_install()
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


# Pinned pre-change completions from d28845f, before --allow-network was added.
# Derive them from the current files with an exact, hash-checked reversal so the
# migration tests never silently skip when the Git object is unavailable. Any
# future completion change must extend the chain below and keep the reviewed
# hashes in PREVIOUS_COMPLETION_HASHES matching.
_HISTORICAL_COMPLETION_REVERSALS: dict[str, tuple[tuple[bytes, bytes], ...]] = {
    "box-rpg.bash": (
        (
            b"--copy-root-file --allow-network --allow-game-writes --x11 --help",
            b"--copy-root-file --help",
        ),
    ),
    "box-rpg.fish": (
        (
            b"complete -c box-rpg -n '__fish_seen_subcommand_from launch' -l allow-network"
            b" -d 'Allow host network access for this launch only'\n"
            b"complete -c box-rpg -n '__fish_seen_subcommand_from launch' -l allow-game-writes"
            b" -d 'Allow game directory writes for this launch only'\n"
            b"complete -c box-rpg -n '__fish_seen_subcommand_from launch' -l x11"
            b" -d 'Use the local X11 display for this launch only'\n",
            b"",
        ),
        (b"ten-version page", b"five-version page"),
    ),
    "_box-rpg": (
        (
            b" '--allow-network[allow host network access for this launch only]'"
            b" '--allow-game-writes[allow game directory writes for this launch only]'"
            b" '--x11[use the local X11 display for this launch only]'",
            b"",
        ),
    ),
}


@pytest.fixture(params=["box-rpg.bash", "box-rpg.fish", "_box-rpg"])
def previous_completion(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, bytes]:
    name = str(request.param)
    current = (install.REPO_ROOT / "res/completions" / name).read_bytes()
    previous = current
    for new, old in _HISTORICAL_COMPLETION_REVERSALS[name]:
        previous = previous.replace(new, old)
    assert previous != current
    assert hashlib.sha256(previous).hexdigest() in install.PREVIOUS_COMPLETION_HASHES[name]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", lambda: home)
    source = tmp_path / name
    source.write_bytes((install.REPO_ROOT / "res/completions" / name).read_bytes())
    assert source.read_bytes() != previous
    target = home / name
    target.write_bytes(previous)
    return source, target, previous


@pytest.mark.parametrize("uninstall", [False, True])
def test_previous_official_completion_upgrade_and_uninstall(
    previous_completion: tuple[Path, Path, bytes], uninstall: bool
) -> None:
    source, target, _previous = previous_completion
    install.update_completion(source, target, uninstall=uninstall)
    if uninstall:
        assert not target.exists()
    else:
        assert target.read_bytes() == source.read_bytes()
        install.update_completion(source, target, uninstall=True)
        assert not target.exists()


def test_intermediate_official_completion_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shipped official copy newer than the oldest pin still upgrades."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install.Path, "home", lambda: home)
    name = "box-rpg.fish"
    source = tmp_path / name
    source.write_bytes((install.REPO_ROOT / "res/completions" / name).read_bytes())
    previous = source.read_bytes().replace(b"ten-version page", b"five-version page")
    previous = previous.replace(
        b"complete -c box-rpg -n '__fish_seen_subcommand_from launch' -l x11"
        b" -d 'Use the local X11 display for this launch only'\n",
        b"",
    )
    assert hashlib.sha256(previous).hexdigest() in install.PREVIOUS_COMPLETION_HASHES[name]
    target = home / name
    target.write_bytes(previous)
    install.update_completion(source, target)
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("uninstall", [False, True])
def test_customized_previous_completion_is_preserved(
    previous_completion: tuple[Path, Path, bytes], uninstall: bool
) -> None:
    source, target, previous = previous_completion
    customized = previous + b"\n# Local customization\n"
    target.write_bytes(customized)
    with pytest.raises(PermissionError):
        install.update_completion(source, target, uninstall=uninstall)
    assert target.read_bytes() == customized


def test_previous_completion_upgrade_preserves_raced_replacement(
    previous_completion: tuple[Path, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _previous = previous_completion
    original = os.rename

    def raced_rename(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"foreign replacement")
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "rename", raced_rename)
    with pytest.raises(PermissionError):
        install.update_completion(source, target)
    assert target.read_bytes() == b"foreign replacement"


def test_previous_completion_upgrade_never_overwrites_raced_publication(
    previous_completion: tuple[Path, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _previous = previous_completion
    original = os.link

    def raced_link(src: str, dst: str, **kwargs: object) -> None:
        target.write_bytes(b"foreign publication")
        original(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install.os, "link", raced_link)
    with pytest.raises(FileExistsError):
        install.update_completion(source, target)
    assert target.read_bytes() == b"foreign publication"


@pytest.mark.parametrize("uninstall", [False, True])
def test_previous_official_completion_symlink_is_never_followed(
    previous_completion: tuple[Path, Path, bytes], tmp_path: Path, uninstall: bool
) -> None:
    source, target, previous = previous_completion
    outside = tmp_path / "outside-completion"
    target.rename(outside)
    target.symlink_to(outside)
    with pytest.raises(OSError):
        install.update_completion(source, target, uninstall=uninstall)
    assert outside.read_bytes() == previous
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
