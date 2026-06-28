"""Read Claude Code and Codex session transcript (.jsonl) files."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

_SKIP_FIRST_PROMPT_RE = re.compile(
    r"^(?:\s*<[a-z][\w-]*[\s>]|\[Request interrupted by user[^\]]*\])"
)

_CLAUDE_HISTORY_CACHE: dict[str, dict[str, str]] = {}
_CLAUDE_HISTORY_CACHE_SIG: tuple[str, int, int] | None = None


def read_session_info(transcript_path: str) -> dict | None:
    """Extract session info from a Claude Code transcript and history.

    Returns dict with keys: last_prompt, timestamp, git_branch, message_count.
    Returns None if the file doesn't exist or has no usable metadata.
    """
    if not os.path.isfile(transcript_path):
        return None

    session_id = ""
    git_branch = ""
    last_prompt = ""
    last_prompt_timestamp = ""
    last_prompt_metadata = ""
    message_count = 0

    try:
        with open(transcript_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if not session_id:
                    session_id = _extract_session_id(entry)

                entry_git_branch = entry.get("gitBranch")
                if isinstance(entry_git_branch, str) and entry_git_branch:
                    git_branch = entry_git_branch

                if entry.get("type") == "last-prompt":
                    maybe_last_prompt = _normalize_prompt(entry.get("lastPrompt"))
                    if maybe_last_prompt:
                        last_prompt_metadata = maybe_last_prompt
                    continue

                prompt = _extract_claude_prompt(entry)
                if not prompt:
                    continue

                message_count += 1
                last_prompt = prompt
                timestamp = entry.get("timestamp")
                if isinstance(timestamp, str):
                    last_prompt_timestamp = timestamp
    except OSError:
        return None

    history_entry = _get_claude_history_entry(session_id)
    history_timestamp = history_entry.get("timestamp", "")
    history_dt = _parse_datetime(history_timestamp)
    transcript_dt = _parse_datetime(last_prompt_timestamp)

    if history_entry.get("last_prompt") and (
        transcript_dt is None or history_dt is None or history_dt >= transcript_dt
    ):
        prompt = history_entry["last_prompt"]
        timestamp = history_timestamp
    else:
        prompt = last_prompt_metadata or last_prompt
        timestamp = last_prompt_timestamp

    if not prompt and not git_branch and message_count == 0:
        return None

    return {
        "last_prompt": prompt,
        "timestamp": timestamp,
        "git_branch": git_branch,
        "message_count": message_count,
    }


def read_codex_session_info(transcript_path: str) -> dict | None:
    """Extract last-prompt info from a Codex JSONL transcript.

    Returns dict with keys: last_prompt, timestamp, git_branch, message_count.
    Returns None if the file doesn't exist or has no user messages.
    """
    if not os.path.isfile(transcript_path):
        return None

    git_branch = ""
    last_prompt = ""
    last_timestamp = ""
    message_count = 0

    try:
        with open(transcript_path, "rb") as f:
            for line in f:
                if b'"session_meta"' in line:
                    try:
                        entry = json.loads(line)
                        git_info = entry.get("payload", {}).get("git", {})
                        git_branch = git_info.get("branch", "")
                    except (json.JSONDecodeError, ValueError):
                        pass
                    continue

                if not _is_codex_user_line(line):
                    continue

                try:
                    entry = json.loads(line)
                    content = entry.get("payload", {}).get("content", [])
                    if not content:
                        continue
                    text = (
                        content[0].get("text", "")
                        if isinstance(content[0], dict)
                        else str(content[0])
                    )
                    # Skip system-injected messages
                    if text.startswith("# AGENTS") or text.startswith("<permissions"):
                        continue
                    message_count += 1
                    last_prompt = text[:80].replace("\n", " ").strip()
                    last_timestamp = entry.get("timestamp", "")
                except (json.JSONDecodeError, ValueError, IndexError):
                    continue
    except OSError:
        return None

    if message_count == 0:
        return None

    return {
        "last_prompt": last_prompt,
        "timestamp": last_timestamp,
        "git_branch": git_branch,
        "message_count": message_count,
    }


def read_session_info_any(transcript_path: str) -> dict | None:
    """Auto-detect transcript format and extract session info."""
    try:
        with open(transcript_path, "rb") as f:
            first_line = f.readline()
    except OSError:
        return None

    if b'"session_meta"' in first_line:
        return read_codex_session_info(transcript_path)
    return read_session_info(transcript_path)


def _extract_session_id(entry: dict[str, Any]) -> str:
    """Extract a Claude session ID from a transcript entry."""
    session_id = entry.get("sessionId")
    if isinstance(session_id, str):
        return session_id
    return ""


def _extract_claude_prompt(entry: dict[str, Any]) -> str:
    """Extract a meaningful Claude user prompt from one transcript entry."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return ""

    message = entry.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)

    command_fallback = ""
    for text in texts:
        if not text:
            continue

        command_name = _extract_tag(text, "command-name")
        if command_name:
            command_args = _normalize_prompt(_extract_tag(text, "command-args"))
            if command_args:
                return _normalize_prompt(f"{command_name} {command_args}")
            if not command_fallback:
                command_fallback = command_name
            continue

        bash_input = _extract_tag(text, "bash-input")
        if bash_input:
            return _normalize_prompt(f"! {bash_input}")

        if _SKIP_FIRST_PROMPT_RE.match(text):
            continue

        return _normalize_prompt(text)

    return command_fallback


def _get_claude_history_entry(session_id: str) -> dict[str, str]:
    """Return the last prompt-history entry for a Claude session."""
    if not session_id:
        return {}
    return _load_claude_history_index().get(session_id, {})


def _load_claude_history_index() -> dict[str, dict[str, str]]:
    """Index Claude's history.jsonl by session ID, newest entry wins."""
    global _CLAUDE_HISTORY_CACHE, _CLAUDE_HISTORY_CACHE_SIG

    history_path = os.path.expanduser("~/.claude/history.jsonl")
    try:
        stat = os.stat(history_path)
    except OSError:
        _CLAUDE_HISTORY_CACHE = {}
        _CLAUDE_HISTORY_CACHE_SIG = None
        return {}

    cache_sig = (history_path, stat.st_mtime_ns, stat.st_size)
    if cache_sig == _CLAUDE_HISTORY_CACHE_SIG:
        return _CLAUDE_HISTORY_CACHE

    index: dict[str, dict[str, str]] = {}
    try:
        with open(history_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                session_id = entry.get("sessionId")
                display = entry.get("display")
                timestamp = entry.get("timestamp")
                if not isinstance(session_id, str) or not isinstance(display, str):
                    continue

                index[session_id] = {
                    "last_prompt": _normalize_prompt(display),
                    "timestamp": _history_timestamp_to_iso(timestamp),
                }
    except OSError:
        return {}

    _CLAUDE_HISTORY_CACHE = index
    _CLAUDE_HISTORY_CACHE_SIG = cache_sig
    return index


def _history_timestamp_to_iso(timestamp: Any) -> str:
    """Convert Claude prompt-history timestamps (ms epoch) to ISO8601 UTC."""
    if not isinstance(timestamp, int | float):
        return ""
    dt = datetime.fromtimestamp(timestamp / 1000, UTC)
    return dt.isoformat().replace("+00:00", "Z")


def _normalize_prompt(value: Any, *, limit: int = 200) -> str:
    """Collapse whitespace and truncate prompts for compact display."""
    if not isinstance(value, str):
        return ""
    prompt = re.sub(r"\s+", " ", value).strip()
    if len(prompt) > limit:
        return prompt[:limit].rstrip() + "..."
    return prompt


def _extract_tag(text: str, tag_name: str) -> str | None:
    """Extract XML-ish tags used by Claude Code prompt wrappers."""
    if not text or not tag_name:
        return None
    pattern = re.compile(
        rf"<{re.escape(tag_name)}(?:\s+[^>]*)?>(.*?)</{re.escape(tag_name)}>",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(1)


def _parse_datetime(value: str) -> datetime | None:
    """Parse ISO8601 timestamps and ignore malformed inputs."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _is_user_prompt_line(line: bytes) -> bool:
    """Check if a JSONL line is a user message with a string content (not tool_result)."""
    if b'"type":"user"' not in line and b'"type": "user"' not in line:
        return False
    # Real user prompts have message.content as a string:
    #   "role":"user","content":"actual prompt text"
    # Tool results have message.content as a list:
    #   "role":"user","content":[{"type":"tool_result",...}]
    # Match the message-level content field (not nested tool_result content).
    return b'"role":"user","content":"' in line or b'"role": "user", "content": "' in line


def _is_codex_user_line(line: bytes) -> bool:
    """Check if a Codex JSONL line is a user response_item."""
    return b'"response_item"' in line and (b'"role":"user"' in line or b'"role": "user"' in line)


def relative_time(iso_ts: str) -> str:
    """Convert ISO timestamp to relative time string like '2 min ago'."""
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = now - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return "just now"
        mins = secs // 60
        if mins < 60:
            return f"{mins} min ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours} hr{'s' if hours != 1 else ''} ago"
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''} ago"
    except (ValueError, TypeError):
        return ""
