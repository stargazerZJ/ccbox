from __future__ import annotations

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccbox.config import HostConfig, ProxyEntry, SandboxEntry, State
from ccbox.sandbox import apply_container_config


class ApplyContainerConfigTests(unittest.TestCase):
    def test_replaces_stale_proxy_device_after_host_migration(self) -> None:
        hostname = socket.gethostname()
        config = SimpleNamespace(state=State(hosts={hostname: HostConfig(wg_ip="10.10.0.2")}))
        entry = SandboxEntry(
            container="ccbox-demo",
            proxies=[
                ProxyEntry(
                    name="exp-tcp-5173",
                    listen="tcp:{wg_ip}:5173",
                    connect="tcp:127.0.0.1:5173",
                )
            ],
        )
        devices = {
            "port-exp-tcp-5173": {
                "type": "proxy",
                "listen": "tcp:10.10.0.1:5173",
                "connect": "tcp:127.0.0.1:5173",
                "bind": "host",
            }
        }

        with (
            patch("ccbox.sandbox.lxd.list_devices", return_value=devices),
            patch("ccbox.sandbox.lxd.remove_device") as remove_device,
            patch("ccbox.sandbox.lxd.add_proxy_device") as add_proxy_device,
        ):
            changes = apply_container_config(config, entry)

        remove_device.assert_called_once_with("ccbox-demo", "port-exp-tcp-5173")
        add_proxy_device.assert_called_once_with(
            "ccbox-demo",
            "port-exp-tcp-5173",
            "tcp:10.10.0.2:5173",
            "tcp:127.0.0.1:5173",
            bind="host",
        )
        self.assertEqual(
            changes,
            [
                "  ~ proxy port-exp-tcp-5173 (tcp:10.10.0.2:5173 -> tcp:127.0.0.1:5173)",
                "  + proxy port-exp-tcp-5173 (tcp:10.10.0.2:5173 -> tcp:127.0.0.1:5173)",
            ],
        )

    def test_proxy_with_host_placeholder_requires_wg_ip(self) -> None:
        config = SimpleNamespace(state=State())
        entry = SandboxEntry(
            container="ccbox-demo",
            proxies=[
                ProxyEntry(
                    name="exp-tcp-5173",
                    listen="tcp:{wg_ip}:5173",
                    connect="tcp:127.0.0.1:5173",
                )
            ],
        )

        with (
            patch("ccbox.sandbox.lxd.list_devices", return_value={}),
            patch("ccbox.sandbox.lxd.add_proxy_device") as add_proxy_device,
            self.assertRaisesRegex(ValueError, "requires hosts.<hostname>.wg_ip"),
        ):
            apply_container_config(config, entry)

        add_proxy_device.assert_not_called()

    def test_replaces_mismatched_gpu_device(self) -> None:
        config = SimpleNamespace(state=State())
        entry = SandboxEntry(container="ccbox-demo", gpu_pci="01:00.0")
        devices = {"nv-gpu": {"type": "gpu", "pci": "02:00.0"}}

        with (
            patch("ccbox.sandbox.lxd.list_devices", return_value=devices),
            patch("ccbox.sandbox.lxd.remove_device") as remove_device,
            patch("ccbox.sandbox.lxd.add_gpu_device") as add_gpu_device,
        ):
            apply_container_config(config, entry)

        remove_device.assert_called_once_with("ccbox-demo", "nv-gpu")
        add_gpu_device.assert_called_once_with("ccbox-demo", "nv-gpu", "01:00.0")


if __name__ == "__main__":
    unittest.main()
