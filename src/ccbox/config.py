"""State file management at ~/.config/ccbox/state.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

STATE_DIR = Path.home() / ".config" / "ccbox"
STATE_FILE = STATE_DIR / "state.json"


@dataclass
class MountEntry:
    path: str            # source path on host
    mode: str            # "rw" or "ro"
    target: str | None = None  # target path in container (None = same as path)

    def to_dict(self) -> dict:
        d: dict = {"path": self.path, "mode": self.mode}
        if self.target is not None:
            d["target"] = self.target
        return d

    @classmethod
    def from_dict(cls, d: dict) -> MountEntry:
        return cls(path=d["path"], mode=d["mode"], target=d.get("target"))


@dataclass
class SandboxEntry:
    container: str
    mounts: list[MountEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "container": self.container,
            "mounts": [m.to_dict() for m in self.mounts],
        }

    @classmethod
    def from_dict(cls, d: dict) -> SandboxEntry:
        return cls(
            container=d["container"],
            mounts=[MountEntry.from_dict(m) for m in d.get("mounts", [])],
        )


RUN_DIR = STATE_DIR / "run"
SHIM_DIR = STATE_DIR / "bin"
UV_SOCK = RUN_DIR / "uv.sock"


def _default_auto_mounts() -> list[MountEntry]:
    home = str(Path.home())
    return [
        MountEntry(path=f"{home}/.claude", mode="rw"),
        MountEntry(path=f"{home}/.local/bin/claude", mode="ro"),
        MountEntry(path=f"{home}/.local/share/claude", mode="ro"),
        MountEntry(path=f"{home}/.cache/uv", mode="rw"),
        # uv shim → ~/.local/bin/uv inside the container
        MountEntry(path=str(SHIM_DIR / "uv"), mode="ro",
                   target=f"{home}/.local/bin/uv"),
        # Socket directory for host↔container uv channel
        MountEntry(path=str(RUN_DIR), mode="rw"),
    ]


@dataclass
class State:
    sandboxes: dict[str, SandboxEntry] = field(default_factory=dict)
    env_whitelist: list[str] = field(default_factory=list)
    storage_pool: str | None = None
    auto_mounts: list[MountEntry] | None = None  # None = use defaults

    def get_auto_mounts(self) -> list[MountEntry]:
        if self.auto_mounts is None:
            return _default_auto_mounts()
        return self.auto_mounts

    def to_dict(self) -> dict:
        d: dict = {
            "sandboxes": {k: v.to_dict() for k, v in self.sandboxes.items()},
            "env_whitelist": self.env_whitelist,
        }
        if self.storage_pool is not None:
            d["storage_pool"] = self.storage_pool
        if self.auto_mounts is not None:
            d["auto_mounts"] = [m.to_dict() for m in self.auto_mounts]
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
        )


class Config:
    def __init__(self) -> None:
        self._state = self._load()

    @property
    def state(self) -> State:
        return self._state

    def _load(self) -> State:
        if not STATE_FILE.exists():
            return State()
        with open(STATE_FILE) as f:
            return State.from_dict(json.load(f))

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

    def set_storage_pool(self, pool: str | None) -> None:
        self._state.storage_pool = pool
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
            m for m in self._state.auto_mounts
            if os.path.realpath(m.path) != resolved
        ]
        self._state.auto_mounts.append(MountEntry(path=resolved, mode=mode))
        self.save()

    def remove_auto_mount(self, path: str) -> bool:
        self._ensure_auto_mounts_materialized()
        resolved = os.path.realpath(path)
        before = len(self._state.auto_mounts)
        self._state.auto_mounts = [
            m for m in self._state.auto_mounts
            if os.path.realpath(m.path) != resolved
        ]
        if len(self._state.auto_mounts) == before:
            return False
        self.save()
        return True
