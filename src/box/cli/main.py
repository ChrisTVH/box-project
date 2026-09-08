"""box-rpg command-line entry point."""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

from box.cli import cleanup as cleanup_command
from box.cli import config as config_command
from box.cli import diagnose as diagnose_command
from box.cli import inspect as inspect_command
from box.cli import launch as launch_command
from box.cli import runtime as runtime_command
from box.cli.parser import build_parser
from box.config.repository import ConfigRepository
from box.engines.registry import default_registry
from box.errors import BoxError, GameValidationError
from box.games.detector import detect_game
from box.paths import AppPaths
from box.runtime.catalog import RuntimeCatalog
from box.runtime.platform import current_architecture, normalize_architecture


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and return a process exit status."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        try:
            detect_game(Path("."), default_registry())
        except GameValidationError:
            print(
                "error: no RPG Maker MV/MZ game was found here; run box-rpg from the game directory.",
                file=sys.stderr,
            )
            parser.print_help()
            return 1
        arguments = Namespace(command="launch", game=".", runtime_version=None, sdk=False)
    try:
        return _dispatch(arguments)
    except (BoxError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(arguments: Namespace) -> int:
    """Dispatch one parsed subcommand to its isolated implementation module."""
    paths = AppPaths.from_environment()
    paths.ensure()
    repository = ConfigRepository(paths)
    if arguments.command == "cleanup":
        return cleanup_command.execute(paths, repository, interactive=sys.stdin.isatty())
    if arguments.command == "inspect":
        return inspect_command.execute(Path(arguments.game))
    if arguments.command == "runtime":
        catalog = RuntimeCatalog(paths)
        if arguments.runtime_command == "list":
            return runtime_command.list_runtimes(catalog)
        architecture = normalize_architecture(arguments.architecture or current_architecture())
        if arguments.runtime_command == "available":
            return runtime_command.available(
                paths,
                arguments.page,
                arguments.interactive,
                architecture,
                arguments.sdk,
            )
        if arguments.runtime_command == "install":
            return runtime_command.install(paths, arguments.version, architecture, arguments.sdk)
        return runtime_command.remove(catalog, arguments.version, architecture, arguments.sdk)
    if arguments.command == "launch":
        return launch_command.execute(
            paths,
            repository,
            Path(arguments.game),
            arguments.runtime_version,
            arguments.sdk,
        )
    if arguments.command == "config":
        if arguments.config_command == "show":
            return config_command.show(repository)
        return config_command.set_value(repository, arguments.key, arguments.value)
    if arguments.command == "diagnose":
        return diagnose_command.execute(
            paths,
            Path(arguments.game),
            arguments.runtime_version,
            arguments.sdk,
        )
    raise ValueError(f"unsupported command: {arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())
