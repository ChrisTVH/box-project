"""Application version and author metadata for footer displays."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata, version

__all__ = ["get_app_author", "get_app_version"]

_DISTRIBUTION_NAME = "box-rpg-maker"
_FALLBACK_AUTHOR = "ChrisTVH"


def get_app_version() -> str:
    """Return the installed distribution version.

    Reads the ``box-rpg-maker`` distribution metadata so the footer always
    matches the installed package. Falls back to ``box_gui.__version__``
    when the distribution is not installed (editable checkouts, tests).
    """
    try:
        app_version = version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        from box_gui import __version__

        return __version__
    if not app_version:
        from box_gui import __version__

        return __version__
    return app_version


def get_app_author() -> str:
    """Return the distribution author.

    Reads the ``Author`` field of the ``box-rpg-maker`` distribution
    metadata. Falls back to ``ChrisTVH`` when the distribution is missing
    or the field is empty.
    """
    try:
        author = metadata(_DISTRIBUTION_NAME).get("Author")
    except PackageNotFoundError:
        return _FALLBACK_AUTHOR
    if not author:
        return _FALLBACK_AUTHOR
    return author
