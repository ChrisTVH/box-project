"""Unit tests for the backend clone + install flow (no GTK dependency)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import box_gui.core.backend_install as backend_install_module
from box_gui.core.backend_install import (
    BackendInstallError,
    InstallOutcome,
    InstallPhase,
    build_clone_command,
    build_install_command,
    install_backend,
    install_button_label,
    is_externally_managed_failure,
    query_installed_version,
    run_command_streaming,
)


def test_clone_command_pins_the_tag_shallow() -> None:
    """The clone checks out the embedded tag with a shallow copy, never HEAD."""
    command = build_clone_command(
        "https://gitlab.com/christvh/box-project", "26.9.43", Path("/tmp/dest")
    )

    assert command == [
        "git",
        "clone",
        "--branch",
        "26.9.43",
        "--depth",
        "1",
        "https://gitlab.com/christvh/box-project",
        "/tmp/dest",
    ]


def test_install_command_runs_the_cloned_installer() -> None:
    """The install runs install.py --install --yes --target cli verbatim."""
    command = build_install_command("/usr/bin/python3", Path("/tmp/repo/install.py"))

    assert command == [
        "/usr/bin/python3",
        "/tmp/repo/install.py",
        "--install",
        "--yes",
        "--target",
        "cli",
    ]


def test_gitlab_is_tried_before_github() -> None:
    """The canonical GitLab remote is first, GitHub only a fallback."""
    assert backend_install_module.CLONE_URLS == (
        "https://gitlab.com/christvh/box-project",
        "https://github.com/ChrisTVH/box-project",
    )


def test_button_labels_follow_the_phase() -> None:
    """The single action label tracks detection, work, and retry states."""
    assert install_button_label(InstallPhase.CHECKING, "26.9.43") == "Install box-rpg 26.9.43"
    assert install_button_label(InstallPhase.READY, "26.9.43") == "Install box-rpg 26.9.43"
    assert install_button_label(InstallPhase.INSTALLING, "26.9.43") == "Installing…"
    assert install_button_label(InstallPhase.FAILED, "26.9.43") == "Retry"
    assert install_button_label(InstallPhase.SUCCEEDED, "26.9.43") == "Installed"


def test_streaming_merges_stdout_and_stderr() -> None:
    """Live lines carry both streams with the exit code."""
    lines: list[str] = []
    code = run_command_streaming(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        lines.append,
    )

    assert code == 0
    # Piped stdout block-buffers while stderr does not, so only the set is stable.
    assert sorted(lines) == ["err", "out"]


def test_streaming_reports_nonzero_exit() -> None:
    """A failing command still returns its code instead of raising."""
    assert (
        run_command_streaming([sys.executable, "-c", "raise SystemExit(3)"], lambda _line: None)
        == 3
    )


def test_streaming_surfaces_missing_executables() -> None:
    """A missing executable raises OSError for verbatim surfacing."""
    with pytest.raises(OSError):
        run_command_streaming(["box-rpg-no-such-tool", "--version"], lambda _line: None)


def test_streaming_enforces_timeout() -> None:
    """A hung child is killed after the timeout instead of blocking forever."""
    with pytest.raises(subprocess.TimeoutExpired):
        run_command_streaming(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            lambda _line: None,
            timeout=1,
        )


def test_query_installed_version_reads_first_token(monkeypatch: Any) -> None:
    """The version probe parses the fresh interpreter output."""

    def _fake_run(*args: Any, **kwargs: Any) -> Any:
        assert args[0][0] == "/usr/bin/python3"
        assert "-I" not in args[0]
        return subprocess.CompletedProcess(args[0], 0, stdout="26.9.43\n", stderr="")

    monkeypatch.setattr(backend_install_module.subprocess, "run", _fake_run)

    assert query_installed_version("/usr/bin/python3") == "26.9.43"


def test_query_installed_version_tolerates_failures(monkeypatch: Any) -> None:
    """Probe failures read as unverified, never a crash."""
    monkeypatch.setattr(
        backend_install_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr="nope"),
    )

    assert query_installed_version("/usr/bin/python3") is None

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("denied")

    monkeypatch.setattr(backend_install_module.subprocess, "run", _boom)

    assert query_installed_version("/usr/bin/python3") is None


def _install_fake_stream(
    monkeypatch: Any,
    calls: list[list[str]],
    *,
    clone_results: list[tuple[int, list[str]]],
    install_code: int = 0,
    install_lines: list[str] | None = None,
) -> None:
    """Serve scripted clone/install runs; clones materialize install.py."""

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None) -> int:
        calls.append(command)
        if command[0] == "git":
            code, output = clone_results.pop(0)
            for line in output:
                on_line(line)
            if code == 0:
                destination = Path(command[-1])
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "install.py").write_text("# fake\n", encoding="utf-8")
            return code
        for line in install_lines or []:
            on_line(line)
        return install_code

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)


def test_install_backend_clones_installs_and_verifies(monkeypatch: Any) -> None:
    """The happy path tries GitLab first and returns the verified outcome."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch,
        calls,
        clone_results=[(0, [])],
        install_lines=["OK: Linux system detected.", "installed 3 shell completions"],
    )
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.43")
    seen_status: list[str] = []
    seen_lines: list[str] = []

    outcome = install_backend(
        "26.9.43",
        python="/usr/bin/python3",
        on_line=seen_lines.append,
        on_status=seen_status.append,
    )

    assert outcome == InstallOutcome(tag="26.9.43", installed_version="26.9.43")
    assert seen_lines == ["OK: Linux system detected.", "installed 3 shell completions"]
    assert seen_status
    clone_command = calls[0]
    assert clone_command[:6] == ["git", "clone", "--branch", "26.9.43", "--depth", "1"]
    assert clone_command[6] == "https://gitlab.com/christvh/box-project"
    install_command = next(call for call in calls if call and call[0] == "/usr/bin/python3")
    assert install_command[2:] == ["--install", "--yes", "--target", "cli"]
    assert install_command[1].endswith("/box-project/install.py")


def test_install_backend_falls_back_to_github(monkeypatch: Any) -> None:
    """A GitLab failure retries the same tag on GitHub before giving up."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch,
        calls,
        clone_results=[(128, ["fatal: unable to connect"]), (0, [])],
    )
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.43")

    outcome = install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)

    assert outcome.installed_version == "26.9.43"
    remotes = [call[6] for call in calls if call and call[0] == "git"]
    assert remotes == [
        "https://gitlab.com/christvh/box-project",
        "https://github.com/ChrisTVH/box-project",
    ]


def test_install_backend_reports_clone_failures_verbatim(monkeypatch: Any) -> None:
    """Exhausted clones raise with the verbatim git stderr."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch,
        calls,
        clone_results=[(128, ["fatal: unable to connect"]), (128, ["fatal: 404 not found"])],
    )

    with pytest.raises(BackendInstallError, match="unable to connect"):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)


def test_install_backend_retries_pep668_with_break_flag(monkeypatch: Any) -> None:
    """A PEP 668 refusal retries once with --break-system-packages."""
    install_commands: list[list[str]] = []
    seen_lines: list[str] = []
    seen_status: list[str] = []

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None) -> int:
        if command[0] == "git":
            destination = Path(command[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "install.py").write_text("# fake\n", encoding="utf-8")
            return 0
        install_commands.append(command)
        if len(install_commands) == 1:
            for line in ("error: externally-managed-environment", "hint: See PEP 668"):
                on_line(line)
            return 1
        on_line("installed 3 shell completions")
        return 0

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.43")

    outcome = install_backend(
        "26.9.43",
        python="/usr/bin/python3",
        on_line=seen_lines.append,
        on_status=seen_status.append,
    )

    assert outcome.installed_version == "26.9.43"
    assert len(install_commands) == 2
    assert "--break-system-packages" not in install_commands[0]
    assert install_commands[1][-1] == "--break-system-packages"
    assert "error: externally-managed-environment" in seen_lines
    assert "installed 3 shell completions" in seen_lines
    assert any("break-system-packages" in message for message in seen_status)


def test_install_backend_retries_pep668_only_once(monkeypatch: Any) -> None:
    """A repeated PEP 668 failure still fails closed after one retry."""
    install_commands: list[list[str]] = []

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None) -> int:
        if command[0] == "git":
            destination = Path(command[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "install.py").write_text("# fake\n", encoding="utf-8")
            return 0
        install_commands.append(command)
        on_line("error: externally-managed-environment")
        return 1

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)

    with pytest.raises(BackendInstallError, match="exit code 1"):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)

    assert len(install_commands) == 2


def test_externally_managed_marker_matches_token_only() -> None:
    """Detection keys on pip's stable token, case-insensitively."""
    assert is_externally_managed_failure(["error: externally-managed-environment"])
    assert is_externally_managed_failure(["ERROR: EXTERNALLY-MANAGED-ENVIRONMENT"])
    assert not is_externally_managed_failure(["error: pip install failed"])
    assert not is_externally_managed_failure([])


def test_install_backend_reports_missing_git(monkeypatch: Any) -> None:
    """A missing git binary raises its OSError text verbatim."""

    def _no_git(command: list[str], on_line: Any, *, cwd: Any = None) -> int:
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _no_git)

    with pytest.raises(BackendInstallError, match="No such file or directory"):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)


def test_install_backend_reports_install_exit_code(monkeypatch: Any) -> None:
    """A failing install.py raises its exit code after streaming its lines."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch, calls, clone_results=[(0, [])], install_code=1, install_lines=["error: boom"]
    )
    seen: list[str] = []

    with pytest.raises(BackendInstallError, match="exit code 1"):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=seen.append)

    assert seen == ["error: boom"]


def test_install_backend_rejects_unverified_versions(monkeypatch: Any) -> None:
    """Exit code zero still fails closed without an exact version match."""
    calls: list[list[str]] = []
    _install_fake_stream(monkeypatch, calls, clone_results=[(0, []), (0, [])])
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.42")

    with pytest.raises(BackendInstallError, match=re.escape("26.9.42")):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)

    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: None)

    with pytest.raises(BackendInstallError, match=r"[Cc]ould not confirm"):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)
