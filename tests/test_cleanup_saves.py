"""Durable sandbox saves are never owned by cache/profile cleanup."""

import subprocess
from pathlib import Path

import pytest

from box.cli.cleanup import execute
from box.config.repository import ConfigRepository
from box.games.identity import game_id
from box.launch.sandbox import Sandbox, clean_environment
from box.models import EngineName, GameInfo
from box.paths import AppPaths, open_directory_without_symlinks


@pytest.mark.parametrize("action", ["profile", "profiles", "all"])
@pytest.mark.parametrize(
    "engine", [EngineName.RPG_MAKER_MV, EngineName.RPG_MAKER_MZ, EngineName.RPG_MAKER_2000_2003]
)
@pytest.mark.parametrize("writes", [False, True])
def test_real_sandbox_saves_survive_profile_and_global_cleanup(
    tmp_path: Path, action: str, engine: EngineName, writes: bool
) -> None:
    paths = AppPaths.from_environment()
    root = tmp_path / "game"
    root.mkdir()
    entrypoint = None
    if engine is not EngineName.RPG_MAKER_2000_2003:
        entrypoint = root / "www/index.html"
        entrypoint.parent.mkdir()
        entrypoint.write_text("fixture")
    game = GameInfo(engine, root, entrypoint)
    identifier = game_id(root)
    repository = ConfigRepository(paths)
    repository.add_allowed_root(root)
    profile = paths.profiles_root / identifier
    saves = (entrypoint.parent if entrypoint else root) / "save"
    saves.mkdir()
    (saves / "previous").write_bytes(b"previous save")

    for launch in range(2):
        with Sandbox(allow_game_writes=writes) as sandbox:
            sandbox.persistence(paths, game)
            if entrypoint is None and writes:
                # game_writable takes ownership; never pre-keep the same descriptor.
                descriptor = open_directory_without_symlinks(root)
                save_descriptor = sandbox.game_saves(game, descriptor)
                sandbox.game_writable(descriptor)
                sandbox.bind(save_descriptor, "/game/save", writable=True)
            else:
                descriptor = sandbox.keep(open_directory_without_symlinks(root))
                save_descriptor = sandbox.game_saves(game, descriptor)
                if entrypoint is None:
                    sandbox.bind(descriptor, "/game")
                    sandbox.bind(save_descriptor, "/game/save", writable=True)
                else:
                    sandbox.nw_game(game, descriptor, save_descriptor)
            script = (
                "from pathlib import Path\n"
                "assert Path('/saves/previous').read_bytes() == b'previous save'\n"
                f"if {launch} == 0:\n"
                "    Path('/saves/slot').write_bytes(b'irreplaceable save')\n"
                "    Path('/profile/settings').write_text('disposable profile')\n"
                "else:\n"
                "    assert Path('/saves/slot').read_bytes() == b'irreplaceable save'\n"
                "    assert not Path('/profile/settings').exists()\n"
            )
            result = subprocess.run(
                sandbox.command(["/usr/bin/python3", "-I", "-S", "-c", script]),
                pass_fds=sandbox.pass_fds,
                env=clean_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        assert result.returncode == 0, result.stderr
        assert (saves / "slot").read_bytes() == b"irreplaceable save"
        assert not saves.is_relative_to(paths.cache_root)
        assert not (profile / "saves").exists()
        if launch == 0:
            assert (profile / "sandbox/settings").is_file()
            if action == "all":
                status = execute(paths, repository, command="all", yes=True, has_tty=False)
            else:
                status = execute(
                    paths,
                    repository,
                    command="remove",
                    category="profiles",
                    selector=identifier if action == "profile" else None,
                    remove_all=action == "profiles",
                    yes=True,
                    has_tty=False,
                )
            assert status == 0
            assert not profile.exists()
            assert (saves / "slot").read_bytes() == b"irreplaceable save"
    assert (saves / "previous").read_bytes() == b"previous save"
    assert not (tmp_path / "xdg-data").exists()
