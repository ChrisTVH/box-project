"""Argument parser construction."""

from __future__ import annotations

import argparse

from box import __version__
from box.utils.i18n import _


def build_parser() -> argparse.ArgumentParser:
    """Build the box-rpg command tree."""
    parser = argparse.ArgumentParser(
        prog="box-rpg",
        description=_("Launch RPG Maker games with managed NW.js or EasyRPG Player runtimes."),
    )
    parser.add_argument("--version", action="version", version=f"box-rpg {__version__}")
    commands = parser.add_subparsers(dest="command")

    cleanup = commands.add_parser("cleanup", help=_("list or remove launcher-managed data"))
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument(
        "--interactive", action="store_true", help=_("open the cleanup selection menu")
    )
    cleanup_mode.add_argument(
        "--yes", action="store_true", help=_("skip the global cleanup confirmation")
    )
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_command")
    cleanup_list = cleanup_commands.add_parser("list", help=_("list stable cleanup selectors"))
    cleanup_list.add_argument(
        "category", nargs="?", choices=("roots", "runtimes", "downloads", "profiles")
    )
    cleanup_remove = cleanup_commands.add_parser(
        "remove", help=_("remove one managed cleanup item")
    )
    cleanup_remove.add_argument("category", choices=("roots", "runtimes", "downloads", "profiles"))
    cleanup_remove.add_argument("selector", nargs="?")
    cleanup_remove.add_argument("--all", dest="remove_all", action="store_true")
    cleanup_remove.add_argument(
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help=_("skip the removal confirmation"),
    )
    cleanup_all = cleanup_commands.add_parser("all", help=_("remove all managed cleanup data"))
    cleanup_all.add_argument(
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help=_("skip the cleanup confirmation"),
    )

    inspect = commands.add_parser("inspect", help=_("inspect a supported game without changing it"))
    inspect.add_argument("game", type=str)

    runtime = commands.add_parser("runtime", help=_("manage cached game runtimes"))
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    nwjs = runtime_commands.add_parser("nwjs", help=_("manage NW.js runtimes"))
    nwjs_commands = nwjs.add_subparsers(dest="nwjs_command", required=True)
    nwjs_commands.add_parser("list", help=_("list installed NW.js runtimes"))
    available = nwjs_commands.add_parser("available", help=_("list online NW.js versions"))
    available.add_argument(
        "--page", type=int, default=1, help=_("online version page (ten versions)")
    )
    available.add_argument(
        "--interactive", action="store_true", help=_("select and install a version")
    )
    available.add_argument("--architecture", help=_("override the detected NW.js architecture"))
    available.add_argument("--sdk", action="store_true", help=_("use the NW.js SDK build"))
    for name, help_text in (
        ("install", _("download and install a runtime")),
        ("remove", _("remove a managed runtime")),
    ):
        action = nwjs_commands.add_parser(name, help=help_text)
        action.add_argument("version")
        action.add_argument("--architecture")
        action.add_argument("--sdk", action="store_true", help=_("use the NW.js SDK build"))

    easyrpg = runtime_commands.add_parser("easyrpg", help=_("manage EasyRPG Player x64 runtimes"))
    easyrpg_commands = easyrpg.add_subparsers(dest="easyrpg_command", required=True)
    easyrpg_commands.add_parser("list", help=_("list installed EasyRPG Player runtimes"))
    easyrpg_available = easyrpg_commands.add_parser(
        "available", help=_("list online EasyRPG Player versions")
    )
    easyrpg_available.add_argument("--page", type=int, default=1, help=_("online version page"))
    easyrpg_available.add_argument(
        "--interactive", action="store_true", help=_("select and install a version")
    )
    for name, help_text in (
        ("install", _("download and install an EasyRPG Player runtime")),
        ("remove", _("remove a managed EasyRPG Player runtime")),
    ):
        action = easyrpg_commands.add_parser(name, help=help_text)
        action.add_argument("version")

    launch = commands.add_parser("launch", help=_("launch an allowed game in an isolated session"))
    launch.add_argument("game", nargs="?", default=".", type=str)
    launch.add_argument("--runtime", dest="runtime_version")
    launch.add_argument("--sdk", action="store_true", help=_("use the NW.js SDK build"))
    launch.add_argument(
        "--allow-network",
        action="store_true",
        help=_("allow host network access for this launch only (including local services)"),
    )
    launch.add_argument(
        "--allow-game-writes",
        action="store_true",
        help=_("allow game directory writes for this launch only"),
    )
    launch.add_argument(
        "--copy-root-file",
        action="append",
        default=[],
        metavar="FILE",
        help=_("copy a direct game-root file into the isolated NW.js session"),
    )

    config = commands.add_parser("config", help=_("show or update launcher configuration"))
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help=_("show current configuration"))
    config_set = config_commands.add_parser(
        "set", help=_("set an allowed game root or preferred runtime")
    )
    config_set.add_argument("key")
    config_set.add_argument("value")

    diagnose = commands.add_parser("diagnose", help=_("print a local diagnostic report"))
    diagnose.add_argument("game", type=str)
    diagnose.add_argument("--runtime", dest="runtime_version")
    diagnose.add_argument("--sdk", action="store_true", help=_("use the NW.js SDK build"))
    return parser
