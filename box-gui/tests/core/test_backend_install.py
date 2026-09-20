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
    resolve_expected_commit,
    resolve_tag_commit,
    run_command_streaming,
    user_site_problems,
    verify_clone,
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
    rev_commit: str = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
    rev_code: int = 0,
    verify_code: int = 0,
    verify_lines: list[str] | None = None,
) -> None:
    """Serve scripted clone/install/verify runs; clones materialize install.py."""

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        calls.append(list(command))
        if len(command) >= 2 and command[0] == "git" and command[1] == "clone":
            code, output = clone_results.pop(0)
            for line in output:
                on_line(line)
            if code == 0:
                destination = Path(command[-1])
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "install.py").write_text("# fake\n", encoding="utf-8")
            return code
        if len(command) >= 2 and command[0] == "git" and command[1] == "rev-parse":
            if rev_code == 0:
                on_line(rev_commit)
            return rev_code
        if len(command) >= 2 and command[0] == "git" and command[1] == "verify-tag":
            for line in verify_lines or []:
                on_line(line)
            return verify_code
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
    remotes = [call[6] for call in calls if len(call) > 6 and call[:2] == ["git", "clone"]]
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

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        if len(command) >= 2 and command[:2] == ["git", "clone"]:
            destination = Path(command[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "install.py").write_text("# fake\n", encoding="utf-8")
            return 0
        if len(command) >= 2 and command[:2] == ["git", "rev-parse"]:
            on_line("9f86d081884c7d659a2feaa0c55ad015a3bf4f1b")
            return 0
        if len(command) >= 2 and command[:2] == ["git", "verify-tag"]:
            return 0
        install_commands.append(list(command))
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

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        if len(command) >= 2 and command[:2] == ["git", "clone"]:
            destination = Path(command[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "install.py").write_text("# fake\n", encoding="utf-8")
            return 0
        if len(command) >= 2 and command[:2] == ["git", "rev-parse"]:
            on_line("9f86d081884c7d659a2feaa0c55ad015a3bf4f1b")
            return 0
        if len(command) >= 2 and command[:2] == ["git", "verify-tag"]:
            return 0
        install_commands.append(list(command))
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


def test_user_site_problems_accepts_clean_tree(tmp_path: Path) -> None:
    """A user-owned tree without group/other write passes the probe."""
    home = tmp_path / "home"
    site_packages = home / ".local" / "lib" / "site-packages"
    site_packages.mkdir(parents=True)

    assert user_site_problems(home, site_packages) == ()


def test_user_site_problems_ignores_missing_tail(tmp_path: Path) -> None:
    """Missing directories are fine; pip creates them itself."""
    home = tmp_path / "home"
    home.mkdir()

    assert user_site_problems(home, home / ".local" / "lib" / "site-packages") == ()


def test_user_site_problems_flags_group_writable_ancestor(tmp_path: Path) -> None:
    """One group-writable ancestor fails the probe with its path."""
    home = tmp_path / "home"
    site_packages = home / ".local" / "lib" / "site-packages"
    site_packages.mkdir(parents=True)
    (home / ".local").chmod(0o775)

    assert user_site_problems(home, site_packages) == (str(home / ".local"),)


def test_user_site_problems_refuses_outside_home(tmp_path: Path) -> None:
    """A site outside the home directory is refused entirely."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert user_site_problems(home, outside) == (str(outside),)


def test_install_backend_prefights_user_site(monkeypatch: Any, tmp_path: Path) -> None:
    """A bad user site fails before any clone attempt."""
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o775)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        backend_install_module.site,
        "getusersitepackages",
        lambda: str(home / ".local" / "lib" / "site-packages"),
    )

    def _no_clone(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        raise AssertionError(f"must not run anything, got {command[0]}")

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _no_clone)

    with pytest.raises(BackendInstallError, match="user site"):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)


def test_install_backend_reports_missing_git(monkeypatch: Any) -> None:
    """A missing git binary raises its OSError text verbatim."""

    def _no_git(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
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


_PIN_COMMIT = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"


def test_install_backend_matching_pin_passes(monkeypatch: Any) -> None:
    """A clone resolving to the pinned commit proceeds to install."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch,
        calls,
        clone_results=[(0, [])],
        rev_commit=_PIN_COMMIT,
        verify_code=0,
    )
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.43")

    outcome = install_backend(
        "26.9.43",
        python="/usr/bin/python3",
        on_line=lambda _l: None,
        expected_commit=_PIN_COMMIT,
    )

    assert outcome == InstallOutcome(tag="26.9.43", installed_version="26.9.43")
    rev_parse = next(call for call in calls if call[:2] == ["git", "rev-parse"])
    assert rev_parse == ["git", "rev-parse", "26.9.43^{commit}"]
    assert any(call[:2] == ["git", "verify-tag"] for call in calls)
    assert any(call and call[0] == "/usr/bin/python3" for call in calls)


def test_install_backend_mismatched_pin_fails_closed(monkeypatch: Any) -> None:
    """A tag resolving away from the pin refuses to install (mirror drift)."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch,
        calls,
        clone_results=[(0, [])],
        rev_commit=_PIN_COMMIT,
        verify_code=0,
    )
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.43")

    with pytest.raises(BackendInstallError, match=r"mirror drift|tag moved"):
        install_backend(
            "26.9.43",
            python="/usr/bin/python3",
            on_line=lambda _l: None,
            expected_commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    assert not any(call and call[0] == "/usr/bin/python3" for call in calls)


def test_install_backend_unsigned_tag_continues(monkeypatch: Any) -> None:
    """An unsigned transitional tag records a status note and still installs."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch,
        calls,
        clone_results=[(0, [])],
        rev_commit=_PIN_COMMIT,
        verify_code=1,
        verify_lines=["error: no signature found for '26.9.43'"],
    )
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.43")
    seen_status: list[str] = []

    outcome = install_backend(
        "26.9.43",
        python="/usr/bin/python3",
        on_line=lambda _l: None,
        on_status=seen_status.append,
    )

    assert outcome.installed_version == "26.9.43"
    assert any("not signed" in message for message in seen_status)
    assert any(call and call[0] == "/usr/bin/python3" for call in calls)


def test_install_backend_bad_signature_fails_closed(monkeypatch: Any) -> None:
    """A BAD tag signature fails closed before install.py runs."""
    calls: list[list[str]] = []
    _install_fake_stream(
        monkeypatch,
        calls,
        clone_results=[(0, [])],
        rev_commit=_PIN_COMMIT,
        verify_code=1,
        verify_lines=["gpg: BAD signature from 'Box Project <box@example.com>'"],
    )
    monkeypatch.setattr(backend_install_module, "query_installed_version", lambda _py: "26.9.43")

    with pytest.raises(BackendInstallError, match=r"[Ss]ignature"):
        install_backend("26.9.43", python="/usr/bin/python3", on_line=lambda _l: None)

    assert not any(call and call[0] == "/usr/bin/python3" for call in calls)


def test_verify_clone_returns_resolved_commit(tmp_path: Path, monkeypatch: Any) -> None:
    """verify_clone returns the rev-parse hash and checks the pin verbatim."""
    repo = tmp_path / "repo"
    repo.mkdir()
    seen: list[list[str]] = []

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        seen.append(list(command))
        assert cwd == repo
        assert timeout is not None
        if command[:2] == ["git", "rev-parse"]:
            assert command == ["git", "rev-parse", "26.9.43^{commit}"]
            on_line(_PIN_COMMIT)
            return 0
        assert command == ["git", "verify-tag", "26.9.43"]
        return 0

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)

    assert verify_clone(repo, "26.9.43", _PIN_COMMIT) == _PIN_COMMIT
    with pytest.raises(BackendInstallError, match=r"mirror drift|tag moved"):
        verify_clone(repo, "26.9.43", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")


def test_verify_clone_rejects_non_hex_commit(tmp_path: Path, monkeypatch: Any) -> None:
    """A non-hex rev-parse output fails closed before any pin comparison."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        if command[:2] == ["git", "rev-parse"]:
            on_line("not-a-commit-hash")
            return 0
        return 0

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)

    with pytest.raises(BackendInstallError, match="invalid commit hash"):
        verify_clone(repo, "26.9.43", None)


def test_verify_clone_rejects_short_hash(tmp_path: Path, monkeypatch: Any) -> None:
    """Short hashes fail closed: only 40 or 64 hex chars count."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        if command[:2] == ["git", "rev-parse"]:
            on_line("9f86d081884c")
            return 0
        return 0

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)

    with pytest.raises(BackendInstallError, match="invalid commit hash"):
        verify_clone(repo, "26.9.43", None)


def test_verify_clone_accepts_sha256_commit(tmp_path: Path, monkeypatch: Any) -> None:
    """A 64-char SHA-256 hash verifies like a 40-char SHA-1 hash."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sha256 = "a" * 64

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        if command[:2] == ["git", "rev-parse"]:
            on_line(sha256)
            return 0
        assert command == ["git", "verify-tag", "26.9.43"]
        return 0

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)

    assert verify_clone(repo, "26.9.43", sha256) == sha256


def test_verify_clone_rejects_lightweight_tag(tmp_path: Path, monkeypatch: Any) -> None:
    """A lightweight tag fails closed instead of taking the unsigned path."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        if command[:2] == ["git", "rev-parse"]:
            on_line(_PIN_COMMIT)
            return 0
        on_line("error: tag '26.9.43' is not an annotated tag")
        return 1

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)

    with pytest.raises(BackendInstallError, match="not an annotated tag"):
        verify_clone(repo, "26.9.43", None)


def test_verify_clone_rejects_non_tag_object(tmp_path: Path, monkeypatch: Any) -> None:
    """A non-tag verify-tag error fails closed, never continues."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _fake_run(command: list[str], on_line: Any, *, cwd: Any = None, timeout: Any = None) -> int:
        if command[:2] == ["git", "rev-parse"]:
            on_line(_PIN_COMMIT)
            return 0
        on_line("error: 26.9.43: not a tag object")
        return 128

    monkeypatch.setattr(backend_install_module, "run_command_streaming", _fake_run)

    with pytest.raises(BackendInstallError, match=r"not an annotated tag|Could not verify"):
        verify_clone(repo, "26.9.43", None)


def test_resolve_tag_commit_prefers_peeled_line(monkeypatch: Any) -> None:
    """The peeled ^{} line wins for annotated tags."""
    tag = "26.9.43"
    peeled = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    plain = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def _fake_run(command: list[str], **kwargs: Any) -> Any:
        # No tag patterns: patterns suppress the peeled ^{} line on real
        # forges, so the full advertisement is filtered locally instead.
        assert command == ["git", "ls-remote", "--tags", "https://example.invalid/repo"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                f"{plain}\trefs/tags/{tag}\n"
                f"{peeled}\trefs/tags/{tag}^{{}}\n"
                "dddddddddddddddddddddddddddddddddddddddd\trefs/tags/26.9.42\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(backend_install_module.subprocess, "run", _fake_run)

    assert resolve_tag_commit("https://example.invalid/repo", tag) == peeled


def test_resolve_tag_commit_returns_none_without_tag(monkeypatch: Any) -> None:
    """Missing tags and non-zero exits resolve as None, not an error."""
    monkeypatch.setattr(
        backend_install_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
    )
    assert resolve_tag_commit("https://example.invalid/repo", "26.9.99") is None

    monkeypatch.setattr(
        backend_install_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr="x"),
    )
    assert resolve_tag_commit("https://example.invalid/repo", "26.9.43") is None


def test_resolve_expected_commit_tries_remotes_in_order(monkeypatch: Any) -> None:
    """The first remote with a valid commit wins; failures fall through."""
    seen: list[str] = []
    commits = {
        "https://gitlab.com/christvh/box-project": None,
        "https://github.com/ChrisTVH/box-project": "cccccccccccccccccccccccccccccccccccccccc",
    }

    def _fake_resolve(url: str, tag: str, *, timeout: float = 15.0) -> str | None:
        seen.append(url)
        if url == "https://gitlab.com/christvh/box-project":
            raise OSError("offline")
        return commits[url]

    monkeypatch.setattr(backend_install_module, "resolve_tag_commit", _fake_resolve)

    assert (
        resolve_expected_commit("26.9.43", backend_install_module.CLONE_URLS)
        == "cccccccccccccccccccccccccccccccccccccccc"
    )
    assert seen[0] == "https://gitlab.com/christvh/box-project"
