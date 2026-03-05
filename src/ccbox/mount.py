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


def _inode_key(path: str) -> str | None:
    """Return 'dev:ino' for a path, or None if it doesn't exist."""
    try:
        st = os.stat(path)
        return f"{st.st_dev}:{st.st_ino}"
    except OSError:
        return None


def _warn_file_mount(path: str) -> None:
    """Warn that bind-mounting a single file won't track replacements."""
    parent = os.path.dirname(path)
    print(
        f"Note: Mounting a single file. LXD bind mounts track by inode — if the\n"
        f"  host file is replaced (e.g. by an editor or sed -i), the container\n"
        f"  will still see the old content. Consider mounting the parent directory\n"
        f"  instead: {parent}",
        file=sys.stderr,
    )


def _container_ino(container: str, path: str) -> int | None:
    """Return the inode number for a path inside the container, or None."""
    r = lxd.exec_cmd(
        container,
        ["stat", "-c", "%i", path],
        capture=True, check=False,
    )
    if r.returncode == 0:
        try:
            return int(r.stdout.strip())
        except ValueError:
            pass
    return None


def prune_stale_mounts(config: Config, sandbox_name: str) -> list[str]:
    """Remove mounts whose host paths no longer exist or whose inode changed.

    A changed inode means the original directory was moved/deleted and a new
    one appeared at the same path — the LXD bind mount still follows the old
    inode, so the device must be removed.

    Returns list of pruned paths (for caller to report).
    """
    entry = config.get_sandbox(sandbox_name)
    if entry is None:
        return []

    pruned: list[str] = []
    keep: list[MountEntry] = []
    for m in entry.mounts:
        reason = None
        if not os.path.exists(m.path):
            reason = "no longer exists on host"
        elif _inode_key(m.path) != m.inode:
            reason = "replaced by a different directory"

        if reason:
            dev_name = device_name_from_path(m.path)
            try:
                lxd.remove_disk_device(entry.container, dev_name)
            except Exception:
                pass  # device may already be gone
            mode_flag = " --ro" if m.mode == "ro" else ""
            pruned.append(m.path)
            print(f"Removing stale mount: {m.path} ({reason})",
                  file=sys.stderr)
            if os.path.exists(m.path):
                print(f"  Re-add: ccbox mount {sandbox_name} {m.path}{mode_flag}",
                      file=sys.stderr)
        else:
            keep.append(m)

    if pruned:
        entry.mounts = keep
        config.set_sandbox(sandbox_name, entry)

    return pruned


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

    # Clean up stale mounts before adding (host paths that no longer exist)
    prune_stale_mounts(config, sandbox_name)

    if os.path.isfile(resolved):
        _warn_file_mount(resolved)

    lxd.add_disk_device(
        entry.container, dev_name, resolved, resolved,
        readonly=readonly, shift=True,
    )

    entry.mounts = [m for m in entry.mounts if os.path.realpath(m.path) != resolved]
    entry.mounts.append(MountEntry(path=resolved, mode=mode, inode=_inode_key(resolved)))
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


def ensure_profile_script() -> None:
    """Deploy assets/ccbox-profile.sh to ~/.config/ccbox/profile.sh.

    Copies on every sandbox start so edits to the asset propagate automatically.
    """
    from ccbox.config import STATE_DIR

    dest = STATE_DIR / "profile.sh"
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    asset_ref = importlib.resources.files("ccbox").parent.parent / "assets" / "ccbox-profile.sh"
    content = asset_ref.read_text()

    try:
        if dest.exists() and dest.read_text() == content:
            return
    except (UnicodeDecodeError, OSError):
        pass
    dest.write_text(content)


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



def sync_auto_mounts(
    config: Config,
    sandbox_name: str,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Sync current auto-mount config to a running sandbox.

    Compares desired auto-mounts against actual LXD devices.
    Adds missing, updates mode-changed, removes stale auto-mount devices
    (but never touches user mounts tracked in SandboxEntry.mounts).

    Returns list of human-readable change descriptions.
    """
    entry = config.get_sandbox(sandbox_name)
    if entry is None:
        raise ValueError(f"Sandbox '{sandbox_name}' not found")

    container = entry.container

    # Desired auto-mounts (after normalization)
    desired: dict[str, tuple[str, str, bool]] = {}  # dev_name -> (source, target, readonly)
    raw_mounts = config.state.get_auto_mounts()
    seen_targets: set[str] = set()
    for raw in raw_mounts:
        m = _normalize_mount(raw)
        if m is None:
            continue
        source = os.path.realpath(m.path)
        target = m.target if m.target is not None else m.path
        if target in seen_targets:
            continue
        seen_targets.add(target)
        if not os.path.exists(source) and m.optional:
            continue
        dev_name = device_name_from_path(target)
        readonly = m.mode == "ro"
        desired[dev_name] = (source, target, readonly)

    # Actual LXD devices
    actual = lxd.list_devices(container)

    # User mount device names (these are sacred — never touch)
    user_dev_names: set[str] = set()
    for m in entry.mounts:
        user_dev_names.add(device_name_from_path(m.path))
        if m.target:
            user_dev_names.add(device_name_from_path(m.target))

    changes: list[str] = []

    # 1. Add missing / update changed / fix stale inodes
    for dev_name, (source, target, readonly) in desired.items():
        if dev_name in user_dev_names:
            continue  # user mount at same path takes precedence
        existing = actual.get(dev_name)
        if existing is None:
            # Missing — add it
            mode_str = "ro" if readonly else "rw"
            changes.append(f"  + {target} ({mode_str})")
            if not dry_run:
                if not os.path.exists(source):
                    if os.path.splitext(source)[1]:
                        os.makedirs(os.path.dirname(source), exist_ok=True)
                        open(source, "a").close()
                    else:
                        os.makedirs(source, exist_ok=True)
                lxd.add_disk_device(
                    container, dev_name, source, target,
                    readonly=readonly, shift=True,
                )
        else:
            needs_readd = False
            reason_parts: list[str] = []

            # Check if mode changed
            is_ro = existing.get("readonly") == "true"
            if is_ro != readonly:
                old_mode = "ro" if is_ro else "rw"
                new_mode = "ro" if readonly else "rw"
                reason_parts.append(f"{old_mode} -> {new_mode}")
                needs_readd = True

            # Check if source inode changed (file was replaced).
            # Bind mounts preserve the inode number, so if the host path
            # now points to a different inode than what the container sees,
            # the file was replaced and the mount is stale.
            if os.path.exists(source) and not os.path.isdir(source):
                host_ino = os.stat(source).st_ino
                container_ino = _container_ino(container, target)
                if container_ino is not None and host_ino != container_ino:
                    reason_parts.append("inode changed")
                    needs_readd = True

            if needs_readd:
                mode_str = "ro" if readonly else "rw"
                reason = ", ".join(reason_parts)
                changes.append(f"  ~ {target} ({reason})")
                if not dry_run:
                    lxd.remove_disk_device(container, dev_name)
                    lxd.add_disk_device(
                        container, dev_name, source, target,
                        readonly=readonly, shift=True,
                    )

    # 2. Remove stale auto-mount devices (in LXD but no longer in config)
    #    Only remove "mount-*" devices that aren't user mounts and aren't desired.
    for dev_name, props in actual.items():
        if not dev_name.startswith("mount-"):
            continue
        if dev_name in desired:
            continue
        if dev_name in user_dev_names:
            continue
        target = props.get("path", "?")
        mode_str = "ro" if props.get("readonly") == "true" else "rw"
        changes.append(f"  - {target} ({mode_str})")
        if not dry_run:
            try:
                lxd.remove_disk_device(container, dev_name)
            except Exception:
                pass

    # Fix parent dirs if we made changes
    if changes and not dry_run:
        fix_mount_parents(container, config)

    return changes


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
            if m.optional:
                continue  # skip optional mounts that don't exist on host
            # Paths with an extension are likely files; others are directories
            if os.path.splitext(source)[1]:
                os.makedirs(os.path.dirname(source), exist_ok=True)
                open(source, "a").close()
            else:
                os.makedirs(source, exist_ok=True)

        dev_name = device_name_from_path(target)
        lxd.add_disk_device(
            container, dev_name, source, target,
            readonly=(m.mode == "ro"), shift=True,
        )
