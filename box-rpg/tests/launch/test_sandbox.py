"""Policy tests and real, harmless Bubblewrap filesystem/network probes."""

# pyright: reportPrivateUsage=false

import errno
import os
import socket
import stat
import subprocess
from collections.abc import Callable, Iterator
from itertools import pairwise
from pathlib import Path

import pytest

from box.diagnostics.versions import _binary_version
from box.errors import GameValidationError, LaunchError
from box.launch.sandbox import (
    _DRI_NODE_NAME,
    Sandbox,
    _is_dri_node,
    clean_environment,
    validate_tree,
)
from box.launch.session import create_session
from box.models import EngineName, GameInfo
from box.paths import AppPaths, open_directory_without_symlinks


@pytest.fixture
def loopback_server() -> Iterator[socket.socket]:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(1)
        yield server


def test_policy_has_mandatory_namespaces_and_no_host_root() -> None:
    with Sandbox() as sandbox:
        command = sandbox.command(["/usr/bin/true"])
        for flag in (
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-net",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ):
            assert flag in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert not any(flag.endswith("-try") for flag in command)
        assert "--share-net" not in command
        assert "--bind" not in command
        # HOME and XDG_RUNTIME_DIR must stay writable: the trailing
        # --remount-ro / turns plain --dir paths read-only (dconf, MESA
        # shader cache), while tmpfs mounts are unaffected.
        pairs = list(pairwise(command))
        assert ("--tmpfs", "/run/user") in pairs
        assert ("--tmpfs", "/home/sandbox") in pairs
        assert ("--dir", "/run/user") not in pairs
        assert ("--dir", "/home/sandbox") not in pairs
        for index, flag in enumerate(command):
            if flag == "--ro-bind":
                assert command[index + 1].startswith("/proc/self/fd/")
                assert command[index + 2] != "/"
        descriptors = sandbox.pass_fds
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_missing_bwrap_never_executes_a_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("box.launch.sandbox.BWRAP", tmp_path / "missing")
    with pytest.raises(LaunchError, match="required"):
        _binary_version(tmp_path / "runtime", "fallback")


def test_network_opt_in_adds_only_readonly_resolver_and_trust_mounts() -> None:
    def mounts(sandbox: Sandbox) -> dict[str, str]:
        return {
            sandbox.options[index + 2]: flag
            for index, flag in enumerate(sandbox.options)
            if flag in {"--bind", "--ro-bind"}
        }

    with Sandbox() as offline, Sandbox(allow_network=True) as online:
        before, after = mounts(offline), mounts(online)
        assert all(after[path] == mode for path, mode in before.items())
        extra = after.keys() - before.keys()
        assert extra <= {
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
            "/etc/ssl/certs",
            "/etc/ssl/cert.pem",
        }
        assert all(after[path] == "--ro-bind" for path in extra)
        assert "--unshare-net" in offline.options
        assert "--unshare-net" not in online.options


def test_real_diagnostic_is_always_offline(
    tmp_path: Path, loopback_server: socket.socket, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_NETWORK", "1")
    probe = tmp_path / "probe"
    probe.write_text(
        "#!/usr/bin/python3\n"
        "import os, socket\n"
        "assert 'ALLOW_NETWORK' not in os.environ\n"
        "with socket.socket() as sock:\n"
        "    sock.settimeout(1)\n"
        f"    try: sock.connect({loopback_server.getsockname()!r})\n"
        "    except OSError: print('offline-version')\n"
        "    else: raise AssertionError('diagnostic has host network')\n"
    )
    probe.chmod(0o700)
    assert _binary_version(probe, "fallback") == "offline-version"
    with pytest.raises(TimeoutError):
        loopback_server.accept()


def test_nonfunctional_bwrap_never_retries_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "host-executed"
    binary = tmp_path / "probe"
    binary.write_text(f"#!/usr/bin/python3\nopen({str(marker)!r}, 'w').close()\n")
    binary.chmod(0o700)
    monkeypatch.setattr("box.launch.sandbox.BWRAP", Path("/usr/bin/false"))
    assert _binary_version(binary, "fallback") == "fallback"
    assert not marker.exists()


def test_preparation_oserror_is_a_launch_error_and_closes_mount_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptors: list[int] = []

    def fail_bind(
        self: Sandbox, descriptor: int, destination: str, *, writable: bool = False
    ) -> None:
        descriptors.append(descriptor)
        raise OSError("mount preparation failed")

    monkeypatch.setattr(Sandbox, "bind", fail_bind)
    with pytest.raises(LaunchError, match="cannot prepare mandatory sandbox"), Sandbox():
        pytest.fail("preparation must fail closed")
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_x11_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="X11"):
        sandbox.desktop()


def test_only_selected_wayland_socket_is_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/secret/bus")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/secret/ssh")
    with socket.socket(socket.AF_UNIX) as server, Sandbox() as sandbox:
        server.bind(str(tmp_path / "wayland-0"))
        before = len(sandbox.options)
        sandbox.desktop()
        extra = sandbox.options[before:]
        assert extra[0] == "--ro-bind"
        assert extra[2] == "/run/user/wayland"
        assert extra.count("--ro-bind") == 1
        assert "/secret/bus" not in extra
        assert "/secret/ssh" not in extra
        assert "DISPLAY" not in clean_environment()


@pytest.mark.parametrize("name", ["../bus", "/run/user/1000/wayland-0", ".", ".."])
def test_unsafe_wayland_names_are_rejected(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", name)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.desktop()


def test_wayland_symlink_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path.chmod(0o700)
    (tmp_path / "wayland-0").symlink_to("bus")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="socket"):
        sandbox.desktop()


@pytest.mark.parametrize("kind", ["symlink", "link-chain", "socket", "hardlink", "asset-hardlink"])
def test_unsafe_trees_are_rejected(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        if kind == "symlink":
            (root / "escape").symlink_to("../../secret")
        elif kind == "link-chain":
            (root / "subdir").mkdir()
            (root / "subdir/up").symlink_to("..")
            (root / "escape").symlink_to("subdir/up/../secret")
        elif kind == "socket":
            server.bind(str(root / "bus"))
        else:
            (tmp_path / "secret").write_text("secret")
            os.link(tmp_path / "secret", root / "linked")
        descriptor = open_directory_without_symlinks(root)
        try:
            with pytest.raises(LaunchError):
                validate_tree(descriptor, persistent=kind == "hardlink")
        finally:
            os.close(descriptor)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("allow_network", [False, True])
def test_real_bwrap_isolates_secrets_network_and_writes(
    tmp_path: Path,
    nested: bool,
    monkeypatch: pytest.MonkeyPatch,
    allow_network: bool,
    loopback_server: socket.socket,
) -> None:
    """Execute only a generated Python probe, never a game or downloaded runtime."""
    secret = tmp_path / "secret"
    secret.write_text("host secret")
    monkeypatch.setenv("SANDBOX_SECRET", "secret environment")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(secret))
    root = tmp_path / "game"
    entry = root / "www" / "index.html" if nested else root / "index.html"
    entry.parent.mkdir(parents=True)
    entry.write_text("fixture")
    (entry.parent / "save").mkdir()
    (entry.parent / "save/legacy").write_text("previous save")
    (root / "package.json").write_text('{"name": "sandbox-probe"}')
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, entry, root / "package.json")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    probe = runtime / "probe"
    save_path = "/game/www/save" if nested else "/game/save"
    host_namespaces = {
        name: os.readlink(f"/proc/self/ns/{name}") for name in ("user", "pid", "ipc", "net")
    }
    probe.write_text(
        "#!/usr/bin/python3\n"
        "import errno, json, os, pathlib, socket\n"
        f"assert not pathlib.Path({str(secret)!r}).exists()\n"
        "assert not pathlib.Path('/home/christopher').exists()\n"
        "assert not pathlib.Path('/run/dbus').exists()\n"
        "assert not pathlib.Path('/tmp/.X11-unix').exists()\n"
        "assert all(k not in os.environ for k in ('SANDBOX_SECRET', 'SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS', 'DISPLAY', 'WAYLAND_DISPLAY'))\n"
        "for fd in os.listdir('/proc/self/fd'):\n"
        "    if int(fd) <= 2: continue\n"
        "    try: target = os.readlink('/proc/self/fd/' + fd)\n"
        "    except FileNotFoundError: continue\n"
        "    raise AssertionError('inherited host descriptor: ' + target)\n"
        "for path in pathlib.Path('/proc/1/fd').iterdir():\n"
        "    try: target = os.readlink(path)\n"
        "    except (PermissionError, FileNotFoundError): continue\n"
        f"    assert not target.startswith({str(tmp_path)!r}), target\n"
        f"for name, host in {host_namespaces!r}.items():\n"
        f"    assert (os.readlink('/proc/self/ns/' + name) == host) == (name == 'net' and {allow_network!r})\n"
        "status = pathlib.Path('/proc/self/status').read_text()\n"
        "assert 'CapEff:\\t0000000000000000' in status\n"
        "with socket.socket() as sock:\n"
        "    sock.settimeout(1)\n"
        f"    try: sock.connect({loopback_server.getsockname()!r})\n"
        f"    except OSError: assert not {allow_network!r}\n"
        f"    else: assert {allow_network!r}; sock.sendall(b'local-probe')\n"
        "assert not pathlib.Path('/etc/ssl/private').exists()\n"
        f"if {allow_network!r}:\n"
        "    import ssl\n"
        "    assert ssl.create_default_context().get_ca_certs()\n"
        "for path in ('/runtime/probe', '/game/new-file', '/game/www/new-file', '/game/package.json', '/usr/new-file'):\n"
        "    try: pathlib.Path(path).write_text('forbidden')\n"
        "    except OSError: pass\n"
        "    else: raise AssertionError('unexpected write: ' + path)\n"
        "# /home/sandbox is an ephemeral writable tmpfs (HOME needs a writable\n"
        "# scratch area); writes there must succeed inside yet never reach hosts.\n"
        "pathlib.Path('/home/sandbox/ephemeral').write_text('scratch')\n"
        "assert pathlib.Path('/home/sandbox/ephemeral').read_text() == 'scratch'\n"
        f"pathlib.Path({save_path!r}, 'slot').write_text('saved')\n"
        f"assert pathlib.Path({save_path!r}, 'legacy').read_text() == 'previous save'\n"
        "assert pathlib.Path('/saves/slot').read_text() == 'saved'\n"
        "assert pathlib.Path('/session/save/slot').read_text() == 'saved'\n"
        "manifest = json.loads(pathlib.Path('/session/package.json').read_text())\n"
        "assert pathlib.Path('/session', manifest['main']).read_text() == 'fixture'\n"
        "pathlib.Path('/profile/settings').write_text('profile')\n"
        "pathlib.Path('/tmp/ephemeral').write_text('temporary')\n"
        "print('sandbox-ok')\n"
    )
    probe.chmod(0o700)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    with create_session(paths, game) as session, Sandbox(allow_network=allow_network) as sandbox:
        executable = sandbox.runtime(probe)
        sandbox.persistence(paths, game)
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        sandbox.nw_game(game, descriptor, saves)
        sandbox.bind(sandbox.keep(os.dup(session.session_descriptor)), "/session")
        sandbox.bind(saves, "/session/save", writable=True)
        result = subprocess.run(
            sandbox.command([executable]),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert (entry.parent / "save/slot").read_text() == "saved", result.stderr
    assert result.returncode == 0, result.stderr
    assert result.stdout == "sandbox-ok\n"
    assert not Path("/home/sandbox").exists()
    assert secret.read_text() == "host secret"
    assert (entry.parent / "save/legacy").read_text() == "previous save"
    assert (entry.parent / "save/slot").read_text() == "saved"
    if allow_network:
        connection, _address = loopback_server.accept()
        with connection:
            assert connection.recv(32) == b"local-probe"
    else:
        with pytest.raises(TimeoutError):
            loopback_server.accept()
    profiles = list(paths.profiles_root.iterdir())
    assert len(profiles) == 1
    assert not (profiles[0] / "saves").exists()
    assert (profiles[0] / "sandbox/settings").read_text() == "profile"
    assert not (tmp_path / "xdg-data").exists()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("engine", "nested"),
    [
        (EngineName.RPG_MAKER_MV, False),
        (EngineName.RPG_MAKER_MV, True),
        (EngineName.RPG_MAKER_MZ, False),
        (EngineName.RPG_MAKER_MZ, True),
        (EngineName.RPG_MAKER_2000_2003, False),
    ],
)
def test_game_saves_creates_only_save_and_preserves_existing_metadata(
    tmp_path: Path, engine: EngineName, nested: bool, existing: bool
) -> None:
    root = tmp_path / "game"
    parent = root / "www" if nested else root
    parent.mkdir(parents=True)
    entry = None if engine is EngineName.RPG_MAKER_2000_2003 else parent / "index.html"
    game = GameInfo(engine, root, entry)
    save = parent / "save"
    if existing:
        save.mkdir(mode=0o750)
        (save / "previous").write_bytes(b"previous save")
        (save / "previous").chmod(0o640)
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        assert Path(os.readlink(f"/proc/self/fd/{saves}")) == save
        assert stat.S_IMODE(os.fstat(saves).st_mode) == (0o750 if existing else 0o700)
        assert sandbox.options[-3:] == ["--bind", f"/proc/self/fd/{saves}", "/saves"]
    if existing:
        assert (save / "previous").read_bytes() == b"previous save"
        assert stat.S_IMODE((save / "previous").stat().st_mode) == 0o640
    else:
        assert list(save.iterdir()) == []
    assert set(parent.iterdir()) == {save}
    assert not (tmp_path / "xdg-data").exists()
    assert not (tmp_path / "xdg-cache").exists()


@pytest.mark.parametrize("engine", [EngineName.RPG_MAKER_MZ, EngineName.RPG_MAKER_2000_2003])
@pytest.mark.parametrize("kind", ["symlink", "file", "hardlink", "socket", "fifo", "escape-link"])
def test_game_save_directory_rejects_unsafe_sources(
    tmp_path: Path, engine: EngineName, kind: str
) -> None:
    root = tmp_path / "game"
    parent = root / "www" if engine is EngineName.RPG_MAKER_MZ else root
    parent.mkdir(parents=True)
    entry = parent / "index.html" if engine is EngineName.RPG_MAKER_MZ else None
    game = GameInfo(engine, root, entry)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("host secret")
    save = parent / "save"
    with socket.socket(socket.AF_UNIX) as server:
        if kind == "symlink":
            save.symlink_to(outside)
        elif kind == "file":
            save.write_text("not a directory")
        else:
            save.mkdir()
            if kind == "hardlink":
                os.link(secret, save / "secret")
            elif kind == "socket":
                server.bind(str(save / "bus"))
            elif kind == "fifo":
                os.mkfifo(save / "pipe")
            else:
                (save / "secret").symlink_to(secret)
        with pytest.raises(LaunchError), Sandbox() as sandbox:
            descriptor = sandbox.keep(open_directory_without_symlinks(root))
            sandbox.game_saves(game, descriptor)
    assert secret.read_text() == "host secret"
    assert list(outside.iterdir()) == [secret]


def test_game_saves_rejects_symlinked_entrypoint_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "www").symlink_to(outside)
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "www/index.html")
    with pytest.raises(LaunchError), Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        sandbox.game_saves(game, descriptor)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("relative", ["../outside/index.html", "www/../../outside/index.html"])
def test_game_saves_rejects_entrypoint_traversal(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "game"
    root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / relative)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="traversal"):
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        sandbox.game_saves(game, descriptor)
    assert list(root.iterdir()) == []


def test_game_saves_rejects_replaced_game_before_creation(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, root)
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        original = tmp_path / "original"
        root.rename(original)
        root.mkdir()
        with pytest.raises(GameValidationError):
            sandbox.game_saves(game, descriptor)
    assert list(root.iterdir()) == []
    assert list(original.iterdir()) == []


def test_game_saves_rejects_relocated_entrypoint_parent_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "game"
    parent = root / "www"
    parent.mkdir(parents=True)
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, parent / "index.html")
    outside = tmp_path / "outside"
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        validate = sandbox._validate_game_directory

        def relocated(descriptor: int, path: Path) -> None:
            parent.rename(outside)
            parent.mkdir()
            validate(descriptor, path)

        monkeypatch.setattr(sandbox, "_validate_game_directory", relocated)
        with pytest.raises(LaunchError, match="changed during preparation"):
            sandbox.game_saves(game, descriptor)
    assert list(parent.iterdir()) == []
    assert list(outside.iterdir()) == []


def test_game_saves_does_not_migrate_partial_cache_or_xdg_data(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, root)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    legacy = [
        paths.profiles_root / "0123456789abcdef/saves/slot",
        tmp_path / "xdg-data/box-rpg/saves/0123456789abcdef/slot",
        root / "Save01.lsd",
    ]
    for file in legacy:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"unmigrated save")
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        sandbox.game_saves(game, descriptor)
    assert list((root / "save").iterdir()) == []
    assert all(file.read_bytes() == b"unmigrated save" for file in legacy)


def test_internal_asset_links_remain_allowed(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/file").write_text("fixture")
    (tmp_path / "assets/link").symlink_to("file")
    (tmp_path / "assets/chain").symlink_to("link")
    (tmp_path / "assets/dangling").symlink_to("missing")
    descriptor = open_directory_without_symlinks(tmp_path)
    try:
        validate_tree(descriptor)
    finally:
        os.close(descriptor)


def test_save_directory_replacement_cannot_redirect_mount(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, game_root)
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(game_root))
        saves = sandbox.game_saves(game, descriptor)
        original = Path(os.readlink(f"/proc/self/fd/{saves}"))
        pinned = original.with_name("old-saves")
        original.rename(pinned)
        original.symlink_to(tmp_path)
        assert Path(os.readlink(f"/proc/self/fd/{saves}")) == pinned
        result = subprocess.run(
            sandbox.command(["/usr/bin/touch", "/saves/pinned"]),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (pinned / "pinned").exists()
        assert not (tmp_path / "pinned").exists()


@pytest.mark.parametrize("allow_network", [False, True])
def test_real_persistence_survives_relaunch_without_following_host_symlinks(
    tmp_path: Path, allow_network: bool
) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "RPG_RT.ini").write_text("fixture")
    (root / "Save01.lsd").write_text("unmigrated root save")
    (root / "save").mkdir()
    (root / "save/Save01.lsd").write_text("previous save")
    secret = tmp_path / "secret"
    secret.write_text("host secret")
    game = GameInfo(EngineName.RPG_MAKER_2000_2003, root)
    paths = AppPaths(tmp_path / "config", tmp_path / "cache")
    for launch in range(2):
        with Sandbox(allow_network=allow_network) as sandbox:
            sandbox.persistence(paths, game)
            descriptor = sandbox.keep(open_directory_without_symlinks(root))
            saves = sandbox.game_saves(game, descriptor)
            sandbox.bind(descriptor, "/game")
            sandbox.bind(saves, "/game/save", writable=True)
            script = (
                "from pathlib import Path\n"
                "assert Path('/game/save/Save01.lsd').read_text() == 'previous save'\n"
                f"if {launch} == 0:\n"
                "    Path('/game/save/Save02.lsd').write_text('saved')\n"
                "    Path('/profile/settings').write_text('profile')\n"
                "else:\n"
                "    assert Path('/saves/Save02.lsd').read_text() == 'saved'\n"
                "    assert Path('/profile/settings').read_text() == 'profile'\n"
                f"Path('/saves/escape').symlink_to({str(secret)!r})\n"
                "assert not Path('/saves/escape').exists()\n"
                "for path in ('/saves/escape', '/game/RPG_RT.ini', '/game/new-file', '/game/Save01.lsd'):\n"
                "    try: Path(path).write_text('forbidden')\n"
                "    except OSError: pass\n"
                "    else: raise AssertionError('host write: ' + path)\n"
                "Path('/saves/escape').unlink()\n"
            )
            result = subprocess.run(
                sandbox.command(["/usr/bin/python3", "-I", "-S", "-c", script]),
                pass_fds=sandbox.pass_fds,
                env=clean_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert (root / "save/Save02.lsd").read_text() == "saved", result.stderr
        assert result.returncode == 0, result.stderr
    assert secret.read_text() == "host secret"
    assert (root / "RPG_RT.ini").read_text() == "fixture"
    assert (root / "Save01.lsd").read_text() == "unmigrated root save"
    assert (root / "save/Save01.lsd").read_text() == "previous save"
    assert not (tmp_path / "xdg-data").exists()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("engine", [EngineName.RPG_MAKER_MV, EngineName.RPG_MAKER_MZ])
def test_real_nw_game_reconstructs_internal_links(
    tmp_path: Path, nested: bool, engine: EngineName
) -> None:
    root = tmp_path / "game"
    parent = root / "www" if nested else root
    parent.mkdir(parents=True)
    entry = parent / "index.html"
    entry.write_text("fixture")
    game = GameInfo(engine, root, entry)
    links: dict[str, str] = {}
    readable: list[str] = []
    dangling: list[str] = []
    for directory in (root, parent) if nested else (root,):
        (directory / "assets").mkdir()
        (directory / "assets/file").write_text("asset")
        for name, target in {
            "assets-link": "assets",
            "chain": "assets-link",
            "file-link": "chain/file",
            "dangling": "missing",
            "dangling-chain": "dangling",
        }.items():
            link = directory / name
            link.symlink_to(target)
            links[str(Path("/game") / link.relative_to(root))] = target
        location = Path("/game") / directory.relative_to(root)
        readable.extend(
            str(location / name) for name in ("assets-link/file", "chain/file", "file-link")
        )
        dangling.extend(str(location / name) for name in ("dangling", "dangling-chain"))
    (root / "entry-link").symlink_to("www" if nested else ".")
    links["/game/entry-link"] = "www" if nested else "."
    readable.append("/game/entry-link/assets-link/file")
    if nested:
        (parent / "parent-assets").symlink_to("../assets")
        links["/game/www/parent-assets"] = "../assets"
        readable.append("/game/www/parent-assets/file")
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        sandbox.nw_game(game, descriptor, saves)
        script = (
            "import os\n"
            "from pathlib import Path\n"
            f"for path, target in {links!r}.items():\n"
            "    assert os.readlink(path) == target\n"
            f"for path in {readable!r}:\n"
            "    assert Path(path).read_text() == 'asset'\n"
            "    try: Path(path).write_text('forbidden')\n"
            "    except OSError: pass\n"
            "    else: raise AssertionError('writable asset link: ' + path)\n"
            f"for path in {dangling!r}:\n"
            "    assert Path(path).is_symlink() and not Path(path).exists()\n"
            "assert Path('/game/entry-link/index.html').read_text() == 'fixture'\n"
            "Path('/game/entry-link/save/slot').write_text('saved')\n"
            "assert Path('/saves/slot').read_text() == 'saved'\n"
            "print('links-ok')\n"
        )
        result = subprocess.run(
            sandbox.command(["/usr/bin/python3", "-I", "-S", "-c", script]),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "links-ok\n"
        assert (parent / "save/slot").read_text() == "saved"


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("kind", ["absolute", "relative", "chain"])
def test_nw_game_revalidates_captured_link_after_tree_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool, kind: str
) -> None:
    root = tmp_path / "game"
    parent = root / "www" if nested else root
    parent.mkdir(parents=True)
    (parent / "assets").mkdir()
    (parent / "subdir").mkdir()
    (parent / "subdir/up").symlink_to("..")
    link = parent / "assets-link"
    link.symlink_to("assets")
    secret = tmp_path / "secret"
    secret.write_text("host secret")
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, parent / "index.html")
    escape = ("../../" if nested else "../") + "secret"
    target = str(secret) if kind == "absolute" else escape
    if kind == "chain":
        target = "subdir/up/" + escape
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)

        def racing_validate(descriptor: int, *, persistent: bool = False) -> None:
            validate_tree(descriptor, persistent=persistent)
            link.rename(parent / "old-link")
            link.symlink_to(target)

        monkeypatch.setattr("box.launch.sandbox.validate_tree", racing_validate)
        with pytest.raises(LaunchError, match="symlink escapes"):
            sandbox.nw_game(game, descriptor, saves)
        assert not any(
            sandbox.options[index : index + 3]
            == ["--symlink", target, str(Path("/game") / link.relative_to(root))]
            for index in range(len(sandbox.options))
        )
    assert secret.read_text() == "host secret"


def test_nw_game_captures_the_pinned_link_not_its_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "game"
    (root / "assets").mkdir(parents=True)
    link = root / "assets-link"
    link.symlink_to("assets")
    metadata = link.lstat()
    game = GameInfo(EngineName.RPG_MAKER_MZ, root, root / "index.html")
    readlink = os.readlink

    def racing_readlink(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> str:
        if path == "" and dir_fd is not None and os.path.samestat(os.fstat(dir_fd), metadata):
            link.rename(root / "original-link")
            link.symlink_to("../outside")
        return readlink(path, dir_fd=dir_fd)

    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        monkeypatch.setattr("box.launch.sandbox.os.readlink", racing_readlink)
        sandbox.nw_game(game, descriptor, saves)
        assert any(
            sandbox.options[index : index + 3] == ["--symlink", "assets", "/game/assets-link"]
            for index in range(len(sandbox.options))
        )
        assert "../outside" not in sandbox.options
    assert link.readlink() == Path("../outside")


def _make_game_tree(root: Path, *, nested: bool = False) -> GameInfo:
    parent = root / "www" if nested else root
    parent.mkdir(parents=True)
    entry = parent / "index.html"
    entry.write_text("fixture")
    (parent / "assets").mkdir(exist_ok=True)
    (parent / "assets/file").write_text("asset")
    return GameInfo(EngineName.RPG_MAKER_MZ, root, entry)


def _game_mounts(sandbox: Sandbox) -> dict[str, str]:
    return {
        sandbox.options[index + 2]: flag
        for index, flag in enumerate(sandbox.options)
        if flag in {"--bind", "--ro-bind"} and sandbox.options[index + 2].startswith("/game")
    }


def test_nw_game_default_remains_readonly(tmp_path: Path) -> None:
    root = tmp_path / "game"
    game = _make_game_tree(root)
    (root / "package.json").write_text('{"name": "probe"}')
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        sandbox.nw_game(game, descriptor, saves)
        mounts = _game_mounts(sandbox)
        assert mounts["/game/package.json"] == "--ro-bind"
        assert mounts["/game/assets"] == "--ro-bind"
        assert "--remount-ro" in sandbox.options
        assert "/game" in [
            sandbox.options[index + 1]
            for index, flag in enumerate(sandbox.options[:-1])
            if flag == "--remount-ro"
        ]
        # Save alias stays writable.
        assert mounts["/game/save"] == "--bind"


def test_nw_game_writable_skips_remount_ro(tmp_path: Path) -> None:
    root = tmp_path / "game"
    game = _make_game_tree(root)
    (root / "package.json").write_text('{"name": "probe"}')
    with Sandbox(allow_game_writes=True) as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        sandbox.nw_game(game, descriptor, saves)
        mounts = _game_mounts(sandbox)
        assert mounts
        for destination, flag in mounts.items():
            assert flag == "--bind", destination
        assert not any(
            sandbox.options[index] == "--remount-ro"
            and sandbox.options[index + 1].startswith("/game")
            for index in range(len(sandbox.options) - 1)
        )
        assert mounts["/game/save"] == "--bind"


def test_game_writable_binds_pinned_tree_writable(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "data").write_text("fixture")
    with Sandbox() as sandbox:
        descriptor = open_directory_without_symlinks(root)
        sandbox.game_writable(descriptor)
        assert ["--bind", f"/proc/self/fd/{descriptor}", "/game"] in [
            sandbox.options[index : index + 3] for index in range(len(sandbox.options))
        ]
        assert "--remount-ro" not in sandbox.options
        assert descriptor in sandbox.pass_fds


def test_game_writable_rejects_unsafe_tree(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "escape").symlink_to("../../secret")
    with pytest.raises(LaunchError), Sandbox() as sandbox:
        descriptor = open_directory_without_symlinks(root)
        try:
            sandbox.game_writable(descriptor)
        finally:
            os.close(descriptor)


@pytest.mark.parametrize(
    ("wayland", "runtime", "display", "expected"),
    [
        ("wayland-0", "/run/user/1000", "", "wayland"),
        ("wayland-0", "/run/user/1000", ":0", "wayland"),
        ("", "/run/user/1000", ":0", "x11"),
        ("", "/run/user/1000", ":0.0", "x11"),
        ("", "", ":0", "x11"),
        ("", "/run/user/1000", "", "none"),
        ("../evil", "/run/user/1000", "", "none"),
        ("wayland-0", "relative/path", ":0", "x11"),
        ("wayland-0", "relative/path", "", "none"),
    ],
)
def test_display_probe_classifies_from_env_only(
    monkeypatch: pytest.MonkeyPatch, wayland: str, runtime: str, display: str, expected: str
) -> None:
    if wayland:
        monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    else:
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", display)
    else:
        monkeypatch.delenv("DISPLAY", raising=False)
    with Sandbox() as sandbox:
        assert sandbox.display_probe() == expected


def _write_xauthority(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    cookie = home / ".Xauthority"
    cookie.write_bytes(b"cookie")
    cookie.chmod(0o600)
    return cookie


@pytest.mark.parametrize("display", [":0", ":0.0"])
def test_x11_accepts_local_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, display: str
) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    number = display[1:].split(".")[0]
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / f"X{number}"))
        home = tmp_path / "home"
        _write_xauthority(home)
        monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
        monkeypatch.setenv("DISPLAY", display)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XAUTHORITY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        with Sandbox() as sandbox:
            before = len(sandbox.options)
            sandbox.x11()
            extra = sandbox.options[before:]
            assert extra.count("--ro-bind") == 2
            assert f"/tmp/.X11-unix/X{number}" in extra
            assert "/home/sandbox/.Xauthority" in extra
            assert "DISPLAY" in extra and display in extra
            assert "SDL_VIDEODRIVER" in extra and "x11" in extra
            assert "WAYLAND_DISPLAY" in extra


@pytest.mark.parametrize(
    "display",
    ["localhost:0", "host:0", "unix:0", "/tmp/.X11-unix/X0", ":0@", "@:0", "0", ":", ":a", ":0-0"],
)
def test_x11_rejects_hosts_paths_and_abstract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, display: str
) -> None:
    home = tmp_path / "home"
    _write_xauthority(home)
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XAUTHORITY", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.x11()


def test_x11_rejects_symlink_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    (x11_dir / "X7").symlink_to("elsewhere")
    home = tmp_path / "home"
    _write_xauthority(home)
    monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XAUTHORITY", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Ss]ocket"):
        sandbox.x11()


def test_x11_rejects_non_socket_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    (x11_dir / "X8").write_text("not a socket")
    home = tmp_path / "home"
    _write_xauthority(home)
    monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
    monkeypatch.setenv("DISPLAY", ":8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XAUTHORITY", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Ss]ocket"):
        sandbox.x11()


def test_x11_proceeds_without_cookie_when_server_needs_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / "X9"))
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
        monkeypatch.setenv("DISPLAY", ":9")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XAUTHORITY", raising=False)
        with Sandbox() as sandbox:
            before = len(sandbox.options)
            sandbox.x11()
            extra = sandbox.options[before:]
            assert "/tmp/.X11-unix/X9" in extra
            assert "XAUTHORITY" not in extra


def test_x11_rejects_unusable_explicit_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / "X9"))
        monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
        monkeypatch.setenv("DISPLAY", ":9")
        monkeypatch.setenv("XAUTHORITY", str(tmp_path / "missing-cookie"))
        with Sandbox() as sandbox, pytest.raises(LaunchError, match="xauth"):
            sandbox.x11()


def test_x11_rejects_group_writable_cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / "X6"))
        home = tmp_path / "home"
        cookie = _write_xauthority(home)
        cookie.chmod(0o664)
        monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
        monkeypatch.setenv("DISPLAY", ":6")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XAUTHORITY", raising=False)
        with Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Cc]ookie"):
            sandbox.x11()


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o666])
def test_x11_rejects_group_or_other_readable_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / "X6"))
        home = tmp_path / "home"
        cookie = _write_xauthority(home)
        cookie.chmod(mode)
        monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
        monkeypatch.setenv("DISPLAY", ":6")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XAUTHORITY", raising=False)
        with Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Cc]ookie"):
            sandbox.x11()


def test_x11_rejects_group_writable_cookie_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / "X6"))
        home = tmp_path / "home"
        _write_xauthority(home)
        home.chmod(0o775)
        monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
        monkeypatch.setenv("DISPLAY", ":6")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XAUTHORITY", raising=False)
        with Sandbox() as sandbox, pytest.raises(LaunchError, match="Xauthority"):
            sandbox.x11()


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_x11_rejects_non_regular_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / "X6"))
        home = tmp_path / "home"
        home.mkdir()
        if kind == "symlink":
            (home / "real-cookie").write_bytes(b"cookie")
            (home / ".Xauthority").symlink_to(home / "real-cookie")
        else:
            (home / ".Xauthority").mkdir()
        monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
        monkeypatch.setenv("DISPLAY", ":6")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XAUTHORITY", raising=False)
        with Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Cc]ookie"):
            sandbox.x11()


def test_x11_rejects_relative_authority_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "relative/cookie")
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="absolute"):
        sandbox.x11()


def test_x11_rejects_missing_home_without_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="xauth"):
        sandbox.x11()


def test_x11_rejects_missing_socket_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", tmp_path / "missing-x11")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="socket directory"):
        sandbox.x11()


def test_devices_skips_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", tmp_path / "missing-dri")
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.devices()
        assert sandbox.options[before:] == []


def test_devices_rejects_symlink_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "dri-link"
    link.symlink_to(real)
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", link)
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.devices()


def test_devices_rejects_non_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = tmp_path / "not-a-dir"
    node.write_text("fixture")
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", node)
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.devices()


def test_dri_node_predicate_accepts_only_character_devices() -> None:
    assert _is_dri_node(stat.S_IFCHR | 0o660)
    assert not _is_dri_node(stat.S_IFREG | 0o644)
    assert not _is_dri_node(stat.S_IFDIR | 0o755)
    assert not _is_dri_node(stat.S_IFLNK | 0o777)
    assert not _is_dri_node(stat.S_IFIFO | 0o600)
    assert not _is_dri_node(stat.S_IFSOCK | 0o700)


def test_devices_exposes_only_gpu_nodes_via_dev_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "card0").write_text("fake-node")
    (dri / "renderD128").write_text("fake-node")
    (dri / "notes.txt").write_text("ignore me")
    udev = tmp_path / "udev"
    udev.mkdir()
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", dri)
    monkeypatch.setattr("box.launch.sandbox._UDEV_DIR", udev)

    def accept_fake_node(mode: int) -> bool:
        """Test fakes are regular files; real DRI nodes are character devices."""
        assert isinstance(mode, int)
        return True

    monkeypatch.setattr("box.launch.sandbox._is_dri_node", accept_fake_node)
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.devices()
        extra = sandbox.options[before:]
        assert "--dir" in extra and "/dev/dri" in extra
        assert "--dev-bind" in extra
        assert "--bind" not in extra
        assert "/dev/dri/card0" in extra
        assert "/dev/dri/renderD128" in extra
        assert "/dev/dri/notes.txt" not in extra
        assert "/sys" in extra
        assert "/run/udev" in extra
        ro_destinations = {
            extra[index + 2] for index, flag in enumerate(extra) if flag == "--ro-bind"
        }
        assert ro_destinations == {"/sys", "/run/udev"}


def _host_dri_usable() -> bool:
    """Check for one readable host DRI node without touching the sandbox."""
    try:
        with os.scandir("/dev/dri") as entries:
            names = [entry.name for entry in entries]
    except OSError:
        return False
    targets = [name for name in names if _DRI_NODE_NAME.fullmatch(name)]
    if not targets:
        return False
    try:
        descriptor = os.open(f"/dev/dri/{min(targets)}", os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(descriptor)
    return True


def test_real_runtime_and_home_writable_and_sys_visible() -> None:
    """Harmless probe: XDG_RUNTIME_DIR/HOME accept writes; /sys iff host DRI."""
    probe = (
        "import pathlib\n"
        "pathlib.Path('/run/user/dconf-test').write_text('writable')\n"
        "pathlib.Path('/home/sandbox/cache-test').write_text('writable')\n"
        "print('drm-visible' if pathlib.Path('/sys/class/drm').exists() else 'drm-hidden')\n"
    )
    with Sandbox() as sandbox:
        if _host_dri_usable():
            sandbox.devices()
            expected = "drm-visible"
        else:
            expected = "drm-hidden"
        result = subprocess.run(
            sandbox.command(["/usr/bin/python3", "-I", "-S", "-c", probe]),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_devices_skips_udev_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "card0").write_text("fake-node")
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", dri)
    monkeypatch.setattr("box.launch.sandbox._UDEV_DIR", tmp_path / "missing-udev")

    def accept_fake_node(mode: int) -> bool:
        return True

    monkeypatch.setattr("box.launch.sandbox._is_dri_node", accept_fake_node)
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.devices()
        extra = sandbox.options[before:]
        assert "/sys" in extra
        assert "/run/udev" not in extra
        assert "/dev/dri/card0" in extra


def test_devices_rejects_symlink_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "card0").symlink_to("elsewhere")
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", dri)
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.devices()


@pytest.mark.parametrize(
    "name", ["cardboard", "card", "card-evil", "renderD", "renderD-evil", "notes"]
)
def test_devices_ignores_off_spec_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / name).write_text("not a node")
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", dri)
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.devices()
        assert sandbox.options[before:] == []


def test_devices_rejects_swapped_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "card0").write_text("listed")
    (dri / "other").write_text("swapped")
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", dri)
    real_open = os.open

    def swapping_open(path: object, *args: object, **kwargs: object) -> int:
        if path == "card0":
            return real_open("other", *args, **kwargs)  # type: ignore[arg-type]
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", swapping_open)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="changed"):
        sandbox.devices()


def test_devices_rejects_non_character_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "card0").write_text("regular file, not a device")
    monkeypatch.setattr("box.launch.sandbox._DRI_DIR", dri)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="device node"):
        sandbox.devices()


def test_audio_skips_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.audio()
        assert sandbox.options[before:] == []


def test_audio_skips_without_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative/path")
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.audio()
        assert sandbox.options[before:] == []


def test_audio_exposes_pipewire_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(runtime / "pipewire-0"))
        with Sandbox() as sandbox:
            before = len(sandbox.options)
            sandbox.audio()
            extra = sandbox.options[before:]
            assert extra[0] == "--ro-bind"
            assert extra[2] == "/run/user/pipewire-0"


def test_audio_supports_custom_remote_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PIPEWIRE_REMOTE", "custom-0")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(runtime / "custom-0"))
        with Sandbox() as sandbox:
            before = len(sandbox.options)
            sandbox.audio()
            extra = sandbox.options[before:]
            assert "/run/user/custom-0" in extra
            assert "PIPEWIRE_REMOTE" in extra and "custom-0" in extra


def test_audio_rejects_symlink_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    (runtime / "pipewire-0").symlink_to("elsewhere")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Ss]ocket"):
        sandbox.audio()


def test_audio_rejects_unsafe_remote_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PIPEWIRE_REMOTE", "../evil")
    with Sandbox() as sandbox, pytest.raises(LaunchError):
        sandbox.audio()


def test_audio_rejects_group_writable_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o775)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match="runtime directory"):
        sandbox.audio()


def _write_pulse_fixture(
    runtime: Path, home: Path, *, cookie_mode: int = 0o600
) -> tuple[socket.socket, Path]:
    pulse = runtime / "pulse"
    pulse.mkdir()
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(pulse / "native"))
    config = home / ".config" / "pulse"
    config.mkdir(parents=True)
    cookie = config / "cookie"
    cookie.write_bytes(b"pulse-cookie")
    cookie.chmod(cookie_mode)
    return server, cookie


def test_audio_exposes_pulse_socket_and_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    home = tmp_path / "home"
    server, _cookie = _write_pulse_fixture(runtime, home)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with server, Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.audio()
        extra = sandbox.options[before:]
        assert "/run/user/pulse/native" in extra
        assert "PULSE_SERVER" in extra and "unix:/run/user/pulse/native" in extra
        assert "PULSE_COOKIE" in extra and "/home/sandbox/.pulse-cookie" in extra


def test_audio_skips_pulse_when_directory_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with Sandbox() as sandbox:
        before = len(sandbox.options)
        sandbox.audio()
        assert sandbox.options[before:] == []


def test_audio_proceeds_without_pulse_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    (runtime / "pulse").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(runtime / "pulse" / "native"))
        with Sandbox() as sandbox:
            before = len(sandbox.options)
            sandbox.audio()
            extra = sandbox.options[before:]
            assert "/run/user/pulse/native" in extra
            assert "PULSE_COOKIE" not in extra


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o666])
def test_audio_rejects_group_or_other_readable_pulse_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    home = tmp_path / "home"
    server, _cookie = _write_pulse_fixture(runtime, home, cookie_mode=mode)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with server, Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Cc]ookie"):
        sandbox.audio()


def test_audio_rejects_group_writable_pulse_cookie_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    home = tmp_path / "home"
    server, _cookie = _write_pulse_fixture(runtime, home)
    (home / ".config" / "pulse").chmod(0o775)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with server, Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Cc]ookie"):
        sandbox.audio()


def test_audio_rejects_symlink_pulse_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    runtime.chmod(0o700)
    (runtime / "pulse").mkdir()
    (runtime / "pulse" / "native").symlink_to("elsewhere")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
    with Sandbox() as sandbox, pytest.raises(LaunchError, match=r"[Ss]ocket"):
        sandbox.audio()


def test_real_writable_game_probe(tmp_path: Path) -> None:
    """Harmless probe: writable /game accepts writes, save alias still works."""
    root = tmp_path / "game"
    game = _make_game_tree(root)
    (root / "package.json").write_text('{"name": "writable-probe"}')
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    probe = runtime / "probe"
    probe.write_text(
        "#!/usr/bin/python3\n"
        "from pathlib import Path\n"
        "Path('/game/package.json').write_text('writable')\n"
        "assert Path('/game/package.json').read_text() == 'writable'\n"
        "Path('/game/save/slot').write_text('saved')\n"
        "assert Path('/saves/slot').read_text() == 'saved'\n"
        "print('writable-ok')\n"
    )
    probe.chmod(0o700)
    with Sandbox(allow_game_writes=True) as sandbox:
        executable = sandbox.runtime(probe)
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        sandbox.nw_game(game, descriptor, saves)
        result = subprocess.run(
            sandbox.command([executable]),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "writable-ok\n"
    assert (root / "package.json").read_text() == "writable"


def test_real_x11_socket_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Harmless probe: X11 socket and cookie are visible after x11()."""
    x11_dir = tmp_path / "x11"
    x11_dir.mkdir()
    home = tmp_path / "home"
    _write_xauthority(home)
    monkeypatch.setattr("box.launch.sandbox._X11_SOCKET_DIR", x11_dir)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XAUTHORITY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(x11_dir / "X99"))
        with Sandbox() as sandbox:
            sandbox.x11()
            script = (
                "import os, stat\n"
                "assert os.environ.get('DISPLAY') == ':99'\n"
                "assert os.environ.get('SDL_VIDEODRIVER') == 'x11'\n"
                "assert 'WAYLAND_DISPLAY' not in os.environ\n"
                "assert stat.S_ISSOCK(os.stat('/tmp/.X11-unix/X99').st_mode)\n"
                "assert os.environ.get('XAUTHORITY') == '/home/sandbox/.Xauthority'\n"
                "open('/home/sandbox/.Xauthority').read()\n"
                "print('x11-ok')\n"
            )
            result = subprocess.run(
                sandbox.command(["/usr/bin/python3", "-I", "-S", "-c", script]),
                pass_fds=sandbox.pass_fds,
                env=clean_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == "x11-ok\n"


def test_real_dri_listing_when_host_has_it() -> None:
    """Harmless probe: list /dev/dri when the host provides it, else skip."""
    from box.launch.sandbox import _DRI_DIR

    if not _DRI_DIR.is_dir():
        pytest.skip("host has no /dev/dri")
    expected = sorted(
        name
        for name in os.listdir(_DRI_DIR)
        if name.startswith("card") or name.startswith("renderD")
    )
    if not expected:
        pytest.skip("host /dev/dri has no GPU nodes")
    with Sandbox() as sandbox:
        sandbox.devices()
        assert "--dev-bind" in sandbox.options
        script = (
            f"import os\nassert sorted(os.listdir('/dev/dri')) == {expected!r}\nprint('dri-ok')\n"
        )
        result = subprocess.run(
            sandbox.command(["/usr/bin/python3", "-I", "-S", "-c", script]),
            pass_fds=sandbox.pass_fds,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "dri-ok\n"


def test_nw_game_names_unreadable_asset_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup miss mid-walk reports the sandbox destination, not a bare name."""
    root = tmp_path / "game"
    parent = root / "www"
    parent.mkdir(parents=True)
    (parent / "index.html").write_text("fixture")
    (root / "vanishing.dat").write_text("asset")
    game = GameInfo(EngineName.RPG_MAKER_MV, root, parent / "index.html")
    real_open: Callable[..., int] = os.open

    def flaky_open(
        path: str | int | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str) and path == "vanishing.dat" and dir_fd is not None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", flaky_open)
    with Sandbox() as sandbox:
        descriptor = sandbox.keep(open_directory_without_symlinks(root))
        saves = sandbox.game_saves(game, descriptor)
        with pytest.raises(LaunchError, match="/game/vanishing"):
            sandbox.nw_game(game, descriptor, saves)


def test_validate_tree_names_unreadable_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup miss during validation reports the walked path, not a bare name."""
    root = tmp_path / "game"
    nested = root / "www" / "audio"
    nested.mkdir(parents=True)
    (nested / "vanishing.dat").write_text("asset")
    real_stat: Callable[..., os.stat_result] = os.stat

    def flaky_stat(
        path: str | int | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if isinstance(path, str) and path == "vanishing.dat" and dir_fd is not None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", flaky_stat)
    descriptor = open_directory_without_symlinks(root)
    try:
        with pytest.raises(LaunchError, match=r"\./www/audio/vanishing"):
            validate_tree(descriptor)
    finally:
        os.close(descriptor)
