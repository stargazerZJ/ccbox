"""Tmux runtime lifecycle and Claude conversation bindings."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ccbox import lxd

CONTAINER_USER = "1000"  # UID for the mapped user
TMUX_CONF = f"{os.path.expanduser('~')}/.config/ccbox/tmux.conf"
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_TMUX_FORMAT = "#{session_name}|#{session_attached}|#{session_created}"


def _validate_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def tmux_socket_path(sandbox_name: str) -> Path:
    """Return the explicit shared tmux socket for a sandbox."""
    from ccbox.config import TMUX_SOCKET_DIR

    return TMUX_SOCKET_DIR / f"{_validate_component(sandbox_name, 'sandbox name')}.sock"


def binding_path(sandbox_name: str, runtime_name: str) -> Path:
    """Return the JSON binding path for one live runtime."""
    from ccbox.config import SESSION_BINDING_DIR

    sandbox = _validate_component(sandbox_name, "sandbox name")
    runtime = _validate_component(runtime_name, "runtime name")
    return SESSION_BINDING_DIR / sandbox / f"{runtime}.json"


def _parse_tmux_sessions(output: str) -> list[dict]:
    sessions = []
    for line in output.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            try:
                attached = int(parts[1]) > 0
                created = int(parts[2])
            except ValueError:
                continue
            sessions.append({"name": parts[0], "attached": attached, "created": created})
    return sessions


def _query_sessions(sandbox_name: str) -> list[dict] | None:
    """Query one socket, distinguishing an unreachable server from a live result."""
    socket_path = tmux_socket_path(sandbox_name)
    try:
        result = subprocess.run(
            ["tmux", "-S", str(socket_path), "list-sessions", "-F", _TMUX_FORMAT],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return _parse_tmux_sessions(result.stdout)


def list_sessions(sandbox_name: str) -> list[dict]:
    """List live runtimes directly through the sandbox's shared tmux socket."""
    sessions = _query_sessions(sandbox_name)
    if sessions is None:
        return []
    return sessions


def list_all_sessions(
    config: Any, *, max_workers: int = 8, container_states: dict[str, str] | None = None
) -> list[dict]:
    """List live runtimes across all configured, running sandboxes."""
    states = container_states if container_states is not None else lxd.all_container_states()
    running = [
        (name, entry)
        for name, entry in config.state.sandboxes.items()
        if states.get(entry.container) == "Running"
    ]
    if not running:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(running))) as pool:
        futures = {pool.submit(_query_sessions, name): (name, entry) for name, entry in running}
        for future in as_completed(futures):
            sandbox_name, entry = futures[future]
            try:
                sessions = future.result()
            except Exception:
                sessions = None
            if sessions is None:
                continue
            prune_stale_bindings(sandbox_name, {session["name"] for session in sessions})
            for session in sessions:
                results.append({"sandbox": sandbox_name, "container": entry.container, **session})
    return results


def detached_sessions(sandbox_name: str) -> list[dict]:
    """Return only detached (unattached) sessions."""
    return [s for s in list_sessions(sandbox_name) if not s["attached"]]


def next_session_name(sandbox_name: str) -> str:
    """Generate the next sequential session name (s-0, s-1, ...)."""
    existing = list_sessions(sandbox_name)
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


def sandbox_env(env: dict[str, str], sandbox_name: str | None = None) -> dict[str, str]:
    """Add standard ccbox internal env vars to *env* (in-place) and return it.

    Called by both ``create_session`` (tmux path) and ``cmd_shell`` (su -l path)
    so that any tool launched inside the container sees a consistent environment.
    """
    import getpass
    from pathlib import Path

    from ccbox.config import UV_SOCK

    env.setdefault("IS_SANDBOX", "1")
    if sandbox_name is not None:
        env.setdefault("CCBOX_SANDBOX", sandbox_name)

    env.setdefault("HOME", str(Path.home()))
    env.setdefault("USER", getpass.getuser())
    env.setdefault("LOGNAME", env["USER"])
    env.setdefault("CLAUDE_CONFIG_DIR", f"{env['HOME']}/.claude")
    env.setdefault("UV_HARDLINK_SOCKET", str(UV_SOCK))
    return env


def create_session(
    container: str,
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    unset_vars: list[str] | None = None,
    session_name: str | None = None,
    sandbox_name: str,
) -> str:
    """Create a new tmux session inside the container.

    All tmux operations are batched into a single lxc exec call
    (~0.2s total instead of ~1.0s for 5 separate calls).
    """
    if env is None:
        env = {}

    sandbox_env(env, sandbox_name)

    # Pin CCBOX_CWD to the target cwd so profile.sh's `cd "$CCBOX_CWD"` lands
    # here instead of inheriting a stale value baked into the tmux server env
    # when it was first started from a different directory.
    if cwd is not None:
        env["CCBOX_CWD"] = cwd

    from ccbox.config import SESSION_BINDING_DIR, TMUX_SOCKET_DIR

    _validate_component(sandbox_name, "sandbox name")
    TMUX_SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    socket_path = tmux_socket_path(sandbox_name)
    if socket_path.exists() and _query_sessions(sandbox_name) is None:
        # A stopped/removed container can leave a refused socket inode behind.
        socket_path.unlink(missing_ok=True)

    # CCBOX_TMUX_SESSION is set inside the script (depends on resolved name)
    script = _build_session_script(
        session_name=session_name,
        cwd=cwd,
        env=env,
        unset_vars=unset_vars or [],
        command=command,
        socket_path=str(socket_path),
        session_binding_dir=str(SESSION_BINDING_DIR),
        sandbox_name=sandbox_name,
    )

    r = lxd.exec_cmd(
        container,
        ["sh", "-c", script],
        user=CONTAINER_USER,
        capture=True,
    )

    # Session name is the last line of output
    name = r.stdout.strip().rsplit("\n", 1)[-1]

    return name


def _build_session_script(
    *,
    session_name: str | None,
    cwd: str | None,
    env: dict[str, str],
    unset_vars: list[str],
    command: str,
    socket_path: str,
    session_binding_dir: str,
    sandbox_name: str,
) -> str:
    """Build a shell script that creates and configures a tmux session.

    Performs session name resolution, creation, env setup, pane respawn,
    and command injection in one shot. Outputs the session name on stdout.
    """
    lines: list[str] = []

    tmux = f"tmux -S {shlex.quote(socket_path)}"

    # Resolve session name.
    if session_name is not None:
        _validate_component(session_name, "runtime name")
        lines.append(f"name={shlex.quote(session_name)}")
    else:
        lines.append(
            f'existing=$({tmux} list-sessions -F "#{{session_name}}" 2>/dev/null || true)\n'
            "n=0\n"
            'while printf "%s\\n" "$existing" | grep -qx "s-$n"; do n=$((n+1)); done\n'
            'name="s-$n"'
        )

    # Create detached session
    new_cmd = f'{tmux} -f {TMUX_CONF} new-session -d -s "$name"'
    if cwd:
        new_cmd += f" -c {shlex.quote(cwd)}"
    lines.append(new_cmd)

    binding_dir = f"{session_binding_dir}/{sandbox_name}"
    lines.append(f'_binding="{binding_dir}/$name.json"')
    lines.append('rm -f "$_binding"')

    # Remember the runtime's original pane: Claude's SessionStart hook compares it
    # against its own TMUX_PANE so tmux-backed teammates (split panes) never steal
    # the runtime binding. respawn-pane keeps the pane id.
    lines.append(f'pane=$({tmux} display-message -p -t "$name" "#{{pane_id}}")')

    # Inject env vars via tmux set-environment (includes CCBOX_TMUX_SESSION)
    env["CCBOX_TMUX_SESSION"] = "$name"  # resolved by the shell
    env["CCBOX_TMUX_PANE"] = "$pane"
    for k, v in env.items():
        if v in ("$name", "$pane"):
            lines.append(f'{tmux} set-environment -t "$name" {shlex.quote(k)} "{v}"')
        else:
            lines.append(f'{tmux} set-environment -t "$name" {shlex.quote(k)} {shlex.quote(v)}')
    del env["CCBOX_TMUX_SESSION"]  # don't mutate caller's dict permanently
    del env["CCBOX_TMUX_PANE"]

    # Explicitly unset whitelisted vars not present on the host.
    # We can't rely on `tmux set-environment -u` because the tmux server's own
    # global environment (frozen at server-start time) still propagates to new
    # shells regardless of session-level overrides. Instead we set CCBOX_UNSET_VARS
    # so ccbox-profile.sh can do an explicit `unset` in the actual shell process.
    if unset_vars:
        lines.append(
            f'{tmux} set-environment -t "$name" CCBOX_UNSET_VARS '
            f"{shlex.quote(','.join(unset_vars))}"
        )

    # Respawn pane so the shell picks up the tmux session environment
    respawn = f'{tmux} respawn-pane -k -t "$name"'
    if cwd:
        respawn += f" -c {shlex.quote(cwd)}"
    lines.append(respawn)

    # exec replaces bash; remain-on-exit is disabled, so the session disappears
    # when the foreground command exits.
    send_cmd = f"exec {command}"
    lines.append(f'{tmux} send-keys -t "$name" {shlex.quote(send_cmd)} Enter')

    # Output session name for the caller to parse
    lines.append('printf "%s" "$name"')

    return "\n".join(lines)


def attach_session(container: str, session_name: str, *, sandbox_name: str) -> bool:
    """Attach interactively. Return False when the runtime vanished before attach."""
    result = lxd.exec_interactive(
        container,
        [
            "tmux",
            "-S",
            str(tmux_socket_path(sandbox_name)),
            "-f",
            TMUX_CONF,
            "attach-session",
            "-t",
            session_name,
        ],
        user=CONTAINER_USER,
        # lxc exec gives the client no HOME; ncurses needs it for ~/.terminfo.
        env={"HOME": os.path.expanduser("~")},
    )
    return result.returncode == 0


def kill_session(container: str, name: str, *, sandbox_name: str) -> None:
    lxd.exec_cmd(
        container,
        ["tmux", "-S", str(tmux_socket_path(sandbox_name)), "kill-session", "-t", name],
        user=CONTAINER_USER,
        check=False,
    )
    clean_session_binding(sandbox_name, name)
    if not _query_sessions(sandbox_name):
        socket_path = tmux_socket_path(sandbox_name)
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            capture_output=True,
            check=False,
        )
        socket_path.unlink(missing_ok=True)


def kill_all_sessions(container: str, *, sandbox_name: str) -> None:
    lxd.exec_cmd(
        container,
        ["tmux", "-S", str(tmux_socket_path(sandbox_name)), "kill-server"],
        user=CONTAINER_USER,
        check=False,
    )
    clean_session_runtime_state(sandbox_name)


# --- SessionStart hook ownership -------------------------------------------
#
# Every Claude process started inside a runtime fires the SessionStart hook:
# the conversation the user is looking at, tmux-backed teammates in split
# panes, in-process teammates, nested ``claude -p`` runs from a Bash tool, ...
# Only the pane's top-level process may (re)bind the runtime.


def _tmux_socket_from_env(env: dict[str, str]) -> str | None:
    """Socket path from tmux's ``TMUX=<socket>,<pid>,<index>`` variable."""
    value = env.get("TMUX", "")
    socket_path = value.split(",", 1)[0] if value else ""
    return socket_path or None


def _tmux_query(socket_path: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def resolve_runtime_pane(sandbox_name: str, runtime_name: str, env: dict[str, str]) -> str | None:
    """Return the runtime's original pane id (``%N``), or None when unknown.

    Prefers ``CCBOX_TMUX_PANE`` recorded at creation; older runtimes fall back
    to the lowest pane id in the session, which is the pane the runtime was
    created with (splits only ever allocate higher ids).
    """
    recorded = env.get("CCBOX_TMUX_PANE", "")
    if recorded.startswith("%"):
        return recorded
    socket_path = _tmux_socket_from_env(env) or str(tmux_socket_path(sandbox_name))
    output = _tmux_query(
        socket_path, ["list-panes", "-s", "-t", f"={runtime_name}", "-F", "#{pane_id}"]
    )
    if not output:
        return None
    ids = []
    for line in output.split():
        if line.startswith("%") and line[1:].isdigit():
            ids.append(int(line[1:]))
    return f"%{min(ids)}" if ids else None


def resolve_pane_pid(sandbox_name: str, pane_id: str, env: dict[str, str]) -> int | None:
    """PID of the process running in *pane_id* (the exec'd claude), or None."""
    socket_path = _tmux_socket_from_env(env) or str(tmux_socket_path(sandbox_name))
    output = _tmux_query(socket_path, ["display-message", "-p", "-t", pane_id, "#{pane_pid}"])
    if output is None:
        return None
    value = output.strip()
    return int(value) if value.isdigit() else None


def _proc_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as stat:
            data = stat.read()
    except OSError:
        return None
    # comm may contain spaces/parens; fields after the last ')' are fixed.
    rest = data.rsplit(")", 1)[-1].split()
    if len(rest) < 2 or not rest[1].isdigit():
        return None
    return int(rest[1])


def _proc_exe(pid: int) -> str | None:
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return None


def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as cmdline:
            data = cmdline.read()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in data.split(b"\0") if part]


def _is_claude_process(pid: int) -> bool:
    exe = _proc_exe(pid) or ""
    if os.path.basename(exe) == "claude" or "/claude/versions/" in exe:
        return True
    argv = _proc_cmdline(pid) or []
    if argv and os.path.basename(argv[0]) == "claude":
        return True
    # npm-style install: node .../@anthropic-ai/claude-code/cli.js
    return any(arg.endswith("/cli.js") and "claude" in arg for arg in argv[1:3])


def nested_claude_between(start_pid: int, pane_pid: int | None) -> bool:
    """True if the hook at *start_pid* runs under a claude nested in the pane's claude.

    Walks ancestors from *start_pid* up to and including *pane_pid* (or init
    when the pane pid is unknown) and counts claude-looking processes. The
    pane's own claude accounts for one; a second one means the hook came from
    a nested run such as ``claude -p`` inside a Bash tool call.
    """
    claude_seen = 0
    pid: int | None = start_pid
    for _ in range(128):
        if pid is None or pid <= 1:
            break
        if _is_claude_process(pid):
            claude_seen += 1
            if claude_seen >= 2:
                return True
        if pane_pid is not None and pid == pane_pid:
            break
        pid = _proc_ppid(pid)
    return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def should_accept_binding(
    payload: dict[str, Any],
    existing: dict[str, Any] | None,
    *,
    runtime_pane: str | None,
    hook_pane: str | None,
    nested_claude: bool,
    owner_alive: Any = pid_alive,
) -> bool:
    """Decide whether a SessionStart hook may (re)bind its runtime.

    Rejects hooks from other panes (tmux-backed teammates), hooks fired by a
    claude nested under the pane's claude (``claude -p`` from a Bash tool), and
    ``startup`` takeovers by a different session while the owning process is
    still alive (in-process teammates). Same-process ``clear``/``resume``/
    ``compact`` transitions always win.
    """
    if runtime_pane and hook_pane and runtime_pane != hook_pane:
        return False
    if nested_claude:
        return False
    if (
        existing is not None
        and existing.get("session_id") != payload.get("session_id")
        and payload.get("source") == "startup"
    ):
        owner_pid = existing.get("owner_pid")
        if isinstance(owner_pid, int) and owner_alive(owner_pid):
            return False
    return True


def bind_session_from_hook(
    sandbox_name: str,
    runtime_name: str,
    payload: dict[str, Any],
    env: dict[str, str] | None = None,
    *,
    hook_pid: int | None = None,
) -> dict[str, Any] | None:
    """Apply the ownership rules and write the binding. Returns the record or None."""
    env = dict(os.environ) if env is None else env
    hook_pid = os.getppid() if hook_pid is None else hook_pid
    runtime_pane = resolve_runtime_pane(sandbox_name, runtime_name, env)
    pane_pid = (
        resolve_pane_pid(sandbox_name, runtime_pane, env) if runtime_pane is not None else None
    )
    accepted = should_accept_binding(
        payload,
        read_session_binding(sandbox_name, runtime_name),
        runtime_pane=runtime_pane,
        hook_pane=env.get("TMUX_PANE") or None,
        nested_claude=nested_claude_between(hook_pid, pane_pid),
    )
    if not accepted:
        return None
    return write_session_binding(sandbox_name, runtime_name, payload, owner_pid=pane_pid)


def write_session_binding(
    sandbox_name: str,
    runtime_name: str,
    payload: dict[str, Any],
    *,
    owner_pid: int | None = None,
) -> dict[str, Any]:
    """Validate and atomically write a SessionStart runtime binding."""
    required = ("session_id", "transcript_path", "cwd")
    for field in required:
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError(f"Binding field {field!r} must be a non-empty string")
    source = payload.get("source")
    if source is not None and not isinstance(source, str):
        raise ValueError("Binding field 'source' must be a string when present")

    record = {
        "schema": 1,
        "sandbox": _validate_component(sandbox_name, "sandbox name"),
        "runtime": _validate_component(runtime_name, "runtime name"),
        "session_id": payload["session_id"],
        "transcript_path": payload["transcript_path"],
        "cwd": payload["cwd"],
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if source is not None:
        record["source"] = source
    if owner_pid is not None:
        record["owner_pid"] = int(owner_pid)

    destination = binding_path(sandbox_name, runtime_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, prefix=f".{runtime_name}.", delete=False
        ) as temp:
            temp_name = temp.name
            json.dump(record, temp, sort_keys=True)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, destination)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return record


def read_session_binding(sandbox_name: str, runtime_name: str) -> dict[str, Any] | None:
    try:
        value = json.loads(binding_path(sandbox_name, runtime_name).read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema") != 1:
        return None
    if value.get("sandbox") != sandbox_name or value.get("runtime") != runtime_name:
        return None
    if not isinstance(value.get("session_id"), str) or not isinstance(
        value.get("transcript_path"), str
    ):
        return None
    return value


def clean_session_binding(sandbox_name: str, runtime_name: str) -> None:
    path = binding_path(sandbox_name, runtime_name)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def prune_stale_bindings(sandbox_name: str, live_runtime_names: set[str]) -> None:
    from ccbox.config import SESSION_BINDING_DIR

    sandbox_dir = SESSION_BINDING_DIR / _validate_component(sandbox_name, "sandbox name")
    if not sandbox_dir.is_dir():
        return
    for path in sandbox_dir.glob("*.json"):
        if path.stem not in live_runtime_names:
            path.unlink(missing_ok=True)
    try:
        sandbox_dir.rmdir()
    except OSError:
        pass


def clean_session_runtime_state(sandbox_name: str) -> None:
    """Remove a sandbox's shared socket and all binding records."""
    from ccbox.config import SESSION_BINDING_DIR

    sandbox = _validate_component(sandbox_name, "sandbox name")
    tmux_socket_path(sandbox).unlink(missing_ok=True)
    shutil.rmtree(SESSION_BINDING_DIR / sandbox, ignore_errors=True)


def build_claude_command(extra_args: list[str] | None = None) -> str:
    """Build the claude invocation command string."""
    parts = ["claude", "--dangerously-skip-permissions"]
    if extra_args:
        # Deduplicate the flag if user passed it
        for arg in extra_args:
            if arg == "--dangerously-skip-permissions":
                continue
            parts.append(arg)
    return shlex.join(parts)


def session_exists(sandbox_name: str, name: str) -> bool:
    """True if a tmux session with exactly *name* is still alive in the container.

    Used after an attach returns to tell a natural exit (session gone) apart from
    a Ctrl+Q detach (session still present).
    """
    return any(s["name"] == name for s in list_sessions(sandbox_name))


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


def get_unset_env_vars(whitelist: list[str]) -> list[str]:
    """Return whitelisted vars that are NOT set on the host (should be unset in container)."""
    return [var for var in whitelist if os.environ.get(var) is None]
