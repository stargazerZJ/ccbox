"""Tmux session lifecycle inside LXD containers."""

from __future__ import annotations

import os
import shlex

from ccbox import lxd

CONTAINER_USER = "1000"  # UID for the mapped user
TMUX_CONF = "/etc/tmux.conf"


def list_sessions(container: str) -> list[dict]:
    """List tmux sessions inside the container.

    Returns list of dicts with keys: name, attached, created.
    """
    r = lxd.exec_cmd(
        container,
        ["tmux", "list-sessions", "-F", "#{session_name}|#{session_attached}|#{session_created}"],
        user=CONTAINER_USER,
        capture=True,
        check=False,
    )
    if r.returncode != 0:
        return []
    sessions = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            sessions.append({
                "name": parts[0],
                "attached": int(parts[1]) > 0,
                "created": parts[2],
            })
    return sessions


def detached_sessions(container: str) -> list[dict]:
    """Return only detached (unattached) sessions."""
    return [s for s in list_sessions(container) if not s["attached"]]


def next_session_name(container: str) -> str:
    """Generate the next sequential session name (s-0, s-1, ...)."""
    existing = list_sessions(container)
    used = set()
    for s in existing:
        name = s["name"]
        if name.startswith("s-"):
            try:
                used.add(int(name[2:]))
            except ValueError:
                pass
    n = 0
    while n in used:
        n += 1
    return f"s-{n}"


def create_session(
    container: str,
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    session_name: str | None = None,
    sandbox_name: str | None = None,
) -> str:
    """Create a new tmux session inside the container.

    Uses tmux new-session -d, then send-keys with exec so the command
    replaces bash — exiting the command kills the tmux session.
    """
    if session_name is None:
        session_name = next_session_name(container)

    if env is None:
        env = {}

    # Sandbox identity vars
    env.setdefault("IS_SANDBOX", "1")
    if sandbox_name is not None:
        env.setdefault("CCBOX_SANDBOX", sandbox_name)

    # Ensure HOME is set — lxc exec with --env flags may not inherit it
    from pathlib import Path
    env.setdefault("HOME", str(Path.home()))

    # Login identity vars — tools like git and claude expect these
    import getpass
    env.setdefault("USER", getpass.getuser())
    env.setdefault("LOGNAME", env["USER"])

    # Keep Claude's mutable config under ~/.claude (rw mount), not ~/.claude.json.
    env.setdefault("CLAUDE_CONFIG_DIR", f"{env['HOME']}/.claude")

    # Always set UV_HARDLINK_SOCKET so patched uv defers hardlinks to host
    from ccbox.config import UV_SOCK
    env.setdefault("UV_HARDLINK_SOCKET", str(UV_SOCK))

    # Build tmux new-session command
    tmux_args = ["tmux", "-f", TMUX_CONF, "new-session", "-d", "-s", session_name]
    if cwd:
        tmux_args += ["-c", cwd]

    lxd.exec_cmd(container, tmux_args, user=CONTAINER_USER, env=env)

    # exec replaces bash with the command — when it exits, the tmux session ends.
    lxd.exec_cmd(
        container,
        ["tmux", "send-keys", "-t", session_name, f"exec {command}", "Enter"],
        user=CONTAINER_USER,
    )

    return session_name


def attach_session(container: str, session_name: str) -> None:
    """Attach to an existing tmux session (interactive, inherited stdio)."""
    lxd.exec_interactive(
        container,
        ["tmux", "-f", TMUX_CONF, "attach-session", "-t", session_name],
        user=CONTAINER_USER,
    )
    print(f"Detached from session '{session_name}'.")


def kill_session(container: str, name: str) -> None:
    lxd.exec_cmd(
        container,
        ["tmux", "kill-session", "-t", name],
        user=CONTAINER_USER,
        check=False,
    )


def kill_all_sessions(container: str) -> None:
    lxd.exec_cmd(
        container,
        ["tmux", "kill-server"],
        user=CONTAINER_USER,
        check=False,
    )


def build_claude_command(extra_args: list[str] | None = None) -> str:
    """Build the claude invocation command string."""
    parts = ["claude", "--allow-dangerously-skip-permissions"]
    if extra_args:
        # Deduplicate the flag if user passed it
        for arg in extra_args:
            if arg == "--allow-dangerously-skip-permissions":
                continue
            parts.append(arg)
    return shlex.join(parts)


def _find_codex() -> str | None:
    """Find the codex binary — check nvm, then PATH."""
    import glob
    import shutil
    # Prefer nvm-installed codex (we know the node version to pair with)
    matches = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/codex"))
    if matches:
        return matches[0]
    # Fall back to whatever's on PATH
    return shutil.which("codex")


def build_codex_command(extra_args: list[str] | None = None) -> str:
    """Build the codex invocation command string with --yolo.

    Uses the full path to the nvm-installed codex and prepends its
    bin dir to PATH so the matching node version is found.
    """
    codex_path = _find_codex()
    if codex_path:
        codex_dir = os.path.dirname(codex_path)
        # Only prepend to PATH if it's an nvm path (needs paired node)
        nvm_bin = codex_dir if "/.nvm/" in codex_path else None
        parts = [codex_path, "--yolo"]
    else:
        nvm_bin = None
        parts = ["codex", "--yolo"]
    if extra_args:
        for arg in extra_args:
            if arg in ("--yolo", "--dangerously-bypass-approvals-and-sandbox"):
                continue
            parts.append(arg)
    cmd = shlex.join(parts)
    if nvm_bin:
        # Use env(1) to prepend nvm bin to PATH — inline VAR=val
        # doesn't work with bash's exec builtin.
        cmd = f"env PATH={shlex.quote(nvm_bin)}:$PATH {cmd}"
    return cmd


def get_forwarded_env(whitelist: list[str]) -> dict[str, str]:
    """Read host env vars that should be forwarded into the container."""
    result = {}
    for var in whitelist:
        val = os.environ.get(var)
        if val is not None:
            result[var] = val
    return result
