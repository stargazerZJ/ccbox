"""Read Claude Code and Codex session transcript (.jsonl) files."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime


def read_session_info(transcript_path: str) -> dict | None:
    """Extract last-prompt info from a Claude Code JSONL transcript.

    Returns dict with keys: last_prompt, timestamp, git_branch, message_count.
    Returns None if the file doesn't exist or has no user messages.
    """
    if not os.path.isfile(transcript_path):
        return None

    last_user_line = _find_last_user_line(transcript_path)
    if last_user_line is None:
        return None

    try:
        entry = json.loads(last_user_line)
    except (json.JSONDecodeError, ValueError):
        return None

    # Extract prompt text from message.content
    msg = entry.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, list):
        # Tool results and multi-part messages — take first text block
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                content = part["text"]
                break
            elif isinstance(part, str):
                content = part
                break
        else:
            content = str(content[0]) if content else ""
    prompt = content[:80].replace("\n", " ").strip()

    # Count user messages (forward scan, byte check only)
    message_count = _count_user_messages(transcript_path)

    return {
        "last_prompt": prompt,
        "timestamp": entry.get("timestamp", ""),
        "git_branch": entry.get("gitBranch", ""),
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


def _count_user_messages(path: str) -> int:
    """Count real user prompt lines in a Claude Code transcript."""
    count = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                if _is_user_prompt_line(line):
                    count += 1
    except OSError:
        pass
    return count


def _find_last_user_line(path: str) -> bytes | None:
    """Reverse-seek through a JSONL file to find the last real user prompt.

    Skips tool_result messages (content is a list of tool_result dicts)
    and looks for messages where content is a plain string.
    """
    chunk_size = 8192
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            buf = b""
            while pos > 0:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                buf = f.read(read_size) + buf
                lines = buf.split(b"\n")
                # Check lines from end (skip last empty element from trailing newline)
                for line in reversed(lines[1:]):
                    if _is_user_prompt_line(line):
                        return line
                # Keep the first (possibly partial) line for next iteration
                buf = lines[0]
            # Check remaining buffer
            if buf and _is_user_prompt_line(buf):
                return buf
    except OSError:
        pass
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
