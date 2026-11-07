"""Remote side: receive, check, return, the deterministic `work` path and the pre_verify decision.

The remote works only in a throwaway worktree of a private cache repository, runs only checks whose
argv is an allowlisted entry (owned by the remote's config, not by the manifest) followed at most by
safe flags and relative paths inside the worktree, with an empty throwaway HOME (and the Hermes home
hidden by bubblewrap when it works here), and cannot return `verified` unless the last green check
matches the current tree.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from . import gitutil, ids, manifest, refs
from .errors import BatonError
from .store import BatonStore

DEFAULT_ALLOWED_CHECKS = [["python", "-m", "pytest"], ["pytest"], ["npm", "test"], ["make", "test"],
                          ["cargo", "test"], ["go", "test"]]
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "USER", "LOGNAME", "TERM")
# Toolchains kept under the real HOME that break without it (rustup proxies, offline crates and go
# modules): the variable is passed through, or points at the real directory when that exists.
TOOLCHAIN_DIRS = {"RUSTUP_HOME": ".rustup", "CARGO_HOME": ".cargo", "GOPATH": "go"}
# Extra arguments a check may add after its allowlisted entry, besides relative paths in the worktree.
SAFE_FLAGS = frozenset({"-q", "-qq", "-v", "-vv", "-x", "-s", "--quiet", "--verbose", "--exitfirst"})
SANDBOX_BASE = ("--dev-bind", "/", "/", "--proc", "/proc", "--unshare-pid", "--die-with-parent")
TAIL_BYTES = 4096
CACHE_EXCLUDES = "__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\nnode_modules/\n.venv/\n"
NUDGE = ("Run baton_check for baton {id}; finish only when it passes, or call baton_return with status "
         "failed and the reason.")


@dataclass
class RemoteSettings:
    remote_git: str
    workspace: Path
    allowed_checks: list = field(default_factory=lambda: [list(a) for a in DEFAULT_ALLOWED_CHECKS])
    check_timeout_s: int = 900
    role: str = "both"
    net_timeout_s: int = 120
    sandbox: str = "auto"  # "auto": bubblewrap when it works here; "off": never

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None, data_dir: Path | str) -> "RemoteSettings":
        cfg = cfg or {}
        allowed = cfg.get("allowed_checks")
        if not (isinstance(allowed, list) and all(isinstance(a, list) and a for a in allowed)):
            allowed = [list(a) for a in DEFAULT_ALLOWED_CHECKS]
        role = str(cfg.get("role") or "both")
        return cls(
            remote_git=str(cfg.get("remote_git") or ""),
            workspace=Path(cfg.get("workspace") or Path(data_dir) / "work"),
            allowed_checks=[[str(x) for x in a] for a in allowed],
            check_timeout_s=int(cfg.get("check_timeout_s") or 900),
            role=role if role in ("local", "remote", "both") else "both",
            sandbox="off" if str(cfg.get("sandbox") or "auto").lower() == "off" else "auto")

    @property
    def cache(self) -> Path:
        return self.workspace / "cache.git"

    def worktree(self, baton_id: str) -> Path:
        return self.workspace / f"baton-{baton_id}"


def _require_remote_git(settings: RemoteSettings) -> None:
    if not settings.remote_git:
        raise BatonError("missing_remote_git", "set remote_git (URL or path of the shared bare repository)")


def _ensure_cache(settings: RemoteSettings) -> Path:
    cache = settings.cache
    if not (cache / "HEAD").exists():
        cache.mkdir(parents=True, exist_ok=True)
        gitutil.run_git(["init", "--quiet", "--bare", str(cache)], settings.workspace)
    exclude = cache / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    if current != CACHE_EXCLUDES:
        exclude.write_text(CACHE_EXCLUDES, encoding="utf-8")
    return cache


def _status(settings: RemoteSettings, baton_id: str, state: str, detail: str | None = None) -> None:
    refs.write_status(settings.cache, settings.remote_git, baton_id, state, "remote", detail, settings.net_timeout_s)


def _read_package(cache: Path, in_sha: str, baton_id: str) -> dict:
    files = {}
    for name in manifest.PACKAGE_FILES:
        data = gitutil.show_file(cache, in_sha, f".baton/{baton_id}/{name}")
        if data is not None:
            files[name] = data
    problems = manifest.verify(files, baton_id)
    if problems:
        raise BatonError("manifest_invalid", "; ".join(problems))
    m = manifest.parse(files[manifest.MANIFEST])
    parent = gitutil.out(["rev-parse", f"{in_sha}^"], cache)
    if parent != m["repo"]["project_commit"]:
        raise BatonError("manifest_invalid", "the package commit is not a child of repo.project_commit")
    changed = gitutil.out(["diff-tree", "-r", "--name-only", "--no-commit-id", parent, in_sha], cache).splitlines()
    if any(not p.startswith(f".baton/{baton_id}/") for p in changed):
        raise BatonError("manifest_invalid", "the package commit changes files outside .baton/<id>/")
    return {"manifest": m, "transcript": files[manifest.TRANSCRIPT].decode("utf-8", "replace")}


def receive(baton_id: str, settings: RemoteSettings, store: BatonStore, session_id: str | None = None) -> dict:
    ids.require_valid(baton_id)
    _require_remote_git(settings)
    cache = _ensure_cache(settings)
    available = refs.remote_refs(cache, settings.remote_git, baton_id, settings.net_timeout_s)
    if refs.in_ref(baton_id) not in available:
        raise BatonError("unknown_baton", f"the shared remote has no {refs.in_ref(baton_id)}")
    wanted = {r: r for r in (refs.in_ref(baton_id), refs.status_ref(baton_id)) if r in available}
    refs.fetch(cache, settings.remote_git, wanted, settings.net_timeout_s)
    status = gitutil.read_json_ref(cache, refs.status_ref(baton_id), "status.json") or {}
    if status.get("state") == "CANCELLED":
        raise BatonError("baton_cancelled", f"baton {baton_id} was cancelled by its sender")
    in_sha = gitutil.ref_sha(cache, refs.in_ref(baton_id)) or ""
    record = store.get_remote(baton_id)
    wt = settings.worktree(baton_id)
    if record and record.get("in_sha") != in_sha:
        raise BatonError("baton_conflict", f"baton {baton_id} was already received with another package")
    package = _read_package(cache, in_sha, baton_id)
    m = package["manifest"]
    if not (record and wt.is_dir()):
        branch = refs.branch_ref(baton_id)
        if not gitutil.ref_sha(cache, branch):
            gitutil.update_ref(cache, branch, m["repo"]["project_commit"])
        gitutil.run_git(["worktree", "prune"], cache, check=False)
        gitutil.run_git(["worktree", "add", "--quiet", str(wt), refs.branch_name(baton_id)], cache)
        escaping = _escaping_links(wt, m["repo"]["project_commit"])
        if escaping:
            gitutil.run_git(["worktree", "remove", "--force", str(wt)], cache, check=False)
            raise BatonError("unsafe_snapshot", "symlinks point outside the worktree: " + ", ".join(escaping[:5]),
                             {"symlinks": escaping})
        record = {"id": baton_id, "in_sha": in_sha, "worktree": str(wt), "manifest": m, "state": "RECEIVED",
                  "session_id": session_id, "checks": 0, "last_check": None, "result": None}
        store.save_remote(baton_id, record)
        _status(settings, baton_id, "RECEIVED")
    if record.get("state") in ("RECEIVED",):
        record["state"] = "RUNNING"
        _status(settings, baton_id, "RUNNING")
    if session_id and record.get("session_id") != session_id:
        record["session_id"] = session_id
    store.save_remote(baton_id, record)
    excerpt = package["transcript"][-4000:]
    return {"id": baton_id, "worktree": str(wt), "task": m["task"], "done_when": m["done_when"],
            "state": record["state"], "transcript_excerpt": excerpt}


def _escaping_links(wt: Path, commit: str) -> list[str]:
    """Symlinks of `commit` that resolve outside the worktree (absolute, `..`, chains, loops): the
    agent's file tools would follow them to the remote's own files."""
    raw = gitutil.run_git(["ls-tree", "-r", "-z", commit], wt).stdout.decode("utf-8", "replace")
    root = wt.resolve()
    out = []
    for entry in raw.split("\0"):
        if entry.startswith("120000 "):
            path = entry.split("\t", 1)[1]
            try:
                inside = (wt / path).resolve().is_relative_to(root)
            except (OSError, RuntimeError):  # symlink loop
                inside = False
            if not inside:
                out.append(path)
    return out


def _require_record(store: BatonStore, baton_id: str) -> dict:
    ids.require_valid(baton_id)
    record = store.get_remote(baton_id)
    if not record:
        raise BatonError("not_received", f"call baton_receive for {baton_id} first")
    if record.get("state") == "CANCELLED":
        raise BatonError("baton_cancelled", f"baton {baton_id} was cancelled by its sender")
    return record


def _refuse_if_cancelled(settings: RemoteSettings, store: BatonStore, record: dict) -> None:
    """Read the shared status before pushing: a return must not overwrite the sender's CANCELLED."""
    ref = refs.status_ref(record["id"])
    if ref in refs.remote_refs(settings.cache, settings.remote_git, record["id"], settings.net_timeout_s):
        refs.fetch(settings.cache, settings.remote_git, {ref: ref}, settings.net_timeout_s)
    if (gitutil.read_json_ref(settings.cache, ref, "status.json") or {}).get("state") == "CANCELLED":
        record["state"] = "CANCELLED"
        store.save_remote(record["id"], record)
        raise BatonError("baton_cancelled", f"baton {record['id']} was cancelled by its sender")


def tree_hash(worktree: Path | str) -> str:
    """Hash of the worktree content as git would commit it (temp index; ignores excluded junk)."""
    with tempfile.TemporaryDirectory(prefix="baton-tree-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        gitutil.run_git(["read-tree", "HEAD"], worktree, env=env)
        gitutil.run_git(["add", "-A", "--", "."], worktree, env=env)
        return gitutil.out(["write-tree"], worktree, env=env)


def check_refusal(argv: list[str], allowed_checks: Iterable[list[str]], worktree: Path | str) -> str | None:
    """Why `argv` may not run, or None. Policy: argv is an `allowed_checks` entry followed only by
    SAFE_FLAGS, `-p no:<plugin>` pairs and relative paths that stay inside the worktree."""
    reason = "it does not start with an allowed check"
    for entry in allowed_checks:
        entry = list(entry)
        if argv[: len(entry)] == entry:
            reason = _extra_refusal(argv[len(entry):], Path(worktree).resolve())
            if reason is None:
                return None
    return reason


def _extra_refusal(extra: list[str], root: Path) -> str | None:
    i = 0
    while i < len(extra):
        arg = extra[i]
        if arg == "-p" and i + 1 < len(extra) and extra[i + 1].startswith("no:"):
            i += 2
            continue
        if arg not in SAFE_FLAGS and (
                arg.startswith(("-", "@")) or "=" in arg or "\0" in arg or PurePosixPath(arg).is_absolute()
                or not (root / arg).resolve().is_relative_to(root)):
            return f"extra argument {arg!r} is neither a safe flag nor a relative path inside the worktree"
        i += 1
    return None


def allowed(argv: list[str], allowed_checks: Iterable[list[str]], worktree: Path | str = ".") -> bool:
    return check_refusal(list(argv), allowed_checks, worktree) is None


def check_env(home: Path | str) -> dict[str, str]:
    """Minimal env for a check: no provider keys, no HERMES_*, HOME = `home` (an empty throwaway dir;
    XDG_* are unset, so their defaults land in it too)."""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["PATH"] = user_path(env.get("PATH") or os.defpath)
    for var, sub in TOOLCHAIN_DIRS.items():
        value = os.environ.get(var) or (str(Path.home() / sub) if (Path.home() / sub).is_dir() else "")
        if value:
            env[var] = value
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", CI="1")
    return env


def user_path(path: str) -> str:
    """`path` with Hermes's own entries moved last: its Python (venv and base interpreter) and
    anything inside a Hermes home. A Hermes process puts its tool store first (pm.install.activate),
    so `python` would be Hermes's interpreter instead of the project's; the terminal tool reorders
    the same way (pm.user_facing)."""
    own = [*hermes_dirs(), Path(os.path.abspath(sys.executable)).parent.resolve(),
           Path(sys.base_prefix).resolve() / "bin"]

    def hermes_owned(entry: str) -> bool:
        try:
            resolved = Path(entry).resolve()
        except (OSError, RuntimeError):
            return False
        return any(resolved.is_relative_to(d) for d in own)

    entries = [e for e in path.split(os.pathsep) if e]
    return os.pathsep.join([e for e in entries if not hermes_owned(e)] + [e for e in entries if hermes_owned(e)])


def hermes_dirs() -> list[Path]:
    """The Hermes homes a check must not see: ~/.hermes and $HERMES_HOME (when they exist).
    This is a deny-list for the sandbox and PATH ordering only; Baton never reads or writes data here
    (its own data lives under get_hermes_home() via the plugin entry point)."""
    dirs = {Path.home() / ".hermes", Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")}
    return sorted(d.resolve() for d in dirs if d.is_dir())


@lru_cache(maxsize=1)
def _bwrap() -> str | None:
    """bubblewrap when installed and allowed to create namespaces here (often not in Docker), else None."""
    path = shutil.which("bwrap")
    if not path:
        return None
    try:
        probe = subprocess.run([path, *SANDBOX_BASE, "--", "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if probe.returncode == 0 else None


def sandboxed(argv: list[str], env: Mapping[str, str], hide: list[Path], keep: list[Path]) -> tuple[list[str], str]:
    """(argv to run, sandbox note). With bubblewrap, `hide` dirs are masked by empty tmpfs, `keep` dirs
    are bound back, and every process of the check dies with it (own pid namespace)."""
    if not hide:
        return argv, "off: no Hermes home to hide"
    bwrap = _bwrap()
    if not bwrap:
        return argv, "off: bubblewrap missing or unusable here"
    exe = shutil.which(argv[0], path=env.get("PATH"))
    if exe and any(p.is_relative_to(h) for p in (Path(os.path.abspath(exe)), Path(exe).resolve()) for h in hide):
        return argv, f"off: {argv[0]} lives inside the Hermes home"
    mounts = [(d, "--tmpfs") for d in hide] + [(Path(d).resolve(), "--bind") for d in keep]
    args = [bwrap, *SANDBOX_BASE]
    for path, op in sorted(mounts, key=lambda m: len(m[0].parts)):  # parents first, children on top
        args += [op, str(path)] if op == "--tmpfs" else [op, str(path), str(path)]
    return [*args, "--", *argv], "bwrap"


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_check(argv: list[str], cwd: Path | str, timeout: float, env: Mapping[str, str]) -> tuple[int | None, bytes, bool]:
    """Run argv without a shell in its own process group. Returns (exit or None, output, timed_out).

    Output goes to a temp file, so a grandchild that keeps it open cannot stall the deadline. The
    group is killed when the check ends or times out, while the leader is still unreaped (no pid
    reuse). A descendant that called setsid() escapes the group (not the bubblewrap pid namespace).
    """
    with tempfile.TemporaryFile(prefix="baton-check-") as log:
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except (FileNotFoundError, PermissionError) as exc:
            return 127, f"cannot run {argv[0]}: {exc}\n".encode(), False
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.05)
        except ChildProcessError:  # reaped by someone else: nothing left to wait for
            pass
        _kill_group(proc.pid)
        proc.wait()
        log.seek(0)
        output = log.read()
    if timed_out:
        return None, output + f"\n[baton] check timed out after {timeout}s\n".encode(), True
    return proc.returncode, output, False


def check(baton_id: str, settings: RemoteSettings, store: BatonStore) -> dict:
    record = _require_record(store, baton_id)
    if record.get("result"):
        raise BatonError("already_returned", f"baton {baton_id} was already returned")
    dw = record["manifest"]["done_when"]
    argv = list(dw["argv"])
    wt = Path(record["worktree"])
    refusal = check_refusal(argv, settings.allowed_checks, wt)
    if refusal:
        raise BatonError("check_not_allowed", f"{argv!r} refused: {refusal}",
                         {"argv": argv, "allowed_checks": settings.allowed_checks})
    tree = tree_hash(wt)
    timeout = min(int(dw.get("timeout_s") or settings.check_timeout_s), settings.check_timeout_s)
    with tempfile.TemporaryDirectory(prefix="baton-home-", ignore_cleanup_errors=True) as home:
        env = check_env(home)
        cmd, sandbox = ((argv, "off: disabled in config") if settings.sandbox == "off"
                        else sandboxed(argv, env, hermes_dirs(), [settings.workspace]))
        code, output, timed_out = run_check(cmd, wt, timeout, env)
    passed = (not timed_out) and code == int(dw.get("expect_exit", 0))
    log_dir = settings.workspace / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"baton-{baton_id}.log").write_bytes(output)
    result = {"argv": argv, "exit": code, "passed": passed, "timed_out": timed_out, "tree": tree,
              "sandbox": sandbox,
              "log_sha256": hashlib.sha256(output).hexdigest(), "log_bytes": len(output),
              "tail": output[-TAIL_BYTES:].decode("utf-8", "replace"), "at": manifest.now_rfc3339()}
    record["last_check"] = result
    record["checks"] = int(record.get("checks") or 0) + 1
    record["state"] = "VERIFIED" if passed else "RUNNING"
    store.save_remote(baton_id, record)
    return {"id": baton_id, **{k: v for k, v in result.items() if k != "tree"}, "tree": tree}


def _commit_worktree(wt: Path, baton_id: str, summary: str) -> None:
    gitutil.run_git(["add", "-A", "--", "."], wt)
    if gitutil.run_git(["diff", "--cached", "--quiet"], wt, check=False).returncode != 0:
        first = (summary.strip().splitlines() or ["work"])[0][:120]
        gitutil.run_git(["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet",
                         "--no-verify", "-m", f"baton {baton_id}: {first}"], wt)


def return_baton(baton_id: str, summary: str, status: str, settings: RemoteSettings, store: BatonStore) -> dict:
    if status not in ("verified", "failed"):
        raise BatonError("invalid_status", "status must be 'verified' or 'failed'")
    record = _require_record(store, baton_id)
    _require_remote_git(settings)
    wt = Path(record["worktree"])
    previous = record.get("result")
    if previous and previous.get("status") == status:
        head_now = gitutil.rev_parse(wt, "HEAD")
        if head_now == previous.get("head") and tree_hash(wt) == gitutil.out(["rev-parse", "HEAD^{tree}"], wt):
            return {"id": baton_id, **previous, "replayed": True}
    _refuse_if_cancelled(settings, store, record)
    tree_before = tree_hash(wt)
    last = record.get("last_check") or {}
    if status == "verified":
        if not last:
            raise BatonError("check_missing", "run baton_check before returning verified")
        if not last.get("passed"):
            raise BatonError("check_failed", "the last baton_check did not pass")
        if last.get("tree") != tree_before:
            raise BatonError("check_stale", "files changed since the last green baton_check; run it again")
    _commit_worktree(wt, baton_id, summary or status)
    head = gitutil.out(["rev-parse", "HEAD"], wt)
    project_commit = record["manifest"]["repo"]["project_commit"]
    if not gitutil.is_ancestor(wt, project_commit, head):
        raise BatonError("base_lost", "the worktree history no longer contains the received snapshot")
    cache = settings.cache
    branch = refs.branch_ref(baton_id)
    result = {
        "schema": 1, "id": baton_id, "status": status, "head": head, "project_commit": project_commit,
        "summary": (summary or "").strip()[:4000], "finished_at": manifest.now_rfc3339(),
        "check": ({k: last.get(k) for k in ("argv", "exit", "log_sha256", "tail")} if last else None)}
    sha = gitutil.json_commit(cache, "result.json", result, f"baton {baton_id}: result {status}",
                              gitutil.ref_sha(cache, refs.result_ref(baton_id)))
    gitutil.update_ref(cache, refs.result_ref(baton_id), sha)
    refs.push(cache, settings.remote_git, [f"+{branch}:{branch}", f"+{sha}:{refs.result_ref(baton_id)}"],
              settings.net_timeout_s)
    _status(settings, baton_id, "RETURNED" if status == "verified" else "FAILED", (summary or "")[:200])
    record["result"] = result
    record["state"] = "RETURNED" if status == "verified" else "FAILED"
    store.save_remote(baton_id, record)
    return {"id": baton_id, **result, "replayed": False}


def work(baton_id: str, settings: RemoteSettings, store: BatonStore, summary: str | None = None) -> dict:
    """Deterministic path (no model): receive, check once, return verified or failed."""
    received = receive(baton_id, settings, store)
    record = store.get_remote(baton_id) or {}
    if record.get("result"):
        return {"id": baton_id, "received": received, "check": record.get("last_check"),
                "result": record["result"], "replayed": True}
    result_check = check(baton_id, settings, store)
    if result_check["passed"]:
        ret = return_baton(baton_id, summary or "check passed on the remote", "verified", settings, store)
    else:
        why = "timed out" if result_check["timed_out"] else f"exit {result_check['exit']}"
        ret = return_baton(baton_id, summary or f"check failed ({why})", "failed", settings, store)
    return {"id": baton_id, "received": received, "check": result_check, "result": ret}


def pre_verify(store: BatonStore, session_id: str | None = None, changed_paths: Iterable[str] | None = None,
               **_ignored: Any) -> dict | None:
    """Hook decision: nudge the agent back to baton_check while its baton has no green check for the tree."""
    changed = [Path(p).resolve() for p in (changed_paths or []) if p]
    for record in store.list_remote():
        if record.get("result") or record.get("state") not in ("RECEIVED", "RUNNING", "VERIFIED"):
            continue
        wt = Path(record.get("worktree") or "")
        mine = bool(session_id) and record.get("session_id") == session_id
        touched = any(p == wt.resolve() or wt.resolve() in p.parents for p in changed) if wt.parts else False
        if not (mine or touched):
            continue
        last = record.get("last_check") or {}
        try:
            current = tree_hash(wt) if wt.is_dir() else None
        except BatonError:
            current = None
        if last.get("passed") and current and last.get("tree") == current:
            continue
        return {"action": "continue", "message": NUDGE.format(id=record["id"])}
    return None
