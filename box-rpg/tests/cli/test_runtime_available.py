"""Available-version listing tests for hide-installed filtering and sizes."""

from __future__ import annotations

from pathlib import Path

import pytest

from box.cli import runtime as runtime_cli
from box.errors import ConfigurationError
from box.errors import RuntimeError as BoxRuntimeError
from box.models import RuntimeInfo, RuntimeSpec
from box.paths import AppPaths
from box.runtime.available import AvailableVersions
from box.runtime.catalog import RuntimeCatalog
from box.runtime.easyrpg import AvailableEasyRPGVersions, EasyRPGCatalog, EasyRPGRuntime


def _paths(tmp_path: Path) -> AppPaths:
    """Build isolated launcher paths inside a temporary directory."""
    return AppPaths(config_root=tmp_path / "config", cache_root=tmp_path / "cache")


def _nwjs_runtime(version: str, architecture: str, sdk: bool, root: Path) -> RuntimeInfo:
    """Build one installed NW.js runtime model without touching the filesystem."""
    return RuntimeInfo(RuntimeSpec(version, architecture, sdk), root, root / "nw")


def test_nwjs_available_hides_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An installed NW.js spec disappears from the non-interactive listing."""
    paths = _paths(tmp_path)
    installed = (_nwjs_runtime("v0.90.0", "x64", False, tmp_path / "rt"),)

    def fake_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        assert (architecture, sdk) == ("x64", False)
        return AvailableVersions(page=page, versions=("v0.90.0", "v0.89.0"), sizes={})

    monkeypatch.setattr(runtime_cli, "fetch_available_versions", fake_fetch)

    def _fake_list_nwjs(catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
        return installed

    monkeypatch.setattr(runtime_cli, "api_list_nwjs", _fake_list_nwjs)

    assert runtime_cli.available(paths, 1, False, "x64", False) == 0

    output = capsys.readouterr().out
    assert "v0.90.0" not in output
    assert "v0.89.0" in output


def test_nwjs_available_keeps_same_version_different_arch_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same version with a different arch or SDK stays visible."""
    paths = _paths(tmp_path)
    installed = (_nwjs_runtime("v0.90.0", "x64", False, tmp_path / "rt"),)

    def _fake_list_nwjs(catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
        return installed

    monkeypatch.setattr(runtime_cli, "api_list_nwjs", _fake_list_nwjs)

    def fake_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        return AvailableVersions(page=page, versions=("v0.90.0",), sizes={})

    monkeypatch.setattr(runtime_cli, "fetch_available_versions", fake_fetch)

    assert runtime_cli.available(paths, 1, False, "ia32", False) == 0
    assert "v0.90.0" in capsys.readouterr().out

    assert runtime_cli.available(paths, 1, False, "x64", True) == 0
    assert "v0.90.0" in capsys.readouterr().out


def test_nwjs_available_shows_sizes_suffix_and_plain_for_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Known sizes render a suffix while unknown sizes stay plain."""
    paths = _paths(tmp_path)

    def _fake_list_nwjs(catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
        return ()

    monkeypatch.setattr(runtime_cli, "api_list_nwjs", _fake_list_nwjs)

    def fake_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        return AvailableVersions(
            page=page,
            versions=("v0.90.0", "v0.89.0", "v0.88.0"),
            sizes={"v0.90.0": 1500, "v0.89.0": None},
        )

    monkeypatch.setattr(runtime_cli, "fetch_available_versions", fake_fetch)

    assert runtime_cli.available(paths, 1, False, "x64", False) == 0

    output = capsys.readouterr().out
    assert "v0.90.0 (1.5 kB)" in output
    assert "  2. v0.89.0\n" in output
    assert "  3. v0.88.0\n" in output


def test_nwjs_available_passes_paths_to_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The NW.js listing fetch receives paths so the disk cache applies."""
    paths = _paths(tmp_path)
    seen: dict[str, object] = {}

    def _fake_list_nwjs(catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
        return ()

    monkeypatch.setattr(runtime_cli, "api_list_nwjs", _fake_list_nwjs)

    def fake_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        seen["paths"] = paths
        return AvailableVersions(page=page, versions=(), sizes={})

    monkeypatch.setattr(runtime_cli, "fetch_available_versions", fake_fetch)

    assert runtime_cli.available(paths, 1, False, "x64", False) == 0

    assert seen["paths"] is paths
    assert "(no stable versions on this page)" in capsys.readouterr().out


def test_nwjs_interactive_hides_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive selection skips the installed version when numbering choices."""
    paths = _paths(tmp_path)
    installed = (_nwjs_runtime("v0.90.0", "x64", False, tmp_path / "rt"),)
    output: list[str] = []
    chosen: list[tuple[str, str, bool]] = []

    def _fake_list_nwjs(catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
        return installed

    monkeypatch.setattr(runtime_cli, "api_list_nwjs", _fake_list_nwjs)

    def fake_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        return AvailableVersions(
            page=page, versions=("v0.90.0", "v0.89.0"), sizes={"v0.89.0": 2000}
        )

    def fake_install(actual_paths: AppPaths, version: str, architecture: str, sdk: bool) -> int:
        chosen.append((version, architecture, sdk))
        return 0

    monkeypatch.setattr(runtime_cli, "fetch_available_versions", fake_fetch)
    monkeypatch.setattr(runtime_cli, "install", fake_install)
    choices = iter(("1", "yes"))

    def _fake_read(prompt: str) -> str:
        return next(choices)

    result = runtime_cli.select_interactively(
        paths, 1, "x64", False, read=_fake_read, write=output.append
    )

    assert result == 0
    assert chosen == [("v0.89.0", "x64", False)]
    assert any("v0.89.0 (2.0 kB)" in line for line in output)
    assert not any("v0.90.0" in line for line in output)


def test_easyrpg_available_hides_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An installed EasyRPG version disappears from the listing."""
    paths = _paths(tmp_path)
    installed = (EasyRPGRuntime("0.8.1", tmp_path / "easy"),)

    def _fake_list_easyrpg(catalog: EasyRPGCatalog) -> tuple[EasyRPGRuntime, ...]:
        return installed

    monkeypatch.setattr(runtime_cli, "api_list_easyrpg", _fake_list_easyrpg)

    def fake_fetch(page: int, *, paths: AppPaths | None = None) -> AvailableEasyRPGVersions:
        return AvailableEasyRPGVersions(page=page, versions=("0.8.1", "0.8.0"), sizes={})

    monkeypatch.setattr(runtime_cli, "fetch_easyrpg_versions", fake_fetch)

    assert runtime_cli._easyrpg_available(paths, 1, False) == 0  # pyright: ignore[reportPrivateUsage]

    output = capsys.readouterr().out
    assert "0.8.1" not in output
    assert "0.8.0" in output


def test_easyrpg_available_shows_sizes_suffix_and_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """EasyRPG sizes render a suffix while unknown sizes stay plain."""
    paths = _paths(tmp_path)

    def _fake_list_easyrpg(catalog: EasyRPGCatalog) -> tuple[EasyRPGRuntime, ...]:
        return ()

    monkeypatch.setattr(runtime_cli, "api_list_easyrpg", _fake_list_easyrpg)

    def fake_fetch(page: int, *, paths: AppPaths | None = None) -> AvailableEasyRPGVersions:
        return AvailableEasyRPGVersions(
            page=page, versions=("0.8.1", "0.8.0"), sizes={"0.8.1": 2500000}
        )

    monkeypatch.setattr(runtime_cli, "fetch_easyrpg_versions", fake_fetch)

    assert runtime_cli._easyrpg_available(paths, 1, False) == 0  # pyright: ignore[reportPrivateUsage]

    output = capsys.readouterr().out
    assert "0.8.1 (2.5 MB)" in output
    assert "  2. 0.8.0\n" in output


def test_easyrpg_available_passes_paths_to_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The EasyRPG listing fetch receives paths so the disk cache applies."""
    paths = _paths(tmp_path)
    seen: dict[str, object] = {}

    def _fake_list_easyrpg(catalog: EasyRPGCatalog) -> tuple[EasyRPGRuntime, ...]:
        return ()

    monkeypatch.setattr(runtime_cli, "api_list_easyrpg", _fake_list_easyrpg)

    def fake_fetch(page: int, *, paths: AppPaths | None = None) -> AvailableEasyRPGVersions:
        seen["paths"] = paths
        return AvailableEasyRPGVersions(page=page, versions=(), sizes={})

    monkeypatch.setattr(runtime_cli, "fetch_easyrpg_versions", fake_fetch)

    assert runtime_cli._easyrpg_available(paths, 1, False) == 0  # pyright: ignore[reportPrivateUsage]

    assert seen["paths"] is paths
    assert "(no versions on this page)" in capsys.readouterr().out


def test_easyrpg_non_interactive_forwards_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The non-interactive EasyRPG listing honors a custom write callable."""
    paths = _paths(tmp_path)

    def _fake_list_easyrpg(catalog: EasyRPGCatalog) -> tuple[EasyRPGRuntime, ...]:
        return ()

    monkeypatch.setattr(runtime_cli, "api_list_easyrpg", _fake_list_easyrpg)

    def fake_fetch(page: int, *, paths: AppPaths | None = None) -> AvailableEasyRPGVersions:
        return AvailableEasyRPGVersions(page=page, versions=("0.8.0",), sizes={})

    monkeypatch.setattr(runtime_cli, "fetch_easyrpg_versions", fake_fetch)
    output: list[str] = []

    assert (
        runtime_cli._easyrpg_available(  # pyright: ignore[reportPrivateUsage]
            paths, 1, False, write=output.append
        )
        == 0
    )

    assert any("0.8.0" in line for line in output)
    assert capsys.readouterr().out == ""


def test_nwjs_interactive_prompt_uses_filtered_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NW.js prompt range reflects the filtered count, not a hardcoded 1-10."""
    paths = _paths(tmp_path)
    installed = (_nwjs_runtime("v0.90.0", "x64", False, tmp_path / "rt"),)
    prompts: list[str] = []

    def _fake_list_nwjs(catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
        return installed

    monkeypatch.setattr(runtime_cli, "api_list_nwjs", _fake_list_nwjs)

    def fake_fetch(
        page: int, architecture: str, sdk: bool, *, paths: AppPaths | None = None
    ) -> AvailableVersions:
        return AvailableVersions(page=page, versions=("v0.90.0", "v0.89.0", "v0.88.0"))

    monkeypatch.setattr(runtime_cli, "fetch_available_versions", fake_fetch)

    def _fake_read(prompt: str) -> str:
        prompts.append(prompt)
        return "q"

    assert (
        runtime_cli.select_interactively(
            paths, 1, "x64", False, read=_fake_read, write=lambda _: None
        )
        == 0
    )

    assert prompts
    assert any("1-2" in prompt for prompt in prompts)
    assert not any("1-10" in prompt for prompt in prompts)


def test_easyrpg_interactive_prompt_uses_filtered_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EasyRPG prompt range reflects the filtered count, not a hardcoded 1-10."""
    paths = _paths(tmp_path)
    installed = (EasyRPGRuntime("0.8.1", tmp_path / "easy"),)
    prompts: list[str] = []

    def _fake_list_easyrpg(catalog: EasyRPGCatalog) -> tuple[EasyRPGRuntime, ...]:
        return installed

    monkeypatch.setattr(runtime_cli, "api_list_easyrpg", _fake_list_easyrpg)

    def fake_fetch(page: int, *, paths: AppPaths | None = None) -> AvailableEasyRPGVersions:
        return AvailableEasyRPGVersions(page=page, versions=("0.8.1", "0.8.0", "0.7.0"))

    monkeypatch.setattr(runtime_cli, "fetch_easyrpg_versions", fake_fetch)

    def _fake_read(prompt: str) -> str:
        prompts.append(prompt)
        return "q"

    assert (
        runtime_cli._easyrpg_available(  # pyright: ignore[reportPrivateUsage]
            paths, 1, True, read=_fake_read, write=lambda _: None
        )
        == 0
    )

    assert prompts
    assert any("1-2" in prompt for prompt in prompts)
    assert not any("1-10" in prompt for prompt in prompts)


def test_display_version_uses_translatable_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size suffix goes through _ with version/size placeholders."""
    seen: dict[str, str] = {}

    def _fake_gettext(message: str) -> str:
        seen["msgid"] = message
        return "X:{version}:Y:{size}:Z"

    monkeypatch.setattr(runtime_cli, "_", _fake_gettext)

    def _fake_format_size(size: int) -> str:
        assert size == 1500
        return "1.5 kB"

    monkeypatch.setattr(runtime_cli, "format_size_decimal", _fake_format_size)

    assert runtime_cli._display_version("v0.90.0", {"v0.90.0": 1500}) == "X:v0.90.0:Y:1.5 kB:Z"  # pyright: ignore[reportPrivateUsage]
    assert seen["msgid"] == "{version} ({size})"
    assert runtime_cli._display_version("v0.89.0", {}) == "v0.89.0"  # pyright: ignore[reportPrivateUsage]


def test_installed_specs_narrow_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only expected backend errors are swallowed when listing installed runtimes."""
    paths = _paths(tmp_path)
    for error in (ConfigurationError("bad"), OSError("io"), BoxRuntimeError("rt")):

        def _failing(
            _catalog: RuntimeCatalog, _error: BaseException = error
        ) -> tuple[RuntimeInfo, ...]:
            raise _error

        monkeypatch.setattr(runtime_cli, "api_list_nwjs", _failing)
        assert runtime_cli._installed_nwjs_specs(paths) == frozenset()  # pyright: ignore[reportPrivateUsage]

        def _failing_easy(
            _catalog: EasyRPGCatalog, _error: BaseException = error
        ) -> tuple[EasyRPGRuntime, ...]:
            raise _error

        monkeypatch.setattr(runtime_cli, "api_list_easyrpg", _failing_easy)
        assert runtime_cli._installed_easyrpg_keys(paths) == frozenset()  # pyright: ignore[reportPrivateUsage]

    def _unexpected(_catalog: RuntimeCatalog) -> tuple[RuntimeInfo, ...]:
        raise ValueError("boom")

    monkeypatch.setattr(runtime_cli, "api_list_nwjs", _unexpected)
    try:
        runtime_cli._installed_nwjs_specs(paths)  # pyright: ignore[reportPrivateUsage]
    except ValueError:
        pass
    else:
        raise AssertionError("unexpected errors must propagate")
