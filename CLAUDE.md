# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

ccbox wraps Claude Code and Codex CLI in LXD system containers for safe sandboxed execution. It uses identity-mapped mounts (host UID 1000 = container UID 1000), tmux for session persistence, and a patched uv binary for cross-mount hardlink deferral.

## Development setup

```bash
uv tool install -e .         # editable install as CLI tool
uv add <pkg>                 # add a dependency
uv tool install -e . --force # reinstall after deps change
ccbox                        # run (auto-creates sandbox for CWD)
```

## Linting

```bash
uvx ruff check src/          # lint
uvx ruff format src/         # format
uvx ruff check --fix src/    # autofix
```

Ruff config is in `pyproject.toml` (line-length 100, Python 3.12 target). Run both check and format before committing.

The project uses Python 3.12+ with `rich` and `textual` as dependencies. No test suite is configured.

## Architecture

**Entry point:** `ccbox` CLI → `src/ccbox/__main__.py` → `cli.main()`

**Module responsibilities:**

- `cli.py` — argparse-based subcommand routing; universal `-s/--sandbox` flag on every subcommand
- `config.py` — State file (`~/.config/ccbox/state.json`), dataclasses: `MountEntry`, `SandboxEntry`, `State`, `Config`
- `sandbox.py` — Container lifecycle (create/start/stop/remove), auto-mount sync, sandbox-to-CWD resolution
- `session.py` — Tmux session lifecycle, env var injection (`tmux set-environment`), command builders for Claude/Codex
- `lxd.py` — Thin wrappers around `lxc` CLI commands (exec, config, publish, etc.)
- `mount.py` — Bind mount management with inode tracking to detect replaced directories
- `picker.py` — Textual TUI for interactive sandbox/session selection (default when no args)
- `port.py` — Port forwarding via LXD proxy devices (TCP/UDP)
- `uv_server.py` — Host-side Unix socket server that performs hardlinks on behalf of the patched uv inside the container
- `transcript.py` — Reads Claude/Codex session transcripts (`.jsonl`) for session info display

**Key data flow:**
1. CLI resolves sandbox for CWD via `resolve_sandbox()` (walks parent dirs to find a matching mount)
2. `ensure_running()` starts the container if stopped
3. `create_session()` spawns a tmux session inside the container via `lxc exec`
4. Environment variables from the host whitelist are injected via `tmux set-environment`
5. The uv hardlink server runs on the host, listening on a Unix socket in `~/.cache/ccbox/run/`

**State:** All persistent state lives in `~/.config/ccbox/state.json` — sandbox definitions, mount entries, env whitelist, storage pool name, and auto-mount config.

## Key conventions

- Container names are prefixed `ccbox-` (e.g., sandbox "mybox" → container "ccbox-mybox")
- Sessions are named `sandbox/N` (e.g., `mybox/0`); the `attach` and `kill` commands accept this format
- Stopped sandboxes auto-start on any command that calls `ensure_running()`
- `lxc publish --reuse` (not `--force`) to replace existing image aliases
- Shell profile is externalized: `assets/ccbox-profile.sh` is mounted read-only and sourced by `.bashrc` in the container — edits take effect on next shell without image rebuild
- `IS_SANDBOX=1` and `CCBOX_SANDBOX=<name>` are set in every container session

## Assets

- `assets/tmux.conf` — Ctrl+Q detach, no prefix key, no status bar, mouse enabled
- `assets/ccbox-profile.sh` — Container shell init (PATH, nvm, sandbox env vars)
- `assets/update-base.sh` — Helper for `/update-base` skill (launch/publish subcommands)
- `patches/uv-hardlink-socket.patch` — Patch against uv v0.10.7 for hardlink deferral
