from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccbox.picker import _collect_recent_sessions


class LiveRuntimeCollectionTests(unittest.TestCase):
    def test_live_runtime_without_binding_is_visible_as_starting(self) -> None:
        config = SimpleNamespace(state=SimpleNamespace(sandboxes={}))
        live = [
            {
                "sandbox": "demo",
                "container": "ccbox-demo",
                "name": "s-0",
                "attached": False,
                "created": 123,
            }
        ]
        with (
            patch("ccbox.picker.list_all_sessions", return_value=live),
            patch("ccbox.picker.read_session_binding", return_value=None),
        ):
            runtimes = _collect_recent_sessions(config)

        self.assertEqual(len(runtimes), 1)
        self.assertEqual(runtimes[0].tmux_name, "s-0")
        self.assertIsNone(runtimes[0].info)


if __name__ == "__main__":
    unittest.main()
