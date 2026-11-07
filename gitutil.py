"""Thin, safe wrapper around the git CLI: argv only, sanitized environment, timeouts.

Commits made by the plugin never use the user's identity or config: the identity is fixed here.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

from .errors import BatonError

DEFAULT_TIMEOUT = 120
IDENTITY = {
    "GIT_AUTHOR_NAME": "baton",
    "GIT_AUTHOR_EMAIL": "baton@localhost",
    "GIT_COMMITTER_NAME": "baton",
    "GIT_COMMITTER_EMAIL": "baton@localhost",
}
# Variables that would redirect git to another repository, index or object store.
_STRIPPED = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_CEILING_DIRECTORIES",
    "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_ASKPASS", "SSH_ASKPASS",
)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED and not k.startswith("GIT_CONFIG")}
    env.update(IDENTITY)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"  # read commands must never rewrite the user's index
    env["LC_ALL"] = "C"
    if extra:
        env.update(extra)
    return env


def run_git(
    args: Iterable[str], cwd: Path | str, *, env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT, check: bool = True, input: bytes | None = None,
) -> subprocess.CompletedProcess:
    argv = ["git", *[str(a) for a in args]]
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), env=git_env(env), input=input, capture_output=True, timeout=timeout,
            stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise BatonError("git_timeout", f"git {argv[1]} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise BatonError("git_missing", "git is not installed") from exc
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise BatonError("git_failed", f"git {' '.join(argv[1:3])} failed: {err[-600:]}")
    return proc


def out(args: Iterable[str], cwd: Path | str, **kw) -> str:
    return run_git(args, cwd, **kw).stdout.decode("utf-8", "replace").strip()


def rev_parse(repo: Path | str, rev: str) -> str | None:
    proc = run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], repo, check=False)
    return proc.stdout.decode().strip() or None if proc.returncode == 0 else None


def ref_sha(repo: Path | str, ref: str) -> str | None:
    proc = run_git(["rev-parse", "--verify", "--quiet", ref], repo, check=False)
    return proc.stdout.decode().strip() or None if proc.returncode == 0 else None


def is_ancestor(repo: Path | str, ancestor: str, descendant: str) -> bool:
    return run_git(["merge-base", "--is-ancestor", ancestor, descendant], repo, check=False).returncode == 0


def show_file(repo: Path | str, rev: str, path: str) -> bytes | None:
    proc = run_git(["cat-file", "blob", f"{rev}:{path}"], repo, check=False)
    return proc.stdout if proc.returncode == 0 else None


def tree_files(repo: Path | str, rev: str) -> list[str]:
    raw = run_git(["ls-tree", "-r", "-z", "--name-only", rev], repo).stdout.decode("utf-8", "replace")
    return [p for p in raw.split("\0") if p]


def update_ref(repo: Path | str, ref: str, sha: str, old: str | None = None) -> None:
    args = ["update-ref", ref, sha] + ([old] if old is not None else [])
    run_git(args, repo)


def delete_ref(repo: Path | str, ref: str) -> None:
    run_git(["update-ref", "-d", ref], repo, check=False)


def ls_remote(repo: Path | str, remote: str, pattern: str, timeout: float = 60) -> dict[str, str]:
    """{ref: sha} for refs on `remote` matching `pattern`."""
    text = out(["ls-remote", remote, pattern], repo, timeout=timeout)
    refs = {}
    for line in text.splitlines():
        sha, _, ref = line.partition("\t")
        if ref:
            refs[ref] = sha
    return refs


def commit_files(
    repo: Path | str, files: Mapping[str, bytes], message: str, *, base: str | None = None,
    parents: Iterable[str] = (),
) -> str:
    """Commit `files` on top of the tree of `base` (or an empty tree) with a throwaway index.

    Never touches the repository's real index, HEAD or working tree.
    """
    with tempfile.TemporaryDirectory(prefix="baton-idx-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if base:
            run_git(["read-tree", base], repo, env=env)
        for path, data in sorted(files.items()):
            blob = out(["hash-object", "-w", "--stdin"], repo, input=data)
            run_git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], repo, env=env)
        tree = out(["write-tree"], repo, env=env)
    args = ["commit-tree", "--no-gpg-sign", tree, "-m", message]
    for parent in parents:
        args += ["-p", parent]
    return out(args, repo)


def json_commit(repo: Path | str, name: str, data: dict, message: str, parent: str | None = None) -> str:
    """A commit whose tree holds only `name` with `data` as JSON (status and result refs)."""
    blob = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    return commit_files(repo, {name: blob}, message, parents=[parent] if parent else [])


def read_json_ref(repo: Path | str, ref: str, name: str) -> dict | None:
    raw = show_file(repo, ref, name)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
