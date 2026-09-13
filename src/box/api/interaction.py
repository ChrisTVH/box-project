"""Terminal interaction replacing direct prompts for later reuse."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from box.errors import GameValidationError
from box.utils.i18n import _
from box.utils.terminal import abbreviate_prompt_path, safe_terminal_text

__all__ = ["ConsoleInteraction", "Interaction"]


class Interaction(Protocol):
    """GUI callbacks replacing terminal prompts.

    Kind values for choose_runtime are "nwjs" and "easyrpg". Candidates are
    version strings newest-first. Title is a preformatted header line the
    frontend shows verbatim (terminal writes it, GTK uses it as dialog
    title). The returned value is the zero-based index into candidates,
    or None to cancel. Denial is False/None; EOFError is also treated as
    cancellation by callers.
    """

    def confirm_x11(self, display: str) -> bool:
        """Return True to expose the X11 socket for display, else deny."""
        ...

    def confirm_add_root(self, path: Path) -> bool:
        """Return True to store path as an allowed game root, else deny."""
        ...

    def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
        """Return the selected candidate index, or None to cancel."""
        ...


def _write_stderr(message: str) -> None:
    """Print one line to standard error."""
    print(message, file=sys.stderr)


class ConsoleInteraction:
    """Terminal implementation of Interaction with injectable I/O.

    The caller decides whether interaction is allowed; there is no
    sys.stdin.isatty check here. Denial is reported as False or None so
    callers can raise GameValidationError exactly like box.cli.launch.
    End of input raises GameValidationError with the same message the
    caller would use for a denial.
    """

    def __init__(
        self,
        read: Callable[[str], str] | None = None,
        write: Callable[[str], None] | None = None,
        write_error: Callable[[str], None] | None = None,
    ) -> None:
        # Resolve terminal I/O at call time so patched builtins keep working.
        self._read = read if read is not None else input
        self._write = write if write is not None else print
        self._write_error = write_error if write_error is not None else _write_stderr

    def confirm_add_root(self, path: Path) -> bool:
        """Ask to store a game root, sanitizing only the displayed text."""
        template = _("Add {path} to allowed game roots? [y/N] ")
        prefix, suffix = template.split("{path}")
        budget = shutil.get_terminal_size().columns - len(prefix) - len(suffix)
        question = template.format(
            path=safe_terminal_text(abbreviate_prompt_path(path, budget, Path.home()))
        )
        try:
            answer = self._read(question).strip().lower()
        except EOFError as exc:
            raise GameValidationError(_("game root was not authorized")) from exc
        return answer in {"y", "yes"}

    def confirm_x11(self, display: str) -> bool:
        """Warn on stderr about X11 insecurity, then ask for consent."""
        warning = _(
            "warning: X11 display {display} is insecure; "
            "X11 clients can capture input and screen contents (keylogging)."
        ).format(display=safe_terminal_text(display))
        self._write_error(warning)
        try:
            answer = self._read(_("Continue with X11? [y/N] ")).strip().lower()
        except EOFError as exc:
            raise GameValidationError(_("X11 launch was not confirmed")) from exc
        return answer in {"y", "yes"}

    def choose_runtime(self, kind: str, candidates: tuple[str, ...], title: str) -> int | None:
        """Return the chosen index, None on quit, defaulting to the first."""
        if not candidates:
            raise GameValidationError(_("runtime selection was cancelled"))
        if kind == "easyrpg":
            template = _("Select an EasyRPG Player runtime 1-{count} (default 1), or [q]uit: ")
        elif kind == "nwjs":
            template = _("Select an NW.js runtime 1-{count} (default 1), or [q]uit: ")
        else:
            raise GameValidationError(_("runtime selection was cancelled"))
        self._write(title)
        prompt = template.format(count=len(candidates))
        for index, label in enumerate(candidates, start=1):
            self._write(f"  {index}. {label}")
        while True:
            try:
                answer = self._read(prompt).strip()
            except EOFError as exc:
                raise GameValidationError(_("runtime selection was cancelled")) from exc
            if answer == "":
                return 0
            if answer.lower() == "q":
                return None
            if answer.isdigit():
                selection = int(answer)
                if 1 <= selection <= len(candidates):
                    return selection - 1
            self._write(_("Invalid selection."))
