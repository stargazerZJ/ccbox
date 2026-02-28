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
    path: str
    mode: str  # "rw" or "ro"

    def to_dict(self) -> dict:
        return {"path": self.path, "mode": self.mode}

    @classmethod
    def from_dict(cls, d: dict) -> MountEntry:
        return cls(path=d["path"], mode=d["mode"])


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


@dataclass
class State:
    sandboxes: dict[str, SandboxEntry] = field(default_factory=dict)
    env_whitelist: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sandboxes": {k: v.to_dict() for k, v in self.sandboxes.items()},
            "env_whitelist": self.env_whitelist,
        }

    @classmethod
    def from_dict(cls, d: dict) -> State:
        return cls(
            sandboxes={k: SandboxEntry.from_dict(v) for k, v in d.get("sandboxes", {}).items()},
            env_whitelist=d.get("env_whitelist", []),
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
