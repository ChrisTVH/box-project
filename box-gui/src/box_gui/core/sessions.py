"""Advisory running-session probes against the stable box.api surface.

The backend may or may not track live launch sessions. Sessions are
keyed by the stable game identifier from ``box.games.identity`` plus a
backend-owned session name. Every helper here degrades gracefully when
the installed box-rpg predates the session APIs: unknown backends report
no live sessions, and stopping raises a friendly error instead of
crashing. Nothing here touches Gtk or Adw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from box.errors import LaunchError
from box.paths import AppPaths

from box_gui.i18n import _

__all__ = [
    "game_identifier",
    "is_session_running",
    "live_session_names",
    "poll_session_status",
    "session_key_for_entry",
    "stop_session",
]


def session_key_for_entry(entry: Any) -> Any:
    """Return the single-key session lookup for one library entry.

    Entries carrying a session id use it; otherwise the game path keys
    the probe. The backend decides what the key means.
    """
    session_id = getattr(entry, "session_id", None)
    if session_id is not None:
        return session_id
    return entry.path


def game_identifier(game_path: Path | str) -> str | None:
    """Return the stable backend identifier for one game path, if available.

    None means the probe cannot run: an old backend without the identity
    helper, or a folder that no longer resolves (ghosts never probe).
    """
    try:
        from box.games.identity import game_id
    except ImportError:
        return None
    try:
        return game_id(Path(game_path))
    except Exception:
        return None


def _launch_api() -> Any | None:
    """Return the backend launch module, or None when it cannot import."""
    try:
        from box.api import launch as launch_api
    except ImportError:
        return None
    return launch_api


def live_session_names(paths: AppPaths | None, entry: Any) -> tuple[str, ...]:
    """Return live backend session names for one entry, or an empty tuple.

    Old backends without the listing probe, missing folders, and any
    probe failure all report no live sessions: the badges are advisory
    and must never block the library.
    """
    if paths is None:
        return ()
    launch_api = _launch_api()
    if launch_api is None:
        return ()
    finder = getattr(launch_api, "find_live_sessions", None)
    if not callable(finder):
        return ()
    identifier = game_identifier(entry.path)
    if identifier is None:
        return ()
    try:
        names: Any = finder(paths, identifier)
    except Exception:
        return ()
    if names is None:
        return ()
    try:
        return tuple(str(name) for name in names)
    except Exception:
        return ()


def is_session_running(paths: AppPaths | None, entry: Any) -> bool:
    """Return True when the backend reports a live session for one entry.

    Entries carrying a session id try the single-key ``is_session_running``
    probe first; anything else (and old backends) falls back to the
    identifier-scoped live-session listing. Any failure means not running.
    """
    session_id = getattr(entry, "session_id", None)
    if session_id is not None:
        launch_api = _launch_api()
        probe = getattr(launch_api, "is_session_running", None) if launch_api else None
        if callable(probe):
            try:
                return bool(probe(session_id))
            except TypeError:
                pass
            except Exception:
                return False
    return bool(live_session_names(paths, entry))


def poll_session_status(paths: AppPaths | None, entry: Any, name: str) -> int | None:
    """Return the exit code for one session, or None while it runs.

    Missing, oversized, corrupt, or still-running status documents report
    None, as do old backends and any probe failure.
    """
    if paths is None:
        return None
    launch_api = _launch_api()
    if launch_api is None:
        return None
    poll = getattr(launch_api, "poll_launch_status", None)
    if not callable(poll):
        return None
    identifier = game_identifier(entry.path)
    if identifier is None:
        return None
    try:
        code = poll(paths, identifier, name)
    except Exception:
        return None
    return code if type(code) is int else None


def stop_session(paths: AppPaths | None, entry: Any, name: str | None = None) -> None:
    """Stop one named session, or every live session for one entry.

    Old box-rpg releases lack ``stop_session``: raise LaunchError instead
    of pretending the session stopped. With no live session to stop, raise
    LaunchError as well so callers can tell nothing happened. Raising here
    routes through run_in_thread into on_error as usual.
    """
    launch_api = _launch_api()
    stop = getattr(launch_api, "stop_session", None) if launch_api else None
    if not callable(stop):
        raise LaunchError(_("Stopping is not supported by the installed backend."))
    identifier: str | None = None
    if paths is not None:
        identifier = game_identifier(entry.path)
    names = (name,) if name is not None else live_session_names(paths, entry)
    if identifier is None or not names:
        raise LaunchError(_("There is no running session to stop."))
    for session_name in names:
        stop(paths, identifier, session_name)
