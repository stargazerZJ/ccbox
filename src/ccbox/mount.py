"""Mount management — adding/removing disk devices on containers."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ccbox import lxd
from ccbox.config import Config, MountEntry

# Directories auto-mounted into every sandbox
AUTO_MOUNTS = [
    (Path.home() / ".claude", "rw"),
    (Path.home() / ".local" / "bin", "ro"),
    (Path.home() / ".local" / "share" / "claude", "ro"),
    (Path.home() / ".cache" / "uv", "ro"),
]


def device_name_from_path(path: str) -> str:
    """Sanitize a path into an LXD device name.

    /home/zj/Projects/X -> mount-home-zj-Projects-X
    """
    clean = path.strip("/")
    clean = re.sub(r"[^a-zA-Z0-9_.-]", "-", clean)
    return f"mount-{clean}"


def add_mount(
    config: Config,
    sandbox_name: str,
    path: str,
    readonly: bool = False,
) -> None:
    """Add a mount to a sandbox (both LXD device and config state)."""
    entry = config.get_sandbox(sandbox_name)
    if entry is None:
        raise ValueError(f"Sandbox '{sandbox_name}' not found")

    resolved = os.path.realpath(path)
    if not os.path.isdir(resolved):
        raise ValueError(f"Path is not a directory: {resolved}")

    mode = "ro" if readonly else "rw"
    dev_name = device_name_from_path(resolved)

    # Add LXD disk device (identity-mapped: host path = container path)
    lxd.add_disk_device(
        entry.container, dev_name, resolved, resolved, readonly=readonly,
    )

    # Update config
    # Remove existing mount for same path if any
    entry.mounts = [m for m in entry.mounts if os.path.realpath(m.path) != resolved]
    entry.mounts.append(MountEntry(path=resolved, mode=mode))
    config.set_sandbox(sandbox_name, entry)


def remove_mount(config: Config, sandbox_name: str, path: str) -> None:
    """Remove a mount from a sandbox."""
    entry = config.get_sandbox(sandbox_name)
    if entry is None:
        raise ValueError(f"Sandbox '{sandbox_name}' not found")

    resolved = os.path.realpath(path)
    dev_name = device_name_from_path(resolved)

    lxd.remove_disk_device(entry.container, dev_name)

    entry.mounts = [m for m in entry.mounts if os.path.realpath(m.path) != resolved]
    config.set_sandbox(sandbox_name, entry)


def add_auto_mounts(container: str) -> None:
    """Add standard auto-mounts for claude tooling."""
    for mount_path, mode in AUTO_MOUNTS:
        resolved = str(mount_path)
        if not os.path.isdir(resolved):
            os.makedirs(resolved, exist_ok=True)
        dev_name = device_name_from_path(resolved)
        lxd.add_disk_device(
            container, dev_name, resolved, resolved, readonly=(mode == "ro"),
        )
