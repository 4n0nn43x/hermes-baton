"""Shared helpers for the tests: temp git repos and byte-level snapshots of a work tree."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1", "HOME": os.environ.get("HOME", "/tmp")}


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=repo, env=ENV, capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def make_repo(path: Path, files: dict[str, str] | None = None, commit: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for name, content in (files or {"a.py": "x = 1\n"}).items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if commit:
        git(path, "add", "-A")
        git(path, "commit", "-qm", "init")
    return path


def make_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "--bare")
    return path


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "absent"


def snapshot(repo: Path) -> dict:
    """HEAD, index, stash and every work tree file, byte for byte."""
    gitdir = repo / ".git"
    files = {}
    for p in sorted(repo.rglob("*")):
        if ".git" in p.relative_to(repo).parts or not p.is_file():
            continue
        files[str(p.relative_to(repo))] = _sha(p)
    return {
        "HEAD": _sha(gitdir / "HEAD"),
        "head_commit": git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False),
        "index": _sha(gitdir / "index"),
        "stash_ref": _sha(gitdir / "refs" / "stash"),
        "stash_log": _sha(gitdir / "logs" / "refs" / "stash"),
        "status": git(repo, "--no-optional-locks", "status", "--porcelain=v1"),
        "files": files,
    }
