# pyright: reportPrivateUsage=false
"""Keep launch shell completions aligned with the argument parser."""

import argparse
import re
from pathlib import Path

import pytest

from box.cli.parser import build_parser

COMPLETIONS_DIR = Path(__file__).resolve().parents[2] / "res" / "completions"
FLAG_RE = re.compile(r"--[a-z0-9][a-z0-9-]*")


def _launch_flags() -> set[str]:
    """Collect long options declared by the launch subcommand."""
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            raw_choices = action.choices  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            assert isinstance(raw_choices, dict)
            launch_candidate: object = raw_choices.get("launch")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            assert isinstance(launch_candidate, argparse.ArgumentParser)
            launch = launch_candidate
            flags: set[str] = set()
            for sub in launch._actions:
                for option in sub.option_strings:
                    if option.startswith("--"):
                        flags.add(option)
            return flags
    raise AssertionError("launch subcommand missing")


def _bash_launch_flags(text: str) -> set[str]:
    """Extract flags from the first launch compgen list in bash completion."""
    start = text.index("launch)")
    match = re.search(r"compgen -W '([^']+)'", text[start:])
    assert match is not None, "launch completion missing in bash source"
    return set(FLAG_RE.findall(match.group(1)))


def _fish_launch_flags(text: str) -> set[str]:
    """Extract launch flags from fish lines whose condition mentions launch."""
    flags: set[str] = set()
    for line in text.splitlines():
        condition = re.search(r"-n '([^']+)'", line)
        option = re.search(r"-l ([\w-]+)", line)
        if condition is None or option is None:
            continue
        if "launch" not in condition.group(1).split():
            continue
        flags.add(f"--{option.group(1)}")
    assert flags, "launch completion missing in fish source"
    return flags


def _zsh_launch_flags(text: str) -> set[str]:
    """Extract flags from the launch _arguments line in the zsh completion."""
    for line in text.splitlines():
        if line.strip().startswith("launch)"):
            return set(FLAG_RE.findall(line))
    raise AssertionError("launch completion missing in zsh source")


@pytest.mark.parametrize("name", ["box-rpg.bash", "box-rpg.fish", "_box-rpg"])
def test_launch_completion_matches_parser(name: str) -> None:
    """Every launch flag must be completed and no unknown flag advertised."""
    expected = _launch_flags()
    text = (COMPLETIONS_DIR / name).read_text(encoding="utf-8")
    if name == "box-rpg.bash":
        advertised = _bash_launch_flags(text)
    elif name == "box-rpg.fish":
        advertised = _fish_launch_flags(text)
    else:
        advertised = _zsh_launch_flags(text)
    assert expected <= advertised, f"{name} missing {sorted(expected - advertised)}"
    assert advertised <= expected, f"{name} advertises unknown {sorted(advertised - expected)}"


@pytest.mark.parametrize("name", ["box-rpg.bash", "box-rpg.fish", "_box-rpg"])
def test_positional_path_completion(name: str) -> None:
    """launch/diagnose complete game paths and config set completes its keys."""
    text = (COMPLETIONS_DIR / name).read_text(encoding="utf-8")
    if name == "box-rpg.bash":
        assert "compgen -f" in text, "bash offers no path completion"
        assert "allowed-game-root preferred-runtime" in text
    elif name == "box-rpg.fish":
        assert "__fish_complete_path" in text, "fish offers no path completion"
        assert "allowed-game-root preferred-runtime" in text
    else:
        assert "_files" in text, "zsh offers no path completion"
        assert "allowed-game-root preferred-runtime" in text
