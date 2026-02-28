You are updating the **ccbox base image** (`ccbox-base`) — the LXD image template used by all future sandboxes.

## Context

- Helper script: `assets/update-base.sh` in this repo (subcommands: `launch`, `publish`)
- Temp container: `ccbox-update-temp`
- Host username and UID: run `id` to discover (typically `zj`, UID 1000)

## Procedure

### 1. Launch temp container
```
bash assets/update-base.sh launch
```
This cleans up any leftover temp container, launches from `ccbox-base`, and applies UID mapping.

### 2. Ask and apply changes

**Ask the user** what they want to change (install packages, update config, patch files, etc.).

Run commands via `lxc exec ccbox-update-temp -- ...`. Use `sudo` for root operations when running as the user.

### 3. Interactive loop

After applying changes, present the user with these options:

**"Changes applied. What next?"**
1. **More changes** — tell me what else to do
2. **Shell** — drop in yourself: `lxc exec ccbox-update-temp -- su -l <username>`
3. **Done** — publish the updated image

Repeat until the user says done.

### 4. Publish
```
bash assets/update-base.sh publish
```

Print: **"Base image `ccbox-base` updated. New sandboxes will use the updated image. Existing sandboxes are unchanged — recreate them to pick up changes (`ccbox remove <name>` then `ccbox`)."**

## Important
- If the launch or publish script fails, fall back to running the equivalent `lxc` commands manually.
- If anything else fails, show the error and ask the user how to proceed.
- For interactive installs (e.g. Rust via rustup), use non-interactive flags.
- If you need to abort, clean up with: `lxc delete ccbox-update-temp --force`
