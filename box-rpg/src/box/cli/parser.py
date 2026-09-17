"""Argument parser construction."""

from __future__ import annotations

import argparse

from box.cli.version_info import version_text
from box.utils.i18n import _


class _AlignedHelpFormatter(argparse.HelpFormatter):
    """Start every help text in one wide column so long options stay inline."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=30)
        # Floor the measured width: argparse only derives the help column
        # from the longest option and never widens it on its own, so long
        # options such as --copy-root-file FILE would wrap without this.
        self._action_max_length = 28  # pyright: ignore[reportPrivateUsage]


def build_parser() -> argparse.ArgumentParser:
    """Build the box-rpg command tree."""
    parser = argparse.ArgumentParser(
        prog="box-rpg",
        formatter_class=_AlignedHelpFormatter,
        description=_("Launch RPG Maker games with managed NW.js or EasyRPG Player runtimes."),
    )
    parser.add_argument("--version", action="version", version=version_text())
    commands = parser.add_subparsers(dest="command")

    cleanup = commands.add_parser(
        "cleanup",
        formatter_class=_AlignedHelpFormatter,
        help=_("list or remove launcher-managed data"),
    )
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument(
        "--interactive", action="store_true", help=_("open the cleanup selection menu")
    )
    cleanup_mode.add_argument(
        "--yes", action="store_true", help=_("skip the global cleanup confirmation")
    )
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_command")
    cleanup_list = cleanup_commands.add_parser(
        "list",
        formatter_class=_AlignedHelpFormatter,
        help=_("list stable cleanup selectors"),
    )
    cleanup_list.add_argument(
        "category",
        nargs="?",
        choices=("roots", "runtimes", "downloads", "profiles"),
        metavar="CATEGORY",
        help=_("cleanup category to list"),
    )
    cleanup_remove = cleanup_commands.add_parser(
        "remove",
        formatter_class=_AlignedHelpFormatter,
        help=_("remove one managed cleanup item"),
    )
    cleanup_remove.add_argument(
        "category",
        choices=("roots", "runtimes", "downloads", "profiles"),
        metavar="CATEGORY",
        help=_("cleanup category to remove"),
    )
    cleanup_remove.add_argument(
        "selector", nargs="?", metavar="SELECTOR", help=_("cleanup selector to remove")
    )
    cleanup_remove.add_argument(
        "--all",
        dest="remove_all",
        action="store_true",
        help=_("remove all selectors in the category"),
    )
    cleanup_remove.add_argument(
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help=_("skip the removal confirmation"),
    )
    cleanup_all = cleanup_commands.add_parser(
        "all",
        formatter_class=_AlignedHelpFormatter,
        help=_("remove all managed cleanup data"),
    )
    cleanup_all.add_argument(
        "--yes",
        action="store_true",
        default=argparse.SUPPRESS,
        help=_("skip the cleanup confirmation"),
    )

    inspect = commands.add_parser(
        "inspect",
        formatter_class=_AlignedHelpFormatter,
        help=_("inspect a supported game without changing it"),
    )
    inspect.add_argument("game", metavar="GAME_DIR", type=str, help=_("game directory to inspect"))

    runtime = commands.add_parser(
        "runtime",
        formatter_class=_AlignedHelpFormatter,
        help=_("manage cached game runtimes"),
    )
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    nwjs = runtime_commands.add_parser(
        "nwjs", formatter_class=_AlignedHelpFormatter, help=_("manage NW.js runtimes")
    )
    nwjs_commands = nwjs.add_subparsers(dest="nwjs_command", required=True)
    nwjs_commands.add_parser(
        "list",
        formatter_class=_AlignedHelpFormatter,
        help=_("list installed NW.js runtimes"),
    )
    available = nwjs_commands.add_parser(
        "available",
        formatter_class=_AlignedHelpFormatter,
        help=_("list online NW.js versions"),
    )
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
        action = nwjs_commands.add_parser(
            name, formatter_class=_AlignedHelpFormatter, help=help_text
        )
        action.add_argument("version", metavar="VERSION", help=_("runtime version"))
        action.add_argument("--architecture", help=_("override the detected NW.js architecture"))
        action.add_argument("--sdk", action="store_true", help=_("use the NW.js SDK build"))

    easyrpg = runtime_commands.add_parser(
        "easyrpg",
        formatter_class=_AlignedHelpFormatter,
        help=_("manage EasyRPG Player x64 runtimes"),
    )
    easyrpg_commands = easyrpg.add_subparsers(dest="easyrpg_command", required=True)
    easyrpg_commands.add_parser(
        "list",
        formatter_class=_AlignedHelpFormatter,
        help=_("list installed EasyRPG Player runtimes"),
    )
    easyrpg_available = easyrpg_commands.add_parser(
        "available",
        formatter_class=_AlignedHelpFormatter,
        help=_("list online EasyRPG Player versions"),
    )
    easyrpg_available.add_argument("--page", type=int, default=1, help=_("online version page"))
    easyrpg_available.add_argument(
        "--interactive", action="store_true", help=_("select and install a version")
    )
    for name, help_text in (
        ("install", _("download and install an EasyRPG Player runtime")),
        ("remove", _("remove a managed EasyRPG Player runtime")),
    ):
        action = easyrpg_commands.add_parser(
            name, formatter_class=_AlignedHelpFormatter, help=help_text
        )
        action.add_argument("version", metavar="VERSION", help=_("runtime version"))

    launch = commands.add_parser(
        "launch",
        formatter_class=_AlignedHelpFormatter,
        help=_("launch an allowed game in an isolated session"),
    )
    launch.add_argument(
        "game",
        nargs="?",
        default=".",
        type=str,
        metavar="GAME_DIR",
        help=_("game directory to launch (default: current directory)"),
    )
    launch.add_argument(
        "--runtime",
        dest="runtime_version",
        metavar="VERSION",
        help=_("runtime version to use (NW.js or EasyRPG Player, autodetected)"),
    )
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
        "--x11",
        action="store_true",
        help=_("use the local X11 display for this launch only (weaker isolation)"),
    )
    launch.add_argument(
        "--gamemode",
        action="store_true",
        help=_("run the game with GameMode for this launch only"),
    )
    launch.add_argument(
        "--ci-mount",
        action="store_true",
        help=_("use a case-insensitive mount for this launch only"),
    )
    launch.add_argument(
        "--copy-root-file",
        action="append",
        default=[],
        metavar="FILE",
        help=_("copy a direct game-root file into the isolated NW.js session"),
    )

    config = commands.add_parser(
        "config",
        formatter_class=_AlignedHelpFormatter,
        help=_("show or update launcher configuration"),
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser(
        "show",
        formatter_class=_AlignedHelpFormatter,
        help=_("show current configuration"),
    )
    config_set = config_commands.add_parser(
        "set",
        formatter_class=_AlignedHelpFormatter,
        help=_("set an allowed game root or preferred runtime"),
    )
    config_set.add_argument(
        "key",
        metavar="KEY",
        help=_("configuration key: allowed-game-root or preferred-runtime"),
    )
    config_set.add_argument(
        "value",
        metavar="VALUE",
        help=_("directory for allowed-game-root, version or none for preferred-runtime"),
    )

    diagnose = commands.add_parser(
        "diagnose",
        formatter_class=_AlignedHelpFormatter,
        help=_("print a local diagnostic report"),
    )
    diagnose.add_argument(
        "game", metavar="GAME_DIR", type=str, help=_("game directory to diagnose")
    )
    diagnose.add_argument(
        "--runtime",
        dest="runtime_version",
        metavar="VERSION",
        help=_("runtime version to use (NW.js or EasyRPG Player, autodetected)"),
    )
    diagnose.add_argument("--sdk", action="store_true", help=_("use the NW.js SDK build"))
    return parser
