"""Baton ref names on the shared git remote and the status ref protocol (both sides use it)."""

from __future__ import annotations

from pathlib import Path

from . import gitutil
from .manifest import now_rfc3339

NET_TIMEOUT = 120


def in_ref(baton_id: str) -> str:
    return f"refs/baton/{baton_id}/in"


def status_ref(baton_id: str) -> str:
    return f"refs/baton/{baton_id}/status"


def result_ref(baton_id: str) -> str:
    return f"refs/baton/{baton_id}/result"


def incoming_ref(baton_id: str) -> str:
    return f"refs/baton/{baton_id}/incoming"


def branch_name(baton_id: str) -> str:
    return f"baton/{baton_id}"


def branch_ref(baton_id: str) -> str:
    return f"refs/heads/baton/{baton_id}"


def remote_refs(repo: Path | str, remote_git: str, baton_id: str, timeout: float = NET_TIMEOUT) -> dict[str, str]:
    text = gitutil.out(["ls-remote", remote_git, f"refs/baton/{baton_id}/*", branch_ref(baton_id)], repo,
                       timeout=timeout)
    refs = {}
    for line in text.splitlines():
        sha, _, ref = line.partition("\t")
        if ref:
            refs[ref] = sha
    return refs


def fetch(repo: Path | str, remote_git: str, mapping: dict[str, str], timeout: float = NET_TIMEOUT) -> None:
    """Force-fetch `{remote_ref: local_ref}`; never writes FETCH_HEAD, HEAD, the index or files."""
    if not mapping:
        return
    specs = [f"+{src}:{dst}" for src, dst in mapping.items()]
    gitutil.run_git(["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", remote_git, *specs], repo,
                    timeout=timeout)


def push(repo: Path | str, remote_git: str, specs: list[str], timeout: float = NET_TIMEOUT) -> None:
    gitutil.run_git(["push", "--quiet", "--no-verify", remote_git, *specs], repo, timeout=timeout)


def write_status(repo: Path | str, remote_git: str, baton_id: str, state: str, by: str,
                 detail: str | None = None, timeout: float = NET_TIMEOUT) -> str:
    """Commit status.json (chained on the previous local status commit) and force-push it."""
    ref = status_ref(baton_id)
    parent = gitutil.ref_sha(repo, ref)
    data = {"schema": 1, "id": baton_id, "state": state, "at": now_rfc3339(), "by": by}
    if detail:
        data["detail"] = detail[:500]
    sha = gitutil.json_commit(repo, "status.json", data, f"baton {baton_id}: {state}", parent)
    gitutil.update_ref(repo, ref, sha)
    push(repo, remote_git, [f"+{sha}:{ref}"], timeout)
    return sha
