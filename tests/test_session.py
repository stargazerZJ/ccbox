from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ccbox import session


class SessionSocketTests(unittest.TestCase):
    def test_list_sessions_uses_explicit_shared_socket(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="s-0|0|123\ns-1|2|456\n", stderr="")
        with (
            patch("ccbox.config.TMUX_SOCKET_DIR", Path("/tmp/ccbox-tmux")),
            patch("ccbox.session.subprocess.run", return_value=completed) as run,
        ):
            sessions = session.list_sessions("demo")

        self.assertEqual(
            sessions,
            [
                {"name": "s-0", "attached": False, "created": 123},
                {"name": "s-1", "attached": True, "created": 456},
            ],
        )
        self.assertEqual(
            run.call_args.args[0][:4],
            ["tmux", "-S", "/tmp/ccbox-tmux/demo.sock", "list-sessions"],
        )

    def test_session_script_puts_socket_on_every_tmux_command(self) -> None:
        script = session._build_session_script(
            session_name=None,
            cwd="/work",
            env={"HOME": "/home/user"},
            unset_vars=[],
            command="claude",
            socket_path="/run/ccbox/demo.sock",
            session_binding_dir="/run/ccbox/bindings",
            sandbox_name="demo",
        )

        tmux_lines = [line for line in script.splitlines() if "tmux " in line]
        self.assertGreaterEqual(len(tmux_lines), 5)
        self.assertTrue(all("tmux -S /run/ccbox/demo.sock" in line for line in tmux_lines))
        self.assertNotIn("_session-cleanup", script)

    def test_unreachable_socket_does_not_prune_binding(self) -> None:
        config = SimpleNamespace(
            state=SimpleNamespace(sandboxes={"demo": SimpleNamespace(container="ccbox-demo")})
        )
        with (
            patch("ccbox.session._query_sessions", return_value=None),
            patch("ccbox.session.prune_stale_bindings") as prune,
        ):
            runtimes = session.list_all_sessions(config, container_states={"ccbox-demo": "Running"})

        self.assertEqual(runtimes, [])
        prune.assert_not_called()


class SessionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.binding_dir = Path(self.tempdir.name) / "bindings"
        self.binding_patch = patch("ccbox.config.SESSION_BINDING_DIR", self.binding_dir)
        self.binding_patch.start()
        self.addCleanup(self.binding_patch.stop)

    def test_binding_is_atomic_json_and_rewrites_on_resume(self) -> None:
        first = {
            "session_id": "session-1",
            "transcript_path": "/tmp/session-1.jsonl",
            "cwd": "/work",
            "source": "startup",
        }
        second = {
            "session_id": "session-2",
            "transcript_path": "/tmp/session-2.jsonl",
            "cwd": "/work",
            "source": "resume",
        }

        session.write_session_binding("demo", "s-0", first)
        session.write_session_binding("demo", "s-0", second)

        path = self.binding_dir / "demo" / "s-0.json"
        record = json.loads(path.read_text())
        self.assertEqual(record["session_id"], "session-2")
        self.assertEqual(record["source"], "resume")
        self.assertEqual(session.read_session_binding("demo", "s-0"), record)
        self.assertEqual(list(path.parent.glob(".s-0.*")), [])

    def test_binding_rejects_unsafe_components_and_non_string_fields(self) -> None:
        payload = {
            "session_id": "session-1",
            "transcript_path": "/tmp/session.jsonl",
            "cwd": "/work",
        }
        with self.assertRaises(ValueError):
            session.write_session_binding("../demo", "s-0", payload)
        with self.assertRaises(ValueError):
            session.write_session_binding("demo", "../s-0", payload)
        with self.assertRaises(ValueError):
            session.write_session_binding("demo", "s-0", {**payload, "cwd": 42})

    def test_prune_removes_only_bindings_without_live_runtimes(self) -> None:
        payload = {
            "session_id": "session-1",
            "transcript_path": "/tmp/session.jsonl",
            "cwd": "/work",
        }
        session.write_session_binding("demo", "s-0", payload)
        session.write_session_binding("demo", "s-1", payload)

        session.prune_stale_bindings("demo", {"s-1"})

        self.assertFalse((self.binding_dir / "demo" / "s-0.json").exists())
        self.assertTrue((self.binding_dir / "demo" / "s-1.json").exists())


if __name__ == "__main__":
    unittest.main()
