from pathlib import Path

import pytest

from box.engines.registry import EngineRegistry, default_registry
from box.errors import GameValidationError
from box.games.detector import detect_game
from box.models import EngineName, GameInfo


def _write_file(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_detect_game_recognizes_rpg_maker_mv_export(tmp_path: Path) -> None:
    game_root = tmp_path / "mv-game"
    _write_file(game_root / "www" / "index.html", "<html></html>")
    _write_file(game_root / "www" / "js" / "plugins.js", "var $plugins = [];")
    _write_file(game_root / "package.json", '{"name": "MV Game"}')

    game = detect_game(game_root, default_registry())

    assert game.engine is EngineName.RPG_MAKER_MV
    assert game.root == game_root
    assert game.entrypoint == game_root / "www" / "index.html"
    assert game.manifest == game_root / "package.json"


def test_detect_game_recognizes_rpg_maker_mz_export(tmp_path: Path) -> None:
    game_root = tmp_path / "mz-game"
    _write_file(game_root / "index.html", "<html></html>")
    _write_file(game_root / "js" / "plugins.js", "var $plugins = [];")
    _write_file(game_root / "js" / "rmmz_core.js", "void 0;")
    _write_file(game_root / "package.json", '{"name": "MZ Game"}')
    (game_root / "data").mkdir()

    game = detect_game(game_root, default_registry())

    assert game.engine is EngineName.RPG_MAKER_MZ
    assert game.root == game_root
    assert game.entrypoint == game_root / "index.html"
    assert game.manifest == game_root / "package.json"


def test_detect_game_recognizes_rpg_maker_2000_2003_project(tmp_path: Path) -> None:
    game_root = tmp_path / "rpg-rt-game"
    for filename in ("RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt"):
        _write_file(game_root / filename)

    game = detect_game(game_root, default_registry())

    assert game.engine is EngineName.RPG_MAKER_2000_2003
    assert game.root == game_root
    assert game.entrypoint is None
    assert game.manifest is None


@pytest.mark.parametrize("missing", ("RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt"))
def test_detect_game_rejects_incomplete_rpg_rt_project(tmp_path: Path, missing: str) -> None:
    game_root = tmp_path / "rpg-rt-game"
    for filename in {"RPG_RT.ini", "RPG_RT.ldb", "RPG_RT.lmt"} - {missing}:
        _write_file(game_root / filename)

    with pytest.raises(GameValidationError, match="unsupported game"):
        detect_game(game_root, default_registry())


def test_detect_game_rejects_export_with_missing_entrypoint(tmp_path: Path) -> None:
    game_root = tmp_path / "incomplete-game"
    _write_file(game_root / "js" / "plugins.js", "var $plugins = [];")
    _write_file(game_root / "js" / "rmmz_core.js", "void 0;")
    _write_file(game_root / "package.json", '{"name": "Incomplete Game"}')
    (game_root / "data").mkdir()

    with pytest.raises(GameValidationError, match="unsupported game"):
        detect_game(game_root, default_registry())


def test_detect_game_rejects_game_artifacts_reached_through_symlinks(tmp_path: Path) -> None:
    game_root = tmp_path / "mz-game"
    outside = tmp_path / "outside-index.html"
    outside.write_text("<html></html>", encoding="utf-8")
    game_root.mkdir()
    (game_root / "index.html").symlink_to(outside)
    _write_file(game_root / "js" / "plugins.js", "var $plugins = [];")
    _write_file(game_root / "js" / "rmmz_core.js", "void 0;")
    _write_file(game_root / "package.json", '{"name": "MZ Game"}')
    (game_root / "data").mkdir()

    with pytest.raises(GameValidationError, match="unsupported game"):
        detect_game(game_root, default_registry())


def test_detect_game_converts_a_racing_game_directory_error_to_validation_error(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()

    class RacingAdapter:
        name = EngineName.RPG_MAKER_MZ

        def detect(self, root: Path) -> GameInfo | None:
            del root
            raise FileNotFoundError("game disappeared")

    with pytest.raises(GameValidationError, match="cannot inspect game path"):
        detect_game(game_root, EngineRegistry((RacingAdapter(),)))
