"""State file management at ~/.config/ccbox/state.json."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

STATE_DIR = Path.home() / ".config" / "ccbox"
STATE_FILE = STATE_DIR / "state.json"


@dataclass
class MountEntry:
    path: str  # source path on host
    mode: str  # "rw" or "ro"
    target: str | None = None  # target path in container (None = same as path)
    optional: bool = False  # if True, skip when source doesn't exist
    inode: str | None = None  # "dev:ino" at mount time — detects path replaced by different dir

    def to_dict(self) -> dict:
        d: dict = {"path": self.path, "mode": self.mode}
        if self.target is not None:
            d["target"] = self.target
        if self.optional:
            d["optional"] = True
        if self.inode is not None:
            d["inode"] = self.inode
        return d

    @classmethod
    def from_dict(cls, d: dict) -> MountEntry:
        return cls(
            path=d["path"],
            mode=d["mode"],
            target=d.get("target"),
            optional=d.get("optional", False),
            inode=d.get("inode"),
        )


@dataclass
class ProxyEntry:
    """An LXD proxy device captured declaratively.

    `listen`/`connect` are LXD proxy address specs (e.g. "tcp:127.0.0.1:5173").
    A literal "{wg_ip}" token in either field is substituted with the active
    host's `wg_ip` (per-host config) when the device is applied, so one spec is
    portable across machines with different WireGuard addresses.
    """

    name: str  # device-name suffix → "port-<name>"
    listen: str
    connect: str
    bind: str = "host"  # "host" (listen on host) or "instance" (listen in container)

    def device_name(self) -> str:
        return f"port-{self.name}"

    def resolve(self, wg_ip: str | None) -> tuple[str, str]:
        """Return (listen, connect) with {wg_ip} substituted."""
        listen = self.listen
        connect = self.connect
        if wg_ip is not None:
            listen = listen.replace("{wg_ip}", wg_ip)
            connect = connect.replace("{wg_ip}", wg_ip)
        return listen, connect

    def to_dict(self) -> dict:
        d: dict = {"name": self.name, "listen": self.listen, "connect": self.connect}
        if self.bind != "host":
            d["bind"] = self.bind
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ProxyEntry:
        return cls(
            name=d["name"],
            listen=d["listen"],
            connect=d["connect"],
            bind=d.get("bind", "host"),
        )


@dataclass
class SandboxEntry:
    container: str
    mounts: list[MountEntry] = field(default_factory=list)
    # Captured container-level config (applied on create, repaired on start).
    nesting: bool = False  # security.nesting
    nvidia_runtime: bool = False  # nvidia.runtime
    gpu_pci: str | None = None  # physical GPU passthrough, e.g. "01:00.0"
    proxies: list[ProxyEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {
            "container": self.container,
            "mounts": [m.to_dict() for m in self.mounts],
        }
        if self.nesting:
            d["nesting"] = True
        if self.nvidia_runtime:
            d["nvidia_runtime"] = True
        if self.gpu_pci is not None:
            d["gpu_pci"] = self.gpu_pci
        if self.proxies:
            d["proxies"] = [p.to_dict() for p in self.proxies]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SandboxEntry:
        return cls(
            container=d["container"],
            mounts=[MountEntry.from_dict(m) for m in d.get("mounts", [])],
            nesting=d.get("nesting", False),
            nvidia_runtime=d.get("nvidia_runtime", False),
            gpu_pci=d.get("gpu_pci"),
            proxies=[ProxyEntry.from_dict(p) for p in d.get("proxies", [])],
        )


CACHE_DIR = Path.home() / ".cache" / "ccbox"
RUN_DIR = CACHE_DIR / "run"
SHIM_DIR = STATE_DIR / "bin"
UV_SOCK = RUN_DIR / "uv.sock"
SESSION_LINK_DIR = RUN_DIR / "session-links"


def _default_auto_mounts() -> list[MountEntry]:
    home = str(Path.home())
    return [
        # Claude tooling
        MountEntry(path=f"{home}/.claude", mode="rw"),
        # Mount the whole bin dir so claude stays a symlink in the container.
        MountEntry(path=f"{home}/.local/bin", mode="ro"),
        MountEntry(path=f"{home}/.local/share/claude", mode="ro"),
        MountEntry(path=f"{home}/.local/share/claude/versions", mode="rw"),
        # uv: cache, managed pythons, config
        MountEntry(path=f"{home}/.cache/uv", mode="rw"),
        MountEntry(path=f"{home}/.local/share/uv", mode="rw"),
        MountEntry(path=f"{home}/.config/uv", mode="ro"),
        # HuggingFace cache shared with the host (content-addressed; like uv).
        # Keeps multi-GB model/dataset blobs out of every container rootfs so
        # ZFS-replicated rootfs stay lean. Optional: host may not have it yet.
        MountEntry(path=f"{home}/.cache/huggingface", mode="rw", optional=True),
        # uv shim → ~/.local/bin/uv inside the container
        MountEntry(path=str(SHIM_DIR / "uv"), mode="ro", target=f"{home}/.local/bin/uv"),
        # Socket directory for host↔container uv channel + session links
        MountEntry(path=str(RUN_DIR), mode="rw"),
        # Codex CLI (via nvm) — optional, only if nvm is installed
        MountEntry(path=f"{home}/.nvm", mode="ro", optional=True),
        MountEntry(path=f"{home}/.codex", mode="rw", optional=True),
        # ccbox config (profile script, bin shims) — directory mount
        MountEntry(path=f"{home}/.config/ccbox", mode="ro"),
    ]


@dataclass
class HostConfig:
    """Per-host bits that must resolve per machine, so one synced state.json is
    portable. Keyed by socket.gethostname() in State.hosts."""

    storage_pool: str | None = None
    network: str | None = None  # LXD bridge/network name (e.g. "lxdbr0")
    wg_ip: str | None = None  # WireGuard IP used by {wg_ip} proxy specs

    def to_dict(self) -> dict:
        d: dict = {}
        if self.storage_pool is not None:
            d["storage_pool"] = self.storage_pool
        if self.network is not None:
            d["network"] = self.network
        if self.wg_ip is not None:
            d["wg_ip"] = self.wg_ip
        return d

    @classmethod
    def from_dict(cls, d: dict) -> HostConfig:
        return cls(
            storage_pool=d.get("storage_pool"),
            network=d.get("network"),
            wg_ip=d.get("wg_ip"),
        )


@dataclass
class State:
    sandboxes: dict[str, SandboxEntry] = field(default_factory=dict)
    env_whitelist: list[str] = field(default_factory=list)
    storage_pool: str | None = None  # legacy global fallback (superseded by hosts)
    auto_mounts: list[MountEntry] | None = None  # None = use defaults
    hosts: dict[str, HostConfig] = field(default_factory=dict)

    def get_auto_mounts(self) -> list[MountEntry]:
        if self.auto_mounts is None:
            return _default_auto_mounts()
        return self.auto_mounts

    def host_config(self, hostname: str | None = None) -> HostConfig:
        """Resolve the active host's config, falling back to the legacy
        top-level storage_pool when the host isn't listed."""
        if hostname is None:
            hostname = socket.gethostname()
        hc = self.hosts.get(hostname)
        if hc is None:
            return HostConfig(storage_pool=self.storage_pool)
        # Fill an unset storage_pool from the legacy global value.
        if hc.storage_pool is None and self.storage_pool is not None:
            return HostConfig(
                storage_pool=self.storage_pool, network=hc.network, wg_ip=hc.wg_ip
            )
        return hc

    def to_dict(self) -> dict:
        d: dict = {
            "sandboxes": {k: v.to_dict() for k, v in self.sandboxes.items()},
            "env_whitelist": self.env_whitelist,
        }
        if self.storage_pool is not None:
            d["storage_pool"] = self.storage_pool
        if self.auto_mounts is not None:
            d["auto_mounts"] = [m.to_dict() for m in self.auto_mounts]
        if self.hosts:
            d["hosts"] = {k: v.to_dict() for k, v in self.hosts.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> State:
        auto_mounts = None
        if "auto_mounts" in d:
            auto_mounts = [MountEntry.from_dict(m) for m in d["auto_mounts"]]
        return cls(
            sandboxes={k: SandboxEntry.from_dict(v) for k, v in d.get("sandboxes", {}).items()},
            env_whitelist=d.get("env_whitelist", []),
            storage_pool=d.get("storage_pool"),
            auto_mounts=auto_mounts,
            hosts={k: HostConfig.from_dict(v) for k, v in d.get("hosts", {}).items()},
        )


class Config:
    def __init__(self) -> None:
        self._state = self._load()
        # One-time migration for legacy mount layouts.
        if self._migrate_legacy_auto_mounts():
            self.save()

    @property
    def state(self) -> State:
        return self._state

    def _load(self) -> State:
        if not STATE_FILE.exists():
            return State()
        with open(STATE_FILE) as f:
            return State.from_dict(json.load(f))

    def _migrate_legacy_auto_mounts(self) -> bool:
        """Migrate legacy auto-mount entries to safer defaults."""
        mounts = self._state.auto_mounts
        if mounts is None:
            return False

        home = str(Path.home())
        claude_link = f"{home}/.local/bin/claude"
        claude_bin_dir = f"{home}/.local/bin"
        claude_json = f"{home}/.claude.json"
        claude_link_real = os.path.realpath(claude_link)
        claude_json_real = os.path.realpath(claude_json)

        changed = False
        rewritten: list[MountEntry] = []

        for m in mounts:
            mount_real = os.path.realpath(m.path)

            # File-mounting .claude.json breaks atomic replace writes.
            if m.target is None and (m.path == claude_json or mount_real == claude_json_real):
                changed = True
                continue

            # Legacy claude file mount flattened the symlink inside containers.
            if m.target is None and (m.path == claude_link or mount_real == claude_link_real):
                rewritten.append(MountEntry(path=claude_bin_dir, mode=m.mode))
                changed = True
                continue

            rewritten.append(m)

        # Keep only one entry per source+target pair.
        deduped: dict[tuple[str, str | None], MountEntry] = {}
        for m in rewritten:
            key = (os.path.realpath(m.path), m.target)
            deduped[key] = m
        deduped_mounts = list(deduped.values())
        if len(deduped_mounts) != len(rewritten):
            changed = True

        if changed:
            self._state.auto_mounts = deduped_mounts
        return changed

    def save(self) -> None:
        """Atomic save: write to tmp file then rename."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._state.to_dict(), f, indent=2)
            f.write("\n")
        os.rename(tmp, STATE_FILE)

    def get_sandbox(self, name: str) -> SandboxEntry | None:
        return self._state.sandboxes.get(name)

    def set_sandbox(self, name: str, entry: SandboxEntry) -> None:
        self._state.sandboxes[name] = entry
        self.save()

    def remove_sandbox(self, name: str) -> None:
        self._state.sandboxes.pop(name, None)
        self.save()

    def sandbox_for_path(self, path: str) -> str | None:
        """Find sandbox whose mount is a parent of the given path."""
        resolved = os.path.realpath(path)
        best_name = None
        best_len = -1
        for name, entry in self._state.sandboxes.items():
            for m in entry.mounts:
                mount_resolved = os.path.realpath(m.path)
                if resolved == mount_resolved or resolved.startswith(mount_resolved + "/"):
                    if len(mount_resolved) > best_len:
                        best_len = len(mount_resolved)
                        best_name = name
        return best_name

    def add_env(self, var: str) -> None:
        if var not in self._state.env_whitelist:
            self._state.env_whitelist.append(var)
            self.save()

    def remove_env(self, var: str) -> None:
        if var in self._state.env_whitelist:
            self._state.env_whitelist.remove(var)
            self.save()

    def set_storage_pool(self, pool: str | None, hostname: str | None = None) -> None:
        if hostname is None:
            hostname = socket.gethostname()
        hc = self._state.host_config(hostname)
        hc.storage_pool = pool
        self._state.hosts[hostname] = hc
        self.save()

    def _ensure_auto_mounts_materialized(self) -> None:
        """Copy defaults into state so they can be edited."""
        if self._state.auto_mounts is None:
            self._state.auto_mounts = _default_auto_mounts()

    def add_auto_mount(self, path: str, mode: str) -> None:
        self._ensure_auto_mounts_materialized()
        resolved = os.path.realpath(path)
        # Replace if same path exists
        self._state.auto_mounts = [
            m for m in self._state.auto_mounts if os.path.realpath(m.path) != resolved
        ]
        self._state.auto_mounts.append(MountEntry(path=resolved, mode=mode))
        self.save()

    def remove_auto_mount(self, path: str) -> bool:
        self._ensure_auto_mounts_materialized()
        resolved = os.path.realpath(path)
        before = len(self._state.auto_mounts)
        self._state.auto_mounts = [
            m for m in self._state.auto_mounts if os.path.realpath(m.path) != resolved
        ]
        if len(self._state.auto_mounts) == before:
            return False
        self.save()
        return True
