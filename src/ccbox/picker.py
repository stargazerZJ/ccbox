"""Interactive pickers for sandbox/session selection using rich + InquirerPy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from InquirerPy import inquirer
from rich.console import Console

from ccbox import lxd
from ccbox.config import Config, SESSION_LINK_DIR
from ccbox.session import list_sessions
from ccbox.transcript import read_session_info_any, relative_time

console = Console()


# -- Result types for pick_no_resolve --

@dataclass
class AttachSession:
    """Resume an existing session in a specific sandbox."""
    sandbox: str
    session: str


@dataclass
class NewSandbox:
    """Create a new sandbox for CWD."""
    pass


@dataclass
class MountToSandbox:
    """Mount CWD into an existing sandbox and start a session."""
    sandbox: str


PickResult = AttachSession | NewSandbox | MountToSandbox


# -- Session info helpers --

@dataclass
class RecentSession:
    sandbox: str
    tmux_name: str
    container: str
    info: dict | None  # from read_session_info_any


def _session_info(sandbox_name: str, tmux_session: str) -> dict | None:
    """Read session info for a tmux session via its session-link pointer."""
    link_file = SESSION_LINK_DIR / sandbox_name / tmux_session
    try:
        transcript_path = link_file.read_text().strip()
    except OSError:
        return None
    if not transcript_path:
        return None
    return read_session_info_any(transcript_path)


def _format_detail(info: dict | None) -> str:
    """Format session metadata into a detail string."""
    if info is None:
        return ""
    parts = []
    if info.get("last_prompt"):
        prompt = info["last_prompt"]
        if len(prompt) > 50:
            prompt = prompt[:47] + "..."
        parts.append(f'"{prompt}"')
    ts = relative_time(info.get("timestamp", ""))
    if ts:
        parts.append(ts)
    if info.get("git_branch"):
        parts.append(info["git_branch"])
    count = info.get("message_count", 0)
    if count:
        parts.append(f"{count} msg{'s' if count != 1 else ''}")
    return " \u00b7 ".join(parts)


def _parse_timestamp(info: dict | None) -> float:
    """Extract a sortable timestamp (epoch seconds) from session info. 0 if unknown."""
    if not info or not info.get("timestamp"):
        return 0.0
    try:
        dt = datetime.fromisoformat(info["timestamp"].replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


# -- Pickers --

def pick_session(detached: list[dict], sandbox_name: str) -> str | None:
    """Interactive picker for detached sessions within a resolved sandbox.

    Returns session name to attach, or None to create a new session.
    """
    if not detached:
        return None
    if len(detached) == 1:
        # Auto-attach: print info and return
        s = detached[0]
        info = _session_info(sandbox_name, s["name"])
        detail = _format_detail(info)
        msg = f"Reattaching to session '{s['name']}'"
        if detail:
            msg += f"  {detail}"
        console.print(msg)
        return s["name"]

    # Build choices for multi-session picker
    choices = []
    for s in detached:
        info = _session_info(sandbox_name, s["name"])
        detail = _format_detail(info)
        label = s["name"]
        if detail:
            label += f"  {detail}"
        choices.append({"name": label, "value": s["name"]})
    choices.append({"name": "\u25c6 New session", "value": None})

    console.print(f"[bold]Sessions in [cyan]{sandbox_name}[/cyan]:[/bold]")
    result = inquirer.select(
        message="",
        choices=choices,
        pointer="\u276f",
        show_cursor=False,
    ).execute()
    return result


def _collect_recent_sessions(config: Config) -> list[RecentSession]:
    """Gather recent sessions across all sandboxes (running containers only)."""
    results: list[RecentSession] = []

    if not SESSION_LINK_DIR.is_dir():
        return results

    for sandbox_dir in SESSION_LINK_DIR.iterdir():
        if not sandbox_dir.is_dir():
            continue
        sandbox_name = sandbox_dir.name
        entry = config.get_sandbox(sandbox_name)
        if entry is None:
            continue
        # Only check running containers to avoid startup latency
        state = lxd.container_state(entry.container)
        if state != "Running":
            continue
        # Get live tmux sessions to filter out stale links
        live_sessions = {s["name"] for s in list_sessions(entry.container)
                         if not s["attached"]}
        for link_file in sandbox_dir.iterdir():
            tmux_name = link_file.name
            if tmux_name not in live_sessions:
                continue
            info = _session_info(sandbox_name, tmux_name)
            results.append(RecentSession(
                sandbox=sandbox_name,
                tmux_name=tmux_name,
                container=entry.container,
                info=info,
            ))

    # Sort by recency (most recent first)
    results.sort(key=lambda r: _parse_timestamp(r.info), reverse=True)
    return results[:10]


def pick_no_resolve(config: Config, cwd: str) -> PickResult:
    """Unified picker when CWD doesn't resolve to any sandbox.

    Shows recent sessions, plus actions to create/mount.
    """
    dirname = os.path.basename(cwd)
    recent = _collect_recent_sessions(config)
    sandboxes = list(config.state.sandboxes.keys())

    choices = []

    # Recent sessions
    if recent:
        choices.append({"name": "\u2500 Recent sessions \u2500\u2500\u2500", "value": "__sep__", "enabled": False})
        for r in recent:
            detail = _format_detail(r.info)
            label = f"  {r.sandbox}/{r.tmux_name}"
            if detail:
                label += f"  {detail}"
            choices.append({"name": label, "value": ("attach", r.sandbox, r.tmux_name)})

    # Actions separator
    if recent:
        choices.append({"name": "\u2500 Actions \u2500\u2500\u2500", "value": "__sep__", "enabled": False})

    choices.append({
        "name": f"\u25c6 New sandbox for {dirname}/",
        "value": ("new",),
    })

    if sandboxes:
        choices.append({
            "name": "\u25c6 Mount to existing sandbox\u2026",
            "value": ("mount",),
        })

    console.print(f"[dim]No sandbox for[/dim] [bold]{cwd}[/bold]")
    result = inquirer.select(
        message="",
        choices=[c for c in choices if c.get("enabled", True) is not False],
        pointer="\u276f",
        show_cursor=False,
    ).execute()

    if result[0] == "attach":
        return AttachSession(sandbox=result[1], session=result[2])
    elif result[0] == "new":
        return NewSandbox()
    elif result[0] == "mount":
        return _pick_sandbox_for_mount(config)
    # unreachable
    return NewSandbox()


def _pick_sandbox_for_mount(config: Config) -> MountToSandbox:
    """Sub-picker: choose which sandbox to mount CWD into."""
    choices = []
    for name, entry in config.state.sandboxes.items():
        mounts = ", ".join(os.path.basename(m.path) for m in entry.mounts[:3])
        label = name
        if mounts:
            label += f"  [dim]({mounts})[/dim]"
        choices.append({"name": label, "value": name})

    result = inquirer.select(
        message="Mount to:",
        choices=choices,
        pointer="\u276f",
        show_cursor=False,
    ).execute()
    return MountToSandbox(sandbox=result)
