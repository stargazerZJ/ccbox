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
Add host mounts so claude binary is available:
```
lxc config device add ccbox-init-temp mount-claude-bin disk source=$HOME/.local/bin path=$HOME/.local/bin readonly=true
lxc config device add ccbox-init-temp mount-claude-share disk source=$HOME/.local/share/claude path=$HOME/.local/share/claude readonly=true
lxc config device add ccbox-init-temp mount-claude-config disk source=$HOME/.claude path=$HOME/.claude readonly=true
lxc config device add ccbox-init-temp mount-uv-cache disk source=$HOME/.cache/uv path=$HOME/.cache/uv readonly=true
```
Restart and test:
```
lxc stop ccbox-init-temp && lxc start ccbox-init-temp
lxc exec ccbox-init-temp -- su -l <username> -c "claude --version"
```

### 11. User's turn
Tell the user: **"The container is ready. Run `lxc exec ccbox-init-temp -- su -l <username>` to drop into a shell and make any manual changes. Tell me when you're done and I'll publish the image."**

Wait for the user to confirm.

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
