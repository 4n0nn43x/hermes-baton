"""Local side: pass (prepare, push, trigger), resume, status, land, cancel, list.

Nothing here touches the user's HEAD, index, working tree or stash: snapshots use a temp index,
refs live under refs/baton/<id>/*, and the returned branch is created with update-ref only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import capture, gitutil, ids, manifest, refs, secretscan, transcript
from .errors import BatonError
from .peers import Peer
from .runs import RunsClient, build_prompt
from .store import (CANCELLED, EXPIRED, FAILED, LANDED, PREPARED, PUSHED, RETURNED, TERMINAL,
                    TRIGGERED, BatonRecord, BatonStore, can_transition)

DEFAULT_DONE_WHEN = ["python", "-m", "pytest", "-q"]
REMOTE_STATES = {"RECEIVED", "RUNNING", "VERIFIED", "RETURNED", "FAILED", "CANCELLED"}


def _prepare(record: BatonRecord, root: Path, *, dry_run: bool, hermes_argv: Sequence[str] | None,
             transcript_runner: Callable | None, ttl_hours: int) -> BatonRecord:
    cap = capture.ghost_commit(root, record.id)
    findings = secretscan.scan(root, cap.project_commit, base=cap.head)
    if findings and not dry_run:
        raise BatonError("secrets_found", f"{len(findings)} possible secret(s) in the outgoing changes; nothing was sent",
                         details=[f.to_dict() for f in findings])
    if transcript_runner is not None:
        tr = transcript.export(record.session_id, hermes_argv, task=record.task, runner=transcript_runner)
    else:
        tr = transcript.export(record.session_id, hermes_argv, task=record.task)
    m = manifest.build(
        baton_id=record.id, peer=record.peer, repo_name=root.name, head=cap.head, branch=cap.branch,
        project_commit=cap.project_commit, task=record.task, done_when_argv=record.done_when["argv"],
        transcript=tr.data, cli_redact=tr.cli_redact, extra_hits=tr.extra_hits,
        timeout_s=record.done_when.get("timeout_s", 900), expect_exit=record.done_when.get("expect_exit", 0),
        ttl_hours=ttl_hours, created_at=record.created_at)
    files = manifest.package_files(m, tr.data)
    record.package_commit = capture.package_commit(root, cap.project_commit, record.id, files)
    record.project_commit, record.head, record.branch = cap.project_commit, cap.head, cap.branch
    sizes = {p: len(gitutil.show_file(root, cap.project_commit, p) or b"") for p in cap.changed}
    record.preview = {
        "files": len(cap.files), "changed": sizes, "untracked_included": cap.untracked_included,
        "transcript_bytes": tr.bytes, "transcript_note": tr.note, "redaction_hits": tr.extra_hits,
        "secret_findings": [f.to_dict() for f in findings], "done_when": record.done_when,
        "package_commit": record.package_commit}
    return record


def pass_baton(
    repo: Path | str, task: str, peer: str, done_when_argv: Sequence[str] | None, remote_git: str,
    session_id: str | None = None, trigger: bool = True, dry_run: bool = False, *, store: BatonStore,
    peer_resolver: Callable[[str], Peer] | None = None, client_factory: Callable = RunsClient,
    hermes_argv: Sequence[str] | None = None, transcript_runner: Callable | None = None,
    baton_id: str | None = None, timeout_s: int = 900, ttl_hours: int = 24,
) -> BatonRecord:
    if not task or not task.strip():
        raise BatonError("missing_task", "describe the task to hand over")
    if not remote_git:
        raise BatonError("missing_remote_git", "set remote_git (URL or path of the shared bare repository)")
    root = capture.repo_root(repo)
    argv = list(done_when_argv or DEFAULT_DONE_WHEN)
    record = BatonRecord(
        id=baton_id or ids.new_id(), state=PREPARED, repo=str(root), peer=peer, task=task.strip(),
        done_when={"argv": argv, "timeout_s": int(timeout_s), "expect_exit": 0}, remote_git=remote_git,
        session_id=session_id, trigger=trigger)
    _prepare(record, root, dry_run=dry_run, hermes_argv=hermes_argv, transcript_runner=transcript_runner,
             ttl_hours=ttl_hours)
    if dry_run:
        return record  # nothing written: no ref, no record
    gitutil.update_ref(root, refs.in_ref(record.id), record.package_commit or "")
    record.history.append({"state": PREPARED, "at": record.created_at})
    store.save(record)
    return _advance(record, store, peer_resolver, client_factory)


def resume(baton_id: str, *, store: BatonStore, peer_resolver: Callable[[str], Peer] | None = None,
           client_factory: Callable = RunsClient) -> BatonRecord:
    """Restart a pass at its first undone step (same id, same Idempotency-Key)."""
    return _advance(store.require(baton_id), store, peer_resolver, client_factory)


def _advance(record: BatonRecord, store: BatonStore, peer_resolver, client_factory) -> BatonRecord:
    root = Path(record.repo)
    try:
        if record.state == PREPARED:
            in_ref = refs.in_ref(record.id)
            refs.push(root, record.remote_git, [f"{in_ref}:{in_ref}"])
            refs.write_status(root, record.remote_git, record.id, PUSHED, "local")
            record = store.transition(record, PUSHED, error=None)
        if record.state == PUSHED and record.trigger:
            if peer_resolver is None:
                raise BatonError("peer_unknown", "no peer resolver configured")
            peer = peer_resolver(record.peer)
            client = client_factory(peer.url, peer.key)
            client.capabilities()
            sid = client.create_session(f"Baton {record.id}", session_id=f"baton-{record.id.lower()}")
            prompt = build_prompt(record.id, record.task, record.done_when["argv"])
            run = client.start_run(prompt, idempotency_key=record.id, session_id=sid)
            record = store.transition(record, TRIGGERED, run_id=run["run_id"], replayed=run["replayed"],
                                      remote_session_id=sid, error=None)
    except BatonError as exc:
        record.error = exc.code
        store.save(record)
        raise
    return record


def _expired(record: BatonRecord, ttl_hours: int = 24) -> bool:
    try:
        created = datetime.strptime(record.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - created > timedelta(hours=ttl_hours)


def refresh(record: BatonRecord, store: BatonStore) -> BatonRecord:
    """Fetch the status and result refs from the shared remote and mirror the remote state."""
    root = Path(record.repo)
    remote = refs.remote_refs(root, record.remote_git, record.id)
    wanted = {r: r for r in (refs.status_ref(record.id), refs.result_ref(record.id)) if r in remote}
    refs.fetch(root, record.remote_git, wanted)
    result = gitutil.read_json_ref(root, refs.result_ref(record.id), "result.json")
    status = gitutil.read_json_ref(root, refs.status_ref(record.id), "status.json")
    target = None
    if result:
        record.result = result
        target = RETURNED if result.get("status") == "verified" else FAILED
    elif status and status.get("state") in REMOTE_STATES and status.get("by") == "remote":
        target = status["state"]
    if target and target != record.state and can_transition(record.state, target):
        record = store.transition(record, target)
    elif record.state in (PREPARED, PUSHED, TRIGGERED) and _expired(record):
        record = store.transition(record, EXPIRED)
    else:
        store.save(record)
    return record


def summary(record: BatonRecord) -> dict:
    out: dict[str, Any] = {"id": record.id, "state": record.state, "peer": record.peer, "task": record.task[:120],
           "created_at": record.created_at, "branch": refs.branch_name(record.id)}
    for key in ("run_id", "error", "landed_sha", "worktree"):
        if getattr(record, key):
            out[key] = getattr(record, key)
    if record.result:
        out["result"] = {k: record.result.get(k) for k in ("status", "summary", "head", "finished_at")}
    return out


def status(baton_id: str | None = None, *, store: BatonStore, offline: bool = False) -> list[dict]:
    records = [store.require(baton_id)] if baton_id else store.list()
    out = []
    for record in records:
        if not offline and record.state not in TERMINAL and record.state != PREPARED:
            try:
                record = refresh(record, store)
            except BatonError as exc:
                record.error = exc.code
        out.append(summary(record))
    return out


def list_batons(*, store: BatonStore) -> list[dict]:
    return [summary(r) for r in store.list()]


def _checked_out(root: Path, ref: str) -> bool:
    porcelain = gitutil.out(["worktree", "list", "--porcelain"], root)
    return any(line.strip() == f"branch {ref}" for line in porcelain.splitlines())


def land(baton_id: str, *, store: BatonStore, worktree: bool = False, allow_failed: bool = False) -> dict:
    """Fetch `baton/<id>` next to the user's work. Never touches HEAD, the index or the working tree."""
    record = store.require(baton_id)
    root = Path(record.repo)
    if record.state in (CANCELLED, EXPIRED):
        raise BatonError("illegal_transition", f"baton {baton_id} is {record.state}")
    if record.state != LANDED:
        record = refresh(record, store)
    result = record.result or gitutil.read_json_ref(root, refs.result_ref(baton_id), "result.json")
    if not result:
        raise BatonError("not_returned", f"baton {baton_id} has no result yet (state {record.state})")
    if result.get("status") != "verified" and not allow_failed:
        raise BatonError("result_failed", f"the remote returned status {result.get('status')!r}: "
                         f"{str(result.get('summary') or '')[:200]}")
    remote = refs.remote_refs(root, record.remote_git, baton_id)
    if refs.branch_ref(baton_id) not in remote:
        raise BatonError("not_returned", f"the remote has no branch {refs.branch_name(baton_id)}")
    incoming = refs.incoming_ref(baton_id)
    refs.fetch(root, record.remote_git, {refs.branch_ref(baton_id): incoming})
    try:
        sha = gitutil.ref_sha(root, incoming)
        if not sha or result.get("head") != sha:
            raise BatonError("result_mismatch", "result.json head differs from the fetched branch")
        if record.project_commit and not gitutil.is_ancestor(root, record.project_commit, sha):
            raise BatonError("not_descendant", "the returned branch does not contain the snapshot that was sent")
        branch = refs.branch_ref(baton_id)
        existing = gitutil.ref_sha(root, branch)
        if existing and existing != sha:
            code = "branch_checked_out" if _checked_out(root, branch) else "branch_diverged"
            raise BatonError(code, f"local {refs.branch_name(baton_id)} exists at {existing[:12]}, remote is {sha[:12]}")
        noop = existing == sha
        if not noop:
            gitutil.update_ref(root, branch, sha, "0" * 40)
    finally:
        gitutil.delete_ref(root, incoming)
    wt_path = None
    if worktree:
        wt = root.parent / f"{root.name}-baton-{baton_id}"
        if not wt.exists():
            gitutil.run_git(["worktree", "add", "--quiet", str(wt), refs.branch_name(baton_id)], root)
        wt_path = str(wt)
    if record.state != LANDED:
        record = store.transition(record, LANDED, landed_sha=sha, worktree=wt_path or record.worktree, force=True)
    return {"id": baton_id, "branch": refs.branch_name(baton_id), "sha": sha, "noop": noop,
            "worktree": wt_path, "status": result.get("status"), "summary": result.get("summary")}


def cancel(baton_id: str, *, store: BatonStore, peer_resolver: Callable[[str], Peer] | None = None,
           client_factory: Callable = RunsClient) -> dict:
    record = store.require(baton_id)
    if not can_transition(record.state, CANCELLED):
        raise BatonError("illegal_transition", f"baton {baton_id} is {record.state}; it cannot be cancelled")
    record = store.transition(record, CANCELLED)
    notified, stopped = False, False
    if gitutil.ref_sha(record.repo, refs.in_ref(baton_id)):
        try:
            refs.write_status(record.repo, record.remote_git, baton_id, CANCELLED, "local")
            notified = True
        except BatonError:
            pass
    if record.run_id and peer_resolver is not None:
        try:
            peer = peer_resolver(record.peer)
            client_factory(peer.url, peer.key).stop_run(record.run_id)
            stopped = True
        except BatonError:
            pass
    return {"id": baton_id, "state": CANCELLED, "remote_notified": notified, "run_stopped": stopped}
