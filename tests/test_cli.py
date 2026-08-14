from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccbox.cli import build_parser, cmd_port
from ccbox.config import HostConfig, ProxyEntry, SandboxEntry, State


class CliParserTests(unittest.TestCase):
    def test_shell_without_command_preserves_subcommand(self) -> None:
        args = build_parser().parse_args(["shell"])

        self.assertEqual(args.command, "shell")
        self.assertEqual(args.shell_command, [])

    def test_shell_with_command_uses_separate_destination(self) -> None:
        args = build_parser().parse_args(["shell", "echo", "hello"])

        self.assertEqual(args.command, "shell")
        self.assertEqual(args.shell_command, ["echo", "hello"])

    def test_internal_session_bind_command_is_registered(self) -> None:
        args = build_parser().parse_args(["_session-bind"])

        self.assertEqual(args.command, "_session-bind")


class FakeConfig:
    def __init__(self) -> None:
        self.state = State(hosts={"test-host": HostConfig(wg_ip="10.99.0.2")})
        self.entry = SandboxEntry(container="ccbox-demo")

    def get_sandbox(self, name: str) -> SandboxEntry | None:
        return self.entry if name == "demo" else None

    def set_sandbox(self, name: str, entry: SandboxEntry) -> None:
        if name != "demo":
            raise AssertionError(name)
        self.entry = entry


class CliPortPersistenceTests(unittest.TestCase):
    def test_port_expose_records_portable_proxy_spec(self) -> None:
        config = FakeConfig()
        args = SimpleNamespace(
            port_action="expose",
            sandbox="demo",
            container_port=5173,
            bind="10.99.0.2:15173",
            udp=False,
        )

        with (
            patch("ccbox.config.socket.gethostname", return_value="test-host"),
            patch("ccbox.cli.ensure_running", return_value="ccbox-demo"),
            patch("ccbox.cli.add_expose", return_value="port-exp-tcp-15173") as add_expose,
        ):
            cmd_port(config, args)

        add_expose.assert_called_once_with("ccbox-demo", 5173, "10.99.0.2", 15173, udp=False)
        self.assertEqual(
            config.entry.proxies,
            [
                ProxyEntry(
                    name="exp-tcp-15173",
                    listen="tcp:{wg_ip}:15173",
                    connect="tcp:127.0.0.1:5173",
                    bind="host",
                )
            ],
        )

    def test_port_rm_removes_recorded_proxy_spec(self) -> None:
        config = FakeConfig()
        config.entry.proxies = [
            ProxyEntry(
                name="exp-tcp-15173",
                listen="tcp:{wg_ip}:15173",
                connect="tcp:127.0.0.1:5173",
            )
        ]
        args = SimpleNamespace(port_action="rm", sandbox="demo", name="port-exp-tcp-15173")

        with (
            patch("ccbox.cli.ensure_running", return_value="ccbox-demo"),
            patch("ccbox.cli.remove_port") as remove_port,
        ):
            cmd_port(config, args)

        remove_port.assert_called_once_with("ccbox-demo", "port-exp-tcp-15173")
        self.assertEqual(config.entry.proxies, [])


if __name__ == "__main__":
    unittest.main()
