from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ccbox import config as config_mod
from ccbox.config import Config, HostConfig


class ConfigPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        state_dir = Path(self.tempdir.name) / "ccbox"
        self.patches = [
            patch.object(config_mod, "STATE_DIR", state_dir),
            patch.object(config_mod, "STATE_FILE", state_dir / "state.json"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_set_storage_pool_updates_active_host_without_clobbering_others(self) -> None:
        config = Config()
        config.state.storage_pool = "legacy-pool"
        config.state.hosts["other-host"] = HostConfig(storage_pool="other-pool")

        config.set_storage_pool("this-pool", hostname="this-host")

        self.assertEqual(config.state.storage_pool, "legacy-pool")
        self.assertEqual(config.state.host_config("this-host").storage_pool, "this-pool")
        self.assertEqual(config.state.host_config("other-host").storage_pool, "other-pool")

    def test_set_storage_pool_preserves_existing_host_network_fields(self) -> None:
        config = Config()
        config.state.hosts["this-host"] = HostConfig(
            storage_pool="old-pool",
            network="lxdbr1",
            wg_ip="10.44.0.2",
        )

        config.set_storage_pool("new-pool", hostname="this-host")

        host = config.state.host_config("this-host")
        self.assertEqual(host.storage_pool, "new-pool")
        self.assertEqual(host.network, "lxdbr1")
        self.assertEqual(host.wg_ip, "10.44.0.2")


if __name__ == "__main__":
    unittest.main()
