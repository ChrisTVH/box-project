"""Mandatory Bubblewrap policy; no host execution or desktop fallback."""

from __future__ import annotations

import os
import re
import stat
from contextlib import ExitStack, suppress
from pathlib import Path
from types import TracebackType

from box.errors import LaunchError
from box.games.files import validate_game_descriptor
from box.launch.gamemode import GAMEMODE_PROXY_SOCKET_NAME
from box.launch.profiles import ProfileCatalog
from box.models import EngineName, GameInfo
from box.paths import AppPaths, open_directory_without_symlinks

BWRAP = Path("/usr/bin/bwrap")

_X11_SOCKET_DIR = Path("/tmp/.X11-unix")
_DRI_DIR = Path("/dev/dri")
_UDEV_DIR = Path("/run/udev")
_SYS_DIR = Path("/sys")
_DRI_NODE_NAME = re.compile(r"card\d+|renderD\d+")


def _is_dri_node(mode: int) -> bool:
    """Accept only character devices as GPU nodes; unit-testable predicate."""
    return stat.S_ISCHR(mode)


def clean_environment() -> dict[str, str]:
    """Use constants, not caller-controlled loader, desktop or interpreter settings."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/sandbox",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
        "XDG_RUNTIME_DIR": "/run/user",
        "XDG_CONFIG_HOME": "/profile/config",
        "XDG_CACHE_HOME": "/profile/cache",
        "XDG_DATA_HOME": "/profile/data",
        "XDG_STATE_HOME": "/profile/state",
    }


class Sandbox:
    """Own pinned mount sources until Bubblewrap has finished using them."""

    def __init__(self, *, allow_network: bool = False, allow_game_writes: bool = False) -> None:
        self._allow_network = allow_network
        self._allow_game_writes = allow_game_writes
        self._stack = ExitStack()
        self._fds: list[int] = []
        self.options: list[str] = []

    def __enter__(self) -> Sandbox:
        try:
            if not BWRAP.is_file() or not os.access(BWRAP, os.X_OK):
                raise LaunchError(
                    "Bubblewrap (/usr/bin/bwrap) is required; refusing unsandboxed execution"
                )
            self.options = [
                str(BWRAP),
                "--unshare-user",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--cap-drop",
                "ALL",
                "--die-with-parent",
                "--new-session",
                "--clearenv",
            ]
            if not self._allow_network:
                self.options.append("--unshare-net")
            for name in ("usr", "lib", "lib64", "bin", "sbin"):
                source = Path("/") / name
                if source.is_symlink():
                    self.options += ["--symlink", os.readlink(source), str(source)]
                elif source.is_dir():
                    self.bind(self.keep(open_directory_without_symlinks(source)), str(source))
            for name in ("ld.so.cache", "fonts"):
                source = Path("/etc") / name
                if source.exists():
                    self.bind(self.open_path(source), str(source))
            if self._allow_network:
                # The host network includes loopback/LAN. Expose only resolver and
                # trust configuration, never the parent /etc or /run directories.
                for name in ("resolv.conf", "hosts", "nsswitch.conf", "ssl/certs", "ssl/cert.pem"):
                    source = Path("/etc") / name
                    if source.exists():
                        self.bind(self.open_path(source.resolve(strict=True)), str(source))
            self.options += [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                # Writable tmpfs mounts: the trailing --remount-ro / turns
                # plain --dir paths read-only, breaking dconf, the MESA
                # shader cache and anything else honoring XDG_RUNTIME_DIR
                # or HOME. Tmpfs mounts stay writable and are discarded.
                "--tmpfs",
                "/home/sandbox",
                "--tmpfs",
                "/run/user",
                "--tmpfs",
                "/profile",
            ]
            for key, value in clean_environment().items():
                self.options += ["--setenv", key, value]
            return self
        except OSError as exc:
            self._stack.close()
            raise LaunchError(f"cannot prepare mandatory sandbox: {exc}") from exc
        except BaseException:
            self._stack.close()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stack.close()
        if isinstance(exc, OSError):
            raise LaunchError(f"cannot prepare mandatory sandbox: {exc}") from exc

    @property
    def pass_fds(self) -> tuple[int, ...]:
        """Pass mount handles to Bubblewrap; the trusted bootstrap closes them."""
        return tuple(self._fds)

    def keep(self, descriptor: int) -> int:
        self._fds.append(descriptor)
        self._stack.callback(os.close, descriptor)
        return descriptor

    def open_path(self, path: Path) -> int:
        """Reject symlinks in every source component, including the leaf."""
        parent = open_directory_without_symlinks(path.parent)
        try:
            descriptor = self.keep(os.open(path.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent))
        finally:
            os.close(parent)
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise LaunchError(f"unsafe sandbox mount source: {path}")
        return descriptor

    def bind(self, descriptor: int, destination: str, *, writable: bool = False) -> None:
        self.options += [
            "--bind" if writable else "--ro-bind",
            f"/proc/self/fd/{descriptor}",
            destination,
        ]

    def runtime(self, executable: Path) -> str:
        """Expose only this runtime directory, at a constant sandbox path."""
        descriptor = self.keep(open_directory_without_symlinks(executable.parent))
        validate_tree(descriptor)
        binary = self.keep(os.open(executable.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=descriptor))
        if not stat.S_ISREG(os.fstat(binary).st_mode):
            raise LaunchError("runtime executable must be a regular file")
        self.bind(descriptor, "/runtime")
        return f"/runtime/{executable.name}"

    def desktop(self) -> None:
        """Share one owned Wayland socket, never X11, D-Bus, SSH or audio sockets.

        The video driver is deliberately not forced: SDL negotiates Wayland on
        its own when the driver is compiled in, and forcing it breaks runtimes
        without Wayland support. An X11 fallback fails closed here because no
        X11 socket is exposed by this method.
        """
        name = os.environ.get("WAYLAND_DISPLAY", "")
        root = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
        if not name or Path(name).name != name or name in {".", ".."} or not root.is_absolute():
            raise LaunchError("a local Wayland socket is required; X11 is not permitted")
        parent = self.keep(open_directory_without_symlinks(root))
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise LaunchError("unsafe Wayland runtime directory")
        socket = self.keep(os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent))
        metadata = os.fstat(socket)
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise LaunchError("unsafe Wayland socket")
        self.bind(socket, "/run/user/wayland")
        self.options += ["--setenv", "WAYLAND_DISPLAY", "wayland"]

    def display_probe(self) -> str:
        """Classify the host display from environment only; never raise for absence."""
        name = os.environ.get("WAYLAND_DISPLAY", "")
        root = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
        if name and Path(name).name == name and name not in {".", ".."} and root.is_absolute():
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "none"

    def x11(self) -> None:
        """Expose only the concrete local X11 socket and its cookie.

        Accepts only local `:N` / `:N.M` displays. Remote, abstract (`@`),
        or path-like values fail closed. The X11 socket directory and the
        socket itself are typically root-owned, so no self-uid check applies
        here; the socket type and exact name match are still required.

        An explicit $XAUTHORITY must be absolute and usable. Without it, the
        default ~/.Xauthority is forwarded when present; when absent the game
        runs without a cookie. That loses nothing: the cookie only
        authenticates the game to the server, and a same-user game could
        connect from the host anyway. A server that does require authentication
        fails with a clear X11 error instead.
        """
        display = os.environ.get("DISPLAY", "")
        match = re.fullmatch(r":(\d+)(?:\.(\d+))?", display)
        if match is None:
            raise LaunchError("unsafe X11 DISPLAY; only local :N or :N.M is permitted")
        number = match.group(1)
        socket_name = f"X{number}"
        try:
            parent = self.keep(open_directory_without_symlinks(_X11_SOCKET_DIR))
        except OSError as exc:
            raise LaunchError(f"cannot access X11 socket directory: {exc}") from exc
        try:
            socket_descriptor = self.keep(
                os.open(socket_name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
            )
        except OSError as exc:
            raise LaunchError(f"cannot access X11 socket for {display}: {exc}") from exc
        metadata = os.fstat(socket_descriptor)
        if not stat.S_ISSOCK(metadata.st_mode):
            raise LaunchError("unsafe X11 socket")
        expected = Path(os.path.abspath(_X11_SOCKET_DIR / socket_name))
        if Path(os.readlink(f"/proc/self/fd/{socket_descriptor}")) != expected:
            raise LaunchError("X11 socket changed during preparation")
        self.bind(socket_descriptor, f"/tmp/.X11-unix/{socket_name}")
        self.options += [
            "--setenv",
            "DISPLAY",
            display,
            "--setenv",
            "SDL_VIDEODRIVER",
            "x11",
            "--unsetenv",
            "WAYLAND_DISPLAY",
        ]
        authority_value = os.environ.get("XAUTHORITY", "")
        if authority_value:
            cookie = Path(authority_value)
            if not cookie.is_absolute():
                raise LaunchError("XAUTHORITY must be an absolute path; see xauth")
            explicit_cookie = True
        else:
            home_value = os.environ.get("HOME", "")
            home = Path(home_value) if home_value else None
            if home is None or not home.is_absolute():
                raise LaunchError(
                    "cannot locate the Xauthority cookie without an absolute HOME; see xauth"
                )
            cookie = home / ".Xauthority"
            explicit_cookie = False
        try:
            cookie_parent = self.keep(open_directory_without_symlinks(cookie.parent))
        except OSError as exc:
            if explicit_cookie:
                raise LaunchError(f"unusable XAUTHORITY path; see xauth: {exc}") from exc
            return
        parent_metadata = os.fstat(cookie_parent)
        if parent_metadata.st_uid != os.getuid() or parent_metadata.st_mode & 0o022:
            raise LaunchError("unsafe Xauthority directory")
        try:
            cookie_descriptor = self.keep(
                os.open(cookie.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=cookie_parent)
            )
        except OSError as exc:
            if explicit_cookie:
                raise LaunchError(f"unusable XAUTHORITY path; see xauth: {exc}") from exc
            return
        cookie_metadata = os.fstat(cookie_descriptor)
        if (
            not stat.S_ISREG(cookie_metadata.st_mode)
            or cookie_metadata.st_uid != os.getuid()
            or cookie_metadata.st_mode & 0o077
        ):
            raise LaunchError("unsafe Xauthority cookie")
        expected_cookie = Path(os.path.abspath(cookie))
        if Path(os.readlink(f"/proc/self/fd/{cookie_descriptor}")) != expected_cookie:
            raise LaunchError("Xauthority cookie changed during preparation")
        self.bind(cookie_descriptor, "/home/sandbox/.Xauthority")
        self.options += ["--setenv", "XAUTHORITY", "/home/sandbox/.Xauthority"]

    def devices(self) -> None:
        """Expose validated /dev/dri nodes with --dev-bind for GPU rendering.

        Only `cardN` and `renderDN` character devices are exposed, each opened
        with O_NOFOLLOW and revalidated against the descriptor actually opened
        (device + inode must match the directory entry) so a swapped path
        cannot smuggle another node in. Never exposes the whole /dev tree. Uses
        --dev-bind (per the Bubblewrap manual) so render nodes remain usable
        device nodes instead of inert bind mounts. Read-only /sys (plus
        /run/udev when present) is exposed alongside so libdrm and MESA can
        enumerate the devices; without it even mounted nodes stay invisible.
        Skips silently when the host has no /dev/dri so software rendering
        still works. /sys is world-readable host hardware information, so a
        read-only bind grants a same-user game nothing it cannot already see.
        """
        if _DRI_DIR.is_symlink():
            raise LaunchError("unsafe GPU device directory")
        try:
            parent = self.keep(open_directory_without_symlinks(_DRI_DIR))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LaunchError(f"cannot access GPU device directory: {exc}") from exc
        if not stat.S_ISDIR(os.fstat(parent).st_mode):
            raise LaunchError("unsafe GPU device source")
        try:
            names = os.listdir(parent)
        except OSError as exc:
            raise LaunchError(f"cannot list GPU device directory: {exc}") from exc
        targets = [name for name in names if _DRI_NODE_NAME.fullmatch(name)]
        if not targets:
            return
        self.bind(self.open_path(_SYS_DIR), "/sys")
        if _UDEV_DIR.is_dir() and not _UDEV_DIR.is_symlink():
            self.bind(self.open_path(_UDEV_DIR), "/run/udev")
        self.options += ["--dir", "/dev/dri"]
        for name in sorted(targets):
            try:
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise LaunchError(f"cannot access GPU device node: {exc}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise LaunchError("unsafe GPU device link")
            try:
                node = self.keep(os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent))
            except OSError as exc:
                raise LaunchError(f"cannot access GPU device node: {exc}") from exc
            opened = os.fstat(node)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise LaunchError("GPU device node changed during preparation")
            if not _is_dri_node(opened.st_mode):
                raise LaunchError(f"unsafe GPU device node: {name}")
            self.options += ["--dev-bind", f"/proc/self/fd/{node}", f"/dev/dri/{name}"]

    def audio(self) -> None:
        """Expose the user's PipeWire and PulseAudio sockets.

        The PipeWire socket gets the same strictness as the Wayland socket:
        bare filename from PIPEWIRE_REMOTE (default `pipewire-0`) under the
        absolute XDG_RUNTIME_DIR, uid-owned socket, no symlinks, and private
        runtime-dir permissions. Skips silently when absent.

        PulseAudio native (`$XDG_RUNTIME_DIR/pulse/native` plus the cookie at
        `~/.config/pulse/cookie`) uses the same validation: uid-owned socket
        and directory without group/other write permission, a regular
        uid-owned cookie without any group/other permission. A missing socket
        or directory skips PulseAudio silently; a missing cookie proceeds
        without one (the cookie only authenticates the game to the server, so
        its absence cannot grant anything). An unsafe cookie or directory
        fails closed.
        """
        root = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
        if not root.is_absolute():
            return
        name = os.environ.get("PIPEWIRE_REMOTE", "") or "pipewire-0"
        if Path(name).name != name or name in {".", ".."}:
            raise LaunchError("unsafe PipeWire socket name")
        try:
            parent = self.keep(open_directory_without_symlinks(root))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LaunchError(f"cannot access audio runtime directory: {exc}") from exc
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise LaunchError("unsafe audio runtime directory")
        try:
            socket_descriptor = self.keep(os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise LaunchError(f"cannot access PipeWire socket: {exc}") from exc
        else:
            socket_metadata = os.fstat(socket_descriptor)
            if not stat.S_ISSOCK(socket_metadata.st_mode) or socket_metadata.st_uid != os.getuid():
                raise LaunchError("unsafe PipeWire socket")
            expected = Path(os.path.abspath(root / name))
            if Path(os.readlink(f"/proc/self/fd/{socket_descriptor}")) != expected:
                raise LaunchError("PipeWire socket changed during preparation")
            self.bind(socket_descriptor, f"/run/user/{name}")
            if name != "pipewire-0":
                self.options += ["--setenv", "PIPEWIRE_REMOTE", name]
        self._pulse_audio(root)

    def _pulse_audio(self, root: Path) -> None:
        """Expose the PulseAudio native socket and cookie, or skip silently."""
        try:
            pulse_parent = self.keep(open_directory_without_symlinks(root / "pulse"))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LaunchError(f"cannot access PulseAudio directory: {exc}") from exc
        pulse_metadata = os.fstat(pulse_parent)
        if pulse_metadata.st_uid != os.getuid() or pulse_metadata.st_mode & 0o022:
            raise LaunchError("unsafe PulseAudio directory")
        try:
            native = self.keep(os.open("native", os.O_PATH | os.O_NOFOLLOW, dir_fd=pulse_parent))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LaunchError(f"cannot access PulseAudio socket: {exc}") from exc
        native_metadata = os.fstat(native)
        if not stat.S_ISSOCK(native_metadata.st_mode) or native_metadata.st_uid != os.getuid():
            raise LaunchError("unsafe PulseAudio socket")
        expected = Path(os.path.abspath(root / "pulse" / "native"))
        if Path(os.readlink(f"/proc/self/fd/{native}")) != expected:
            raise LaunchError("PulseAudio socket changed during preparation")
        home_value = os.environ.get("HOME", "")
        home = Path(home_value) if home_value else None
        cookie_descriptor: int | None = None
        if home is not None and home.is_absolute():
            candidate = home / ".config" / "pulse" / "cookie"
            try:
                config_parent = self.keep(open_directory_without_symlinks(candidate.parent))
            except OSError:
                pass
            else:
                config_metadata = os.fstat(config_parent)
                if config_metadata.st_uid != os.getuid() or config_metadata.st_mode & 0o022:
                    raise LaunchError("unsafe PulseAudio cookie directory")
                try:
                    descriptor = self.keep(
                        os.open(candidate.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=config_parent)
                    )
                except OSError:
                    pass
                else:
                    cookie_metadata = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(cookie_metadata.st_mode)
                        or cookie_metadata.st_uid != os.getuid()
                        or cookie_metadata.st_mode & 0o077
                    ):
                        raise LaunchError("unsafe PulseAudio cookie")
                    cookie_descriptor = descriptor
        self.options += ["--dir", "/run/user/pulse"]
        self.bind(native, "/run/user/pulse/native")
        self.options += ["--setenv", "PULSE_SERVER", "unix:/run/user/pulse/native"]
        if cookie_descriptor is not None:
            self.bind(cookie_descriptor, "/home/sandbox/.pulse-cookie")
            self.options += ["--setenv", "PULSE_COOKIE", "/home/sandbox/.pulse-cookie"]

    def gamemode(self, proxy_host_path: Path) -> None:
        """Expose only the filtered GameMode proxy socket, never the host bus.

        Binds the supervisor-created socket at ``proxy_host_path`` read-only to
        ``/run/user/gamemode-proxy`` and points ``DBUS_SESSION_BUS_ADDRESS`` at
        ``unix:path=/run/user/gamemode-proxy``. Only the ``com.feralinteractive.GameMode``
        name is allowed through the proxy (--filter --talk, no see/own/broadcast).
        The socket itself is created by the supervisor after this method runs,
        so only the uid-owned 0700 parent directory is validated here.
        """
        if proxy_host_path.name != GAMEMODE_PROXY_SOCKET_NAME or not proxy_host_path.is_absolute():
            raise LaunchError("unsafe GameMode proxy path")
        try:
            parent = self.keep(open_directory_without_symlinks(proxy_host_path.parent))
        except OSError as exc:
            raise LaunchError(f"cannot access GameMode proxy directory: {exc}") from exc
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise LaunchError("unsafe GameMode proxy directory")
        expected = Path(os.path.abspath(proxy_host_path.parent))
        if Path(os.readlink(f"/proc/self/fd/{parent}")) != expected:
            raise LaunchError("GameMode proxy directory changed during preparation")
        destination = f"/run/user/{GAMEMODE_PROXY_SOCKET_NAME}"
        self.options += ["--ro-bind", str(proxy_host_path), destination]
        self.options += ["--setenv", "DBUS_SESSION_BUS_ADDRESS", f"unix:path={destination}"]

    def persistence(self, paths: AppPaths, game: GameInfo, game_root: Path | None = None) -> None:
        """Mount only the disposable runtime profile from the launcher cache."""
        profile = ProfileCatalog(paths).create_for_game(game, game_root)
        profile_descriptor = self.keep(
            paths.open_or_create_private_cache_directory("profiles", profile.name, "sandbox")
        )
        validate_tree(profile_descriptor, persistent=True)
        self.bind(profile_descriptor, "/profile", writable=True)

    def game_saves(self, game: GameInfo, descriptor: int) -> int:
        """Pin the game's own save/ directory; never change existing permissions or files."""
        validate_game_descriptor(game, descriptor)
        parts: tuple[str, ...] = ()
        if game.engine is not EngineName.RPG_MAKER_2000_2003:
            if game.entrypoint is None:
                raise LaunchError("NW.js requires an entrypoint")
            try:
                parts = game.entrypoint.parent.relative_to(game.root).parts
            except ValueError as exc:
                raise LaunchError("save path is outside the game") from exc
            if ".." in parts:
                raise LaunchError("save path contains traversal")
        parent = descriptor
        for component in parts:
            parent = self.keep(
                os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            )
        path = game.root.joinpath(*parts)
        validate_game_descriptor(game, descriptor)
        self._validate_game_directory(parent, path)
        with suppress(FileExistsError):
            os.mkdir("save", 0o700, dir_fd=parent)
        saves = self.keep(
            os.open("save", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        )
        metadata = os.fstat(saves)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise LaunchError("game save directory has unsafe ownership or permissions")
        validate_tree(saves)
        validate_game_descriptor(game, descriptor)
        self._validate_game_directory(saves, path / "save")
        self.bind(saves, "/saves", writable=True)
        return saves

    def game_source_saves(self, source: GameInfo, descriptor: int) -> int:
        """Pin the consented source save/ directory for EasyRPG launches.

        The unpacked game tree stays the read-only /game mount while saves
        live in the user-pointed source folder; --save-path stays /game/save.
        Only RPG Maker 2000/2003 games qualify (save/ at the root, no
        entrypoint parts). Validation mirrors game_saves: descriptor pinning,
        0700 creation, ownership and permission checks, and tree validation.
        """
        if source.engine is not EngineName.RPG_MAKER_2000_2003:
            raise LaunchError("source saves are only available for RPG Maker 2000/2003 games")
        return self.game_saves(source, descriptor)

    def _validate_game_directory(self, descriptor: int, path: Path) -> None:
        """Reject an ancestor or save directory relocated while it was being opened."""
        current = open_directory_without_symlinks(path)
        try:
            if (
                not os.path.samestat(os.fstat(descriptor), os.fstat(current))
                or Path(os.readlink(f"/proc/self/fd/{descriptor}")) != path
            ):
                raise LaunchError("game save path changed during preparation")
        finally:
            os.close(current)

    def game_writable(self, descriptor: int) -> None:
        """Bind the pinned game tree writable at /game for the EasyRPG branch.

        Takes ownership of the descriptor so the mount source reaches Bubblewrap
        through pass_fds; the caller keeps the save overlay (game_saves bind
        at /game/save). Validation is identical to the read-only path.
        """
        validate_tree(descriptor)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise LaunchError("game tree must be a directory")
        descriptor = self.keep(descriptor)
        self.bind(descriptor, "/game", writable=True)

    def nw_game(self, game: GameInfo, descriptor: int, saves: int) -> None:
        """Build the game view with the game's pinned save directory mounted writable.

        Only the entrypoint ancestor chain is reconstructed. Other entries are
        pinned read-only mounts or validated links; game_saves creates only save/.
        With allow_game_writes, the validated tree is mounted writable and the
        per-level --remount-ro is skipped; save aliasing is unchanged.
        """
        if game.entrypoint is None:
            raise LaunchError("NW.js requires an entrypoint")
        tree_root = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        validate_tree(descriptor)
        parts = game.entrypoint.parent.relative_to(game.root).parts
        self._nw_directory(descriptor, "/game", parts, saves, tree_root=tree_root)

    def _nw_directory(
        self,
        descriptor: int,
        destination: str,
        parts: tuple[str, ...],
        saves: int,
        *,
        tree_root: Path,
    ) -> None:
        writable = self._allow_game_writes
        self.options += ["--tmpfs", destination]
        next_name = parts[0] if parts else "save"
        for name in os.listdir(descriptor):
            if name == next_name:
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise LaunchError("unsafe NW.js save path or entrypoint ancestor")
                continue
            child = self.keep(os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=descriptor))
            mode = os.fstat(child).st_mode
            if stat.S_ISLNK(mode):
                # Read the pinned link, not a possibly replaced directory entry.
                target = os.readlink("", dir_fd=child)
                self._validate_game_directory(
                    descriptor, tree_root / Path(destination).relative_to("/game")
                )
                try:
                    resolved = Path(f"/proc/self/fd/{descriptor}", target).resolve()
                except OSError as exc:
                    raise LaunchError("cannot resolve sandbox asset symlink") from exc
                if target.startswith("/") or not resolved.is_relative_to(tree_root):
                    raise LaunchError("sandbox asset symlink escapes its tree")
                self.options += ["--symlink", target, f"{destination}/{name}"]
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise LaunchError(f"unsafe game entry in sandbox: {name}")
            self.bind(child, f"{destination}/{name}", writable=writable)
        if parts:
            child = self.keep(
                os.open(next_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            )
            self._nw_directory(
                child, f"{destination}/{next_name}", parts[1:], saves, tree_root=tree_root
            )
        else:
            self.bind(saves, f"{destination}/save", writable=True)
        if not writable:
            self.options += ["--remount-ro", destination]

    def command(self, arguments: list[str], *, cwd: str = "/") -> list[str]:
        """Close inherited host handles before loading any untrusted executable.

        Bubblewrap does not consume descriptors used as /proc/self/fd sources.
        The system Python bootstrap runs isolated, without site or cwd imports.
        """
        bootstrap = (
            "import os,sys; "
            f"os.closerange(3, {max(self._fds, default=2) + 1}); "
            "os.execv(sys.argv[1], sys.argv[1:])"
        )
        return [
            *self.options,
            "--chdir",
            cwd,
            "--remount-ro",
            "/",
            "--",
            "/usr/bin/python3",
            "-I",
            "-S",
            "-c",
            bootstrap,
            *arguments,
        ]


def validate_tree(descriptor: int, *, persistent: bool = False) -> None:
    """Reject host sockets/devices and escaping asset links before exposing a tree.

    Persistent symlinks resolve only inside the sandbox. Hard links could expose
    a host inode outside an asset tree or mutate one outside the profile.
    """

    def failed(error: OSError) -> None:
        raise error

    tree_root = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
    for _root, directories, files, parent in os.fwalk(".", dir_fd=descriptor, onerror=failed):
        for name in (*directories, *files):
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(name, dir_fd=parent)
                if not persistent:
                    # Resolve link chains before checking containment: lexical '..'
                    # normalization alone misses escapes through directory links.
                    try:
                        resolved = Path(f"/proc/self/fd/{parent}", name).resolve()
                    except OSError as exc:
                        raise LaunchError("cannot resolve sandbox asset symlink") from exc
                    if target.startswith("/") or not resolved.is_relative_to(tree_root):
                        raise LaunchError("sandbox asset symlink escapes its tree")
            elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise LaunchError("sandbox trees must not contain sockets or special files")
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise LaunchError("sandbox files must not have hard links")
