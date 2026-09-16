"""Gettext catalog loading and translation helpers for Box RPG Maker."""

from __future__ import annotations

import gettext
import os
from collections.abc import Mapping
from pathlib import Path

_DOMAIN = "box-rpg-maker"
_LOCALE_DIRECTORY = Path(__file__).resolve().parent / "locale"
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


def _(message: str) -> str:
    """Return a translated user-facing message or its English msgid."""
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    """Return the translated plural form for one count."""
    return _translation.ngettext(singular, plural, count)


def _languages(environ: Mapping[str, str]) -> tuple[str, ...] | None:
    """Derive gettext language preferences from locale environment variables."""
    language = environ.get("LANGUAGE")
    if language:
        return tuple(value for value in language.split(":") if value)
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(name)
        if value:
            return (value,)
    return None
