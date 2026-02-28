"""Sandbox lifecycle — create, start, stop, remove, list."""

from __future__ import annotations

import os
import sys

from ccbox import lxd
from ccbox.config import Config, SandboxEntry
from ccbox.mount import add_auto_mounts, add_mount, ensure_uv_shim
from ccbox.session import list_sessions
from ccbox.uv_server import ensure_server_running

CONTAINER_PREFIX = "ccbox-"
BASE_IMAGE = "ccbox-base"
IDMAP_VALUE = "both 1000 1000"


def container_name(sandbox_name: str) -> str:
    return f"{CONTAINER_PREFIX}{sandbox_name}"


def create_sandbox(
    config: Config,
    name: str,
    mounts: list[tuple[str, bool]] | None = None,
) -> str:
    """Create a new sandbox.

    Args:
        config: Config instance
        name: Sandbox name
        mounts: List of (path, readonly) tuples for user mounts

    Returns:
        Container name
    """
    cname = container_name(name)

    if config.get_sandbox(name) is not None:
        raise ValueError(f"Sandbox '{name}' already exists")

    if not lxd.image_exists(BASE_IMAGE):
        print("Base image not found. Run 'ccbox init' first.", file=sys.stderr)
        raise SystemExit(1)

    # Ensure uv shim and server are ready before creating container
    ensure_uv_shim()
    ensure_server_running()

    # Init container from base image (don't start yet — configure first)
    lxd.init_container(BASE_IMAGE, cname, storage=config.state.storage_pool)

    # Set UID mapping
    lxd.set_config(cname, "raw.idmap", IDMAP_VALUE)

    # Add auto-mounts (claude tooling + user-configured)
    add_auto_mounts(cname, config)

    # Register in config before user mounts (so add_mount can find it)
    entry = SandboxEntry(container=cname)
    config.set_sandbox(name, entry)

    # Add user-requested mounts
    if mounts:
        for path, readonly in mounts:
            add_mount(config, name, path, readonly)

    # Now start
    lxd.start(cname)

    return cname


def ensure_running(config: Config, name: str) -> str:
    """Ensure sandbox is running. Returns container name."""
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")

    state = lxd.container_state(entry.container)
    if state == "NotFound":
        # Container deleted externally — clean up config
        config.remove_sandbox(name)
        raise ValueError(f"Container for sandbox '{name}' no longer exists. Removed from config.")
    if state == "Stopped":
        ensure_uv_shim()
        ensure_server_running()
        lxd.start(entry.container)
    return entry.container


def stop_sandbox(config: Config, name: str) -> None:
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")
    state = lxd.container_state(entry.container)
    if state == "Running":
        lxd.stop(entry.container)


def remove_sandbox(config: Config, name: str) -> None:
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")
    # Container may already be gone (deleted externally)
    if lxd.container_exists(entry.container):
        lxd.delete(entry.container, force=True)
    config.remove_sandbox(name)


def list_sandboxes(config: Config) -> list[dict]:
    """List all sandboxes with their state and session count.

    Detects state/LXD mismatches (container deleted externally).
    """
    result = []
    stale = []
    for name, entry in config.state.sandboxes.items():
        state = lxd.container_state(entry.container)
        if state == "NotFound":
            stale.append(name)
            continue
        sessions = 0
        if state == "Running":
            sessions = len(list_sessions(entry.container))
        result.append({
            "name": name,
            "container": entry.container,
            "state": state,
            "sessions": sessions,
            "mounts": len(entry.mounts),
        })
    # Clean up stale entries
    for name in stale:
        print(f"Warning: sandbox '{name}' container no longer exists. Removing from config.",
              file=sys.stderr)
        config.remove_sandbox(name)
    return result


def sandbox_status(config: Config, name: str) -> dict:
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")

    state = lxd.container_state(entry.container)
    sessions = []
    if state == "Running":
        sessions = list_sessions(entry.container)

    return {
        "name": name,
        "container": entry.container,
        "state": state,
        "sessions": sessions,
        "mounts": [m.to_dict() for m in entry.mounts],
    }


def resolve_sandbox(config: Config, name: str | None) -> str:
    """Resolve sandbox name. If None, find from CWD."""
    if name is not None:
        if config.get_sandbox(name) is None:
            raise ValueError(f"Sandbox '{name}' not found")
        return name

    found = config.sandbox_for_path(os.getcwd())
    if found is not None:
        return found

    raise ValueError("No sandbox specified and none found for current directory")


def auto_sandbox_name_from_cwd() -> str:
    """Generate a sandbox name from the current working directory basename."""
    base = os.path.basename(os.getcwd())
    # Sanitize: only keep alphanumeric, dash, underscore
    sanitized = ""
    for c in base:
        if c.isalnum() or c in "-_":
            sanitized += c
    return sanitized or "default"
