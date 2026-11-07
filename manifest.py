"""BATON.json (schema 1) and SHA256SUMS: build and verify the package files."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import ids

SCHEMA = 1
MANIFEST = "BATON.json"
TRANSCRIPT = "transcript.md"
SUMS = "SHA256SUMS"
PACKAGE_FILES = (MANIFEST, TRANSCRIPT, SUMS)


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(
    *, baton_id: str, peer: str, repo_name: str, head: str | None, branch: str | None, project_commit: str,
    task: str, done_when_argv: list[str], transcript: bytes, cli_redact: bool, extra_hits: int,
    timeout_s: int = 900, expect_exit: int = 0, ttl_hours: int = 24, created_at: str | None = None,
) -> dict:
    return {
        "schema": SCHEMA,
        "id": baton_id,
        "created_at": created_at or now_rfc3339(),
        "peer": peer,
        "repo": {"name": repo_name, "head": head, "branch": branch, "project_commit": project_commit},
        "task": task,
        "done_when": {"argv": list(done_when_argv), "timeout_s": int(timeout_s), "expect_exit": int(expect_exit)},
        "transcript": {"path": f".baton/{baton_id}/{TRANSCRIPT}", "bytes": len(transcript), "sha256": sha256(transcript)},
        "redaction": {"cli_redact": bool(cli_redact), "extra_hits": int(extra_hits)},
        "return": {"branch": f"baton/{baton_id}", "result_ref": f"refs/baton/{baton_id}/result"},
        "ttl_hours": int(ttl_hours),
    }


def dumps(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256sums(files: dict[str, bytes]) -> bytes:
    return "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(files.items())).encode()


def package_files(manifest: dict, transcript: bytes) -> dict[str, bytes]:
    """The three files of `.baton/<id>/`, ready for capture.package_commit."""
    body = {MANIFEST: dumps(manifest), TRANSCRIPT: transcript}
    return {**body, SUMS: sha256sums(body)}


def parse(raw: bytes) -> dict:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest is not a JSON object")
    return data


def _schema_problems(m: dict, baton_id: str | None) -> list[str]:
    problems = []
    if m.get("schema") != SCHEMA:
        problems.append(f"unsupported schema: {m.get('schema')!r}")
    if not ids.is_valid(m.get("id")):
        problems.append("invalid id")
    elif baton_id and m["id"] != baton_id:
        problems.append(f"id mismatch: {m['id']} != {baton_id}")
    repo = m.get("repo")
    if not isinstance(repo, dict) or not isinstance(repo.get("project_commit"), str) or len(repo["project_commit"]) < 40:
        problems.append("missing repo.project_commit")
    if not isinstance(m.get("task"), str):
        problems.append("missing task")
    dw = m.get("done_when")
    argv = dw.get("argv") if isinstance(dw, dict) else None
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
        problems.append("done_when.argv must be a non-empty list of strings")
    elif not isinstance(dw.get("timeout_s"), int) or dw["timeout_s"] <= 0:
        problems.append("done_when.timeout_s must be a positive integer")
    elif not isinstance(dw.get("expect_exit"), int) or isinstance(dw["expect_exit"], bool):
        problems.append("done_when.expect_exit must be an integer")
    return problems


def verify(tree_files: dict[str, bytes], baton_id: str | None = None) -> list[str]:
    """Problems found in the package files (keys are names inside `.baton/<id>/`). Empty list = valid."""
    problems = [f"missing file: {name}" for name in PACKAGE_FILES if name not in tree_files]
    if problems:
        return problems
    listed: dict[str, str] = {}
    for line in tree_files[SUMS].decode("utf-8", "replace").splitlines():
        digest, sep, name = line.partition("  ")
        if sep:
            listed[name.strip()] = digest.strip()
    for name in (MANIFEST, TRANSCRIPT):
        if name not in listed:
            problems.append(f"{SUMS} does not list {name}")
        elif listed[name] != sha256(tree_files[name]):
            problems.append(f"sha256 mismatch: {name}")
    try:
        m = parse(tree_files[MANIFEST])
    except ValueError as exc:
        return problems + [f"invalid {MANIFEST}: {exc}"]
    problems += _schema_problems(m, baton_id)
    t = m.get("transcript") if isinstance(m.get("transcript"), dict) else {}
    if t.get("sha256") != sha256(tree_files[TRANSCRIPT]) or t.get("bytes") != len(tree_files[TRANSCRIPT]):
        problems.append("transcript size or sha256 differs from the manifest")
    return problems
