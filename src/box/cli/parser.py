"""Argument parser construction."""

from __future__ import annotations

import argparse

from box import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the box-rpg command tree."""
    parser = argparse.ArgumentParser(
        prog="box-rpg",
        description="Launch RPG Maker games with managed NW.js or EasyRPG Player runtimes.",
    )
    parser.add_argument("--version", action="version", version=f"box-rpg {__version__}")
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("cleanup", help="interactively remove launcher-managed data")

    inspect = commands.add_parser("inspect", help="inspect a supported game without changing it")
    inspect.add_argument("game", type=str)

    runtime = commands.add_parser("runtime", help="manage cached game runtimes")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_commands.add_parser("list", help="list installed runtimes")
    available = runtime_commands.add_parser("available", help="list online NW.js versions")
    available.add_argument(
        "--page", type=int, default=1, help="online version page (five versions)"
    )
    available.add_argument(
        "--interactive", action="store_true", help="select and install a version"
    )
    available.add_argument("--architecture", help="override the detected NW.js architecture")
    available.add_argument("--sdk", action="store_true", help="use the NW.js SDK build")
    for name, help_text in (
        ("install", "download and install a runtime"),
        ("remove", "remove a managed runtime"),
    ):
        action = runtime_commands.add_parser(name, help=help_text)
        action.add_argument("version")
        action.add_argument("--architecture")
        action.add_argument("--sdk", action="store_true", help="use the NW.js SDK build")

    easyrpg = runtime_commands.add_parser("easyrpg", help="manage EasyRPG Player x64 runtimes")
    easyrpg_commands = easyrpg.add_subparsers(dest="easyrpg_command", required=True)
    easyrpg_commands.add_parser("list", help="list installed EasyRPG Player runtimes")
    easyrpg_available = easyrpg_commands.add_parser(
        "available", help="list online EasyRPG Player versions"
    )
    easyrpg_available.add_argument("--page", type=int, default=1, help="online version page")
    easyrpg_available.add_argument(
        "--interactive", action="store_true", help="select and install a version"
    )
    for name, help_text in (
        ("install", "download and install an EasyRPG Player runtime"),
        ("remove", "remove a managed EasyRPG Player runtime"),
    ):
        action = easyrpg_commands.add_parser(name, help=help_text)
        action.add_argument("version")

    launch = commands.add_parser("launch", help="launch an allowed game in an isolated session")
    launch.add_argument("game", nargs="?", default=".", type=str)
    launch.add_argument("--runtime", dest="runtime_version")
    launch.add_argument("--sdk", action="store_true", help="use the NW.js SDK build")
    launch.add_argument(
        "--game-cwd",
        action="store_true",
        help="run NW.js with the game root as its working directory",
    )
    launch.add_argument(
        "--copy-root-file",
        action="append",
        default=[],
        metavar="FILE",
        help="copy a direct game-root file into the isolated NW.js session",
    )

    config = commands.add_parser("config", help="show or update launcher configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="show current configuration")
    config_set = config_commands.add_parser(
        "set", help="set an allowed game root or preferred runtime"
    )
    config_set.add_argument("key")
    config_set.add_argument("value")

    diagnose = commands.add_parser("diagnose", help="print a local diagnostic report")
    diagnose.add_argument("game", type=str)
    diagnose.add_argument("--runtime", dest="runtime_version")
    diagnose.add_argument("--sdk", action="store_true", help="use the NW.js SDK build")
    return parser
