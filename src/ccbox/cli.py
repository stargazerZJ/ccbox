"""CLI entry point and subcommand routing."""

from __future__ import annotations

import argparse
import os
import sys

from ccbox import lxd
from ccbox.config import SESSION_LINK_DIR, Config
from ccbox.mount import add_mount, remove_mount, sync_auto_mounts
from ccbox.picker import (
    AttachSession,
    MountToSandbox,
    NewSandbox,
    pick_no_resolve,
    pick_session,
    pick_session_all,
)
from ccbox.port import (
    _parse_addr_port,
    add_expose,
    add_forward,
    list_ports,
    remove_port,
)
from ccbox.sandbox import (
    create_sandbox,
    ensure_running,
    list_sandboxes,
    remove_sandbox,
    resolve_sandbox,
    sandbox_status,
    stop_sandbox,
)
from ccbox.session import (
    attach_session,
    build_claude_command,
    build_codex_command,
    cached_sessions_with_state,
    clean_session_link,
    create_session,
    get_forwarded_env,
    kill_all_sessions,
    kill_session,
    list_sessions,
)
from ccbox.transcript import read_session_info_any, relative_time


def check_lxd_group() -> None:
    """Check if the current user is in the lxd group."""
    import grp

    try:
        lxd_group = grp.getgrnam("lxd")
        import getpass

        username = getpass.getuser()
        if username not in lxd_group.gr_mem:
            # Also check primary group
            import pwd

            pw = pwd.getpwnam(username)
            if pw.pw_gid != lxd_group.gr_gid:
                print("Error: Your user is not in the 'lxd' group.", file=sys.stderr)
                print("Run: sudo usermod -aG lxd $USER", file=sys.stderr)
                print("Then re-login or run: newgrp lxd", file=sys.stderr)
                raise SystemExit(1)
    except KeyError:
        # lxd group doesn't exist — LXD may not be installed
        pass


def _container_username(container: str) -> str:
    """Resolve the username for UID 1000 inside the container."""
    r = lxd.exec_cmd(container, ["id", "-un", "1000"], capture=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else "ubuntu"


def _parse_sandbox_session(spec: str) -> tuple[str, str] | None:
    """Parse 'sandbox/session' spec. Returns (sandbox, session) or None if plain name."""
    if "/" in spec:
        sandbox, _, session = spec.partition("/")
        if sandbox and session:
            return sandbox, session
    return None


def resolve_session(container: str, name: str | None) -> str:
    """Return a session name, prompting with a picker if name is None and multiple exist."""
    sessions = list_sessions(container)
    if name is not None:
        return name  # trust caller; tmux will error if invalid
    if len(sessions) == 0:
        raise ValueError("No sessions in sandbox")
    if len(sessions) == 1:
        return sessions[0]["name"]
    # picker
    for i, s in enumerate(sessions):
        status = "attached" if s["attached"] else "detached"
        print(f"  [{i}] {s['name']} ({status})")
    choice = input("Select session: ").strip()
    try:
        return sessions[int(choice)]["name"]
    except (ValueError, IndexError) as e:
        raise ValueError("Invalid selection") from e


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


def _format_session_line(
    index: int, tmux_name: str, info: dict | None, attached: bool = False
) -> str:
    """Format a single session line for the picker."""
    status = "attached" if attached else "detached"
    if info is None:
        return f"  [{index}] {tmux_name} ({status})"
    parts = []
    if info["last_prompt"]:
        prompt = info["last_prompt"]
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        parts.append(f'"{prompt}"')
    ts = relative_time(info["timestamp"])
    if ts:
        parts.append(ts)
    if info["git_branch"]:
        parts.append(info["git_branch"])
    count = info.get("message_count", 0)
    if count:
        parts.append(f"{count} msg{'s' if count != 1 else ''}")
    detail = " · ".join(parts)
    return f"  [{index}] {tmux_name} ({status})  {detail}"


def cmd_session_cleanup(config: Config, args: argparse.Namespace) -> None:
    """Internal: called by session shell to clean up session-link on natural exit."""
    sandbox = os.environ.get("CCBOX_SANDBOX", "")
    tmux_session = os.environ.get("CCBOX_TMUX_SESSION", "")
    if not sandbox or not tmux_session:
        return  # no-op outside ccbox containers
    clean_session_link(sandbox, tmux_session)


def cmd_session_link(config: Config, args: argparse.Namespace) -> None:
    """Internal: called by SessionStart hook to link tmux↔transcript session."""
    import json as _json

    sandbox = os.environ.get("CCBOX_SANDBOX", "")
    tmux_session = os.environ.get("CCBOX_TMUX_SESSION", "")
    if not sandbox or not tmux_session:
        return  # no-op outside ccbox containers

    try:
        hook_input = _json.load(sys.stdin)
    except (_json.JSONDecodeError, ValueError):
        return

    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path:
        return

    link_dir = SESSION_LINK_DIR / sandbox
    link_dir.mkdir(parents=True, exist_ok=True)
    link_file = link_dir / tmux_session
    link_file.write_text(transcript_path + "\n")


def cmd_resolve(config: Config, args: argparse.Namespace) -> None:
    """Show which sandbox resolves for the current directory."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    entry = config.get_sandbox(sandbox_name)
    print(f"Sandbox: {sandbox_name}")
    print(f"Container: {entry.container}")
    if entry.mounts:
        for m in entry.mounts:
            print(f"  {m.path} ({m.mode})")


def cmd_default(config: Config, args: argparse.Namespace) -> None:
    """Default command: find/create sandbox for CWD, manage sessions."""
    cwd = os.getcwd()
    env = get_forwarded_env(config.state.env_whitelist)

    sandbox_name = config.sandbox_for_path(cwd)

    if sandbox_name is not None:
        # CWD resolves — use session picker within this sandbox
        container = ensure_running(config, sandbox_name)
        sessions = list_sessions(container)

        chosen = pick_session(sessions, sandbox_name)
        if chosen is not None:
            attach_session(container, chosen, sandbox_name=sandbox_name)
        else:
            cmd = build_claude_command()
            name = create_session(container, cmd, cwd=cwd, env=env, sandbox_name=sandbox_name)
            attach_session(container, name, sandbox_name=sandbox_name)
    else:
        # CWD doesn't resolve — show unified picker
        result = pick_no_resolve(config, cwd)

        if isinstance(result, AttachSession):
            container = ensure_running(config, result.sandbox)
            attach_session(container, result.session, sandbox_name=result.sandbox)
        elif isinstance(result, NewSandbox):
            sandbox_name = result.name
            # Deduplicate name if it already exists
            base_name = sandbox_name
            if config.get_sandbox(sandbox_name) is not None:
                n = 1
                while config.get_sandbox(f"{base_name}-{n}") is not None:
                    n += 1
                sandbox_name = f"{base_name}-{n}"
            print(f"Creating sandbox '{sandbox_name}' for {cwd}...")
            create_sandbox(config, sandbox_name, mounts=[(cwd, False)])
            container = ensure_running(config, sandbox_name)
            cmd = build_claude_command()
            name = create_session(container, cmd, cwd=cwd, env=env, sandbox_name=sandbox_name)
            attach_session(container, name, sandbox_name=sandbox_name)
        elif isinstance(result, MountToSandbox):
            from ccbox.mount import add_mount

            add_mount(config, result.sandbox, cwd, readonly=result.readonly)
            container = ensure_running(config, result.sandbox)
            cmd = build_claude_command()
            name = create_session(container, cmd, cwd=cwd, env=env, sandbox_name=result.sandbox)
            attach_session(container, name, sandbox_name=result.sandbox)


def cmd_claude(config: Config, args: argparse.Namespace) -> None:
    """Always create a new session running claude with given args."""
    cwd = os.getcwd()

    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)
    env = get_forwarded_env(config.state.env_whitelist)

    claude_args = args.claude_args
    if claude_args and claude_args[0] == "--":
        claude_args = claude_args[1:]
    cmd = build_claude_command(claude_args)
    name = create_session(container, cmd, cwd=cwd, env=env, sandbox_name=sandbox_name)
    attach_session(container, name, sandbox_name=sandbox_name)


def cmd_codex(config: Config, args: argparse.Namespace) -> None:
    """Always create a new session running codex --yolo with given args."""
    cwd = os.getcwd()

    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)
    env = get_forwarded_env(config.state.env_whitelist)

    codex_args = args.codex_args
    if codex_args and codex_args[0] == "--":
        codex_args = codex_args[1:]
    cmd = build_codex_command(codex_args)
    name = create_session(container, cmd, cwd=cwd, env=env, sandbox_name=sandbox_name)
    attach_session(container, name, sandbox_name=sandbox_name)


def cmd_ls(config: Config, args: argparse.Namespace) -> None:
    """List sandboxes."""
    sandboxes = list_sandboxes(config)
    if not sandboxes:
        print("No sandboxes.")
        return

    # Table header
    print(f"{'NAME':<20} {'STATE':<10} {'SESSIONS':<10} {'MOUNTS':<8}")
    print("-" * 50)
    for s in sandboxes:
        print(f"{s['name']:<20} {s['state']:<10} {s['sessions']:<10} {s['mounts']:<8}")


def cmd_create(config: Config, args: argparse.Namespace) -> None:
    """Create a new sandbox."""
    cwd = os.getcwd()
    mounts = None if args.bare else [(cwd, False)]
    create_sandbox(config, args.name, mounts=mounts)
    print(f"Sandbox '{args.name}' created.")
    if not args.bare:
        print(f"Mounted '{cwd}' (rw).")


def cmd_mount(config: Config, args: argparse.Namespace) -> None:
    """Add a mount to a sandbox."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    add_mount(config, sandbox_name, args.path, readonly=args.ro)
    mode = "ro" if args.ro else "rw"
    print(f"Mounted '{args.path}' ({mode}) in sandbox '{sandbox_name}'.")


def cmd_unmount(config: Config, args: argparse.Namespace) -> None:
    """Remove a mount from a sandbox."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    remove_mount(config, sandbox_name, args.path)
    print(f"Unmounted '{args.path}' from sandbox '{sandbox_name}'.")


def cmd_sessions(config: Config, args: argparse.Namespace) -> None:
    """List sessions in a sandbox (or all sandboxes with -a)."""
    if args.all:
        _cmd_sessions_all(config)
        return
    try:
        sandbox_name = resolve_sandbox(config, args.sandbox)
    except ValueError:
        if args.sandbox is None:
            print(
                "No sandbox found for current directory. Use -a to list all.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise
    entry = config.get_sandbox(sandbox_name)
    if entry is None:
        print(f"No sessions in sandbox '{sandbox_name}'.")
        return

    # Live tmux query for accurate attached state
    sessions = list_sessions(entry.container)

    if not sessions:
        print(f"No sessions in sandbox '{sandbox_name}'.")
        return

    print(f"Sessions in sandbox '{sandbox_name}':")
    for i, s in enumerate(sessions):
        info = _session_info(sandbox_name, s["name"])
        print(_format_session_line(i, s["name"], info, attached=s["attached"]))


def _cmd_sessions_all(config: Config) -> None:
    """List sessions across all sandboxes."""
    from ccbox import lxd as _lxd

    # Batch-fetch all container states in one API call
    container_states = _lxd.all_container_states()

    # Use session-link cache with .attached markers — no lxc exec calls needed
    total = 0
    for name, entry in config.state.sandboxes.items():
        if container_states.get(entry.container) != "Running":
            continue
        sessions = cached_sessions_with_state(name)
        if not sessions:
            continue
        print(f"{name}:")
        for i, s in enumerate(sessions):
            info = _session_info(name, s["name"])
            print(_format_session_line(i, f"{name}/{s['name']}", info, attached=s["attached"]))
        total += len(sessions)
        print()
    if total == 0:
        print("No sessions in any sandbox.")


def cmd_attach(config: Config, args: argparse.Namespace) -> None:
    """Attach to a session."""
    if getattr(args, "all", False) and args.session is None:
        _cmd_attach_all(config)
        return
    # Parse sandbox/session combined spec
    sandbox_arg = args.sandbox
    session_arg = args.session
    if session_arg is not None:
        parsed = _parse_sandbox_session(session_arg)
        if parsed:
            sandbox_arg, session_arg = parsed
    try:
        sandbox_name = resolve_sandbox(config, sandbox_arg)
    except ValueError:
        if sandbox_arg is None:
            print(
                "No sandbox found for current directory. Use -a to pick from all.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        raise
    container = ensure_running(config, sandbox_name)
    if session_arg is not None:
        session_name = resolve_session(container, session_arg)
    else:
        sessions = list_sessions(container)
        session_name = pick_session(sessions, sandbox_name)
        if session_name is None:
            # User chose "new session" — not applicable for attach
            print("No session selected.")
            return
    attach_session(container, session_name, sandbox_name=sandbox_name)


def _cmd_attach_all(config: Config) -> None:
    """Pick a session from all sandboxes and attach."""
    result = pick_session_all(config)
    if result is None:
        print("No session selected.")
        return
    container = ensure_running(config, result.sandbox)
    attach_session(container, result.session, sandbox_name=result.sandbox)


def cmd_kill(config: Config, args: argparse.Namespace) -> None:
    """Kill session(s)."""
    # Parse sandbox/session combined spec
    sandbox_arg = args.sandbox
    session_arg = args.session
    if session_arg is not None:
        parsed = _parse_sandbox_session(session_arg)
        if parsed:
            sandbox_arg, session_arg = parsed
    sandbox_name = resolve_sandbox(config, sandbox_arg)
    container = ensure_running(config, sandbox_name)

    if args.all:
        kill_all_sessions(container, sandbox_name=sandbox_name)
        print(f"All sessions killed in sandbox '{sandbox_name}'.")
    else:
        session_name = resolve_session(container, args.session)
        kill_session(container, session_name, sandbox_name=sandbox_name)
        print(f"Session '{session_name}' killed.")


def cmd_stop(config: Config, args: argparse.Namespace) -> None:
    """Stop a sandbox."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    stop_sandbox(config, sandbox_name)
    print(f"Sandbox '{sandbox_name}' stopped.")


def cmd_rm(config: Config, args: argparse.Namespace) -> None:
    """Remove a sandbox."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    remove_sandbox(config, sandbox_name)
    print(f"Sandbox '{sandbox_name}' removed.")


def cmd_status(config: Config, args: argparse.Namespace) -> None:
    """Show sandbox details."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    status = sandbox_status(config, sandbox_name)

    print(f"Sandbox: {status['name']}")
    print(f"Container: {status['container']}")
    print(f"State: {status['state']}")

    if status["mounts"]:
        print("Mounts:")
        for m in status["mounts"]:
            inode = f" inode={m['inode']}" if m.get("inode") else ""
            print(f"  {m['path']} ({m['mode']}{inode})")

    if status["sessions"]:
        print("Sessions:")
        for s in status["sessions"]:
            st = "attached" if s["attached"] else "detached"
            print(f"  {s['name']:<12} {st}")


def cmd_shell(config: Config, args: argparse.Namespace) -> None:
    """Drop into a bash shell in the sandbox (no tmux)."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)
    cwd = os.getcwd()
    username = _container_username(container)
    env = get_forwarded_env(config.state.env_whitelist)
    env["CCBOX_CWD"] = cwd
    env["CCBOX_SANDBOX"] = sandbox_name
    # su -l gives a full login env (PAM, /etc/environment, profiles).
    # -w preserves listed vars through the login env reset; .bashrc cd's to CCBOX_CWD.
    preserve = ",".join(env.keys())
    lxd.exec_interactive(
        container,
        ["su", "-l", username, "-w", preserve],
        env=env,
    )


def cmd_port(config: Config, args: argparse.Namespace) -> None:
    """Dispatch port subcommands."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)

    if args.port_action == "forward":
        host_addr, host_port = _parse_addr_port(args.target)
        name = add_forward(
            container,
            args.container_port,
            host_addr,
            host_port,
            udp=args.udp,
        )
        proto = "udp" if args.udp else "tcp"
        msg = f"Forward ({proto}): container:{args.container_port} → {host_addr}:{host_port}"
        print(f"{msg}  [{name}]")

    elif args.port_action == "expose":
        bind_addr, bind_port = (
            _parse_addr_port(args.bind, default_addr="127.0.0.1")
            if args.bind
            else ("127.0.0.1", None)
        )
        name = add_expose(
            container,
            args.container_port,
            bind_addr,
            bind_port,
            udp=args.udp,
        )
        proto = "udp" if args.udp else "tcp"
        effective_port = bind_port if bind_port is not None else args.container_port
        msg = f"Expose ({proto}): {bind_addr}:{effective_port} → container:{args.container_port}"
        print(f"{msg}  [{name}]")

    elif args.port_action == "ls":
        ports = list_ports(container)
        if not ports:
            print("No port forwards.")
            return
        print(f"{'NAME':<24} {'DIRECTION':<10} {'LISTEN':<28} {'CONNECT':<28}")
        print("-" * 92)
        for p in ports:
            print(f"{p['name']:<24} {p['direction']:<10} {p['listen']:<28} {p['connect']:<28}")

    elif args.port_action == "rm":
        remove_port(container, args.name)
        print(f"Removed '{args.name}'.")


def cmd_config(config: Config, args: argparse.Namespace) -> None:
    """Dispatch config subcommands."""
    if args.config_type == "env":
        if args.env_action == "add":
            config.add_env(args.var)
            print(f"Added '{args.var}' to env whitelist.")
        elif args.env_action == "remove":
            config.remove_env(args.var)
            print(f"Removed '{args.var}' from env whitelist.")
        elif args.env_action == "list":
            wl = config.state.env_whitelist
            if not wl:
                print("No env vars in whitelist.")
            else:
                for v in wl:
                    print(f"  {v}")
    elif args.config_type == "pool":
        if args.pool_name is not None:
            config.set_storage_pool(args.pool_name)
            print(f"Storage pool set to '{args.pool_name}'.")
        else:
            pool = config.state.storage_pool
            if pool:
                print(f"Storage pool: {pool}")
            else:
                print("No storage pool configured (using LXD default).")
    elif args.config_type == "mounts":
        if args.mounts_action == "add":
            mode = "ro" if args.ro else "rw"
            config.add_auto_mount(args.path, mode)
            resolved = os.path.realpath(args.path)
            print(f"Added auto-mount: {resolved} ({mode})")
            if os.path.isfile(resolved):
                from ccbox.mount import _warn_file_mount

                _warn_file_mount(resolved)
        elif args.mounts_action == "remove":
            if config.remove_auto_mount(args.path):
                print(f"Removed auto-mount: {os.path.realpath(args.path)}")
            else:
                print(
                    f"Not found in auto-mounts: {os.path.realpath(args.path)}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        elif args.mounts_action == "list":
            mounts = config.state.get_auto_mounts()
            if not mounts:
                print("No auto-mounts configured.")
            else:
                for m in mounts:
                    if m.target and m.target != m.path:
                        print(f"  {m.path} -> {m.target} ({m.mode})")
                    else:
                        print(f"  {m.path} ({m.mode})")
        elif args.mounts_action == "reset":
            config._state.auto_mounts = None
            config.save()
            print("Auto-mounts reset to defaults.")


def cmd_sync_automount(config: Config, args: argparse.Namespace) -> None:
    """Sync current auto-mount config to running sandbox(es)."""
    if args.all:
        targets = list(config.state.sandboxes.keys())
        if not targets:
            print("No sandboxes.")
            return
    else:
        name = resolve_sandbox(config, args.sandbox)
        targets = [name]

    total_changes = 0
    for name in targets:
        entry = config.get_sandbox(name)
        if entry is None:
            continue
        # Ensure container is running
        try:
            ensure_running(config, name)
        except Exception as e:
            print(f"Sandbox '{name}': skipping ({e})", file=sys.stderr)
            continue

        changes = sync_auto_mounts(config, name, dry_run=args.dry_run)
        if changes:
            print(f"Sandbox '{name}':")
            for line in changes:
                print(line)
            total_changes += len(changes)
        elif not args.all:
            print(f"Sandbox '{name}': already in sync.")

    if total_changes:
        suffix = " (dry run)" if args.dry_run else ""
        print(f"\n{total_changes} change(s){suffix}.")
    elif args.all:
        print("All sandboxes in sync.")


def cmd_cp(config: Config, args: argparse.Namespace) -> None:
    """Copy a file or directory from a sandbox to the host, then mount it rw."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)

    # Resolve src to absolute path
    src = args.src if os.path.isabs(args.src) else os.path.join(os.getcwd(), args.src)
    src = os.path.normpath(src)

    # Default dest = src (identity-mapped paths)
    dest = args.dest if args.dest else src
    dest = dest if os.path.isabs(dest) else os.path.join(os.getcwd(), dest)
    dest = os.path.normpath(dest)

    # Validate source exists in container
    if not lxd.path_exists(container, src):
        print(f"Error: '{src}' does not exist in container.", file=sys.stderr)
        raise SystemExit(1)

    # Refuse to overwrite existing host path
    if os.path.exists(dest):
        print(f"Error: '{dest}' already exists on host.", file=sys.stderr)
        raise SystemExit(1)

    is_dir = lxd.is_directory(container, src)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)

    if is_dir:
        # lxc file pull -r puts basename/ inside the target dir
        lxd.pull_path(container, src, parent, recursive=True)
        # If src basename differs from dest basename, rename
        pulled = os.path.join(parent, os.path.basename(src))
        if pulled != dest:
            os.rename(pulled, dest)
        add_mount(config, sandbox_name, dest)
        print(f"Copied directory {src} → {dest} (mounted rw)")
    else:
        lxd.pull_path(container, src, dest)
        add_mount(config, sandbox_name, dest)
        print(f"Copied {src} → {dest} (mounted rw)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccbox",
        description="Run Claude Code in isolated LXD containers.",
    )
    sub = parser.add_subparsers(dest="command")

    # ccbox claude [-s SANDBOX] [-- args...]
    p_claude = sub.add_parser("claude", help="New session running claude with given args")
    p_claude.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )
    p_claude.add_argument(
        "claude_args",
        nargs=argparse.REMAINDER,
        help="Arguments to pass to claude (use -- to separate)",
    )

    # ccbox codex [-s SANDBOX] [-- args...]
    p_codex = sub.add_parser("codex", help="New session running codex --yolo with given args")
    p_codex.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )
    p_codex.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments to pass to codex (use -- to separate)",
    )

    # ccbox ls
    sub.add_parser("ls", help="List sandboxes")

    # ccbox create <name> [--bare]
    p_create = sub.add_parser("create", help="Create a sandbox")
    p_create.add_argument("name", help="Sandbox name")
    p_create.add_argument(
        "--bare",
        action="store_true",
        help="Don't mount current directory (use ccbox mount -s NAME later)",
    )

    # ccbox mount <path> [-s SANDBOX] [--ro]
    p_mount = sub.add_parser("mount", help="Add a mount")
    p_mount.add_argument("path", help="Host directory to mount")
    p_mount.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )
    p_mount.add_argument("--ro", action="store_true", help="Read-only mount")

    # ccbox unmount <path> [-s SANDBOX]
    p_unmount = sub.add_parser("unmount", help="Remove a mount")
    p_unmount.add_argument("path", help="Mount path to remove")
    p_unmount.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox sessions [-s SANDBOX] [-a]
    p_sessions = sub.add_parser("sessions", help="List sessions")
    p_sessions.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )
    p_sessions.add_argument(
        "-a", "--all", action="store_true", help="List sessions across all sandboxes"
    )

    # ccbox attach [session] [-s SANDBOX] [-a]
    p_attach = sub.add_parser("attach", help="Attach to a session")
    p_attach.add_argument(
        "session", nargs="?", default=None, help="Session name (picker if omitted)"
    )
    p_attach.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )
    p_attach.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Pick from sessions across all sandboxes",
    )

    # ccbox kill [session] [--all] [-s SANDBOX]
    p_kill = sub.add_parser("kill", help="Kill session(s)")
    p_kill.add_argument(
        "session",
        nargs="?",
        default=None,
        help="Session name (picker if omitted and not --all)",
    )
    p_kill.add_argument("--all", action="store_true", help="Kill all sessions")
    p_kill.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox stop [-s SANDBOX]
    p_stop = sub.add_parser("stop", help="Stop a sandbox")
    p_stop.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox rm [-s SANDBOX]
    p_rm = sub.add_parser("rm", help="Remove a sandbox")
    p_rm.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox status [-s SANDBOX]
    p_status = sub.add_parser("status", help="Show sandbox details")
    p_status.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox shell [-s SANDBOX]
    p_shell = sub.add_parser("shell", help="Bash shell in sandbox")
    p_shell.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox port {forward,expose,ls,rm}
    p_port = sub.add_parser("port", help="Port forwarding")
    port_sub = p_port.add_subparsers(dest="port_action")

    # ccbox port forward <container_port> [addr:]<host_port> [--udp] [-s SANDBOX]
    p_fwd = port_sub.add_parser("forward", help="Container→Host forwarding")
    p_fwd.add_argument("container_port", type=int, help="Port inside container")
    p_fwd.add_argument("target", help="[addr:]port on host side")
    p_fwd.add_argument("--udp", action="store_true", help="Use UDP instead of TCP")
    p_fwd.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox port expose <container_port> [[addr:]<bind_port>] [--udp] [-s SANDBOX]
    p_exp = port_sub.add_parser("expose", help="Host→Container forwarding")
    p_exp.add_argument("container_port", type=int, help="Port inside container")
    p_exp.add_argument(
        "bind",
        nargs="?",
        default=None,
        help="[addr:]port to bind on host (default: localhost:container_port)",
    )
    p_exp.add_argument("--udp", action="store_true", help="Use UDP instead of TCP")
    p_exp.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox port ls [-s SANDBOX]
    p_port_ls = port_sub.add_parser("ls", help="List port forwards")
    p_port_ls.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox port rm <name> [-s SANDBOX]
    p_port_rm = port_sub.add_parser("rm", help="Remove a port forward")
    p_port_rm.add_argument("name", help="Device name (from port ls)")
    p_port_rm.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox sync-automount [-s SANDBOX] [--all] [--dry-run]
    p_sync = sub.add_parser("sync-automount", help="Sync auto-mount config to running sandbox(es)")
    p_sync.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )
    p_sync.add_argument("--all", action="store_true", help="Sync all sandboxes")
    p_sync.add_argument(
        "--dry-run", "-n", action="store_true", help="Show changes without applying"
    )

    # ccbox cp <src> [dest] [-s/--sandbox NAME]
    p_cp = sub.add_parser("cp", help="Copy file/dir from sandbox to host")
    p_cp.add_argument("src", help="Path inside the container")
    p_cp.add_argument(
        "dest", nargs="?", default=None, help="Destination on host (default: same path)"
    )
    p_cp.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox resolve [-s SANDBOX]
    p_resolve = sub.add_parser("resolve", help="Show which sandbox resolves for current directory")
    p_resolve.add_argument(
        "-s",
        "--sandbox",
        default=None,
        metavar="SANDBOX",
        help="Sandbox name (default: auto from CWD)",
    )

    # ccbox config env add/remove/list
    p_config = sub.add_parser("config", help="Configuration management")
    config_sub = p_config.add_subparsers(dest="config_type")
    p_env = config_sub.add_parser("env", help="Manage env whitelist")
    env_sub = p_env.add_subparsers(dest="env_action")
    p_env_add = env_sub.add_parser("add", help="Add env var to whitelist")
    p_env_add.add_argument("var", help="Environment variable name")
    p_env_remove = env_sub.add_parser("remove", help="Remove env var from whitelist")
    p_env_remove.add_argument("var", help="Environment variable name")
    env_sub.add_parser("list", help="List whitelisted env vars")

    # ccbox config pool [name]
    p_pool = config_sub.add_parser("pool", help="Get/set LXD storage pool")
    p_pool.add_argument(
        "pool_name", nargs="?", default=None, help="Pool name (omit to show current)"
    )

    # ccbox config mounts add/remove/list/reset
    p_mounts = config_sub.add_parser(
        "mounts", help="Manage auto-mounts (applied to every new sandbox)"
    )
    mounts_sub = p_mounts.add_subparsers(dest="mounts_action")
    p_mounts_add = mounts_sub.add_parser("add", help="Add auto-mount")
    p_mounts_add.add_argument("path", help="Host path (file or directory)")
    p_mounts_add.add_argument("--ro", action="store_true", help="Read-only (default: rw)")
    p_mounts_rm = mounts_sub.add_parser("remove", help="Remove auto-mount")
    p_mounts_rm.add_argument("path", help="Path to remove")
    mounts_sub.add_parser("list", help="List auto-mounts")
    mounts_sub.add_parser("reset", help="Reset to defaults")

    # ccbox _session-link  (internal: Claude hook)
    sub.add_parser("_session-link", help=argparse.SUPPRESS)

    # ccbox _session-cleanup  (internal: called by pane shell on natural exit)
    sub.add_parser("_session-cleanup", help=argparse.SUPPRESS)

    return parser


COMMAND_MAP = {
    None: cmd_default,
    "claude": cmd_claude,
    "codex": cmd_codex,
    "ls": cmd_ls,
    "create": cmd_create,
    "mount": cmd_mount,
    "unmount": cmd_unmount,
    "sessions": cmd_sessions,
    "attach": cmd_attach,
    "kill": cmd_kill,
    "stop": cmd_stop,
    "rm": cmd_rm,
    "status": cmd_status,
    "shell": cmd_shell,
    "port": cmd_port,
    "sync-automount": cmd_sync_automount,
    "cp": cmd_cp,
    "resolve": cmd_resolve,
    "config": cmd_config,
    "_session-link": cmd_session_link,
    "_session-cleanup": cmd_session_cleanup,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    check_lxd_group()

    config = Config()

    handler = COMMAND_MAP.get(args.command)
    if handler is None:
        parser.print_help()
        raise SystemExit(1)

    # Special handling for config subcommands
    if args.command == "config":
        if not hasattr(args, "config_type") or args.config_type is None:
            parser.parse_args(["config", "--help"])
            raise SystemExit(1)
        if args.config_type == "env":
            if not hasattr(args, "env_action") or args.env_action is None:
                parser.parse_args(["config", "env", "--help"])
                raise SystemExit(1)
        if args.config_type == "mounts":
            if not hasattr(args, "mounts_action") or args.mounts_action is None:
                parser.parse_args(["config", "mounts", "--help"])
                raise SystemExit(1)

    if args.command == "port":
        if not hasattr(args, "port_action") or args.port_action is None:
            parser.parse_args(["port", "--help"])
            raise SystemExit(1)

    try:
        handler(config, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print()
        raise SystemExit(130) from None
