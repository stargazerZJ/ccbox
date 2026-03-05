"""CLI entry point and subcommand routing."""

from __future__ import annotations

import argparse
import os
import sys

from ccbox.config import Config
from ccbox.init import run_init
from ccbox.mount import add_mount, remove_mount, sync_auto_mounts
from ccbox.sandbox import (
    auto_sandbox_name_from_cwd,
    create_sandbox,
    ensure_running,
    list_sandboxes,
    remove_sandbox,
    resolve_sandbox,
    sandbox_status,
    stop_sandbox,
)
from ccbox.port import (
    add_expose,
    add_forward,
    list_ports,
    remove_port,
    _parse_addr_port,
)
from ccbox.session import (
    attach_session,
    build_claude_command,
    build_codex_command,
    create_session,
    detached_sessions,
    get_forwarded_env,
    kill_all_sessions,
    kill_session,
    list_sessions,
)
from ccbox import lxd


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


def cmd_default(config: Config, args: argparse.Namespace) -> None:
    """Default command: find/create sandbox for CWD, manage sessions."""
    cwd = os.getcwd()

    # Try to find existing sandbox for CWD
    sandbox_name = config.sandbox_for_path(cwd)

    if sandbox_name is None:
        # Auto-create sandbox
        sandbox_name = auto_sandbox_name_from_cwd()
        # Check for collision
        if config.get_sandbox(sandbox_name) is not None:
            n = 1
            while config.get_sandbox(f"{sandbox_name}-{n}") is not None:
                n += 1
            sandbox_name = f"{sandbox_name}-{n}"

        print(f"Creating sandbox '{sandbox_name}' for {cwd}...")
        create_sandbox(config, sandbox_name, mounts=[(cwd, False)])

    container = ensure_running(config, sandbox_name)
    env = get_forwarded_env(config.state.env_whitelist)

    # Check for detached sessions
    detached = detached_sessions(container)

    if len(detached) == 1:
        # Reattach to the single detached session
        print(f"Reattaching to session '{detached[0]['name']}'...")
        attach_session(container, detached[0]["name"])
    elif len(detached) > 1:
        # Show picker
        print("Detached sessions:")
        for i, s in enumerate(detached):
            print(f"  [{i}] {s['name']}")
        print(f"  [n] New session")
        choice = input("Select: ").strip()
        if choice == "n":
            cmd = build_claude_command()
            name = create_session(container, cmd, cwd=cwd, env=env)
            attach_session(container, name)
        else:
            try:
                idx = int(choice)
                attach_session(container, detached[idx]["name"])
            except (ValueError, IndexError):
                print("Invalid selection.", file=sys.stderr)
                raise SystemExit(1)
    else:
        # No detached sessions — create new one
        cmd = build_claude_command()
        name = create_session(container, cmd, cwd=cwd, env=env)
        attach_session(container, name)


def cmd_claude(config: Config, args: argparse.Namespace) -> None:
    """Always create a new session running claude with given args."""
    cwd = os.getcwd()
    sandbox_name = config.sandbox_for_path(cwd)

    if sandbox_name is None:
        sandbox_name = auto_sandbox_name_from_cwd()
        if config.get_sandbox(sandbox_name) is not None:
            n = 1
            while config.get_sandbox(f"{sandbox_name}-{n}") is not None:
                n += 1
            sandbox_name = f"{sandbox_name}-{n}"
        print(f"Creating sandbox '{sandbox_name}' for {cwd}...")
        create_sandbox(config, sandbox_name, mounts=[(cwd, False)])

    container = ensure_running(config, sandbox_name)
    env = get_forwarded_env(config.state.env_whitelist)

    cmd = build_claude_command(args.claude_args)
    name = create_session(container, cmd, cwd=cwd, env=env)
    attach_session(container, name)


def cmd_codex(config: Config, args: argparse.Namespace) -> None:
    """Always create a new session running codex --yolo with given args."""
    cwd = os.getcwd()
    sandbox_name = config.sandbox_for_path(cwd)

    if sandbox_name is None:
        sandbox_name = auto_sandbox_name_from_cwd()
        if config.get_sandbox(sandbox_name) is not None:
            n = 1
            while config.get_sandbox(f"{sandbox_name}-{n}") is not None:
                n += 1
            sandbox_name = f"{sandbox_name}-{n}"
        print(f"Creating sandbox '{sandbox_name}' for {cwd}...")
        create_sandbox(config, sandbox_name, mounts=[(cwd, False)])

    container = ensure_running(config, sandbox_name)
    env = get_forwarded_env(config.state.env_whitelist)

    cmd = build_codex_command(args.codex_args)
    name = create_session(container, cmd, cwd=cwd, env=env)
    attach_session(container, name)


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
    create_sandbox(config, args.name)
    print(f"Sandbox '{args.name}' created.")


def cmd_mount(config: Config, args: argparse.Namespace) -> None:
    """Add a mount to a sandbox."""
    add_mount(config, args.sandbox, args.path, readonly=args.ro)
    mode = "ro" if args.ro else "rw"
    print(f"Mounted '{args.path}' ({mode}) in sandbox '{args.sandbox}'.")


def cmd_unmount(config: Config, args: argparse.Namespace) -> None:
    """Remove a mount from a sandbox."""
    remove_mount(config, args.sandbox, args.path)
    print(f"Unmounted '{args.path}' from sandbox '{args.sandbox}'.")


def cmd_sessions(config: Config, args: argparse.Namespace) -> None:
    """List sessions in a sandbox."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)
    sessions = list_sessions(container)

    if not sessions:
        print(f"No sessions in sandbox '{sandbox_name}'.")
        return

    print(f"Sessions in sandbox '{sandbox_name}':")
    for s in sessions:
        status = "attached" if s["attached"] else "detached"
        print(f"  {s['name']:<12} {status}")


def cmd_attach(config: Config, args: argparse.Namespace) -> None:
    """Attach to a session."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)
    attach_session(container, args.session)


def cmd_kill(config: Config, args: argparse.Namespace) -> None:
    """Kill session(s)."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)

    if args.all:
        kill_all_sessions(container)
        print(f"All sessions killed in sandbox '{sandbox_name}'.")
    else:
        if args.session is None:
            print("Specify a session name or use --all.", file=sys.stderr)
            raise SystemExit(1)
        kill_session(container, args.session)
        print(f"Session '{args.session}' killed.")


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
    # su -l gives a full login env (PAM, /etc/environment, profiles).
    # -w preserves CCBOX_CWD through the login env reset; .bashrc cd's to it.
    lxd.exec_interactive(
        container,
        ["su", "-l", username, "-w", "CCBOX_CWD"],
        env={"CCBOX_CWD": cwd},
    )


def cmd_port(config: Config, args: argparse.Namespace) -> None:
    """Dispatch port subcommands."""
    sandbox_name = resolve_sandbox(config, args.sandbox)
    container = ensure_running(config, sandbox_name)

    if args.port_action == "forward":
        host_addr, host_port = _parse_addr_port(args.target)
        name = add_forward(
            container, args.container_port, host_addr, host_port, udp=args.udp,
        )
        proto = "udp" if args.udp else "tcp"
        print(f"Forward ({proto}): container:{args.container_port} → {host_addr}:{host_port}  [{name}]")

    elif args.port_action == "expose":
        bind_addr, bind_port = _parse_addr_port(args.bind, default_addr="127.0.0.1") if args.bind else ("127.0.0.1", None)
        name = add_expose(
            container, args.container_port, bind_addr, bind_port, udp=args.udp,
        )
        proto = "udp" if args.udp else "tcp"
        effective_port = bind_port if bind_port is not None else args.container_port
        print(f"Expose ({proto}): {bind_addr}:{effective_port} → container:{args.container_port}  [{name}]")

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
                print(f"Not found in auto-mounts: {os.path.realpath(args.path)}", file=sys.stderr)
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


def cmd_init(config: Config, args: argparse.Namespace) -> None:
    """Create the base image."""
    # --storage flag overrides and persists the pool setting
    storage = args.storage or config.state.storage_pool
    if args.storage:
        config.set_storage_pool(args.storage)
    run_init(force=args.force, storage_pool=storage)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccbox",
        description="Run Claude Code in isolated LXD containers.",
    )
    sub = parser.add_subparsers(dest="command")

    # ccbox claude [args...]
    p_claude = sub.add_parser("claude", help="New session running claude with given args")
    p_claude.add_argument("claude_args", nargs=argparse.REMAINDER, help="Arguments to pass to claude")

    # ccbox codex [-- args...]
    p_codex = sub.add_parser("codex", help="New session running codex --yolo with given args")
    p_codex.add_argument("codex_args", nargs=argparse.REMAINDER, help="Arguments to pass to codex")

    # ccbox ls
    sub.add_parser("ls", help="List sandboxes")

    # ccbox create <name>
    p_create = sub.add_parser("create", help="Create a sandbox")
    p_create.add_argument("name", help="Sandbox name")

    # ccbox mount <sandbox> <path> [--ro]
    p_mount = sub.add_parser("mount", help="Add a mount")
    p_mount.add_argument("sandbox", help="Sandbox name")
    p_mount.add_argument("path", help="Host directory to mount")
    p_mount.add_argument("--ro", action="store_true", help="Read-only mount")

    # ccbox unmount <sandbox> <path>
    p_unmount = sub.add_parser("unmount", help="Remove a mount")
    p_unmount.add_argument("sandbox", help="Sandbox name")
    p_unmount.add_argument("path", help="Mount path to remove")

    # ccbox sessions [sandbox]
    p_sessions = sub.add_parser("sessions", help="List sessions")
    p_sessions.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox attach <session> [sandbox]
    p_attach = sub.add_parser("attach", help="Attach to a session")
    p_attach.add_argument("session", help="Session name")
    p_attach.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox kill [session] [--all] [sandbox]
    p_kill = sub.add_parser("kill", help="Kill session(s)")
    p_kill.add_argument("session", nargs="?", default=None, help="Session name")
    p_kill.add_argument("--all", action="store_true", help="Kill all sessions")
    p_kill.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox stop [sandbox]
    p_stop = sub.add_parser("stop", help="Stop a sandbox")
    p_stop.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox rm [sandbox]
    p_rm = sub.add_parser("rm", help="Remove a sandbox")
    p_rm.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox status [sandbox]
    p_status = sub.add_parser("status", help="Show sandbox details")
    p_status.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox shell [sandbox]
    p_shell = sub.add_parser("shell", help="Bash shell in sandbox")
    p_shell.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox port {forward,expose,ls,rm}
    p_port = sub.add_parser("port", help="Port forwarding")
    port_sub = p_port.add_subparsers(dest="port_action")

    # ccbox port forward <container_port> [addr:]<host_port> [--udp] [sandbox]
    p_fwd = port_sub.add_parser("forward", help="Container→Host forwarding")
    p_fwd.add_argument("container_port", type=int, help="Port inside container")
    p_fwd.add_argument("target", help="[addr:]port on host side")
    p_fwd.add_argument("--udp", action="store_true", help="Use UDP instead of TCP")
    p_fwd.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox port expose <container_port> [[addr:]<bind_port>] [--udp] [sandbox]
    p_exp = port_sub.add_parser("expose", help="Host→Container forwarding")
    p_exp.add_argument("container_port", type=int, help="Port inside container")
    p_exp.add_argument("bind", nargs="?", default=None, help="[addr:]port to bind on host (default: localhost:container_port)")
    p_exp.add_argument("--udp", action="store_true", help="Use UDP instead of TCP")
    p_exp.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox port ls [sandbox]
    p_port_ls = port_sub.add_parser("ls", help="List port forwards")
    p_port_ls.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox port rm <name> [sandbox]
    p_port_rm = port_sub.add_parser("rm", help="Remove a port forward")
    p_port_rm.add_argument("name", help="Device name (from port ls)")
    p_port_rm.add_argument("sandbox", nargs="?", default=None, help="Sandbox name")

    # ccbox sync-automount [sandbox] [--all] [--dry-run]
    p_sync = sub.add_parser("sync-automount", help="Sync auto-mount config to running sandbox(es)")
    p_sync.add_argument("sandbox", nargs="?", default=None, help="Sandbox name (default: auto from CWD)")
    p_sync.add_argument("--all", action="store_true", help="Sync all sandboxes")
    p_sync.add_argument("--dry-run", "-n", action="store_true", help="Show changes without applying")

    # ccbox cp <src> [dest] [--sandbox NAME]
    p_cp = sub.add_parser("cp", help="Copy file/dir from sandbox to host")
    p_cp.add_argument("src", help="Path inside the container")
    p_cp.add_argument("dest", nargs="?", default=None, help="Destination on host (default: same path)")
    p_cp.add_argument("--sandbox", default=None, help="Sandbox name (default: auto from CWD)")

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
    p_pool.add_argument("pool_name", nargs="?", default=None, help="Pool name (omit to show current)")

    # ccbox config mounts add/remove/list/reset
    p_mounts = config_sub.add_parser("mounts", help="Manage auto-mounts (applied to every new sandbox)")
    mounts_sub = p_mounts.add_subparsers(dest="mounts_action")
    p_mounts_add = mounts_sub.add_parser("add", help="Add auto-mount")
    p_mounts_add.add_argument("path", help="Host path (file or directory)")
    p_mounts_add.add_argument("--ro", action="store_true", help="Read-only (default: rw)")
    p_mounts_rm = mounts_sub.add_parser("remove", help="Remove auto-mount")
    p_mounts_rm.add_argument("path", help="Path to remove")
    mounts_sub.add_parser("list", help="List auto-mounts")
    mounts_sub.add_parser("reset", help="Reset to defaults")

    # ccbox init [--force] [--storage POOL]
    p_init = sub.add_parser("init", help="Create base image")
    p_init.add_argument("--force", action="store_true", help="Rebuild existing base image")
    p_init.add_argument("--storage", "-s", metavar="POOL", help="LXD storage pool to use (saved for future sandboxes)")

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
    "config": cmd_config,
    "init": cmd_init,
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
        raise SystemExit(1)
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
