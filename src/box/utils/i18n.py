"""Gettext catalog loading and translation helpers."""

from __future__ import annotations

import argparse
import gettext
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

_DOMAIN = "box"
_LOCALE_DIRECTORY = Path(__file__).resolve().parent.parent / "locale"
_translation: gettext.NullTranslations = gettext.NullTranslations()


def configure(environ: Mapping[str, str] | None = None) -> None:
    """Load the best available catalog from the process locale environment."""
    global _translation
    values = os.environ if environ is None else environ
    _translation = gettext.translation(
        _DOMAIN,
        localedir=_LOCALE_DIRECTORY,
        languages=_languages(values),
        fallback=True,
    )
    _argparse_messages()
    cast(Any, argparse)._ = _


def _(message: str) -> str:
    """Return a translated user-facing message or its English msgid."""
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    """Return the translated plural form for one count."""
    return _translation.ngettext(singular, plural, count)


def _languages(environ: Mapping[str, str]) -> tuple[str, ...] | None:
    language = environ.get("LANGUAGE")
    if language:
        return tuple(value for value in language.split(":") if value)
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(name)
        if value:
            return (value,)
    return None


def _argparse_messages() -> tuple[str, ...]:
    """Expose argparse's built-in UI messages to gettext extraction."""
    return (
        _("%(heading)s:"),
        _("usage: "),
        _(" (default: %(default)s)"),
        _("argument %(argument_name)s: %(message)s"),
        _("unknown parser %(parser_name)r (choices: %(choices)s)"),
        _("argument '-' with mode %r"),
        _("positional arguments"),
        _("options"),
        _("show this help message and exit"),
        _("show program's version number and exit"),
        _("subcommands"),
        _("unrecognized arguments: %s"),
        _("not allowed with argument %s"),
        _("ambiguous option: %(option)s could match %(matches)s"),
        _("ignored explicit argument %r"),
        _("the following arguments are required: %s"),
        _("one of the arguments %s is required"),
        _("expected one argument"),
        _("expected at most one argument"),
        _("expected at least one argument"),
        _("unexpected option string: %s"),
        _("invalid %(type)s value: %(value)r"),
        _("invalid choice: %(value)r (choose from %(choices)s)"),
        _("invalid choice: %(value)r, maybe you meant %(closest)r? (choose from %(choices)s)"),
        _("%(prog)s: error: %(message)s\n"),
        _("%(prog)s: warning: %(message)s\n"),
    )
