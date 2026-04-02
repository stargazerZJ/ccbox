"""Host-side hardlink server for ccbox.

Listens on a Unix socket. The patched uv running inside a container
connects and requests hardlinks that can't cross mount boundaries.
The server performs the hardlink on the host filesystem and responds.

Protocol (newline-delimited JSON over Unix socket):
  Request:  {"src": "/path/to/cache/file", "dst": "/path/to/venv/file"}
  Response: {"ok": true} or {"ok": false, "error": "..."}

Security: src must be under ~/.cache/uv, dst must be under a registered
sandbox mount path. Both paths are resolved to prevent traversal.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
from pathlib import Path

from ccbox.config import RUN_DIR, UV_SOCK, Config

PID_FILE = RUN_DIR / "uv-server.pid"

# Resolved at server start
_UV_CACHE = str(Path.home() / ".cache" / "uv")


def _allowed_dst_prefixes() -> list[str]:
    """Return resolved paths of all rw sandbox mounts (dst targets)."""
    config = Config()
    prefixes = []
    for entry in config.state.sandboxes.values():
        for m in entry.mounts:
            if m.mode == "rw":
                resolved = os.path.realpath(m.target or m.path)
                prefixes.append(resolved)
    return prefixes


def _validate_paths(src: str, dst: str, dst_prefixes: list[str]) -> str | None:
    """Return an error message if paths are invalid, None if OK."""
    # Must be absolute
    if not os.path.isabs(src) or not os.path.isabs(dst):
        return "paths must be absolute"

    # Resolve to prevent traversal via symlinks or ..
    src_resolved = os.path.realpath(src)
    dst_resolved = os.path.realpath(os.path.dirname(dst))
    dst_resolved = os.path.join(dst_resolved, os.path.basename(dst))

    # src must be under uv cache
    if not src_resolved.startswith(_UV_CACHE + "/"):
        return f"src not under uv cache: {src_resolved}"

    # dst must be under a sandbox mount
    if not any(dst_resolved.startswith(p + "/") or dst_resolved == p for p in dst_prefixes):
        return f"dst not under any sandbox mount: {dst_resolved}"

    return None


def _handle_client(conn: socket.socket, dst_prefixes: list[str]) -> None:
    try:
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk

        req = json.loads(data.decode())
        src = req["src"]
        dst = req["dst"]

        if not isinstance(src, str) or not isinstance(dst, str):
            raise ValueError("src and dst must be strings")

        error = _validate_paths(src, dst, dst_prefixes)
        if error:
            conn.sendall(json.dumps({"ok": False, "error": error}).encode() + b"\n")
            return

        # Ensure parent directory exists
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.link(src, dst)
        conn.sendall(b'{"ok":true}\n')

    except KeyError as e:
        conn.sendall(json.dumps({"ok": False, "error": f"missing field: {e}"}).encode() + b"\n")
    except OSError as e:
        conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
    except Exception as e:
        try:
            conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
        except OSError:
            pass
    finally:
        conn.close()


def run_server() -> None:
    """Run the hardlink server (blocking, intended to be daemonized)."""
    dst_prefixes = _allowed_dst_prefixes()

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if UV_SOCK.exists():
        UV_SOCK.unlink()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(UV_SOCK))
    # Make socket accessible to container user (UID 1000)
    os.chmod(str(UV_SOCK), 0o666)
    sock.listen(8)

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    def _shutdown(signum, frame):
        sock.close()
        if UV_SOCK.exists():
            UV_SOCK.unlink()
        if PID_FILE.exists():
            PID_FILE.unlink()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while True:
            conn, _ = sock.accept()
            threading.Thread(
                target=_handle_client,
                args=(conn, dst_prefixes),
                daemon=True,
            ).start()
    except OSError:
        pass  # socket closed by signal handler
    finally:
        if UV_SOCK.exists():
            UV_SOCK.unlink()
        if PID_FILE.exists():
            PID_FILE.unlink()


def ensure_server_running() -> None:
    """Start the uv server if it's not already running."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # check if alive
            return  # already running
        except (ProcessLookupError, ValueError):
            # Stale PID file
            PID_FILE.unlink(missing_ok=True)
            UV_SOCK.unlink(missing_ok=True)

    # Fork a daemon process
    pid = os.fork()
    if pid == 0:
        # Child: detach and run server
        os.setsid()
        # Fork again to fully daemonize
        pid2 = os.fork()
        if pid2 > 0:
            os._exit(0)
        # Redirect stdio
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        run_server()
        os._exit(0)
    else:
        # Parent: wait for child (intermediate fork)
        os.waitpid(pid, 0)
        # Brief wait for the socket to appear
        import time

        for _ in range(20):
            if UV_SOCK.exists():
                return
            time.sleep(0.1)


def stop_server() -> None:
    """Stop the uv server if running."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        PID_FILE.unlink(missing_ok=True)
    UV_SOCK.unlink(missing_ok=True)
