"""Tests for the Python-pure AppImage builder (Fase 1 + Fase 2)."""

import io
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import install
import pytest

# The builder lives in the monorepo tools/ directory, which shares its
# package name with this test package (box-rpg/tests/tools). Prefer the
# real tools/ directory so `import build_appimage` never resolves here.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))

import appimage_tag
import build_appimage

# Tracks the aligned repo versions: main() compares tags against the live
# checkout, so a hardcoded tag rots on every release-alignment commit.
TAG = build_appimage.gui_repo_version()


def test_reader_contract_constants_match() -> None:
    """The builder and the fallback reader must share one contract."""
    assert build_appimage.APPIMAGE_TAG_ENV_VAR == appimage_tag.APPIMAGE_TAG_ENV_VAR
    assert build_appimage.APPIMAGE_TAG_FILENAME == appimage_tag.APPIMAGE_TAG_FILENAME
    assert build_appimage.APPIMAGE_TAG_ENV_VAR == "BOX_RPG_MAKER_APPIMAGE_TAG"
    assert build_appimage.APPIMAGE_TAG_FILENAME == "appimage_tag.txt"


def test_is_valid_tag_accepts_only_version_scheme() -> None:
    assert appimage_tag.is_valid_tag("26.9.43")
    assert not appimage_tag.is_valid_tag("v26.9.43")
    assert not appimage_tag.is_valid_tag("26.9")
    assert not appimage_tag.is_valid_tag("latest")
    assert not appimage_tag.is_valid_tag("26.9.43\n")


def test_repo_versions_match_install_helpers() -> None:
    """The builder reuses install.py version semantics without GUI imports."""
    assert build_appimage.repo_version() == install.repo_version()
    assert build_appimage.gui_repo_version() == install.gui_repo_version()
    assert build_appimage.gui_repo_version() != "unknown"


def test_expected_version_counts_month_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(args: list[str]) -> str:
        assert args[0] == "log"
        assert args[1].startswith("--since=2026-09-01")
        return "aaa one\nbbb two\n"

    monkeypatch.setattr(build_appimage, "_run_git", fake_git)

    assert build_appimage.expected_version(datetime(2026, 9, 17)) == "26.9.2"


def test_stage_appdir_layout(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"

    build_appimage.stage_appdir(appdir, TAG)

    payload = appdir / "usr/share/box-rpg-maker"
    staged_gui = payload / "src" / "box_gui"
    assert (staged_gui / "app.py").is_file()
    assert (staged_gui / appimage_tag.APPIMAGE_TAG_FILENAME).read_text() == TAG + "\n"
    assert list(staged_gui.glob("locale/**/box-rpg-maker.mo")), "locale catalogs ship along"
    assert not (payload / "src" / "box").exists(), "backend must never be staged"
    assert not list(payload.rglob("*.whl"))

    apprun = appdir / "AppRun"
    assert apprun.is_file() and os.access(apprun, os.X_OK)
    assert apprun.read_text().splitlines()[0] == "#!/usr/bin/python3"

    desktop = appdir / "io.gitlab.christvh.BoxRpgApp.desktop"
    content = desktop.read_text()
    assert "Exec=box-rpg-maker" in content
    assert "Icon=io.gitlab.christvh.BoxRpgApp" in content
    assert f"X-AppImage-Version={TAG}" in content
    assert (
        appdir / "usr/share/applications/io.gitlab.christvh.BoxRpgApp.desktop"
    ).read_text() == content

    icons = list((appdir / "usr/share/icons/hicolor/scalable/apps").glob("*.svg"))
    assert icons, "expected staged GUI icons"
    assert (appdir / "io.gitlab.christvh.BoxRpgApp.svg").is_file()
    assert (appdir / ".DirIcon").is_file()
    assert (payload / "res/io.gitlab.christvh.BoxRpgApp.desktop").is_file()


def test_stage_appdir_refuses_existing_or_malformed_tag(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    appdir.mkdir()

    with pytest.raises(FileExistsError):
        build_appimage.stage_appdir(appdir, TAG)
    with pytest.raises(RuntimeError, match="malformed tag"):
        build_appimage.stage_appdir(tmp_path / "other", "latest")


def test_reject_backend_payload_fails_closed(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / "src" / "box").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="backend"):
        build_appimage._reject_backend_payload(payload)


def test_apprun_source_runs_on_system_python_with_baked_tag() -> None:
    source = build_appimage.apprun_source(TAG)

    assert f'BUILD_TAG = "{TAG}"' in source
    assert appimage_tag.APPIMAGE_TAG_ENV_VAR in source
    assert "from box_gui.app import main" in source
    assert "import box.api" not in source
    assert "sys.version_info < (3, 14)" in source
    assert "staged payload missing" in source
    assert "staged frontend failed to import" in source
    assert "venv" not in source


def test_ensure_on_tag_creates_missing_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    head = "abc123"

    def fake_git(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["rev-parse", "--verify"]:
            raise RuntimeError("no such tag")
        if args == ["rev-parse", "HEAD"]:
            return head
        if args[0] == "tag":
            return ""
        if args[0] == "describe":
            return TAG
        if args[:2] == ["rev-list", "-n"]:
            return head
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(build_appimage, "_run_git", fake_git)

    build_appimage.ensure_on_tag(TAG, create=True)

    assert ["tag", "-a", TAG, "-m", f"box-rpg-maker {TAG}"] in calls


def test_ensure_on_tag_refuses_a_moved_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_git(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["rev-parse", "--verify"]:
            return "tag-sha"
        if args[:2] == ["rev-list", "-n"]:
            return "other-commit"
        if args == ["rev-parse", "HEAD"]:
            return "head-commit"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(build_appimage, "_run_git", fake_git)

    with pytest.raises(RuntimeError, match="another commit"):
        build_appimage.ensure_on_tag(TAG, create=True)
    assert all(call[0] != "tag" for call in calls)


def test_ensure_on_tag_requires_exact_describe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(args: list[str]) -> str:
        if args[:2] == ["rev-parse", "--verify"]:
            return "tag-sha"
        if args == ["rev-parse", "HEAD"] or args[:2] == ["rev-list", "-n"]:
            return "head-commit"
        if args[0] == "describe":
            return "26.9.42"
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(build_appimage, "_run_git", fake_git)

    with pytest.raises(RuntimeError, match="not exactly at tag"):
        build_appimage.ensure_on_tag(TAG, create=True)


def _mock_build_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_appimage, "working_tree_clean", lambda: True)
    monkeypatch.setattr(build_appimage, "describe_head", lambda: TAG)


def test_main_check_passes_without_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_build_prerequisites(monkeypatch)
    monkeypatch.setattr(build_appimage.sys, "argv", ["build_appimage.py", "--check"])

    def forbidden(tag: str, *, create: bool) -> None:
        raise AssertionError("check mode must not create tags")

    monkeypatch.setattr(build_appimage, "ensure_on_tag", forbidden)

    assert build_appimage.main() == 0
    assert "Check passed" in capsys.readouterr().out


def test_main_print_tag_resolves_without_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--print-tag reports the tag without touching git state or disk."""
    monkeypatch.setattr(build_appimage.sys, "argv", ["build_appimage.py", "--print-tag"])

    assert build_appimage.main() == 0
    assert capsys.readouterr().out.strip() == build_appimage.expected_version()


def test_ensure_on_tag_without_creation_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local test builds embed without creating or verifying any tag."""

    def forbidden(args: list[str]) -> str:
        raise AssertionError(f"must not call git, got {args}")

    monkeypatch.setattr(build_appimage, "_run_git", forbidden)

    assert build_appimage.ensure_on_tag(TAG, create=False) is None


def test_main_refuses_misaligned_versions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_appimage, "repo_version", lambda: "0.0.0")
    monkeypatch.setattr(build_appimage.sys, "argv", ["build_appimage.py", "--tag", TAG])

    assert build_appimage.main() == 1
    assert "not aligned" in capsys.readouterr().err


def test_main_refuses_dirty_tree(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_appimage, "working_tree_clean", lambda: False)
    monkeypatch.setattr(build_appimage.sys, "argv", ["build_appimage.py", "--tag", TAG])

    assert build_appimage.main() == 1
    assert "uncommitted changes" in capsys.readouterr().err


def test_get_appimage_tag_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(appimage_tag.APPIMAGE_TAG_ENV_VAR, "  26.9.43  ")

    assert appimage_tag.get_appimage_tag() == "26.9.43"


def test_get_appimage_tag_reads_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(appimage_tag.APPIMAGE_TAG_ENV_VAR, raising=False)
    tag_file = tmp_path / appimage_tag.APPIMAGE_TAG_FILENAME
    tag_file.write_text(TAG + "\n")

    assert appimage_tag.get_appimage_tag(tmp_path) == TAG
    assert appimage_tag.get_appimage_tag(tag_file) == TAG


def test_get_appimage_tag_returns_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(appimage_tag.APPIMAGE_TAG_ENV_VAR, raising=False)

    assert appimage_tag.get_appimage_tag() is None
    assert appimage_tag.get_appimage_tag(tmp_path) is None
    assert appimage_tag.read_tag_file(tmp_path / "missing.txt") is None


def test_ensure_appimagetool_rejects_unusable_binary(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not executable"):
        build_appimage.ensure_appimagetool(
            tmp_path / "missing", build_appimage.APPIMAGETOOL_URL, tmp_path
        )
    plain = tmp_path / "plain"
    plain.write_bytes(b"not executable")
    with pytest.raises(RuntimeError, match="not executable"):
        build_appimage.ensure_appimagetool(plain, build_appimage.APPIMAGETOOL_URL, tmp_path)


def test_download_appimagetool_rejects_truncated_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        build_appimage.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"tiny")
    )

    with pytest.raises(RuntimeError, match="truncated"):
        build_appimage.download_appimagetool("https://example.invalid/tool", tmp_path / "tool")


def test_extract_appimagetool_reports_missing_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_appimage, "run", lambda args, cwd=None: 0)

    with pytest.raises(RuntimeError, match="missing its AppRun"):
        build_appimage.extract_appimagetool(tmp_path / "tool", tmp_path)


def test_build_artifact_runs_the_tool_and_marks_the_output(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    runner.write_text('#!/bin/sh\necho "artifact" > "$2"\n')
    runner.chmod(0o755)
    output = tmp_path / "dist" / "box-rpg-maker.appimage"

    result = build_appimage.build_artifact(runner, tmp_path / "AppDir", output)

    assert result == output
    assert output.is_file() and os.access(output, os.X_OK)


def test_build_artifact_fails_closed_on_tool_error(tmp_path: Path) -> None:
    runner = tmp_path / "runner"
    runner.write_text("#!/bin/sh\nexit 1\n")
    runner.chmod(0o755)

    with pytest.raises(RuntimeError, match="failed to build"):
        build_appimage.build_artifact(runner, tmp_path / "AppDir", tmp_path / "out.appimage")


def _load_apprun_namespace() -> dict[str, Any]:
    """Exec the generated AppRun in isolation for re-exec tests."""
    source = build_appimage.apprun_source(TAG)
    namespace: dict[str, Any] = {
        "__name__": "apprun_under_test",
        "__file__": "/fake/AppRun",
    }
    exec(source, namespace)
    return namespace


class _ExecCalled(Exception):
    """Sentinel standing in for the noreturn os.execve in tests."""


def test_apprun_reexec_picks_suitable_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first suitable python is execed once, after earlier ones fail."""
    namespace = _load_apprun_namespace()
    monkeypatch.delenv("BOX_RPG_MAKER_PYTHON", raising=False)
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")

    def fake_which(name: str) -> str | None:
        assert name == "python3"
        return "/opt/python3"

    monkeypatch.setattr(shutil, "which", fake_which)
    probed: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        probed.append(args[0])
        code = 0 if args[0] == "/usr/local/bin/python3" else 1
        return subprocess.CompletedProcess(args, code, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        exec_calls.append((path, list(argv)))
        raise _ExecCalled

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(sys, "argv", ["AppRun", "--foo"])

    with pytest.raises(_ExecCalled):
        namespace["_reexec_with_suitable_python"]()

    assert probed == ["/opt/python3", "/usr/local/bin/python3"]
    assert exec_calls == [("/usr/local/bin/python3", ["/usr/local/bin/python3", "AppRun", "--foo"])]


def test_apprun_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env override is probed before PATH and the well-known paths."""
    namespace = _load_apprun_namespace()
    monkeypatch.setenv("BOX_RPG_MAKER_PYTHON", "/usr/local/bin/python3.14")
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")

    def fake_which(name: str) -> str | None:
        assert name == "python3"
        return "/opt/other/python3"

    monkeypatch.setattr(shutil, "which", fake_which)
    probed: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        probed.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        exec_calls.append((path, list(argv)))
        assert env.get("BOX_RPG_MAKER_REEXECED") == "1"
        raise _ExecCalled

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(sys, "argv", ["AppRun"])

    with pytest.raises(_ExecCalled):
        namespace["_reexec_with_suitable_python"]()

    assert probed == ["/usr/local/bin/python3.14"]
    assert exec_calls == [
        (
            "/usr/local/bin/python3.14",
            ["/usr/local/bin/python3.14", "AppRun"],
        )
    ]


def test_apprun_exec_failure_falls_through_to_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError at exec time moves on to the next suitable candidate."""
    namespace = _load_apprun_namespace()
    monkeypatch.delenv("BOX_RPG_MAKER_PYTHON", raising=False)
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")
    monkeypatch.setattr(sys, "argv", ["AppRun"])
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/python3")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("timeout") == 10
        assert kwargs.get("stdin") is subprocess.DEVNULL
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    exec_calls: list[str] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        exec_calls.append(path)
        if len(exec_calls) == 1:
            raise OSError("busy")
        raise _ExecCalled

    monkeypatch.setattr(os, "execve", fake_execve)

    with pytest.raises(_ExecCalled):
        namespace["_reexec_with_suitable_python"]()

    assert exec_calls == ["/opt/python3", "/usr/local/bin/python3"]


def test_apprun_reexec_marker_stops_second_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A preset marker returns quietly without probing or execing anything."""
    namespace = _load_apprun_namespace()
    monkeypatch.setenv("BOX_RPG_MAKER_REEXECED", "1")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")

    def forbidden_which(name: str) -> str | None:
        raise AssertionError("must not look up interpreters")

    def forbidden_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not probe interpreters")

    def forbidden_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise AssertionError("must not exec")

    monkeypatch.setattr(shutil, "which", forbidden_which)
    monkeypatch.setattr(subprocess, "run", forbidden_run)
    monkeypatch.setattr(os, "execve", forbidden_execve)

    namespace["_reexec_with_suitable_python"]()


def test_apprun_relative_override_and_current_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative override is ignored and the running interpreter is skipped."""
    namespace = _load_apprun_namespace()
    monkeypatch.setenv("BOX_RPG_MAKER_PYTHON", "python3.14")
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/python3")
    monkeypatch.setattr(sys, "argv", ["AppRun"])
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/python3")
    probed: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        probed.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise _ExecCalled

    monkeypatch.setattr(os, "execve", fake_execve)

    with pytest.raises(_ExecCalled):
        namespace["_reexec_with_suitable_python"]()
    assert probed == ["/usr/local/bin/python3"]


def test_apprun_main_reexecs_before_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """On an old interpreter, main() re-execs instead of failing directly."""
    namespace = _load_apprun_namespace()
    monkeypatch.delenv("BOX_RPG_MAKER_PYTHON", raising=False)
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")

    def fake_which(name: str) -> str | None:
        assert name == "python3"
        return "/opt/python3"

    monkeypatch.setattr(shutil, "which", fake_which)

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise _ExecCalled

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(sys, "version_info", (3, 13))
    monkeypatch.setattr(sys, "argv", ["AppRun"])

    with pytest.raises(_ExecCalled):
        namespace["main"]()


def test_apprun_nogui_reports_bindings_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Runners without GTK bindings get the PyGObject hint, not the version one."""
    namespace = _load_apprun_namespace()
    monkeypatch.delenv("BOX_RPG_MAKER_PYTHON", raising=False)
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")

    def fake_which(name: str) -> str | None:
        assert name == "python3"
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        exec_calls.append((path, list(argv)))

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(sys, "version_info", (3, 13))
    monkeypatch.setattr(sys, "argv", ["AppRun"])

    assert namespace["main"]() == 1
    err = capsys.readouterr().err
    assert "No Python 3.14 with GTK bindings found" in err
    assert "BOX_RPG_MAKER_PYTHON" in err
    assert exec_calls == []


def test_apprun_old_gui_keeps_version_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bindings on old interpreters only keep the plain version message."""
    namespace = _load_apprun_namespace()
    monkeypatch.delenv("BOX_RPG_MAKER_PYTHON", raising=False)
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")
    monkeypatch.setattr(shutil, "which", lambda name: None)

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 2, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def forbidden_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise AssertionError("must not exec without an ok candidate")

    monkeypatch.setattr(os, "execve", forbidden_execve)
    monkeypatch.setattr(sys, "version_info", (3, 13))
    monkeypatch.setattr(sys, "argv", ["AppRun"])

    assert namespace["main"]() == 1
    assert "Python 3.14+ required" in capsys.readouterr().err


def test_apprun_mixed_old_and_nogui_prefers_bindings_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 3.14 without bindings beats an old interpreter with them in the hint."""
    namespace = _load_apprun_namespace()
    monkeypatch.delenv("BOX_RPG_MAKER_PYTHON", raising=False)
    monkeypatch.delenv("BOX_RPG_MAKER_REEXECED", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3.13-test")
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/old-python3")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        code = 2 if args[0] == "/opt/old-python3" else 1
        return subprocess.CompletedProcess(args, code, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def forbidden_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        raise AssertionError("must not exec without an ok candidate")

    monkeypatch.setattr(os, "execve", forbidden_execve)
    monkeypatch.setattr(sys, "version_info", (3, 13))
    monkeypatch.setattr(sys, "argv", ["AppRun"])

    assert namespace["main"]() == 1
    assert "No Python 3.14 with GTK bindings found" in capsys.readouterr().err


def test_apprun_probe_classifies_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit codes and failures map to ok, old, nogui, and unusable."""
    namespace = _load_apprun_namespace()
    probe = namespace["_probe_python"]

    def run_with(code: int | None) -> str:
        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if code is None:
                raise OSError("gone")
            return subprocess.CompletedProcess(args, code, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return probe("/opt/python3")

    assert run_with(0) == "ok"
    assert run_with(2) == "old"
    assert run_with(1) == "nogui"
    assert run_with(None) == "unusable"

    def timeout_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, 10)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    assert probe("/opt/python3") == "unusable"
