from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ccbox import transcript


class ClaudeSessionInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.home = Path(self.tempdir.name)
        self.claude_dir = self.home / ".claude"
        self.claude_dir.mkdir()
        self.history_path = self.claude_dir / "history.jsonl"
        self.transcript_path = self.home / "session.jsonl"

        transcript._CLAUDE_HISTORY_CACHE = {}
        transcript._CLAUDE_HISTORY_CACHE_SIG = None

        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_prefers_history_display_when_it_is_newer(self) -> None:
        self._write_transcript(
            [
                {"type": "permission-mode", "sessionId": "sess-1"},
                self._user_entry(
                    "sess-1",
                    "2026-04-15T01:10:00Z",
                    "Check training status",
                    git_branch="feature/parser",
                ),
            ]
        )
        self._write_history(
            [
                {
                    "display": "job running.\ncheck every 15m",
                    "timestamp": self._epoch_ms("2026-04-15T01:11:00Z"),
                    "project": "/tmp/project",
                    "sessionId": "sess-1",
                }
            ]
        )

        info = transcript.read_session_info(str(self.transcript_path))

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["last_prompt"], "job running. check every 15m")
        self.assertEqual(info["timestamp"], "2026-04-15T01:11:00Z")
        self.assertEqual(info["git_branch"], "feature/parser")
        self.assertEqual(info["message_count"], 1)

    def test_fallback_uses_real_text_after_ide_metadata_blocks(self) -> None:
        self._write_transcript(
            [
                {"type": "permission-mode", "sessionId": "sess-2"},
                {
                    "type": "user",
                    "timestamp": "2026-04-15T01:00:00Z",
                    "sessionId": "sess-2",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "<ide_selection>app.py</ide_selection>"},
                            {"type": "text", "text": "Fix the parser please"},
                        ],
                    },
                },
            ]
        )

        info = transcript.read_session_info(str(self.transcript_path))

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["last_prompt"], "Fix the parser please")
        self.assertEqual(info["message_count"], 1)

    def test_fallback_formats_bash_input(self) -> None:
        self._write_transcript(
            [
                {"type": "permission-mode", "sessionId": "sess-3"},
                self._user_entry(
                    "sess-3",
                    "2026-04-15T01:00:00Z",
                    "<bash-input>git status\n--short</bash-input>",
                ),
            ]
        )

        info = transcript.read_session_info(str(self.transcript_path))

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["last_prompt"], "! git status --short")
        self.assertEqual(info["message_count"], 1)

    def test_fallback_skips_tool_results_and_compact_summaries(self) -> None:
        self._write_transcript(
            [
                {"type": "permission-mode", "sessionId": "sess-4"},
                self._user_entry("sess-4", "2026-04-15T01:00:00Z", "Real prompt"),
                {
                    "type": "user",
                    "timestamp": "2026-04-15T01:05:00Z",
                    "sessionId": "sess-4",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                                "content": "output",
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-04-15T01:06:00Z",
                    "sessionId": "sess-4",
                    "isCompactSummary": True,
                    "message": {"role": "user", "content": "Ignore this summary"},
                },
            ]
        )

        info = transcript.read_session_info(str(self.transcript_path))

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["last_prompt"], "Real prompt")
        self.assertEqual(info["timestamp"], "2026-04-15T01:00:00Z")
        self.assertEqual(info["message_count"], 1)

    def test_last_prompt_metadata_beats_command_only_transcript(self) -> None:
        self._write_transcript(
            [
                {"type": "permission-mode", "sessionId": "sess-5"},
                self._user_entry(
                    "sess-5",
                    "2026-04-15T01:00:00Z",
                    "<command-name>/clear</command-name>",
                ),
                {
                    "type": "last-prompt",
                    "sessionId": "sess-5",
                    "lastPrompt": "Continue fixing picker parsing",
                },
            ]
        )

        info = transcript.read_session_info(str(self.transcript_path))

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["last_prompt"], "Continue fixing picker parsing")
        self.assertEqual(info["timestamp"], "2026-04-15T01:00:00Z")
        self.assertEqual(info["message_count"], 1)

    def _write_transcript(self, entries: list[dict]) -> None:
        with self.transcript_path.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry))
                f.write("\n")

    def _write_history(self, entries: list[dict]) -> None:
        with self.history_path.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry))
                f.write("\n")

    @staticmethod
    def _epoch_ms(iso_timestamp: str) -> int:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)

    @staticmethod
    def _user_entry(
        session_id: str,
        timestamp: str,
        content: str,
        *,
        git_branch: str | None = None,
    ) -> dict:
        entry = {
            "type": "user",
            "timestamp": timestamp,
            "sessionId": session_id,
            "message": {"role": "user", "content": content},
        }
        if git_branch is not None:
            entry["gitBranch"] = git_branch
        return entry


if __name__ == "__main__":
    unittest.main()
