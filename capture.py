"""Ghost commit: snapshot the working tree (tracked changes plus untracked, non-ignored files) as a
commit object without touching HEAD, the real index, the working tree or the stash."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import gitutil
from .errors import BatonError


@dataclass
class CaptureResult:
    project_commit: str
    files: list[str]
    untracked_included: list[str]
    head: str | None = None
    branch: str | None = None
    tree: str = ""
    changed: list[str] = field(default_factory=list)


def repo_root(path: Path | str) -> Path:
    """Top level of the work tree containing `path`, or BatonError(not_a_repo)."""
    p = Path(path)
    if not p.is_dir():
        raise BatonError("not_a_repo", f"{p} is not a directory")
    proc = gitutil.run_git(["rev-parse", "--show-toplevel"], p, check=False)
    top = proc.stdout.decode().strip()
    if proc.returncode != 0 or not top:
        raise BatonError("not_a_repo", f"{p} is not inside a git work tree")
    return Path(top)


def _has_submodules(root: Path, index_env: Mapping[str, str] | None = None) -> bool:
    if (root / ".gitmodules").exists():
        return True
    staged = gitutil.out(["ls-files", "-s"], root, env=index_env)
    return any(line.startswith("160000 ") for line in staged.splitlines())


def _escaping_links(root: Path, tree: str) -> list[str]:
    """Symlinks of `tree` that resolve outside the repository (absolute, `..`, chains, loops): the
    remote refuses them anyway (unsafe_snapshot), so refuse before anything is sent."""
    raw = gitutil.run_git(["ls-tree", "-r", "-z", tree], root).stdout.decode("utf-8", "replace")
    top = root.resolve()
    out = []
    for entry in raw.split("\0"):
        if entry.startswith("120000 "):
            path = entry.split("\t", 1)[1]
            try:
                inside = (root / path).resolve().is_relative_to(top)
            except (OSError, RuntimeError):  # symlink loop
                inside = False
            if not inside:
                out.append(path)
    return out


def ghost_commit(repo: Path | str, baton_id: str = "", timeout: float = 300) -> CaptureResult:
    root = repo_root(repo)
    if _has_submodules(root):
        raise BatonError("submodules_unsupported", "repositories with submodules are not supported")
    head = gitutil.rev_parse(root, "HEAD")
    branch_proc = gitutil.run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], root, check=False)
    branch = branch_proc.stdout.decode().strip() or None
    real_index = Path(gitutil.out(["rev-parse", "--path-format=absolute", "--git-path", "index"], root))
    with tempfile.TemporaryDirectory(prefix="baton-capture-") as tmp:
        tmp_index = Path(tmp) / "index"
        if real_index.exists():
            shutil.copyfile(real_index, tmp_index)
        env = {"GIT_INDEX_FILE": str(tmp_index)}
        gitutil.run_git(["add", "-A", "--", "."], root, env=env, timeout=timeout)
        if _has_submodules(root, env):
            raise BatonError("submodules_unsupported", "a nested git repository would be captured as a submodule")
        tree = gitutil.out(["write-tree"], root, env=env)
    escaping = _escaping_links(root, tree)
    if escaping:
        raise BatonError("unsafe_snapshot", "symlinks point outside the repository: " + ", ".join(escaping[:5]),
                         {"symlinks": escaping})
    message = f"baton: snapshot {baton_id}".rstrip()
    args = ["commit-tree", "--no-gpg-sign", tree, "-m", message] + (["-p", head] if head else [])
    project_commit = gitutil.out(args, root)
    files = gitutil.tree_files(root, project_commit)
    tracked = set(gitutil.tree_files(root, head)) if head else set()
    staged = set(p for p in gitutil.run_git(["ls-files", "-z"], root).stdout.decode("utf-8", "replace").split("\0") if p)
    untracked = [f for f in files if f not in tracked and f not in staged]
    if head:
        raw = gitutil.run_git(
            ["diff-tree", "-r", "-z", "--no-renames", "--name-only", "--diff-filter=ACMRT", head, project_commit],
            root).stdout.decode("utf-8", "replace")
        changed = [p for p in raw.split("\0") if p]
    else:
        changed = list(files)
    return CaptureResult(project_commit, files, untracked, head, branch, tree, changed)


def package_commit(repo: Path | str, project_commit: str, baton_id: str, files: Mapping[str, bytes]) -> str:
    """Child of `project_commit` adding only `.baton/<id>/<name>` for each entry of `files`."""
    prefixed = {f".baton/{baton_id}/{name}": data for name, data in files.items()}
    return gitutil.commit_files(
        repo, prefixed, f"baton: package {baton_id}", base=project_commit, parents=[project_commit])
