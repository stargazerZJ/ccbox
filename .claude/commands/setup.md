You are setting up **ccbox** from scratch on this machine. Detect what's missing, resolve it, build the patched uv, create the base image. Only ask the user when there's a genuine choice (e.g. apt mirrors, extra packages).

Run commands directly — do not ask for confirmation at each step.

## Context

- LXC binary: `/snap/bin/lxc`
- Base OS: `ubuntu:24.04`
- Temp container: `ccbox-init-temp`
- Image alias: `ccbox-base`
- ccbox project root: the repo containing this file (find it via `git rev-parse --show-toplevel` or look for `patches/uv-hardlink-socket.patch`)

## Phase 0: Prerequisites

Check and resolve each prerequisite. Fix problems directly — don't just report them.

### LXD / snap
Run `/snap/bin/lxc version`. If it fails:
```
sudo snap install lxd
sudo lxd init --auto
```

### lxd group
Check `id` output for `lxd` group. If missing:
```
sudo usermod -aG lxd $USER
newgrp lxd
```
Then re-check `/snap/bin/lxc version`.

### Storage pool
Check `~/.config/ccbox/state.json` field `"storage_pool"`. If set, use it.
Otherwise run `lxc storage list` — if exactly one pool exists, use it. If multiple, **ask the user** which one. If none, create one:
```
lxc storage create default zfs
```
Save the chosen pool to `~/.config/ccbox/state.json` via `ccbox config pool <name>` (if ccbox is installed) or by writing the JSON directly.

### Rust toolchain (for building patched uv)
Check `cargo --version`. If missing:
```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
```

### ccbox itself
Check `which ccbox`. If not installed, install it from the project root:
```
pip install -e <project-root>
```
Use `uv pip install -e <project-root>` if uv is available on the host, otherwise plain pip.

## Phase 1: Build patched uv

Check `~/.config/ccbox/bin/uv` — if it exists and `file` shows it's an ELF binary, skip this phase.

Otherwise, build it:
```
mkdir -p ~/.config/ccbox/bin
cd /tmp
rm -rf uv-build
git clone --depth 1 --branch 0.10.7 https://github.com/astral-sh/uv.git uv-build
cd uv-build
git apply <project-root>/patches/uv-hardlink-socket.patch
cargo build -p uv --release
cp target/release/uv ~/.config/ccbox/bin/uv
cd /tmp && rm -rf uv-build
```

Verify: `~/.config/ccbox/bin/uv --version` should print uv 0.10.7.

## Phase 2: Detect host tools & configure auto-mounts

The goal: make sure the default auto-mounts in `src/ccbox/config.py` (`_default_auto_mounts()`) match what's actually on this machine. If they don't, either adjust the mounts via `ccbox config mounts` or fix the code.

### Claude Code
Check `which claude` or `ls ~/.local/bin/claude`. If claude isn't installed, warn the user but continue — they can install it later.

### Codex CLI
Codex may be installed via nvm (`~/.nvm/versions/node/*/bin/codex`), npm global, cargo, or not at all.

Run: `which codex 2>/dev/null` and check `~/.nvm/versions/node/*/bin/codex`.

- If found under `~/.nvm` — the default auto-mounts already handle this, no action needed.
- If found elsewhere (e.g. `/usr/local/bin/codex`, `~/.cargo/bin/codex`) — the `~/.nvm` ro mount is unnecessary and `build_codex_command()` in `src/ccbox/session.py` won't find it. Add the codex binary's parent directory as an auto-mount via `ccbox config mounts add <dir> --ro`.
- If not installed — no action needed, codex support is optional.

### uv (host)
Check `which uv` on the host. The host uv is used for `pip install -e .` etc. The *container* uv is the patched binary from Phase 1. These are separate.

## Phase 3: Create base image

### 1. Cleanup
If `ccbox-init-temp` already exists: `lxc delete ccbox-init-temp --force`

### 2. Launch
```
lxc launch ubuntu:24.04 ccbox-init-temp [-s <pool>]
```
Wait: `lxc exec ccbox-init-temp -- cloud-init status --wait`

### 3. UID mapping
```
lxc config set ccbox-init-temp raw.idmap "both 1000 1000"
```

### 4. Rename default user
Run `id` on the host to get the username. Ubuntu 24.04 ships user `ubuntu` at UID 1000. Rename to match:
```
lxc exec ccbox-init-temp -- pkill -u ubuntu || true
lxc exec ccbox-init-temp -- usermod -l <username> -d /home/<username> -m ubuntu
lxc exec ccbox-init-temp -- groupmod -n <username> ubuntu
```

### 5. Sudo
```
lxc exec ccbox-init-temp -- bash -c "echo '<username> ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/<username> && chmod 0440 /etc/sudoers.d/<username>"
```

### 6. Mount-point stubs + profile source
```
lxc exec ccbox-init-temp -- su -l <username> -c "mkdir -p ~/.local/bin ~/.local/share/claude ~/.cache/uv ~/.claude ~/.config/ccbox"
```
Append to `.bashrc`:
```
[ -f ~/.config/ccbox/profile.sh ] && . ~/.config/ccbox/profile.sh
```

### 7. Push tmux.conf
```
lxc file push <project-root>/assets/tmux.conf ccbox-init-temp/etc/tmux.conf --mode 0644
```

### 8. Restart to apply idmap
```
lxc stop ccbox-init-temp && lxc start ccbox-init-temp
```

### 9. Install packages
**Ask the user** what packages to install and any pre-install instructions (e.g. apt mirror changes). Then run via `lxc exec ccbox-init-temp -- ...`.

At minimum install: `tmux git curl build-essential python3 python3-venv sudo locales`
Also: `lxc exec ccbox-init-temp -- locale-gen en_US.UTF-8`

### 10. Mount host tools + test
Deploy the profile script: ensure `~/.config/ccbox/profile.sh` exists (copy from `<project-root>/assets/ccbox-profile.sh`).

Read auto-mounts from `~/.config/ccbox/state.json` field `"auto_mounts"`. If not set, use the defaults from `src/ccbox/config.py` `_default_auto_mounts()`.

For each mount: if it has a `"target"` field, use `source=<path> path=<target>`. Otherwise identity-map.
```
lxc config device add ccbox-init-temp <device-name> disk source=<path> path=<target> [readonly=true]
```
Device name: sanitize the **target** path — strip leading `/`, replace `/` with `-`, prefix with `mount-`.

Skip mounts whose source path doesn't exist on the host (e.g. `~/.nvm` if nvm isn't installed). Print a note for each skipped mount.

Restart and test:
```
lxc stop ccbox-init-temp && lxc start ccbox-init-temp
lxc exec ccbox-init-temp -- su -l <username> -c "claude --version"
```

### 11. Interactive loop

**"The container is ready. What would you like to do?"**
1. **Install/configure something** — tell me what and I'll run it via `lxc exec`
2. **Shell** — run `lxc exec ccbox-init-temp -- su -l <username>` in another terminal
3. **Done** — publish the image

Repeat until the user says done.

### 12. Publish
```
lxc stop ccbox-init-temp
```
Remove temporary disk mounts:
```
lxc config device list ccbox-init-temp
```
Remove every device starting with `mount-`:
```
lxc config device remove ccbox-init-temp <device-name>
```
Publish:
```
lxc publish ccbox-init-temp --alias=ccbox-base [--reuse if rebuilding]
lxc delete ccbox-init-temp
```

Print: **"ccbox is ready. Run `ccbox` in any project directory to start a sandboxed session."**

## Phase 4: Hook configuration

Set up session-link hooks so the session picker shows rich info (last prompt, time, branch).

### Claude Code
Add to `~/.claude/settings.json` (create the `hooks` key if it doesn't exist):
```json
"hooks": {
  "SessionStart": [{ "hooks": [{ "type": "command", "command": "ccbox _session-link" }] }]
}
```

### Codex CLI
Add to `~/.codex/hooks.json` (create the file if it doesn't exist):
```json
{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"ccbox _session-link"}]}]}}
```

Enable hooks in `~/.codex/config.toml`:
```toml
[features]
codex_hooks = true
```

## Important
- If anything fails, show the error and try to fix it. Only ask the user if you can't resolve it yourself.
- The `--reuse` flag on publish is needed if `ccbox-base` already exists.
- The patched uv build takes a while (~5-15 min depending on hardware). Let the user know.
- If the user provides extra instructions (apt mirrors, packages, config), incorporate them at the appropriate step.
- If ccbox is installed as an editable package (`uv tool install -e .` or `pip install -e .`), the source directory must be added as a read-only auto-mount so it's accessible inside sandboxes: `ccbox config mounts add <source-dir> --ro`.
