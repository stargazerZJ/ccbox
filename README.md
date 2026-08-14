# ccbox

Sandboxed Claude Code and Codex CLI sessions in LXD containers. Run `claude --dangerously-skip-permissions` or `codex --yolo` safely by isolating them inside a container with identity-mapped mounts.

## Why ccbox?

Most AI sandboxes treat your environment like a disposable wrapper: they mess up your file permissions, erase your installed tools when they restart, and drop your Claude session if you close your laptop.

**`ccbox` is built differently.** We use LXD system containers to give Claude a sandbox that actually feels like a permanent, well-equipped remote workspace.

Here is what that solves:

- **🚫 No more `sudo chown` permission hell:**
  *The Pain:* Docker sandboxes run as root or random UIDs, leaving your generated files with locked permissions so you can't edit or commit them on your host.
  *The Fix:* `ccbox` maps your host User ID to the container 1:1. Files created by Claude are owned by *you*. Git, your IDE, and your host tools just work seamlessly.

- **🧠 You never lose context when you close the terminal:**
  *The Pain:* Close a Docker shell, and your Claude session is gone.
  *The Fix:* `ccbox` wraps every session in `tmux` automatically. Close your laptop, hit `Ctrl+Q` to detach, and come back later—run `ccbox` again and you're instantly reattached to Claude, mid-thought. Plus, it shares the `~/.claude` state dynamically with your host system.

- **⚡ Blazing fast Python dependency caching (across mounts):**
  *The Pain:* Insanely fast package managers like `uv` rely on filesystem hardlinks. In normal containers, your caching breaks across volume boundaries, grinding package installs to a halt.
  *The Fix:* We ship a securely patched `uv` binary. It delegates hardlink creation to a host-side Unix socket, allowing lightning-fast, zero-copy package caching across the container boundary.

- **🛠️ A sandbox that feels like a real machine:**
  *The Pain:* Single-process Docker containers lack full init systems. Try asking Claude to start a background database service or run complex build tools, and it fails.
  *The Fix:* LXD provides a *full system container*. It acts perfectly like a persistent Ubuntu/Linux machine. Install Rust, build tools, or spin up systemd services. Thanks to ZFS backing, the whole OS state persists between sessions without having to "rebuild the image" every time.

- *(Admittedly, spinning up a brand new environment takes a few seconds. But subsequent sessions reuse the existing container and attach instantly).*

## How it works

```
ccbox              # auto-create sandbox for CWD, launch Claude Code in tmux
ccbox claude       # same, explicit
ccbox codex        # launch Codex CLI with --yolo in tmux
Ctrl+Q             # detach from session (reattach on next ccbox run)
```

ccbox creates an LXD container, bind-mounts your project directory (rw) and tooling paths, then drops you into a tmux session running Claude Code with `--dangerously-skip-permissions`.

It also sets `CLAUDE_CONFIG_DIR=~/.claude` inside the container, so Claude writes mutable config under the mounted `~/.claude` directory instead of `~/.claude.json`.

Live runtimes are discovered directly from a per-sandbox tmux socket. Claude's
`SessionStart` hook should run `ccbox _session-bind`; this binds the runtime to
the exact Claude session UUID and lets the picker display Claude's automatic title.

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
- UV (for Python package management)
- A base image published as `ccbox-base` (run `claude /setup` inside a sandbox to create one)
- Python 3.12+

## Install

```bash
uv pip install -e .
claude /setup # creates the base image
```

## Project structure

```
src/ccbox/
  cli.py          # CLI entry point and subcommand routing
  config.py       # State file (~/.config/ccbox/state.json), mount definitions
  sandbox.py      # Sandbox lifecycle (create, start, stop, remove)
  session.py      # Shared tmux sockets, runtime bindings, env forwarding
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
| `~/.cache/ccbox/run` | rw | uv/tmux sockets and Claude runtime bindings |
| `~/.nvm` | ro | Node.js runtime and Codex CLI binary |
| `~/.codex` | rw | Codex auth, config, state, and sessions |

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
ccbox codex [-- args]    # Codex CLI with --yolo (extra args passed through)
ccbox list               # list sandboxes
ccbox status [name]      # show sandbox status
ccbox stop [name]        # stop sandbox container
ccbox rm [name]          # remove sandbox and container
ccbox sessions [name]    # list tmux sessions
ccbox kill [name] [ses]  # kill a session
ccbox uv-server start    # start hardlink server
ccbox uv-server stop     # stop hardlink server
```
