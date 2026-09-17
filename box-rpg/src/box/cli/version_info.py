"""Version banner for --version, aligned with the GUI library footer."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, metadata, version

from box import __version__
from box.utils.i18n import _

__all__ = ["version_text"]

_DISTRIBUTION_NAME = "box-rpg"
_FALLBACK_AUTHOR = "ChrisTVH"
_REPOSITORY_URL = "https://gitlab.com/christvh/box-project"


def version_text() -> str:
    """Return the --version banner with version, author, and repository link.

    Reads the installed ``box-rpg`` distribution metadata so the banner
    matches the installed package, falling back to ``box.__version__``
    and the known author when the distribution is missing (checkouts).
    The repository URL stays literal through a ``{url}`` placeholder.
    """
    try:
        app_version = version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        app_version = ""
    if not app_version:
        app_version = __version__
    try:
        author = metadata(_DISTRIBUTION_NAME).get("Author")
    except PackageNotFoundError:
        author = ""
    if not author:
        author = _FALLBACK_AUTHOR
    return _("box-rpg {version} by {author} ({url})").format(
        version=app_version, author=author, url=_REPOSITORY_URL
    )
