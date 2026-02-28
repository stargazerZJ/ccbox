You are setting up a **ccbox base image** — an LXD container that will be published as `ccbox-base` and used as a template for all future sandboxes.

## Context

- LXC binary: `/snap/bin/lxc`
- Base OS: `ubuntu:24.04`
- Temp container name: `ccbox-init-temp`
- Published image alias: `ccbox-base`
- Host username and UID: run `id` to discover (typically `zj`, UID 1000)
- Storage pool: check `~/.config/ccbox/state.json` field `"storage_pool"` — if set, pass `-s <pool>` to `lxc launch`. If not set, ask the user which pool to use (`lxc storage list` to show options).

## Procedure

Follow these steps. Run commands directly — do not ask for confirmation at each step.

### 1. Cleanup
If `ccbox-init-temp` already exists, delete it: `lxc delete ccbox-init-temp --force`

### 2. Launch
```
lxc launch ubuntu:24.04 ccbox-init-temp [-s <pool>]
```
Wait for it: `lxc exec ccbox-init-temp -- cloud-init status --wait`

### 3. UID mapping
```
lxc config set ccbox-init-temp raw.idmap "both 1000 1000"
```

### 4. Rename default user
Ubuntu 24.04 ships user `ubuntu` at UID 1000. Rename to match the host user:
```
lxc exec ccbox-init-temp -- pkill -u ubuntu || true
lxc exec ccbox-init-temp -- usermod -l <username> -d /home/<username> -m ubuntu
lxc exec ccbox-init-temp -- groupmod -n <username> ubuntu
```

### 5. Sudo
```
lxc exec ccbox-init-temp -- bash -c "echo '<username> ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/<username> && chmod 0440 /etc/sudoers.d/<username>"
```

### 6. Mount-point stubs + PATH + stty
Create directories that will be used as mount points:
```
lxc exec ccbox-init-temp -- su -l <username> -c "mkdir -p ~/.local/bin ~/.local/share/claude ~/.cache/uv ~/.claude"
```
Append to `.bashrc`:
```
export PATH="$HOME/.local/bin:$PATH"
stty -ixon 2>/dev/null || true
```

### 7. Push tmux.conf
Push the file `assets/tmux.conf` from this repo into the container at `/etc/tmux.conf`:
```
lxc file push assets/tmux.conf ccbox-init-temp/etc/tmux.conf --mode 0644
```

### 8. Restart to apply idmap
```
lxc stop ccbox-init-temp
lxc start ccbox-init-temp
```

### 9. Install packages
**Now ask the user** what packages to install and any pre-install instructions (e.g. changing apt sources). Then run the install commands inside the container via `lxc exec ccbox-init-temp -- ...`.

At minimum, install: `tmux git curl build-essential python3 python3-venv sudo locales`

Also run: `lxc exec ccbox-init-temp -- locale-gen en_US.UTF-8`

### 10. Mount claude + test
Read auto-mounts from `~/.config/ccbox/state.json` field `"auto_mounts"`. If not set, use defaults:
- `~/.claude` (rw)
- `~/.local/bin/claude` (ro) — claude symlink/binary
- `~/.local/share/claude` (ro) — claude installation
- `~/.cache/uv` (rw) — shared uv cache
- `~/.config/ccbox/bin/uv` -> `~/.local/bin/uv` (ro) — uv shim (calls host uv via socket)
- `~/.config/ccbox/run` (rw) — contains the Unix socket for host↔container uv communication

The user may have added custom auto-mounts (e.g. `~/.vim`, `~/.oh-my-zsh`) via `ccbox config mounts add`. Read the config and add ALL of them.

For each mount, if it has a `"target"` field, use `source=<path> path=<target>`. Otherwise identity-map: `source=<path> path=<path>`.
```
lxc config device add ccbox-init-temp <device-name> disk source=<path> path=<target> [readonly=true]
```
Device name: sanitize the **target** path — replace `/` with `-`, prefix with `mount-`.

**Important**: Before adding mounts, ensure the uv shim exists by checking `~/.config/ccbox/bin/uv`. If it doesn't exist, copy it from `assets/uv-shim` in this project.

Restart and test:
```
lxc stop ccbox-init-temp && lxc start ccbox-init-temp
lxc exec ccbox-init-temp -- su -l <username> -c "claude --version"
```

### 11. Interactive loop

Present the user with these options:

**"The container is ready. What would you like to do?"**
1. **Install/configure something** — tell me what to do and I'll run it via `lxc exec`
2. **Shell** — drop into the container yourself: `lxc exec ccbox-init-temp -- su -l <username>`
3. **Done** — publish the image

Repeat this loop:
- If the user asks you to install or configure something, run the commands via `lxc exec ccbox-init-temp -- ...` and then ask again.
- If the user wants a shell, tell them to run `lxc exec ccbox-init-temp -- su -l <username>` in another terminal, and wait for them to say they're done. Then ask again.
- Only proceed to step 12 when the user says they're done / ok / publish.

### 12. Publish
```
lxc stop ccbox-init-temp
```
Remove temporary disk mounts before publishing:
```
lxc config device list ccbox-init-temp
```
Remove every device starting with `mount-`:
```
lxc config device remove ccbox-init-temp <device-name>
```
Then publish:
```
lxc publish ccbox-init-temp --alias=ccbox-base [--force if rebuilding]
lxc delete ccbox-init-temp
```

Print: **"Base image `ccbox-base` created. You can now use `ccbox` to create sandboxes."**

## Important
- If anything fails, show the error and ask the user how to proceed — do not silently continue.
- If the user provides extra instructions (apt mirrors, additional packages, config changes), incorporate them at the appropriate step.
- The `--force` flag on publish is needed if `ccbox-base` already exists.
