# ccbox

Sandboxed Claude Code sessions in LXD containers. Run `claude --dangerously-skip-permissions` safely by isolating it inside a container with identity-mapped mounts.

## How it works

```
ccbox              # auto-create sandbox for CWD, launch Claude Code in tmux
ccbox claude       # same, explicit
Ctrl+Q             # detach from session (reattach on next ccbox run)
```

ccbox creates an LXD container, bind-mounts your project directory (rw) and tooling paths, then drops you into a tmux session running Claude Code with `--dangerously-skip-permissions`.

It also sets `CLAUDE_CONFIG_DIR=~/.claude` inside the container, so Claude writes mutable config under the mounted `~/.claude` directory instead of `~/.claude.json`.

### uv hardlink deferral

Python package managers like uv use hardlinks from a shared cache to `.venv` for fast installs. Inside a container, the cache and project live on different mount points, so hardlinks fail.

ccbox solves this with a patched uv binary:
- uv runs natively inside the container
- When creating hardlinks, it checks `UV_HARDLINK_SOCKET` env var
- If set, it sends `{"src":"...","dst":"..."}` to a host-side Unix socket server
- The host server performs the hardlink on the real filesystem and responds

This replaces the old approach of proxying entire uv commands to the host.

## Prerequisites

- LXD (with your user in the `lxd` group)
- ZFS storage pool (default: `home-zfs`)
- A base image published as `ccbox-base` (run `/setup` inside a sandbox to create one)
- Python 3.12+

## Install

```bash
pip install -e .
```

## Project structure

```
src/ccbox/
  cli.py          # CLI entry point and subcommand routing
  config.py       # State file (~/.config/ccbox/state.json), mount definitions
  sandbox.py      # Sandbox lifecycle (create, start, stop, remove)
  session.py      # Tmux session management, env forwarding
  lxd.py          # LXD command wrappers (lxc exec, config, etc.)
  mount.py        # Bind mount management (add, remove, auto-mounts)
  init.py         # First-run initialization
  uv_server.py    # Host-side hardlink server (Unix socket)

assets/
  tmux.conf       # Tmux config (Ctrl+Q detach, no status bar)
  uv-shim         # Legacy uv shim (replaced by patched binary)
  bin/uv-patched  # Patched uv release binary (gitignored)

patches/
  uv-hardlink-socket.patch  # Patch against uv v0.10.7
```

## Auto-mounts

These host paths are bind-mounted into every sandbox:

| Path | Mode | Purpose |
|------|------|---------|
| `~/.claude` | rw | Claude config and project memory |
| `~/.local/bin` | ro | Claude launcher symlink and helper binaries |
| `~/.local/share/claude` | ro | Claude data |
| `~/.local/share/claude/versions` | rw | Claude version management |
| `~/.cache/uv` | rw | uv package cache |
| `~/.local/share/uv` | rw | Managed Python installations |
| `~/.config/uv` | ro | uv settings |
| `~/.config/ccbox/bin/uv` → `~/.local/bin/uv` | ro | Patched uv binary |
| `~/.config/ccbox/run` | rw | Unix socket for hardlink server |

## Building the patched uv

```bash
git clone --depth 1 --branch 0.10.7 https://github.com/astral-sh/uv.git uv-src
cd uv-src
git apply ../patches/uv-hardlink-socket.patch
cargo build -p uv --release
cp target/release/uv ~/.config/ccbox/bin/uv
```

## Configuration

State lives in `~/.config/ccbox/state.json`:

```bash
ccbox config env add ANTHROPIC_API_KEY   # forward env var into containers
ccbox config env remove ANTHROPIC_API_KEY
ccbox config pool set home-zfs           # set ZFS storage pool
ccbox config mount add ~/.ssh ro         # add auto-mount
ccbox config mount remove ~/.ssh
```

## Commands

```bash
ccbox                    # auto-sandbox for CWD + Claude Code session
ccbox claude [args]      # explicit Claude Code with extra args
ccbox list               # list sandboxes
ccbox status [name]      # show sandbox status
ccbox stop [name]        # stop sandbox container
ccbox rm [name]          # remove sandbox and container
ccbox sessions [name]    # list tmux sessions
ccbox kill [name] [ses]  # kill a session
ccbox uv-server start    # start hardlink server
ccbox uv-server stop     # stop hardlink server
```
