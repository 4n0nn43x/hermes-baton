"""Local and remote halves, then the full loop without Hermes: two temp repos and a bare remote."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fakegateway import KEY, FakeGateway  # noqa: E402
from helpers import git, make_bare, make_repo, snapshot  # noqa: E402

from baton_plugin import gitutil, local, refs, remote  # noqa: E402
from baton_plugin.errors import BatonError  # noqa: E402
from baton_plugin.peers import Peer  # noqa: E402
from baton_plugin.store import BatonStore  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "demo" / "sample"
CHECK = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class World:
    def __init__(self, tmp: Path) -> None:
        self.repo = tmp / "laptop" / "sample"
        shutil.copytree(SAMPLE, self.repo)
        make_repo(self.repo, files={})
        (self.repo / "NOTES.md").write_text("work in progress\n")  # untracked, travels
        self.bare = str(make_bare(tmp / "shared.git"))
        self.local_store = BatonStore(tmp / "local-data")
        self.remote_store = BatonStore(tmp / "remote-data")
        self.settings = remote.RemoteSettings(
            remote_git=self.bare, workspace=tmp / "remote-data" / "work",
            allowed_checks=[[sys.executable, "-m", "pytest"], [sys.executable, "-c"]], check_timeout_s=120)

    def pass_(self, **kw):
        kw.setdefault("trigger", False)
        return local.pass_baton(self.repo, "fix mean(), done when pytest passes", "box", CHECK, self.bare,
                                store=self.local_store, **kw)

    def fix(self, bid: str) -> Path:
        wt = self.settings.worktree(bid)
        calc = wt / "calc.py"
        calc.write_text(calc.read_text().replace("(len(values) - 1)", "len(values)"))
        return wt


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# local -------------------------------------------------------------------------------------------
def test_pass_pushes_refs_without_touching_repo(world):
    before = snapshot(world.repo)
    rec = world.pass_()
    assert rec.state == "PUSHED" and snapshot(world.repo) == before
    remote_refs = refs.remote_refs(world.repo, world.bare, rec.id)
    assert remote_refs[refs.in_ref(rec.id)] == rec.package_commit
    assert refs.status_ref(rec.id) in remote_refs
    assert gitutil.read_json_ref(world.bare, refs.status_ref(rec.id), "status.json")["state"] == "PUSHED"
    assert world.local_store.get(rec.id).state == "PUSHED"


def test_pass_dry_run_writes_nothing(world):
    before = snapshot(world.repo)
    rec = world.pass_(dry_run=True)
    assert rec.state == "PREPARED" and rec.preview["transcript_bytes"] > 0
    assert "NOTES.md" in rec.preview["untracked_included"]
    assert world.local_store.list() == [] and refs.remote_refs(world.repo, world.bare, rec.id) == {}
    assert snapshot(world.repo) == before


def test_pass_blocks_secrets(world):
    (world.repo / "settings.py").write_text(f"TOKEN = '{GHP}'\n")
    with pytest.raises(BatonError) as exc:
        world.pass_()
    assert exc.value.code == "secrets_found" and exc.value.details[0]["file"] == "settings.py"
    assert GHP not in str(exc.value.details)
    assert world.local_store.list() == []


def test_pass_dry_run_reports_secrets(world):
    (world.repo / ".env").write_text("X=1\n")
    rec = world.pass_(dry_run=True)
    assert rec.preview["secret_findings"][0]["rule"] == "forbidden_name:.env"


def test_pass_trigger_and_resume_after_network_cut(world):
    gw = FakeGateway().start()
    try:
        calls = []

        def resolver(name):
            calls.append(name)
            if len(calls) == 1:
                return Peer(name, "http://127.0.0.1:9", KEY)  # nothing listens: cut network
            return Peer(name, gw.url, KEY)

        with pytest.raises(BatonError) as exc:
            world.pass_(trigger=True, peer_resolver=resolver)
        assert exc.value.code == "peer_unreachable"
        bid = world.local_store.list()[0].id
        assert world.local_store.get(bid).state == "PUSHED"
        rec = local.resume(bid, store=world.local_store, peer_resolver=resolver)
        assert rec.state == "TRIGGERED" and rec.run_id and rec.remote_session_id == f"baton-{bid.lower()}"
        run_req = [r for r in gw.requests if r["path"] == "/v1/runs"][-1]
        assert run_req["headers"]["Idempotency-Key"] == bid and bid in run_req["body"]["input"]
        # Same id again (lost acceptance): replayed, same run, no duplicate.
        rec2 = world.local_store.get(bid)
        rec2.state = "PUSHED"
        world.local_store.save(rec2)
        rec3 = local.resume(bid, store=world.local_store, peer_resolver=resolver)
        assert rec3.replayed is True and rec3.run_id == rec.run_id and len(gw.runs) == 1
    finally:
        gw.stop()


def test_cancel_writes_remote_status(world):
    rec = world.pass_()
    out = local.cancel(rec.id, store=world.local_store)
    assert out["remote_notified"] and world.local_store.get(rec.id).state == "CANCELLED"
    with pytest.raises(BatonError) as exc:
        remote.receive(rec.id, world.settings, world.remote_store)
    assert exc.value.code == "baton_cancelled"


def test_land_before_return_refused(world):
    rec = world.pass_()
    with pytest.raises(BatonError) as exc:
        local.land(rec.id, store=world.local_store)
    assert exc.value.code == "not_returned"


# remote ------------------------------------------------------------------------------------------
def test_receive_creates_worktree_idempotently(world):
    rec = world.pass_()
    out = remote.receive(rec.id, world.settings, world.remote_store, session_id="s1")
    wt = Path(out["worktree"])
    assert (wt / "calc.py").exists() and (wt / "NOTES.md").exists() and not (wt / ".baton").exists()
    assert out["done_when"]["argv"] == CHECK and "fix mean()" in out["task"]
    assert out["state"] == "RUNNING"
    assert gitutil.read_json_ref(world.bare, refs.status_ref(rec.id), "status.json")["state"] == "RUNNING"
    again = remote.receive(rec.id, world.settings, world.remote_store)
    assert again["worktree"] == out["worktree"]


def test_receive_unknown_baton(world):
    rec = world.pass_(dry_run=True)
    with pytest.raises(BatonError) as exc:
        remote.receive(rec.id, world.settings, world.remote_store)
    assert exc.value.code == "unknown_baton"


def test_receive_rejects_tampered_package(world):
    rec = world.pass_()
    bad = gitutil.commit_files(world.repo, {f".baton/{rec.id}/transcript.md": b"evil"}, "x",
                               base=rec.package_commit, parents=[rec.project_commit])
    git(world.repo, "push", "-q", "-f", world.bare, f"{bad}:refs/baton/{rec.id}/in")
    with pytest.raises(BatonError) as exc:
        remote.receive(rec.id, world.settings, world.remote_store)
    assert exc.value.code == "manifest_invalid"


def test_check_red_then_green(world):
    rec = world.pass_()
    remote.receive(rec.id, world.settings, world.remote_store)
    red = remote.check(rec.id, world.settings, world.remote_store)
    assert red["passed"] is False and red["exit"] == 1 and "test_mean" in red["tail"] and len(red["log_sha256"]) == 64
    world.fix(rec.id)
    green = remote.check(rec.id, world.settings, world.remote_store)
    assert green["passed"] is True and green["exit"] == 0


def test_check_not_allowed(world):
    rec = world.pass_()
    remote.receive(rec.id, world.settings, world.remote_store)
    world.settings.allowed_checks = [["npm", "test"]]
    with pytest.raises(BatonError) as exc:
        remote.check(rec.id, world.settings, world.remote_store)
    assert exc.value.code == "check_not_allowed"


def test_check_env_has_no_provider_keys(world, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "leak")
    env = remote.check_env(world.repo)
    assert "OPENAI_API_KEY" not in env and "PATH" in env


def test_check_timeout(world):
    rec = local.pass_baton(world.repo, "t", "box", [sys.executable, "-c", "import time; time.sleep(30)"],
                           world.bare, store=world.local_store, trigger=False, timeout_s=1)
    remote.receive(rec.id, world.settings, world.remote_store)
    out = remote.check(rec.id, world.settings, world.remote_store)
    assert out["timed_out"] is True and out["passed"] is False and out["exit"] is None


def test_return_verified_requires_fresh_green_check(world):
    rec = world.pass_()
    remote.receive(rec.id, world.settings, world.remote_store)
    with pytest.raises(BatonError) as exc:
        remote.return_baton(rec.id, "done", "verified", world.settings, world.remote_store)
    assert exc.value.code == "check_missing"
    world.fix(rec.id)
    assert remote.check(rec.id, world.settings, world.remote_store)["passed"]
    (world.settings.worktree(rec.id) / "extra.py").write_text("x = 1\n")  # edit after the check
    with pytest.raises(BatonError) as exc:
        remote.return_baton(rec.id, "done", "verified", world.settings, world.remote_store)
    assert exc.value.code == "check_stale"


def test_return_failed_and_idempotent(world):
    rec = world.pass_()
    remote.receive(rec.id, world.settings, world.remote_store)
    first = remote.return_baton(rec.id, "cannot fix: needs a DB", "failed", world.settings, world.remote_store)
    again = remote.return_baton(rec.id, "cannot fix: needs a DB", "failed", world.settings, world.remote_store)
    assert first["status"] == "failed" and again["replayed"] and again["head"] == first["head"]
    result = gitutil.read_json_ref(world.bare, refs.result_ref(rec.id), "result.json")
    assert result["status"] == "failed" and result["summary"].startswith("cannot fix")
    assert gitutil.read_json_ref(world.bare, refs.status_ref(rec.id), "status.json")["state"] == "FAILED"


def test_work_red_returns_failed(world):
    rec = world.pass_()
    out = remote.work(rec.id, world.settings, world.remote_store)
    assert out["check"]["passed"] is False and out["result"]["status"] == "failed"
    with pytest.raises(BatonError) as exc:
        local.land(rec.id, store=world.local_store)
    assert exc.value.code == "result_failed"
    landed = local.land(rec.id, store=world.local_store, allow_failed=True)
    assert landed["status"] == "failed"


def test_pre_verify_decisions(world):
    rec = world.pass_()
    assert remote.pre_verify(world.remote_store, session_id="s1") is None  # nothing received
    remote.receive(rec.id, world.settings, world.remote_store, session_id="s1")
    nudge = remote.pre_verify(world.remote_store, session_id="s1", changed_paths=[])
    assert nudge["action"] == "continue" and rec.id in nudge["message"] and "baton_check" in nudge["message"]
    assert remote.pre_verify(world.remote_store, session_id="other") is None
    wt = world.fix(rec.id)
    assert remote.pre_verify(world.remote_store, session_id="other", changed_paths=[str(wt / "calc.py")])
    remote.check(rec.id, world.settings, world.remote_store)
    assert remote.pre_verify(world.remote_store, session_id="s1", changed_paths=[str(wt / "calc.py")]) is None
    (wt / "calc.py").write_text((wt / "calc.py").read_text() + "\n# later edit\n")
    assert remote.pre_verify(world.remote_store, session_id="s1") is not None
    remote.check(rec.id, world.settings, world.remote_store)
    remote.return_baton(rec.id, "fixed", "verified", world.settings, world.remote_store)
    assert remote.pre_verify(world.remote_store, session_id="s1") is None  # returned: no more nudges


def test_settings_from_config(tmp_path):
    s = remote.RemoteSettings.from_config({"remote_git": "/g", "role": "weird"}, tmp_path)
    assert s.role == "both" and s.workspace == tmp_path / "work" and ["pytest"] in s.allowed_checks
    assert s.check_timeout_s == 900


# full loop ---------------------------------------------------------------------------------------
def test_full_loop_byte_identical(world):
    git(world.repo, "stash", "list")
    (world.repo / "calc.py").write_text((world.repo / "calc.py").read_text() + "\n# local wip\n")
    git(world.repo, "add", "calc.py")
    (world.repo / "README.md").write_text("unstaged wip\n")
    before = snapshot(world.repo)

    gw = FakeGateway().start()
    try:
        rec = world.pass_(trigger=True, peer_resolver=lambda n: Peer(n, gw.url, KEY))
        assert rec.state == "TRIGGERED"
        # The remote agent's deterministic stand-in: receive, fix, check, return.
        remote.receive(rec.id, world.settings, world.remote_store, session_id="remote-s")
        world.fix(rec.id)
        assert remote.check(rec.id, world.settings, world.remote_store)["passed"]
        ret = remote.return_baton(rec.id, "fix mean() off-by-one", "verified", world.settings, world.remote_store)
    finally:
        gw.stop()

    st = local.status(rec.id, store=world.local_store)[0]
    assert st["state"] == "RETURNED" and st["result"]["status"] == "verified"
    landed = local.land(rec.id, store=world.local_store)
    assert landed["sha"] == ret["head"] and landed["noop"] is False
    assert snapshot(world.repo) == before
    assert git(world.repo, "rev-parse", f"baton/{rec.id}") == ret["head"]
    assert "len(values)\n" in git(world.repo, "show", f"baton/{rec.id}:calc.py")
    assert "# local wip" in git(world.repo, "show", f"baton/{rec.id}:calc.py")  # staged wip travelled
    assert git(world.repo, "log", "-1", "--format=%s", f"baton/{rec.id}") == f"baton {rec.id}: fix mean() off-by-one"
    # Landing again is a no-op.
    assert local.land(rec.id, store=world.local_store)["noop"] is True
    assert world.local_store.get(rec.id).state == "LANDED"


def test_work_green_then_land_variants(world, tmp_path):
    (world.repo / "calc.py").write_text((world.repo / "calc.py").read_text().replace("(len(values) - 1)", "len(values)"))
    rec = world.pass_()
    out = remote.work(rec.id, world.settings, world.remote_store)
    assert out["result"]["status"] == "verified"
    assert remote.work(rec.id, world.settings, world.remote_store)["replayed"] is True
    # Diverged local branch with the same name: refused, nothing moves.
    git(world.repo, "branch", f"baton/{rec.id}", "HEAD")
    before = snapshot(world.repo)
    with pytest.raises(BatonError) as exc:
        local.land(rec.id, store=world.local_store)
    assert exc.value.code == "branch_diverged" and snapshot(world.repo) == before
    # Checked out with another sha: refused as well.
    git(world.repo, "stash", "-u", "-q")
    git(world.repo, "checkout", "-q", f"baton/{rec.id}")
    with pytest.raises(BatonError) as exc:
        local.land(rec.id, store=world.local_store)
    assert exc.value.code == "branch_checked_out"
    git(world.repo, "checkout", "-q", "main")
    git(world.repo, "branch", "-D", f"baton/{rec.id}")
    landed = local.land(rec.id, store=world.local_store, worktree=True)
    wt = Path(landed["worktree"])
    assert wt.name == f"sample-baton-{rec.id}" and (wt / "calc.py").exists()
    assert git(world.repo, "rev-parse", "HEAD") == git(world.repo, "rev-parse", "main")
