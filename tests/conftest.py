from pathlib import Path

import pytest

from box.utils.i18n import configure


@pytest.fixture(autouse=True)
def isolate_xdg_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test isolated from the developer's real configuration, cache and data."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("LANGUAGE", "en")
    configure({"LANGUAGE": "en"})
