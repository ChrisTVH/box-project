"""Application version and author metadata for footer displays."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata, version

__all__ = ["get_app_author", "get_app_version", "get_embedded_tag", "get_expected_backend_version"]

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


def get_embedded_tag() -> str | None:
    """Return the AppImage build tag embedded at packaging time, if any.

    Reads the tag through ``box_gui.appimage_tag`` (environment first,
    then the staged tag file). Any failure means "no embedded tag", never
    a crash: dev checkouts simply have nothing embedded.
    """
    try:
        from box_gui.appimage_tag import get_appimage_tag
    except ImportError:
        return None
    try:
        tag = get_appimage_tag()
    except Exception:
        return None
    if not tag:
        return None
    return tag


def get_expected_backend_version() -> str:
    """Return the box-rpg version the frontend expects to find installed.

    The embedded AppImage tag wins when present so an AppImage gates on
    the exact backend it was built against. Otherwise the frontend
    distribution version doubles as the expectation: both projects share
    the aligned year.month.commit-count scheme and release together, so a
    dev checkout expects the matching backend checkout.
    """
    return get_embedded_tag() or get_app_version()
