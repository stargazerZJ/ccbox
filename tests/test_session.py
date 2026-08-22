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


class SessionBindOwnershipTests(unittest.TestCase):
    """Only the runtime pane's top-level claude may (re)bind the runtime."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.binding_dir = Path(self.tempdir.name) / "bindings"
        self.binding_patch = patch("ccbox.config.SESSION_BINDING_DIR", self.binding_dir)
        self.binding_patch.start()
        self.addCleanup(self.binding_patch.stop)
        self.leader = {
            "session_id": "leader",
            "transcript_path": "/tmp/leader.jsonl",
            "cwd": "/work",
            "source": "startup",
        }

    def test_session_script_records_original_pane(self) -> None:
        script = session._build_session_script(
            session_name="s-0",
            cwd="/work",
            env={},
            unset_vars=[],
            command="claude",
            socket_path="/run/ccbox/demo.sock",
            session_binding_dir="/run/ccbox/bindings",
            sandbox_name="demo",
        )
        lines = script.splitlines()
        pane_line = next(i for i, line in enumerate(lines) if line.startswith("pane=$("))
        self.assertIn('display-message -p -t "$name" "#{pane_id}"', lines[pane_line])
        env_line = next(i for i, line in enumerate(lines) if "CCBOX_TMUX_PANE" in line)
        respawn_line = next(i for i, line in enumerate(lines) if "respawn-pane" in line)
        self.assertIn('set-environment -t "$name" CCBOX_TMUX_PANE "$pane"', lines[env_line])
        self.assertLess(pane_line, env_line)
        self.assertLess(env_line, respawn_line)

    def test_other_pane_is_rejected(self) -> None:
        teammate = {**self.leader, "session_id": "teammate"}
        self.assertFalse(
            session.should_accept_binding(
                teammate, None, runtime_pane="%29", hook_pane="%31", nested_claude=False
            )
        )

    def test_nested_claude_is_rejected(self) -> None:
        nested = {**self.leader, "session_id": "nested", "source": "resume"}
        self.assertFalse(
            session.should_accept_binding(
                nested, None, runtime_pane="%29", hook_pane="%29", nested_claude=True
            )
        )

    def test_startup_takeover_rejected_only_while_owner_alive(self) -> None:
        existing = {**self.leader, "owner_pid": 4242}
        other = {**self.leader, "session_id": "in-process-teammate"}
        self.assertFalse(
            session.should_accept_binding(
                other,
                existing,
                runtime_pane="%29",
                hook_pane="%29",
                nested_claude=False,
                owner_alive=lambda pid: pid == 4242,
            )
        )
        self.assertTrue(
            session.should_accept_binding(
                other,
                existing,
                runtime_pane="%29",
                hook_pane="%29",
                nested_claude=False,
                owner_alive=lambda pid: False,
            )
        )

    def test_same_process_clear_and_resume_always_win(self) -> None:
        existing = {**self.leader, "owner_pid": 4242}
        for source in ("clear", "resume", "compact"):
            payload = {**self.leader, "session_id": "next", "source": source}
            self.assertTrue(
                session.should_accept_binding(
                    payload,
                    existing,
                    runtime_pane="%29",
                    hook_pane="%29",
                    nested_claude=False,
                    owner_alive=lambda pid: True,
                ),
                source,
            )

    def test_unknown_pane_falls_back_to_accepting(self) -> None:
        self.assertTrue(
            session.should_accept_binding(
                self.leader, None, runtime_pane=None, hook_pane="%29", nested_claude=False
            )
        )

    def test_nested_claude_between_walks_ancestry(self) -> None:
        # pid 50 = hook shell, 40 = nested claude, 30 = Bash tool shell, 20 = pane claude
        ppid = {50: 40, 40: 30, 30: 20, 20: 1}
        exe = {
            50: "/usr/bin/bash",
            40: "/home/u/.local/share/claude/versions/2.1.240",
            30: "/usr/bin/bash",
            20: "/home/u/.local/share/claude/versions/2.1.240",
        }
        with (
            patch.object(session, "_proc_ppid", ppid.get),
            patch.object(session, "_proc_exe", exe.get),
            patch.object(session, "_proc_cmdline", lambda pid: None),
        ):
            self.assertTrue(session.nested_claude_between(50, 20))
            self.assertFalse(session.nested_claude_between(30, 20))
            # Unknown pane pid: still reject when a claude sits above the hook.
            self.assertTrue(session.nested_claude_between(50, None))
            self.assertFalse(session.nested_claude_between(30, None))

    def test_bind_session_from_hook_records_owner_and_ignores_teammates(self) -> None:
        env = {"CCBOX_TMUX_PANE": "%29", "TMUX_PANE": "%29"}
        with (
            patch.object(session, "resolve_pane_pid", return_value=777),
            patch.object(session, "nested_claude_between", return_value=False),
        ):
            record = session.bind_session_from_hook("demo", "s-0", self.leader, env, hook_pid=1)
        self.assertIsNotNone(record)
        self.assertEqual(record["owner_pid"], 777)

        teammate = {**self.leader, "session_id": "teammate"}
        teammate_env = {"CCBOX_TMUX_PANE": "%29", "TMUX_PANE": "%31"}
        with (
            patch.object(session, "resolve_pane_pid", return_value=777),
            patch.object(session, "nested_claude_between", return_value=False),
        ):
            self.assertIsNone(
                session.bind_session_from_hook("demo", "s-0", teammate, teammate_env, hook_pid=1)
            )
        self.assertEqual(session.read_session_binding("demo", "s-0")["session_id"], "leader")
