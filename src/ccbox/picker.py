"""Interactive pickers for sandbox/session selection using Textual."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import OptionList
from textual.widgets.option_list import Option

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
    readonly: bool = False


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


def _styled_option(primary: str, detail: str = "", *, prefix: str = "", key: str = "") -> Text:
    """Build a Rich Text with optional dim detail and key hint."""
    t = Text()
    if key:
        t.append(f"[{key}] ", style="dim")
    if prefix:
        t.append(prefix)
    t.append(primary)
    if detail:
        t.append(f"  {detail}", style="dim")
    return t


# -- Inline picker app --

class _PickerApp(App[str | None]):
    """Generic inline picker. Subclass or instantiate with options."""

    CSS = """
    OptionList {
        height: auto;
        max-height: 20;
        background: $surface;
    }
    OptionList > .option-list--option-highlighted {
        background: $accent;
    }
    """

    def __init__(self, options: list[Option | None], shortcuts: dict[str, str] | None = None) -> None:
        super().__init__()
        self._options = options
        self._shortcuts = shortcuts or {}

    def compose(self) -> ComposeResult:
        yield OptionList(*self._options)

    def on_key(self, event) -> None:
        if event.character in self._shortcuts:
            event.prevent_default()
            event.stop()
            self.exit(self._shortcuts[event.character])
        elif event.key == "escape":
            event.prevent_default()
            self.exit(None)
        elif event.key == "backspace":
            event.prevent_default()
            self.exit("__back__")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.exit(event.option.id)


def _run_picker(options: list[Option | None], shortcuts: dict[str, str] | None = None, *, numbered: bool = True) -> str | None:
    """Run an inline picker and return the selected option id.

    If numbered=True, selectable options get 1-9 digit prefixes and shortcuts.
    Returns '__back__' on backspace, None on escape.
    """
    all_shortcuts = dict(shortcuts) if shortcuts else {}
    if numbered:
        idx = 0
        for i, opt in enumerate(options):
            if isinstance(opt, Option) and idx < 9:
                idx += 1
                labeled = Text()
                labeled.append(f"{idx} ", style="dim")
                labeled.append_text(opt.prompt if isinstance(opt.prompt, Text) else Text(str(opt.prompt)))
                options[i] = Option(labeled, id=opt.id)
                all_shortcuts[str(idx)] = opt.id
    app = _PickerApp(options, all_shortcuts)
    return app.run(inline=True)


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

    console.print(f"[bold]Sessions in [cyan]{sandbox_name}[/cyan]:[/bold]")

    options: list[Option | None] = []
    shortcuts: dict[str, str] = {"n": "__new__", "q": "__quit__"}
    for s in detached:
        info = _session_info(sandbox_name, s["name"])
        detail = _format_detail(info)
        prompt = _styled_option(s["name"], detail)
        options.append(Option(prompt, id=s["name"]))
    options.append(None)
    options.append(Option(_styled_option("New session", key="n"), id="__new__"))
    options.append(Option(_styled_option("Quit", key="q"), id="__quit__"))

    result = _run_picker(options, shortcuts)
    if result in (None, "__back__", "__quit__"):
        raise SystemExit(0)
    if result == "__new__":
        return None
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

    console.print(f"[dim]No sandbox for[/dim] [bold]{cwd}[/bold]")

    while True:
        options: list[Option | None] = []
        shortcuts: dict[str, str] = {"n": "new", "q": "__quit__"}

        # Recent sessions
        if recent:
            options.append(None)
            for idx, r in enumerate(recent):
                detail = _format_detail(r.info)
                oid = f"attach:{r.sandbox}:{r.tmux_name}"
                prompt = _styled_option(f"{r.sandbox}/{r.tmux_name}", detail, key=str(idx + 1))
                options.append(Option(prompt, id=oid))
                if idx < 9:
                    shortcuts[str(idx + 1)] = oid

        # Actions
        options.append(None)
        options.append(Option(
            _styled_option(f"New sandbox for {dirname}/", key="n"),
            id="new",
        ))

        if sandboxes:
            options.append(Option(
                _styled_option("Mount to existing sandbox\u2026", key="m"),
                id="mount",
            ))
            shortcuts["m"] = "mount"
            options.append(Option(
                _styled_option("Mount to existing sandbox (read-only)\u2026", key="r"),
                id="mount_ro",
            ))
            shortcuts["r"] = "mount_ro"

        options.append(Option(
            _styled_option("Quit", key="q"),
            id="__quit__",
        ))
        bindings.append(("q", "pick('__quit__')", "Quit"))

        result = _run_picker(options, shortcuts, numbered=False)

        if result in (None, "__back__", "__quit__"):
            raise SystemExit(0)
        if result.startswith("attach:"):
            _, sandbox, session = result.split(":", 2)
            return AttachSession(sandbox=sandbox, session=session)
        elif result == "new":
            return NewSandbox()
        elif result in ("mount", "mount_ro"):
            readonly = result == "mount_ro"
            mount_result = _pick_sandbox_for_mount(config, readonly=readonly)
            if mount_result is not None:
                return mount_result
            # __back__ from sub-picker: loop to show main menu again
            continue
        # unreachable
        return NewSandbox()


def _pick_sandbox_for_mount(config: Config, readonly: bool = False) -> MountToSandbox | None:
    """Sub-picker: choose which sandbox to mount CWD into. Returns None on back."""
    options: list[Option | None] = []
    for name, entry in config.state.sandboxes.items():
        mounts = ", ".join(os.path.basename(m.path) for m in entry.mounts[:3])
        prompt = _styled_option(name, f"({mounts})" if mounts else "")
        options.append(Option(prompt, id=name))

    console.print("[bold]Mount to:[/bold]")
    result = _run_picker(options)
    if result is None:
        raise SystemExit(0)
    if result == "__back__":
        return None
    return MountToSandbox(sandbox=result, readonly=readonly)
