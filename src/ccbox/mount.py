"""Mount management — adding/removing disk devices on containers."""

from __future__ import annotations

import importlib.resources
import os
import re
import stat
import sys

from ccbox import lxd
from ccbox.config import Config, MountEntry, SHIM_DIR


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
    if not os.path.exists(resolved):
        raise ValueError(f"Path does not exist: {resolved}")

    mode = "ro" if readonly else "rw"
    dev_name = device_name_from_path(resolved)

    lxd.add_disk_device(
        entry.container, dev_name, resolved, resolved,
        readonly=readonly, shift=True,
    )

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


def ensure_uv_shim() -> None:
    """Ensure a uv binary exists at ~/.config/ccbox/bin/uv.

    If a patched binary is already deployed, leave it alone.
    Otherwise, write the legacy Python shim from project assets.
    """
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    shim_path = SHIM_DIR / "uv"

    # If a binary already exists (e.g. patched uv), don't overwrite it
    if shim_path.exists():
        try:
            with open(shim_path, "rb") as f:
                magic = f.read(4)
            if magic == b"\x7fELF":
                return  # patched binary, leave it
        except OSError:
            pass

    # Read shim content from package assets
    asset_ref = importlib.resources.files("ccbox").parent.parent / "assets" / "uv-shim"
    shim_content = asset_ref.read_text()

    # Write if changed or missing
    try:
        if shim_path.exists() and shim_path.read_text() == shim_content:
            return
    except (UnicodeDecodeError, OSError):
        pass
    shim_path.write_text(shim_content)
    shim_path.chmod(shim_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _normalize_mount(m: MountEntry) -> MountEntry | None:
    """Normalize legacy/problematic mount entries before applying to LXD."""
    home = os.path.expanduser("~")
    claude_link = f"{home}/.local/bin/claude"
    claude_json = f"{home}/.claude.json"
    mount_real = os.path.realpath(m.path)

    # .claude.json is rewritten atomically by claude; file mounts break rename().
    if m.target is None and (
        m.path == claude_json or mount_real == os.path.realpath(claude_json)
    ):
        print(
            "Warning: skipping auto-mount for ~/.claude.json; "
            "file mounts block atomic writes and can hang Claude startup.",
            file=sys.stderr,
        )
        return None

    # Preserve claude symlink semantics by mounting ~/.local/bin instead.
    if m.target is None and (
        m.path == claude_link or mount_real == os.path.realpath(claude_link)
    ):
        return MountEntry(path=f"{home}/.local/bin", mode=m.mode)

    return m


def fix_mount_parents(container: str, config: Config | None = None) -> None:
    """Fix ownership of parent directories created by LXD for mount points.

    LXD auto-creates missing parent dirs with d--------- root:root.
    This makes them traversable by the container user.
    """
    if config is not None:
        mounts = config.state.get_auto_mounts()
    else:
        from ccbox.config import _default_auto_mounts
        mounts = _default_auto_mounts()

    # Collect unique parent dirs that need fixing
    parents: set[str] = set()
    for m in mounts:
        target = m.target if m.target is not None else m.path
        parent = os.path.dirname(target)
        while parent and parent != "/":
            parents.add(parent)
            parent = os.path.dirname(parent)

    if not parents:
        return

    # Single exec: chown + chmod all parent dirs (ignore errors for already-correct ones)
    dirs = " ".join(sorted(parents))
    lxd.exec_cmd(
        container,
        ["sh", "-c", f"chown -f 1000:1000 {dirs}; chmod -f 755 {dirs}"],
        check=False,
    )



def add_auto_mounts(container: str, config: Config | None = None) -> None:
    """Add auto-mounts to a container. Reads from config if provided."""
    if config is not None:
        mounts = config.state.get_auto_mounts()
    else:
        from ccbox.config import _default_auto_mounts
        mounts = _default_auto_mounts()

    seen_targets: set[str] = set()
    for raw in mounts:
        m = _normalize_mount(raw)
        if m is None:
            continue
        source = os.path.realpath(m.path)
        # Use original path as container target (not resolved) so symlinks
        # like ~/.local/bin/claude appear at the right path inside container.
        target = m.target if m.target is not None else m.path
        if target in seen_targets:
            continue
        seen_targets.add(target)

        # Create source stubs if they don't exist
        if not os.path.exists(source):
            # For paths ending without extension, assume directory
            os.makedirs(source, exist_ok=True)

        dev_name = device_name_from_path(target)
        lxd.add_disk_device(
            container, dev_name, source, target,
            readonly=(m.mode == "ro"), shift=True,
        )
