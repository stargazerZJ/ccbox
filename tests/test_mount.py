from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from ccbox.config import MountEntry, SandboxEntry, State
from ccbox.mount import _inode_key, prune_stale_mounts


class FakeConfig:
    """Minimal Config stand-in that records saves in memory."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.saves = 0

    def get_sandbox(self, name: str) -> SandboxEntry | None:
        return self.state.sandboxes.get(name)

    def set_sandbox(self, name: str, entry: SandboxEntry) -> None:
        self.state.sandboxes[name] = entry
        self.saves += 1


def _config_with_mount(path: str, *, stored_inode: str | None) -> FakeConfig:
    entry = SandboxEntry(
        container="ccbox-demo",
        mounts=[MountEntry(path=path, mode="rw", inode=stored_inode)],
    )
    return FakeConfig(State(sandboxes={"demo": entry}))


class PruneStaleMountsTests(unittest.TestCase):
    def test_foreign_stored_inode_on_stopped_container_is_not_pruned(self) -> None:
        """The qy regression: a state.json replicated from another host carries
        that host's dev:ino. A stopped standby container must keep its mounts and
        adopt the local inode instead of wiping every device."""
        with tempfile.TemporaryDirectory() as d:
            cfg = _config_with_mount(d, stored_inode="424242:424242")  # foreign
            expected = _inode_key(d)  # capture before the tempdir is removed
            with (
                patch("ccbox.mount.lxd.container_state", return_value="Stopped"),
                patch("ccbox.mount.lxd.remove_disk_device") as remove,
                patch("ccbox.mount._container_ino") as cino,
            ):
                pruned = prune_stale_mounts(cfg, "demo")

            self.assertEqual(pruned, [])
            remove.assert_not_called()
            cino.assert_not_called()  # stopped → no live inode probe at all
            # inode adopted to the real local value
            self.assertEqual(cfg.get_sandbox("demo").mounts[0].inode, expected)
            self.assertEqual(cfg.saves, 1)

    def test_running_container_matching_live_inode_is_not_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = _config_with_mount(d, stored_inode="424242:424242")  # foreign
            host_ino = os.stat(d).st_ino
            with (
                patch("ccbox.mount.lxd.container_state", return_value="Running"),
                patch("ccbox.mount.lxd.remove_disk_device") as remove,
                patch("ccbox.mount._container_ino", return_value=host_ino),
            ):
                pruned = prune_stale_mounts(cfg, "demo")

        self.assertEqual(pruned, [])
        remove.assert_not_called()

    def test_running_container_orphaned_inode_is_pruned(self) -> None:
        # A genuine replacement changes the host key, so stored != current is the
        # trigger; the container still holds the old (orphaned) inode.
        with tempfile.TemporaryDirectory() as d:
            cfg = _config_with_mount(d, stored_inode="111:111")  # differs from current
            host_ino = os.stat(d).st_ino
            with (
                patch("ccbox.mount.lxd.container_state", return_value="Running"),
                patch("ccbox.mount.lxd.remove_disk_device") as remove,
                patch("ccbox.mount._container_ino", return_value=host_ino + 1),
            ):
                pruned = prune_stale_mounts(cfg, "demo")

        self.assertEqual(pruned, [d])
        remove.assert_called_once()
        self.assertEqual(cfg.get_sandbox("demo").mounts, [])

    def test_matching_stored_inode_skips_container_probe(self) -> None:
        """Perf: when the stored key already matches the host, no container_state
        query and no per-mount `lxc exec` inode probe happen at all."""
        with tempfile.TemporaryDirectory() as d:
            cfg = _config_with_mount(d, stored_inode=_inode_key(d))  # already adopted
            with (
                patch("ccbox.mount.lxd.container_state") as cstate,
                patch("ccbox.mount.lxd.remove_disk_device") as remove,
                patch("ccbox.mount._container_ino") as cino,
            ):
                pruned = prune_stale_mounts(cfg, "demo")

            self.assertEqual(pruned, [])
            cstate.assert_not_called()  # lazy: not even the state query runs
            cino.assert_not_called()
            remove.assert_not_called()
            self.assertEqual(cfg.saves, 0)  # nothing changed → no write

    def test_missing_host_path_is_pruned_regardless_of_state(self) -> None:
        missing = "/nonexistent/ccbox/mount/path"
        cfg = _config_with_mount(missing, stored_inode="1:1")
        with (
            patch("ccbox.mount.lxd.container_state", return_value="Stopped"),
            patch("ccbox.mount.lxd.remove_disk_device") as remove,
            patch("ccbox.mount._container_ino"),
        ):
            pruned = prune_stale_mounts(cfg, "demo")

        self.assertEqual(pruned, [missing])
        remove.assert_called_once()


if __name__ == "__main__":
    unittest.main()
