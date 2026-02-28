"""Base image creation — ccbox init command."""

from __future__ import annotations

import importlib.resources
import sys

from ccbox import lxd
from ccbox.mount import add_auto_mounts

TEMP_CONTAINER = "ccbox-init-temp"
BASE_IMAGE = "ccbox-base"
BASE_OS_IMAGE = "ubuntu:24.04"
CONTAINER_USER = "1000"
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


def run_init(force: bool = False, storage_pool: str | None = None) -> None:
    """Create the ccbox-base image.

    1. Launch temp container from ubuntu:24.04
    2. Push and run setup script
    3. Push tmux.conf
    4. Drop into shell for manual package installs
    5. On exit: stop, publish as ccbox-base, delete temp
    """
    check_prerequisites()

    if lxd.image_exists(BASE_IMAGE) and not force:
        print(f"Base image '{BASE_IMAGE}' already exists. Use 'ccbox init --force' to rebuild.")
        return

    # Clean up any leftover temp container
    if lxd.container_exists(TEMP_CONTAINER):
        print(f"Cleaning up leftover '{TEMP_CONTAINER}'...")
        lxd.delete(TEMP_CONTAINER, force=True)

    try:
        print(f"Creating temporary container from {BASE_OS_IMAGE}...")
        launch_args = ["launch", BASE_OS_IMAGE, TEMP_CONTAINER]
        if storage_pool:
            launch_args += ["-s", storage_pool]
        lxd.run_lxc(*launch_args)

        # Wait for container to be ready
        print("Waiting for container to start...")
        lxd.exec_cmd(TEMP_CONTAINER, ["cloud-init", "status", "--wait"], check=False, capture=True)

        # Set UID mapping
        lxd.set_config(TEMP_CONTAINER, "raw.idmap", IDMAP_VALUE)

        # Push and run setup script
        setup_script = _asset_path("setup-base.sh")
        print("Running setup script...")
        lxd.push_file(TEMP_CONTAINER, setup_script, "/tmp/setup-base.sh", mode="0755")
        lxd.exec_cmd(TEMP_CONTAINER, ["bash", "/tmp/setup-base.sh"])

        # Push tmux.conf
        tmux_conf = _asset_path("tmux.conf")
        lxd.push_file(TEMP_CONTAINER, tmux_conf, "/etc/tmux.conf", mode="0644")

        # Restart to apply idmap
        print("Restarting container to apply UID mapping...")
        lxd.stop(TEMP_CONTAINER)
        lxd.start(TEMP_CONTAINER)

        # Add auto-mounts so we can test claude
        add_auto_mounts(TEMP_CONTAINER)

        # Restart again for mounts to take effect
        lxd.stop(TEMP_CONTAINER)
        lxd.start(TEMP_CONTAINER)

        # Try running claude --version to trigger Node.js install
        print("Testing claude binary...")
        r = lxd.exec_cmd(
            TEMP_CONTAINER,
            ["bash", "-lc", "claude --version"],
            user=CONTAINER_USER,
            capture=True,
            check=False,
        )
        if r.returncode == 0:
            print(f"Claude version: {r.stdout.strip()}")
        else:
            print("Warning: claude --version failed. You may need to install it manually.")

        # Drop into interactive shell
        print("\n" + "=" * 60)
        print("Install any additional packages you need, then exit the shell.")
        print("The container will be published as the base image.")
        print("=" * 60 + "\n")

        lxd.exec_interactive(TEMP_CONTAINER, ["bash"], user=CONTAINER_USER)

        # Remove auto-mounts before publishing (they'll be re-added per sandbox)
        print("\nRemoving temporary mounts...")
        lxd.stop(TEMP_CONTAINER)

        # Remove all disk devices
        r = lxd.run_lxc("config", "device", "list", TEMP_CONTAINER, capture=True, check=False)
        if r.returncode == 0:
            for dev in r.stdout.strip().splitlines():
                dev = dev.strip()
                if dev and dev.startswith("mount-"):
                    lxd.remove_disk_device(TEMP_CONTAINER, dev)

        # Publish as base image
        print(f"Publishing as '{BASE_IMAGE}'...")
        lxd.publish(TEMP_CONTAINER, BASE_IMAGE, force=force)
        print("Base image created successfully.")

    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up...")
    finally:
        # Clean up temp container
        if lxd.container_exists(TEMP_CONTAINER):
            print(f"Removing temporary container '{TEMP_CONTAINER}'...")
            lxd.delete(TEMP_CONTAINER, force=True)
