"""Low-level LXD command wrappers. All LXC interaction goes through this module."""

from __future__ import annotations

import json
import subprocess
import sys

LXC = "/snap/bin/lxc"


def run_lxc(
    *args: str,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an lxc command."""
    cmd = [LXC, *args]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def container_exists(name: str) -> bool:
    r = run_lxc("info", name, check=False, capture=True)
    return r.returncode == 0


def container_state(name: str) -> str:
    """Return 'Running', 'Stopped', or 'NotFound'."""
    r = run_lxc("info", name, check=False, capture=True)
    if r.returncode != 0:
        return "NotFound"
    for line in r.stdout.splitlines():
        if line.startswith("Status:"):
            return line.split(":", 1)[1].strip()
    return "NotFound"


def init_container(image: str, name: str, *, storage: str | None = None) -> None:
    args = ["init", image, name]
    if storage:
        args += ["-s", storage]
    run_lxc(*args)


def start(name: str) -> None:
    run_lxc("start", name)


def stop(name: str) -> None:
    run_lxc("stop", name)


def delete(name: str, force: bool = False) -> None:
    args = ["delete", name]
    if force:
        args.append("--force")
    run_lxc(*args)


def exec_cmd(
    container: str,
    cmd: list[str],
    *,
    user: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Non-interactive exec inside a container."""
    args = ["exec", container]
    if user:
        args += ["--user", user]
    if cwd:
        args += ["--cwd", cwd]
    if env:
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
    args += ["--", *cmd]
    return run_lxc(*args, capture=capture, check=check)


def exec_interactive(
    container: str,
    cmd: list[str],
    *,
    user: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Interactive exec with inherited stdio (subprocess.run, not execvp)."""
    args = [LXC, "exec", container]
    if user:
        args += ["--user", user]
    if cwd:
        args += ["--cwd", cwd]
    if env:
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
    args += ["--", *cmd]
    return subprocess.run(args, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


def add_disk_device(
    container: str,
    dev_name: str,
    source: str,
    path: str,
    readonly: bool = False,
    shift: bool = False,
) -> None:
    args = ["config", "device", "add", container, dev_name, "disk",
            f"source={source}", f"path={path}"]
    if readonly:
        args.append("readonly=true")
    if shift:
        args.append("shift=true")
    run_lxc(*args)


def remove_disk_device(container: str, dev_name: str) -> None:
    run_lxc("config", "device", "remove", container, dev_name)


def push_file(
    container: str,
    local: str,
    remote: str,
    *,
    uid: int | None = None,
    gid: int | None = None,
    mode: str | None = None,
) -> None:
    args = ["file", "push", local, f"{container}{remote}"]
    if uid is not None:
        args += ["--uid", str(uid)]
    if gid is not None:
        args += ["--gid", str(gid)]
    if mode is not None:
        args += ["--mode", mode]
    run_lxc(*args)


def publish(container: str, alias: str, force: bool = False) -> None:
    args = ["publish", container, f"--alias={alias}"]
    if force:
        args.append("--reuse")
    run_lxc(*args)


def image_exists(alias: str) -> bool:
    r = run_lxc("image", "info", alias, check=False, capture=True)
    return r.returncode == 0


def list_containers(prefix: str = "ccbox-") -> list[dict]:
    """List containers matching prefix. Returns parsed JSON."""
    r = run_lxc("list", f"^{prefix}", "--format=json", capture=True)
    return json.loads(r.stdout)


def set_config(container: str, key: str, value: str) -> None:
    run_lxc("config", "set", container, key, value)
