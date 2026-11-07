"""Baton: hand your session and repository to your remote Hermes, land a tested branch.

`register(ctx)` wires the Hermes-free core (local.py, remote.py, ...) to Hermes v0.23.0 public seams:
the tools baton_receive / baton_check / baton_return, the pre_verify hook, the `/baton` slash command,
the `hermes baton ...` CLI, two plugin skills and extra redaction patterns. This is the only module that
imports Hermes, always lazily and only public modules (hermes_constants, hermes_cli.config,
agent.secret_scope), so the core and its tests run without Hermes installed.

Settings are read at call time from `plugins.entries.baton.settings.*` (never frozen in register) and
Baton never writes Hermes config, approvals or yolo.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from . import gitutil, ids, local, peers, remote
from .errors import BatonError
from .runs import RunsClient
from .store import BatonStore

PLUGIN = "baton"
TOOLSET = "baton"
TOOL_NAMES = ("baton_receive", "baton_check", "baton_return")
HOOK_NAMES = ("pre_verify",)
SETTING_KEYS = ("role", "remote_git", "workspace", "allowed_checks", "check_timeout_s", "peers")
ROLES = ("local", "remote", "both")
SKILLS = {
    "baton-protocol": "Use when a run hands you a baton. Work, check, return.",
    "baton": "Use when the user wants to hand work to their remote Hermes.",
}
# Exit codes of `hermes baton ...`: 0 ok, 1 operation refused or failed (BatonError), 2 usage,
# 3 the operation completed but the agreed check is red (work returned FAILED, check-only red).
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CHECK_RED = 0, 1, 2, 3

# Token shapes from secretscan.RULES, rewritten for agent.redact (v0.23.0 refuses top-level
# alternation, nested unbounded quantifiers and fewer than 2 leading literal characters). The
# telegram and generic rules have no literal prefix: they stay in Baton's own blocking scan and
# transcript pass only.
REDACTION_PATTERNS = (
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY[A-Z ]{0,10}-----",
    r"sk-ant-[A-Za-z0-9_-]{20,}",
    r"sk-or-v1-[A-Za-z0-9_-]{20,}",
    r"sk-proj-[A-Za-z0-9_-]{20,}",
    r"sk-svcacct-[A-Za-z0-9_-]{20,}",
    r"sk-[A-Za-z0-9_-]{20,}",
    r"ghp_[A-Za-z0-9]{36}",
    r"github_pat_[A-Za-z0-9_]{22,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[abp]-[A-Za-z0-9-]{10,}",
)

TOOL_SCHEMAS: dict[str, dict] = {
    "baton_receive": {
        "name": "baton_receive",
        "description": ("Receive a baton handed over from another machine: fetch it from the shared git remote, "
                        "verify its manifest and create the worktree to work in. Returns the worktree path, "
                        "the task, the agreed check (done_when) and a transcript excerpt. Idempotent."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "The baton id (26 characters) from the run prompt."}},
            "required": ["id"]},
    },
    "baton_check": {
        "name": "baton_check",
        "description": ("Run the agreed check of a received baton in its worktree (no shell, minimal environment, "
                        "timeout, allowlisted command only). Returns passed, exit, the last 4 KB of output "
                        "and the sha256 of the full log."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "The baton id."}}, "required": ["id"]},
    },
    "baton_return": {
        "name": "baton_return",
        "description": ("Return a baton: commit the worktree changes and push the branch baton/<id> and the result. "
                        "status 'verified' is refused unless the last baton_check passed on the current files; "
                        "use status 'failed' with the reason when the check cannot pass."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "The baton id."},
            "summary": {"type": "string", "description": "What you changed and why (or why it failed)."},
            "status": {"type": "string", "enum": ["verified", "failed"],
                       "description": "verified (check green) or failed."}},
            "required": ["id", "summary", "status"]},
    },
}


# ---------------------------------------------------------------------------------------------------
# Hermes public seams (lazy imports; fallbacks keep the core testable without Hermes)

def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env) if env else Path.home() / ".hermes"


def data_dir() -> Path:
    """`$HERMES_HOME/plugin-data/baton/` (follows the active profile; resolved on every call)."""
    return hermes_home() / "plugin-data" / PLUGIN


def hermes_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        return load_config_readonly() or {}
    except Exception:
        return {}


def secret_getter() -> Callable[[str], Any] | None:
    try:
        from agent.secret_scope import get_secret
        return get_secret
    except ImportError:
        return None


def _say(text: str = "") -> None:
    print(text, flush=True)


def _json_out(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str), flush=True)


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting (used inside the /baton slash command)."""

    def error(self, message: str):  # type: ignore[override]
        raise _UsageError(message)


# ---------------------------------------------------------------------------------------------------
# The glue object: everything reads settings and paths at call time.

class Baton:
    SLASH_USAGE = ('Usage: /baton <task> [--peer NAME] [--done-when "python -m pytest -q"] [--dry-run]\n'
                   "       /baton status [<id>]\n"
                   "Then: hermes baton status <id> / hermes baton land <id>")

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # settings ---------------------------------------------------------------------------------------
    def settings(self) -> dict:
        out: dict[str, Any] = {}
        for key in SETTING_KEYS:
            try:
                value = self.ctx.get_config(key, None)
            except Exception:
                value = None
            if value is not None and value != "":
                out[key] = value
        return out

    def role(self) -> str:
        role = str(self.settings().get("role") or "both").strip().lower()
        return role if role in ROLES else "both"

    def remote_enabled(self, *_args: Any, **_kw: Any) -> bool:
        return self.role() in ("remote", "both")

    def store(self) -> BatonStore:
        return BatonStore(data_dir())

    def remote_settings(self) -> remote.RemoteSettings:
        return remote.RemoteSettings.from_config(self.settings(), data_dir())

    def resolve_peer(self, name: str) -> peers.Peer:
        return peers.resolve(name, hermes_config(), self.settings(), secret_getter=secret_getter())

    def peer_names(self) -> list[str]:
        names: set[str] = set()
        bot_peers = hermes_config().get("bot_peers")
        if isinstance(bot_peers, Mapping):
            names.update(str(n) for n in bot_peers)
        plugin_peers = self.settings().get("peers")
        if isinstance(plugin_peers, Mapping):
            names.update(str(n) for n in plugin_peers)
        return sorted(names)

    def default_peer(self, explicit: str | None) -> str:
        if explicit:
            return explicit
        names = self.peer_names()
        if len(names) == 1:
            return names[0]
        raise BatonError("peer_required", "name the remote with --peer NAME" +
                         (f" (configured: {', '.join(names)})" if names else " (none configured: hermes peer add)"))

    def require_role(self, side: str) -> None:
        role = self.role()
        if side == "remote" and role == "local":
            raise BatonError("role_local", "this Hermes has baton role 'local'; remote work is disabled")
        if side == "local" and role == "remote":
            raise BatonError("role_remote", "this Hermes has baton role 'remote'; passing batons is disabled")

    def remote_git(self) -> str:
        value = str(self.settings().get("remote_git") or "")
        if not value:
            raise BatonError("missing_remote_git",
                             "set plugins.entries.baton.settings.remote_git (URL or path of the shared bare repo)")
        return value

    def check_timeout(self) -> int:
        try:
            return int(self.settings().get("check_timeout_s") or 900)
        except (TypeError, ValueError):
            return 900

    # tools ------------------------------------------------------------------------------------------
    def _tool(self, fn: Callable[[dict, str | None], dict]) -> Callable[..., str]:
        def handler(args: dict, session_id: str | None = None, **_kw: Any) -> str:
            try:
                self.require_role("remote")
                return json.dumps(fn(dict(args or {}), session_id), default=str)
            except BatonError as exc:
                return json.dumps(exc.to_dict())
            except ValueError as exc:
                return json.dumps({"error": "invalid_argument", "message": str(exc)})
        return handler

    def tool_receive(self, args: dict, session_id: str | None) -> dict:
        return remote.receive(str(args.get("id") or ""), self.remote_settings(), self.store(), session_id=session_id)

    def tool_check(self, args: dict, _session_id: str | None) -> dict:
        return remote.check(str(args.get("id") or ""), self.remote_settings(), self.store())

    def tool_return(self, args: dict, _session_id: str | None) -> dict:
        return remote.return_baton(str(args.get("id") or ""), str(args.get("summary") or ""),
                                   str(args.get("status") or ""), self.remote_settings(), self.store())

    # hook -------------------------------------------------------------------------------------------
    def pre_verify(self, session_id: str = "", changed_paths: list | None = None, **_kw: Any) -> dict | None:
        try:
            if not self.remote_enabled():
                return None
            return remote.pre_verify(self.store(), session_id=session_id or None, changed_paths=changed_paths)
        except Exception:  # a hook must never break the agent's turn
            return None

    # slash command ----------------------------------------------------------------------------------
    def slash_parser(self) -> _Parser:
        p = _Parser(prog="/baton", add_help=False,
                    description="Hand this session and its repository to your remote Hermes.")
        p.add_argument("task", nargs="*")
        p.add_argument("--peer")
        p.add_argument("--done-when", dest="done_when")
        p.add_argument("--dry-run", dest="dry_run", action="store_true")
        p.add_argument("--no-trigger", dest="no_trigger", action="store_true")
        return p

    def slash(self, raw_args: str = "", session_id: str | None = None, platform: str | None = None) -> str:
        try:
            tokens = shlex.split(raw_args or "")
        except ValueError as exc:
            return f"baton: {exc}\n{self.SLASH_USAGE}"
        if not tokens or tokens[0] in ("help", "-h", "--help"):
            return self.SLASH_USAGE
        try:
            if tokens[0] == "status" and len(tokens) <= 2 and (len(tokens) == 1 or ids.is_valid(tokens[1])):
                rows = local.status(tokens[1] if len(tokens) == 2 else None, store=self.store())
                return "\n".join(_status_line(r) for r in rows) or "baton: no batons yet"
            ns = self.slash_parser().parse_args(tokens)
            task = " ".join(ns.task).strip()
            if not task:
                raise _UsageError("describe the task")
            try:
                repo = self.ctx.session_cwd() or os.getcwd()
            except Exception:
                repo = os.getcwd()
            record = self.do_pass(repo, task, ns.peer, ns.done_when, dry_run=ns.dry_run,
                                  trigger=not ns.no_trigger, session_id=session_id)
            return "\n".join(_record_lines(record, dry_run=ns.dry_run))
        except _UsageError as exc:
            return f"baton: {exc}\n{self.SLASH_USAGE}"
        except BatonError as exc:
            return "\n".join(_error_lines(exc))
        except ValueError as exc:
            return f"baton: invalid_argument: {exc}"

    # shared operations ------------------------------------------------------------------------------
    def do_pass(self, repo: str | Path, task: str, peer: str | None, done_when: str | None, *,
                dry_run: bool = False, trigger: bool = True, session_id: str | None = None,
                baton_id: str | None = None):
        self.require_role("local")
        argv = shlex.split(done_when) if done_when else None
        peer_name = self.default_peer(peer) if (trigger and not dry_run) else (peer or "unnamed")
        return local.pass_baton(
            repo, task, peer_name, argv, self.remote_git(), session_id=session_id, trigger=trigger,
            dry_run=dry_run, store=self.store(), peer_resolver=self.resolve_peer,
            baton_id=baton_id or ids.new_id(), timeout_s=self.check_timeout())

    def run_status(self, summary: dict) -> dict | None:
        """Best effort: the remote Runs API view of the run that a pass triggered."""
        if not summary.get("run_id"):
            return None
        try:
            peer = self.resolve_peer(summary["peer"])
            run = RunsClient(peer.url, peer.key, timeout=15).get_run(summary["run_id"])
        except BatonError as exc:
            return {"status": "unknown", "error": exc.code}
        out = {"status": run.get("status")}
        for key in ("error", "last_error", "failure_reason", "message"):
            value = run.get(key)
            if value:
                out[key] = (json.dumps(value) if not isinstance(value, str) else value)[:300]
        return out

    # CLI --------------------------------------------------------------------------------------------
    def cli_setup(self, parser: argparse.ArgumentParser) -> None:
        parser.epilog = ("exit codes: 0 ok, 1 refused or failed (machine code printed), 2 usage, "
                         "3 completed but the agreed check is red")
        sub = parser.add_subparsers(dest="baton_command", metavar="{pass,status,land,list,cancel,work,doctor}")
        p = sub.add_parser("pass", help="snapshot this repo, push it to the shared remote, trigger the peer")
        p.add_argument("task", nargs="?", default=None, help="what the remote agent must do")
        p.add_argument("--peer", help="peer name (hermes peer add, or plugin setting peers)")
        p.add_argument("--done-when", dest="done_when", help='check argv, e.g. "python -m pytest -q"')
        p.add_argument("--repo", default=".", help="repository to hand over (default: current directory)")
        p.add_argument("--dry-run", dest="dry_run", action="store_true", help="show what would be sent; send nothing")
        p.add_argument("--no-trigger", dest="no_trigger", action="store_true",
                       help="push only; the remote picks it up with `hermes baton work <id>`")
        p.add_argument("--resume", metavar="ID", help="restart an interrupted pass (same id, same remote run)")
        p.add_argument("--json", action="store_true")
        s = sub.add_parser("status", help="refresh and show baton states (from git refs and the Runs API)")
        s.add_argument("id", nargs="?")
        s.add_argument("--offline", action="store_true", help="do not contact the remote")
        s.add_argument("--json", action="store_true")
        land = sub.add_parser("land", help="fetch baton/<id> next to your work (never touches HEAD/index/tree)")
        land.add_argument("id")
        land.add_argument("--worktree", action="store_true", help="also create ../<repo>-baton-<id>")
        land.add_argument("--allow-failed", dest="allow_failed", action="store_true",
                          help="land a branch whose check failed")
        land.add_argument("--json", action="store_true")
        ls = sub.add_parser("list", help="list local batons")
        ls.add_argument("--json", action="store_true")
        c = sub.add_parser("cancel", help="cancel a baton (status ref + stop the remote run)")
        c.add_argument("id")
        c.add_argument("--json", action="store_true")
        w = sub.add_parser("work", help="REMOTE side, no model: receive, check, return (deterministic path)")
        w.add_argument("id")
        w.add_argument("--no-agent", dest="no_agent", action="store_true",
                       help="explicit marker: this path never calls a model (it never does)")
        mode = w.add_mutually_exclusive_group()
        mode.add_argument("--receive-only", dest="receive_only", action="store_true",
                          help="only receive (prints the worktree path)")
        mode.add_argument("--check-only", dest="check_only", action="store_true",
                          help="receive if needed and run the check once; do not return")
        w.add_argument("--summary", help="summary written in result.json")
        w.add_argument("--json", action="store_true")
        d = sub.add_parser("doctor", help="check settings, the shared git remote and a peer")
        d.add_argument("--peer", help="peer to probe: reachability, auth, runs_idempotency")
        d.add_argument("--json", action="store_true")
        parser.set_defaults(baton_parser=parser)

    def cli(self, args: argparse.Namespace) -> int:
        command = getattr(args, "baton_command", None)
        if not command:
            parser = getattr(args, "baton_parser", None)
            if parser is not None:
                parser.print_help()
            return EXIT_USAGE
        handler = getattr(self, f"cli_{command}")
        try:
            return handler(args)
        except BatonError as exc:
            if getattr(args, "json", False):
                _json_out(exc.to_dict())
            else:
                for line in _error_lines(exc):
                    _say(line)
            return EXIT_ERROR
        except ValueError as exc:
            _say(f"baton: invalid_argument: {exc}")
            return EXIT_USAGE

    def cli_pass(self, args) -> int:
        if args.resume:
            self.require_role("local")
            record = local.resume(ids.require_valid(args.resume), store=self.store(), peer_resolver=self.resolve_peer)
        else:
            if not args.task or not args.task.strip():
                _say("baton: missing_task: describe the task to hand over")
                return EXIT_USAGE
            baton_id = ids.new_id()
            try:
                record = self.do_pass(args.repo, args.task, args.peer, args.done_when, dry_run=args.dry_run,
                                      trigger=not args.no_trigger, baton_id=baton_id)
            except BatonError:
                if self.store().get(baton_id):
                    _say(f"baton {baton_id} is saved; after fixing the cause run: hermes baton pass --resume {baton_id}")
                raise
        if args.json:
            _json_out(record.to_dict())
        else:
            for line in _record_lines(record, dry_run=args.dry_run):
                _say(line)
        return EXIT_OK

    def cli_status(self, args) -> int:
        if args.id:
            ids.require_valid(args.id)
        rows = local.status(args.id, store=self.store(), offline=args.offline)
        if not args.offline:
            for row in rows:
                run = self.run_status(row) if row.get("state") not in ("LANDED", "CANCELLED", "EXPIRED") else None
                if run:
                    row["run"] = run
        if args.json:
            _json_out(rows)
        elif not rows:
            _say("baton: no batons yet")
        else:
            for row in rows:
                _say(_status_line(row))
        return EXIT_OK

    def cli_land(self, args) -> int:
        self.require_role("local")
        out = local.land(ids.require_valid(args.id), store=self.store(), worktree=args.worktree,
                         allow_failed=args.allow_failed)
        if args.json:
            _json_out(out)
        else:
            verb = "already landed (no-op)" if out["noop"] else "landed"
            _say(f"baton {out['id']}: {verb} as branch {out['branch']} at {out['sha'][:12]} (status {out['status']})")
            if out.get("summary"):
                _say(f"  summary: {str(out['summary'])[:300]}")
            if out.get("worktree"):
                _say(f"  worktree: {out['worktree']}")
            _say("  your HEAD, index, working tree and stash were not touched")
        return EXIT_OK

    def cli_list(self, args) -> int:
        rows = local.list_batons(store=self.store())
        if args.json:
            _json_out(rows)
        elif not rows:
            _say("baton: no batons yet")
        else:
            for row in rows:
                _say(_status_line(row))
        return EXIT_OK

    def cli_cancel(self, args) -> int:
        self.require_role("local")
        out = local.cancel(ids.require_valid(args.id), store=self.store(), peer_resolver=self.resolve_peer)
        if args.json:
            _json_out(out)
        else:
            _say(f"baton {out['id']}: CANCELLED (remote notified: {out['remote_notified']}, "
                 f"run stopped: {out['run_stopped']})")
        return EXIT_OK

    def cli_work(self, args) -> int:
        self.require_role("remote")
        baton_id = ids.require_valid(args.id)
        rs, store = self.remote_settings(), self.store()
        received = remote.receive(baton_id, rs, store)
        if args.receive_only:
            if args.json:
                _json_out(received)
            else:
                _say(f"baton {baton_id}: received, state {received['state']}")
                _say(f"  worktree: {received['worktree']}")
                _say(f"  task: {received['task']}")
                _say(f"  done when: {' '.join(received['done_when']['argv'])}")
            return EXIT_OK
        if args.check_only:
            chk = remote.check(baton_id, rs, store)
            if args.json:
                _json_out(chk)
            else:
                for line in _check_lines(chk):
                    _say(line)
            return EXIT_OK if chk["passed"] else EXIT_CHECK_RED
        out = remote.work(baton_id, rs, store, summary=args.summary)
        result = out["result"] or {}
        if args.json:
            _json_out(out)
        else:
            _say(f"baton {baton_id}: received in {received['worktree']}")
            if out.get("check"):
                for line in _check_lines(out["check"]):
                    _say(line)
            replay = " (already returned, replayed)" if out.get("replayed") or result.get("replayed") else ""
            _say(f"baton {baton_id}: returned {str(result.get('status')).upper()}{replay}, "
                 f"branch baton/{baton_id} at {str(result.get('head'))[:12]}")
        return EXIT_OK if result.get("status") == "verified" else EXIT_CHECK_RED

    def cli_doctor(self, args) -> int:
        rows: list[tuple[str, bool, str]] = []
        settings = self.settings()
        role = self.role()
        rows.append(("role", True, role))
        rows.append(("data dir", True, str(data_dir())))
        remote_git = str(settings.get("remote_git") or "")
        if not remote_git:
            rows.append(("remote_git", False, "not set (plugins.entries.baton.settings.remote_git)"))
        else:
            try:
                home = hermes_home()
                cwd = home if home.is_dir() else Path.cwd()
                text = gitutil.out(["ls-remote", remote_git, "refs/heads/*", "refs/baton/*"], cwd, timeout=60)
                batons = {line.split("\t")[1].split("/")[2] for line in text.splitlines()
                          if "\trefs/baton/" in line}
                rows.append(("git remote", True, f"{remote_git} readable ({len(batons)} baton(s) on it)"))
            except BatonError as exc:
                rows.append(("git remote", False, f"{remote_git}: {exc.code}: {exc.message[:200]}"))
        if role in ("remote", "both"):
            rs = self.remote_settings()
            rows.append(("allowed checks", True, "; ".join(" ".join(a) for a in rs.allowed_checks)))
            rows.append(("workspace", True, str(rs.workspace)))
        if args.peer:
            rows.extend(self._doctor_peer(args.peer))
        ok = all(r[1] for r in rows)
        if args.json:
            _json_out({"ok": ok, "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in rows]})
        else:
            for name, good, detail in rows:
                _say(f"{'OK  ' if good else 'FAIL'} {name}: {detail}")
            _say("baton doctor: all checks passed" if ok else "baton doctor: some checks failed")
        return EXIT_OK if ok else EXIT_ERROR

    def _doctor_peer(self, name: str) -> list[tuple[str, bool, str]]:
        rows: list[tuple[str, bool, str]] = []
        try:
            peer = self.resolve_peer(name)
        except BatonError as exc:
            return [("peer", False, f"{exc.code}: {exc.message}")]
        rows.append(("peer", True, f"{peer.name} -> {peer.url} (from {peer.source}; key present, not shown)"))
        try:
            req = urllib.request.Request(peer.url + "/health", headers={"User-Agent": "hermes-baton"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                rows.append(("reachable", True, f"GET /health -> HTTP {resp.status}"))
        except urllib.error.HTTPError as exc:
            rows.append(("reachable", True, f"GET /health -> HTTP {exc.code} (server answers)"))
        except (urllib.error.URLError, OSError) as exc:
            rows.append(("reachable", False, f"cannot reach {peer.url}: {getattr(exc, 'reason', exc)}"))
            return rows
        try:
            caps = RunsClient(peer.url, peer.key, timeout=15).capabilities()
            feature = (caps.get("features") or {}).get("runs_idempotency") or {}
            rows.append(("auth", True, "API key accepted"))
            rows.append(("runs_idempotency", True, f"supported (durable={feature.get('durable')}, "
                                                    f"retention_s={feature.get('retention_seconds')})"))
        except BatonError as exc:
            label = "auth" if exc.code == "auth_failed" else "runs_idempotency"
            rows.append((label, False, f"{exc.code}: {exc.message[:200]}"))
        return rows


# ---------------------------------------------------------------------------------------------------
# Formatting helpers

def _error_lines(exc: BatonError) -> list[str]:
    lines = [f"baton: {exc.code}: {exc.message}"]
    if exc.code == "secrets_found" and isinstance(exc.details, list):
        for f in exc.details[:20]:
            lines.append(f"  {f.get('file')}:{f.get('line')} {f.get('rule')} {f.get('excerpt')}")
        lines.append("  nothing was sent; remove the secret (code is never masked) and pass again")
    return lines


def _record_lines(record, dry_run: bool = False) -> list[str]:
    pv = record.preview or {}
    lines = []
    if dry_run:
        lines.append("baton dry run: nothing was written or sent")
    lines.append(f"baton {record.id}: {record.state} -> peer {record.peer}, branch baton/{record.id}")
    if pv:
        changed = pv.get("changed") or {}
        lines.append(f"  snapshot: {pv.get('files')} files, {len(changed)} changed vs HEAD "
                     f"({', '.join(sorted(changed)[:8])}{'...' if len(changed) > 8 else ''}), "
                     f"untracked included: {len(pv.get('untracked_included') or [])}")
        lines.append(f"  transcript: {pv.get('transcript_bytes')} bytes, extra redactions: {pv.get('redaction_hits')}"
                     + (f" ({pv['transcript_note']})" if pv.get("transcript_note") else ""))
        findings = pv.get("secret_findings") or []
        lines.append(f"  secret scan: {len(findings)} finding(s)" + (" (a real pass would be BLOCKED)" if findings else ""))
        for f in findings[:10]:
            lines.append(f"    {f.get('file')}:{f.get('line')} {f.get('rule')} {f.get('excerpt')}")
    lines.append(f"  done when: {' '.join(record.done_when.get('argv') or [])}")
    if record.run_id:
        lines.append(f"  remote run: {record.run_id}" + (" (replayed: same id, no duplicate)" if record.replayed else ""))
    if not dry_run:
        lines.append(f"  follow: hermes baton status {record.id}    land: hermes baton land {record.id}")
    return lines


def _status_line(row: dict) -> str:
    line = f"{row['id']}  {row['state']:<10} peer={row.get('peer')}  {row.get('task', '')[:60]!r}"
    if row.get("error"):
        line += f"  error={row['error']}"
    result = row.get("result") or {}
    if result:
        line += (f"\n    result: {result.get('status')} head={str(result.get('head'))[:12]} "
                 f"{str(result.get('summary') or '')[:80]!r}")
    run = row.get("run")
    if run:
        extra = "; ".join(f"{k}={v}" for k, v in run.items() if k != "status")
        line += f"\n    remote run {row.get('run_id')}: {run.get('status')}" + (f" ({extra})" if extra else "")
    if row.get("landed_sha"):
        line += f"\n    landed: {row['branch']} at {row['landed_sha'][:12]}"
    return line


def _check_lines(chk: dict) -> list[str]:
    verdict = "GREEN" if chk.get("passed") else ("TIMED OUT" if chk.get("timed_out") else "RED")
    lines = [f"check {' '.join(chk.get('argv') or [])}: {verdict} (exit {chk.get('exit')}, "
             f"log sha256 {str(chk.get('log_sha256'))[:16]}...)"]
    tail = [ln for ln in str(chk.get("tail") or "").splitlines() if ln.strip()][-6:]
    lines.extend(f"  | {ln[:160]}" for ln in tail)
    return lines


# ---------------------------------------------------------------------------------------------------

def register(ctx) -> None:
    baton = Baton(ctx)
    handlers = {"baton_receive": baton.tool_receive, "baton_check": baton.tool_check,
                "baton_return": baton.tool_return}
    for name in TOOL_NAMES:
        ctx.register_tool(
            name=name, toolset=TOOLSET, schema=TOOL_SCHEMAS[name], handler=baton._tool(handlers[name]),
            check_fn=baton.remote_enabled, description=TOOL_SCHEMAS[name]["description"])
    ctx.register_hook("pre_verify", baton.pre_verify)
    ctx.register_command("baton", baton.slash, description="Hand this session and repo to your remote Hermes",
                         args_hint='<task> [--peer NAME] [--done-when "argv"] [--dry-run]')
    ctx.register_cli_command("baton", "Hand work to your remote Hermes and land the tested branch",
                             baton.cli_setup, baton.cli,
                             description="Baton: pass | status | land | list | cancel | work | doctor")
    root = Path(__file__).resolve().parent
    for name, description in SKILLS.items():
        ctx.register_skill(name=name, path=root / "skills" / name / "SKILL.md", description=description)
    ctx.register_redaction_patterns(list(REDACTION_PATTERNS))


__all__ = ["register", "Baton", "TOOL_NAMES", "HOOK_NAMES", "SETTING_KEYS", "REDACTION_PATTERNS"]
