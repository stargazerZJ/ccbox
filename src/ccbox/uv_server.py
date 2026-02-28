"""Host-side uv proxy server.

Listens on a Unix socket. The in-container uv shim connects, sends
args/cwd/env, and the server runs the real uv on the host and streams
output back. This way uv runs on the host filesystem — hardlinks from
cache to .venv work natively without bind-mount boundaries.

Protocol (binary framing over Unix socket):
  Request:  client sends a JSON line terminated by newline
            {"args": [...], "cwd": "...", "env": {"UV_*": "..."}}
  Response: server sends framed chunks:
            1-byte tag (1=stdout, 2=stderr) + 4-byte BE length + data
            Final: 1-byte tag=0 + 4-byte BE exit code
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading

from ccbox.config import RUN_DIR, UV_SOCK

PID_FILE = RUN_DIR / "uv-server.pid"

TAG_EXIT = 0
TAG_STDOUT = 1
TAG_STDERR = 2


def _find_uv() -> str:
    path = shutil.which("uv")
    if not path:
        print("Error: uv binary not found in PATH", file=sys.stderr)
        raise SystemExit(1)
    return path


def _handle_client(conn: socket.socket, uv_binary: str) -> None:
    try:
        # Read JSON request (terminated by newline)
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk

        req = json.loads(data.decode())
        args = req.get("args", [])
        cwd = req.get("cwd")

        # Build environment: host env as base, force hardlink, apply client UV_* overrides
        env = dict(os.environ)
        env["UV_LINK_MODE"] = "hardlink"
        for k, v in req.get("env", {}).items():
            if k.startswith("UV_") or k == "VIRTUAL_ENV":
                env[k] = v

        proc = subprocess.Popen(
            [uv_binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        def _stream(pipe, tag: int) -> None:
            try:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    header = struct.pack("!BI", tag, len(chunk))
                    conn.sendall(header + chunk)
            except (BrokenPipeError, ConnectionResetError):
                proc.kill()

        t_out = threading.Thread(target=_stream, args=(proc.stdout, TAG_STDOUT))
        t_err = threading.Thread(target=_stream, args=(proc.stderr, TAG_STDERR))
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()

        exit_code = proc.wait()
        conn.sendall(struct.pack("!BI", TAG_EXIT, exit_code))

    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        try:
            msg = f"uv-server error: {e}\n".encode()
            conn.sendall(struct.pack("!BI", TAG_STDERR, len(msg)) + msg)
            conn.sendall(struct.pack("!BI", TAG_EXIT, 1))
        except OSError:
            pass
    finally:
        conn.close()


def run_server() -> None:
    """Run the uv proxy server (blocking, intended to be daemonized)."""
    uv_binary = _find_uv()

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
                target=_handle_client, args=(conn, uv_binary), daemon=True,
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
