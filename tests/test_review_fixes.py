"""Regression tests for the adversarial review of Baton (BT-FIX): ids, check policy, check env and
sandbox, check deadline, and the remote receive path."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from helpers import make_bare, make_repo  # noqa: E402

from baton_plugin import gitutil, ids, local, refs, remote  # noqa: E402
from baton_plugin.errors import BatonError  # noqa: E402
from baton_plugin.store import BatonStore  # noqa: E402

ALLOWED = remote.DEFAULT_ALLOWED_CHECKS


def received(tmp: Path, argv: list[str], files: dict[str, str] | None = None, links: dict[str, str] | None = None,
             **settings_kw):
    """Pass a baton from a fresh repo (plus untracked `links`) to a bare remote and receive it.
    Returns (id, settings, store)."""
    repo = make_repo(tmp / "repo", files or {"a.py": "x = 1\n"})
    for name, target in (links or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / name)
    bare = str(make_bare(tmp / "shared.git"))
    settings_kw.setdefault("workspace", tmp / "work")
    settings_kw.setdefault("allowed_checks", [[sys.executable]])
    settings = remote.RemoteSettings(remote_git=bare, check_timeout_s=60, **settings_kw)
    rec = local.pass_baton(repo, "probe", "box", argv, bare, store=BatonStore(tmp / "local"), trigger=False)
    store = BatonStore(tmp / "remote")
    remote.receive(rec.id, settings, store)
    return rec.id, settings, store


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


# P01 ---------------------------------------------------------------------------------------------

def test_ids_refuse_trailing_newline():
    good = ids.new_id()
    assert ids.is_valid(good) and not ids.is_valid(good + "\n")
    with pytest.raises(ValueError):
        ids.require_valid(good + "\n")


# P02a: check policy ------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["python", "-m", "pytest"], ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    ["pytest", "-x", "-v", "tests/test_a.py::test_one"], ["go", "test", "./..."], ["cargo", "test", "--quiet"],
    ["npm", "test"], ["make", "test"], ["pytest", "tests", "."],
])
def test_check_policy_accepts(argv, tmp_path):
    assert remote.check_refusal(argv, ALLOWED, tmp_path) is None


@pytest.mark.parametrize("argv", [
    ["make", "test", "--eval=$(shell touch /tmp/pwned)", "-f", "/dev/null"],
    ["python", "-m", "pytest", "--basetemp=/home/u/.hermes"], ["python", "-m", "pytest", "--basetemp", "x"],
    ["python", "-m", "pytest", "-p", "evil_plugin"], ["python", "-m", "pytest", "-c", "/etc/hostname"],
    ["pytest", "/home/u/other-project"], ["pytest", "../outside"], ["pytest", "tests/../../x"],
    ["pytest", "@args.txt"], ["make", "test", "SHELL=/bin/sh"], ["npm", "test", "--", "x"], ["pytest", "a\0b"],
    ["python", "-c", "print(1)"], ["sh", "-c", "pytest"], ["python", "-m", "pytesting"], ["pytest", "-p"],
])
def test_check_policy_refuses(argv, tmp_path):
    assert remote.check_refusal(argv, ALLOWED, tmp_path)


def test_check_policy_refuses_symlink_out_of_worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "out").symlink_to(tmp_path)
    (wt / "tests").mkdir()
    assert remote.check_refusal(["pytest", "out"], ALLOWED, wt)
    assert remote.check_refusal(["pytest", "out/x"], ALLOWED, wt)
    assert remote.check_refusal(["pytest", "tests"], ALLOWED, wt) is None


def test_check_refuses_make_eval_before_running(tmp_path):
    marker = tmp_path / "pwned"
    bid, settings, store = received(tmp_path, ["make", "test", f"--eval=$(shell touch {marker})", "-f", "/dev/null"],
                                    allowed_checks=[list(a) for a in ALLOWED])
    with pytest.raises(BatonError) as exc:
        remote.check(bid, settings, store)
    assert exc.value.code == "check_not_allowed" and "--eval" in exc.value.message and not marker.exists()


# P02b: check env and sandbox ---------------------------------------------------------------------

PROBE_HOME = """import os, pathlib
p = pathlib.Path.home() / '.hermes' / '.env'
print('ENV-READ', p.read_text().strip() if p.exists() else 'none')
print('LEAKED-VARS', sorted(k for k in os.environ if k.startswith(('HERMES', 'XDG')) or k.endswith('_KEY')))
"""


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / ".env").write_text("OPENROUTER_API_KEY=dummy-not-a-real-key\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-env-key")
    return home


def test_check_env_is_a_fresh_home(fake_hermes, tmp_path):
    env = remote.check_env(tmp_path / "throwaway")
    assert env["HOME"] == str(tmp_path / "throwaway")
    assert not [k for k in env if k.startswith(("HERMES", "XDG")) or k.endswith("_KEY")]


def test_check_does_not_see_the_hermes_home_via_home(fake_hermes, tmp_path):
    bid, settings, store = received(tmp_path, [sys.executable, "probe.py"], {"probe.py": PROBE_HOME},
                                    sandbox="off")
    out = remote.check(bid, settings, store)
    assert out["passed"] and out["sandbox"] == "off: disabled in config"
    assert "ENV-READ none" in out["tail"] and "LEAKED-VARS []" in out["tail"] and "dummy" not in out["tail"]


@pytest.mark.skipif(not remote._bwrap(), reason="bubblewrap missing or unusable here")
def test_bwrap_hides_hermes_home_and_kills_escaped_children(fake_hermes, tmp_path):
    secret, marker = fake_hermes / ".hermes" / ".env", tmp_path / "escaped"
    late = f"import pathlib, time; time.sleep(1.5); pathlib.Path({str(marker)!r}).touch()"
    probe = (f"import pathlib, subprocess, sys\n"
             f"p = pathlib.Path({str(secret)!r})\n"
             f"print('ABS-READ', p.read_text().strip() if p.exists() else 'hidden')\n"
             f"pathlib.Path('written.txt').write_text('ok')\n"
             f"subprocess.Popen([sys.executable, '-c', {late!r}], start_new_session=True)\n")
    workspace = fake_hermes / ".hermes" / "plugin-data" / "baton" / "work"  # the default layout
    bid, settings, store = received(tmp_path, [sys.executable, "probe.py"], {"probe.py": probe}, workspace=workspace)
    out = remote.check(bid, settings, store)
    assert out["sandbox"] == "bwrap" and out["passed"], out["tail"]
    assert "ABS-READ hidden" in out["tail"] and (settings.worktree(bid) / "written.txt").read_text() == "ok"
    time.sleep(2.5)
    assert not marker.exists()  # the setsid child died with the sandbox pid namespace


def test_sandbox_off_when_the_command_lives_in_the_hermes_home(tmp_path):
    hidden = tmp_path / ".hermes"
    (hidden / "venv" / "bin").mkdir(parents=True)
    exe = hidden / "venv" / "bin" / "python"
    exe.symlink_to(sys.executable)
    cmd, note = remote.sandboxed([str(exe), "-m", "pytest"], {"PATH": os.defpath}, [hidden], [tmp_path / "work"])
    assert cmd == [str(exe), "-m", "pytest"] and note.startswith("off")


# P02c: deadline ----------------------------------------------------------------------------------

def test_run_check_deadline_with_grandchild_holding_stdout(tmp_path):
    pidfile = tmp_path / "pid"
    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'], start_new_session=True); "
            f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(30)")
    start = time.monotonic()
    rc, out, timed_out = remote.run_check([sys.executable, "-c", code], tmp_path, 1, remote.check_env(tmp_path))
    elapsed = time.monotonic() - start
    os.kill(int(pidfile.read_text()), signal.SIGKILL)  # escaped the group with setsid: clean up here
    assert timed_out and rc is None and elapsed < 2.5 and b"timed out" in out


def test_run_check_kills_leftover_group_members(tmp_path):
    pidfile = tmp_path / "pid"
    code = ("import subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            f"open({str(pidfile)!r}, 'w').write(str(p.pid)); print('done')")
    rc, out, timed_out = remote.run_check([sys.executable, "-c", code], tmp_path, 10, remote.check_env(tmp_path))
    assert rc == 0 and not timed_out and b"done" in out
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


# remote receive path ------------------------------------------------------------------------------

def test_receive_refuses_symlinks_out_of_the_worktree(tmp_path):
    secret = tmp_path / "remote-secret"
    secret.write_text("dummy-not-a-real-key\n")
    links = {"notes.md": str(secret), "up": "../x", "deep/l": "..", "m": "deep/l/..", "readme": "a.py"}
    with pytest.raises(BatonError) as exc:
        received(tmp_path, [sys.executable], links=links)
    assert exc.value.code == "unsafe_snapshot"
    assert sorted(exc.value.details["symlinks"]) == ["m", "notes.md", "up"]  # deep/l and readme stay inside
    assert not list((tmp_path / "work").glob("baton-*"))  # the worktree was removed


def test_receive_keeps_inner_symlinks(tmp_path):
    bid, settings, _store = received(tmp_path, [sys.executable], links={"readme": "a.py", "deep/l": ".."})
    assert (settings.worktree(bid) / "readme").read_text() == "x = 1\n"


def test_return_after_the_sender_cancelled_is_refused(tmp_path):
    bid, settings, store = received(tmp_path, [sys.executable, "-c", "pass"],
                                    allowed_checks=[[sys.executable, "-c"]])
    local.cancel(bid, store=BatonStore(tmp_path / "local"))
    with pytest.raises(BatonError) as exc:
        remote.return_baton(bid, "done anyway", "failed", settings, store)
    assert exc.value.code == "baton_cancelled"
    assert gitutil.read_json_ref(settings.remote_git, refs.status_ref(bid), "status.json")["state"] == "CANCELLED"
    assert gitutil.ref_sha(settings.remote_git, refs.result_ref(bid)) is None
    with pytest.raises(BatonError) as exc:
        remote.check(bid, settings, store)
    assert exc.value.code == "baton_cancelled"
    assert remote.pre_verify(store, changed_paths=[str(settings.worktree(bid) / "a.py")]) is None
