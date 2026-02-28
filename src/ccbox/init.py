"""Base image creation — ccbox init command."""

from __future__ import annotations

import getpass
import importlib.resources
import os
import shlex
import sys

from ccbox import lxd
from ccbox.mount import add_auto_mounts

TEMP_CONTAINER = "ccbox-init-temp"
BASE_IMAGE = "ccbox-base"
BASE_OS_IMAGE = "ubuntu:24.04"
IDMAP_VALUE = "both 1000 1000"


def _asset_path(name: str) -> str:
    """Get path to a bundled asset file."""
    ref = importlib.resources.files("ccbox").parent.parent / "assets" / name
    return str(ref)


def check_prerequisites() -> None:
    """Verify LXD is accessible."""
    r = lxd.run_lxc("version", check=False, capture=True)
    if r.returncode != 0:
        print("Error: Cannot access LXD. Make sure:", file=sys.stderr)
        print("  1. LXD/snap is installed", file=sys.stderr)
        print("  2. Your user is in the 'lxd' group", file=sys.stderr)
        print("  3. You've re-logged after adding to the group", file=sys.stderr)
        raise SystemExit(1)


def _bootstrap(container: str, username: str) -> None:
    """Minimal inline bootstrap: rename ubuntu user, configure sudo and PATH.

    Ubuntu 24.04 ships with user 'ubuntu' at UID 1000. We rename it to
    match the host user so identity-mapped mounts work seamlessly.
    """
    # Rename ubuntu -> username (user may already match)
    r = lxd.exec_cmd(container, ["id", "-un", "1000"], capture=True, check=False)
    existing = r.stdout.strip() if r.returncode == 0 else ""

    if existing and existing != username:
        # Kill any processes owned by the user before renaming
        lxd.exec_cmd(container, ["pkill", "-u", existing], check=False, capture=True)
        lxd.exec_cmd(container, ["usermod", "-l", username, "-d", f"/home/{username}",
                                  "-m", existing])
        lxd.exec_cmd(container, ["groupmod", "-n", username, existing])
    elif not existing:
        lxd.exec_cmd(container, ["useradd", "-m", "-s", "/bin/bash", username])

    # NOPASSWD sudo
    lxd.exec_cmd(container, ["bash", "-c",
        f"echo '{username} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/{username} "
        f"&& chmod 0440 /etc/sudoers.d/{username}"])

    # Create mount-point stubs
    dirs = [".local/bin", ".local/share/claude", ".cache/uv", ".claude"]
    for d in dirs:
        lxd.exec_cmd(container, ["mkdir", "-p", f"/home/{username}/{d}"])
    lxd.exec_cmd(container, ["chown", "-R", f"{username}:{username}", f"/home/{username}"])

    # PATH + disable XON/XOFF (for Ctrl+Q tmux detach)
    bashrc_snippet = (
        '\n# ccbox\n'
        'export PATH="$HOME/.local/bin:$PATH"\n'
        'stty -ixon 2>/dev/null || true\n'
        '[ -n "$CCBOX_CWD" ] && cd "$CCBOX_CWD"\n'
    )
    lxd.exec_cmd(container, ["bash", "-c",
        f"echo {shlex.quote(bashrc_snippet)} >> /home/{username}/.bashrc"])


def run_init(force: bool = False, storage_pool: str | None = None) -> None:
    """Create the ccbox-base image interactively.

    1. Launch temp container, minimal bootstrap (rename user, sudo, PATH)
    2. Mount claude binary, push tmux.conf
    3. Ask user for instructions, run Claude inside to do the setup
    4. Drop into shell for manual tweaks
    5. Publish as ccbox-base
    """
    check_prerequisites()

    if lxd.image_exists(BASE_IMAGE) and not force:
        print(f"Base image '{BASE_IMAGE}' already exists. Use 'ccbox init --force' to rebuild.")
        return

    username = getpass.getuser()
    container_user = "1000"

    # Clean up any leftover temp container
    if lxd.container_exists(TEMP_CONTAINER):
        print(f"Cleaning up leftover '{TEMP_CONTAINER}'...")
        lxd.delete(TEMP_CONTAINER, force=True)

    try:
        # --- Launch ---
        print(f"Creating temporary container from {BASE_OS_IMAGE}...")
        launch_args = ["launch", BASE_OS_IMAGE, TEMP_CONTAINER]
        if storage_pool:
            launch_args += ["-s", storage_pool]
        lxd.run_lxc(*launch_args)

        print("Waiting for container to be ready...")
        lxd.exec_cmd(TEMP_CONTAINER, ["cloud-init", "status", "--wait"],
                      check=False, capture=True)

        # --- UID mapping ---
        lxd.set_config(TEMP_CONTAINER, "raw.idmap", IDMAP_VALUE)

        # --- Minimal bootstrap ---
        print(f"Bootstrapping (renaming default user to '{username}')...")
        _bootstrap(TEMP_CONTAINER, username)

        # --- tmux.conf ---
        tmux_conf = _asset_path("tmux.conf")
        lxd.push_file(TEMP_CONTAINER, tmux_conf, "/etc/tmux.conf", mode="0644")

        # --- Restart to apply idmap, then add mounts ---
        print("Restarting to apply UID mapping...")
        lxd.stop(TEMP_CONTAINER)
        add_auto_mounts(TEMP_CONTAINER)
        lxd.start(TEMP_CONTAINER)

        # --- Test claude ---
        print("Testing claude binary...")
        r = lxd.exec_cmd(
            TEMP_CONTAINER,
            ["bash", "-lc", "claude --version"],
            user=container_user,
            capture=True,
            check=False,
        )
        if r.returncode == 0:
            print(f"Claude: {r.stdout.strip()}")
        else:
            print("Warning: claude not found. You can install it in the shell later.")

        # --- Collect user instructions for inner Claude ---
        print()
        print("=" * 60)
        print("What should Claude install/configure in the base image?")
        print("Examples:")
        print("  - install tmux git curl build-essential python3")
        print("  - set apt source to mirrors.tuna.tsinghua.edu.cn first")
        print("  - install rust toolchain")
        print()
        print("Enter instructions (empty line to skip, Ctrl+D to finish multi-line):")
        print("=" * 60)

        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        user_instructions = "\n".join(lines).strip()

        if user_instructions:
            prompt = (
                f"You are setting up an Ubuntu 24.04 container for development. "
                f"The user is '{username}' with sudo. "
                f"Follow these instructions:\n\n{user_instructions}\n\n"
                f"Run commands directly. Do not ask for confirmation."
            )
            print(f"\nStarting Claude inside the container...")
            lxd.exec_interactive(
                TEMP_CONTAINER,
                ["bash", "-lc",
                 f"claude --allow-dangerously-skip-permissions -p {shlex.quote(prompt)}"],
                user=container_user,
            )
        else:
            print("No instructions — skipping Claude setup.")

        # --- Interactive shell for manual tweaks ---
        print()
        print("=" * 60)
        print("Dropping into shell. Make any manual changes, then exit.")
        print("The container will be published as the base image.")
        print("=" * 60)
        print()

        lxd.exec_interactive(TEMP_CONTAINER, ["bash", "-l"], user=container_user)

        # --- Publish ---
        print("\nPreparing to publish...")
        lxd.stop(TEMP_CONTAINER)

        # Remove disk devices (they get re-added per sandbox)
        r = lxd.run_lxc("config", "device", "list", TEMP_CONTAINER,
                          capture=True, check=False)
        if r.returncode == 0:
            for dev in r.stdout.strip().splitlines():
                dev = dev.strip()
                if dev and dev.startswith("mount-"):
                    lxd.remove_disk_device(TEMP_CONTAINER, dev)

        print(f"Publishing as '{BASE_IMAGE}'...")
        lxd.publish(TEMP_CONTAINER, BASE_IMAGE, force=force)
        print("Base image created successfully.")

    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up...")
    finally:
        if lxd.container_exists(TEMP_CONTAINER):
            print(f"Removing temporary container '{TEMP_CONTAINER}'...")
            lxd.delete(TEMP_CONTAINER, force=True)
