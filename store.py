"""Local baton records (JSON files under the plugin data dir) and the state machine."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import ids
from .errors import BatonError
from .manifest import now_rfc3339

PREPARED, PUSHED, TRIGGERED = "PREPARED", "PUSHED", "TRIGGERED"
RECEIVED, RUNNING, VERIFIED, RETURNED = "RECEIVED", "RUNNING", "VERIFIED", "RETURNED"
FAILED, LANDED, CANCELLED, EXPIRED = "FAILED", "LANDED", "CANCELLED", "EXPIRED"
STATES = (PREPARED, PUSHED, TRIGGERED, RECEIVED, RUNNING, VERIFIED, RETURNED, FAILED, LANDED, CANCELLED, EXPIRED)
TERMINAL = (LANDED, CANCELLED, EXPIRED)
_ABORT = {FAILED, CANCELLED, EXPIRED}
TRANSITIONS: dict[str, set[str]] = {
    PREPARED: {PUSHED} | _ABORT,
    PUSHED: {TRIGGERED, RECEIVED, RUNNING, VERIFIED, RETURNED} | _ABORT,
    TRIGGERED: {RECEIVED, RUNNING, VERIFIED, RETURNED} | _ABORT,
    RECEIVED: {RUNNING, VERIFIED, RETURNED} | _ABORT,
    RUNNING: {VERIFIED, RETURNED} | _ABORT,
    VERIFIED: {RUNNING, RETURNED} | _ABORT,
    RETURNED: {LANDED},
    FAILED: {LANDED},  # landing a failed branch needs an explicit opt-in (local.land)
    LANDED: set(), CANCELLED: set(), EXPIRED: set(),
}


def can_transition(current: str, target: str) -> bool:
    return current == target or target in TRANSITIONS.get(current, set())


@dataclass
class BatonRecord:
    id: str
    state: str
    repo: str
    peer: str
    task: str
    done_when: dict
    remote_git: str
    created_at: str = field(default_factory=now_rfc3339)
    updated_at: str = field(default_factory=now_rfc3339)
    session_id: str | None = None
    head: str | None = None
    branch: str | None = None
    project_commit: str | None = None
    package_commit: str | None = None
    remote_session_id: str | None = None
    run_id: str | None = None
    replayed: bool = False
    trigger: bool = True
    error: str | None = None
    result: dict | None = None
    landed_sha: str | None = None
    worktree: str | None = None
    preview: dict | None = None
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BatonRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class BatonStore:
    """Records live in `<root>/batons/<id>.json` (local side) and `<root>/remote/<id>.json` (remote side)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, kind: str, baton_id: str) -> Path:
        return self.root / kind / f"{ids.require_valid(baton_id)}.json"

    # local records
    def save(self, record: BatonRecord) -> BatonRecord:
        record.updated_at = now_rfc3339()
        atomic_write_json(self._path("batons", record.id), record.to_dict())
        return record

    def get(self, baton_id: str) -> BatonRecord | None:
        try:
            path = self._path("batons", baton_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return BatonRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise BatonError("registry_corrupt", f"unreadable baton record {path}: {exc}") from None

    def require(self, baton_id: str) -> BatonRecord:
        record = self.get(baton_id)
        if record is None:
            raise BatonError("unknown_baton", f"no local baton {baton_id}")
        return record

    def list(self) -> list[BatonRecord]:
        folder = self.root / "batons"
        if not folder.is_dir():
            return []
        records = []
        for path in sorted(folder.glob("*.json")):
            try:
                records.append(BatonRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, TypeError):
                continue
        return records

    def transition(self, record: BatonRecord, target: str, *, force: bool = False, **changes: Any) -> BatonRecord:
        if target not in STATES:
            raise BatonError("illegal_transition", f"unknown state {target}")
        if not force and not can_transition(record.state, target):
            raise BatonError("illegal_transition", f"{record.state} -> {target} is not allowed for {record.id}")
        for key, value in changes.items():
            setattr(record, key, value)
        if record.state != target:
            record.history.append({"state": target, "at": now_rfc3339()})
        record.state = target
        return self.save(record)

    # remote records (plain dicts)
    def get_remote(self, baton_id: str) -> dict | None:
        path = self._path("remote", baton_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise BatonError("registry_corrupt", f"unreadable remote baton record {path}: {exc}") from None
        if not isinstance(data, dict):
            raise BatonError("registry_corrupt", f"remote baton record {path} is not an object")
        return data

    def save_remote(self, baton_id: str, data: dict) -> dict:
        data["updated_at"] = now_rfc3339()
        atomic_write_json(self._path("remote", baton_id), data)
        return data

    def list_remote(self) -> list[dict]:
        folder = self.root / "remote"
        if not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out
