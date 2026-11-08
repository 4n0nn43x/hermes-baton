"""Review follow-ups applied after the judge run: send-side symlink refusal, corrupt registry records,
and the manifest's expect_exit type."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from helpers import make_repo, snapshot  # noqa: E402

from baton_plugin import capture, ids, manifest  # noqa: E402
from baton_plugin.errors import BatonError  # noqa: E402
from baton_plugin.store import BatonRecord, BatonStore  # noqa: E402


@pytest.mark.parametrize("target", ["/etc/passwd", "../outside.txt", "loop"])
def test_capture_refuses_symlinks_leaving_the_repo(tmp_path, target):
    repo = make_repo(tmp_path / "r", {"a.py": "x = 1\n"})
    (tmp_path / "outside.txt").write_text("secret\n")
    os.symlink(target, repo / ("loop" if target == "loop" else "link"))
    before = snapshot(repo)
    with pytest.raises(BatonError) as exc:
        capture.ghost_commit(repo, ids.new_id())
    assert exc.value.code == "unsafe_snapshot"
    assert snapshot(repo) == before


def test_capture_keeps_symlinks_inside_the_repo(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
    os.symlink("a.py", repo / "alias.py")
    os.symlink("../a.py", repo / "pkg" / "up.py")
    cap = capture.ghost_commit(repo, ids.new_id())
    assert {"alias.py", "pkg/up.py"} <= set(cap.files)


def test_store_get_corrupt_local_record_is_registry_corrupt(tmp_path):
    store = BatonStore(tmp_path)
    bid = ids.new_id()
    store.save(BatonRecord(id=bid, state="PREPARED", repo="/r", peer="p", task="t", done_when={},
                           remote_git="/g"))
    (tmp_path / "batons" / f"{bid}.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BatonError) as exc:
        store.get(bid)
    assert exc.value.code == "registry_corrupt"
    assert store.get("not-an-id") is None  # an invalid id still reads as absent


def test_store_get_remote_corrupt_record_is_registry_corrupt(tmp_path):
    store = BatonStore(tmp_path)
    bid, other = ids.new_id(), ids.new_id()
    store.save_remote(bid, {"id": bid})
    (tmp_path / "remote" / f"{bid}.json").write_text("{truncated", encoding="utf-8")
    store.save_remote(other, {"id": other})
    (tmp_path / "remote" / f"{other}.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    for baton_id in (bid, other):
        with pytest.raises(BatonError) as exc:
            store.get_remote(baton_id)
        assert exc.value.code == "registry_corrupt"


@pytest.mark.parametrize("value", ["0", 0.0, None, True])
def test_manifest_expect_exit_must_be_an_integer(value):
    bid = ids.new_id()
    m = manifest.build(baton_id=bid, peer="box", repo_name="r", head="a" * 40, branch="main",
                       project_commit="b" * 40, task="t", done_when_argv=["pytest"], transcript=b"# t\n",
                       cli_redact=True, extra_hits=0)
    assert manifest.verify(manifest.package_files(m, b"# t\n"), bid) == []
    m["done_when"]["expect_exit"] = value
    problems = manifest.verify(manifest.package_files(m, b"# t\n"), bid)
    assert "done_when.expect_exit must be an integer" in problems


def test_sandbox_setting_reaches_the_remote_settings(tmp_path, monkeypatch):
    import baton_plugin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    class Ctx:
        def __init__(self, settings):
            self.settings = settings

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

    assert "sandbox" in baton_plugin.SETTING_KEYS
    assert baton_plugin.Baton(Ctx({"sandbox": "off"})).remote_settings().sandbox == "off"
    assert baton_plugin.Baton(Ctx({})).remote_settings().sandbox == "auto"


def test_check_path_puts_the_hermes_tool_store_last(tmp_path, monkeypatch):
    """Inside a Hermes process PATH starts with Hermes's own tools (its Python has no pytest):
    the real judge run got `No module named pytest` for `python -m pytest -q`."""
    from baton_plugin import remote

    home = tmp_path / "hermes-home"
    tools = home / "tools" / "python-3.14" / "bin"
    tools.mkdir(parents=True)
    user = tmp_path / "project-venv" / "bin"
    user.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), str(user), "/usr/bin"]))
    path = remote.check_env(tmp_path / "throwaway")["PATH"].split(os.pathsep)
    assert path[:2] == [str(user), "/usr/bin"] and path[-1] == str(tools)
    own_python = os.path.dirname(os.path.abspath(sys.executable))
    monkeypatch.setenv("PATH", os.pathsep.join([own_python, str(user)]))
    assert remote.check_env(tmp_path / "throwaway")["PATH"].split(os.pathsep) == [str(user), own_python]


def test_check_path_keeps_user_dirs_when_the_hermes_home_is_home(tmp_path, monkeypatch):
    """The official Docker image sets HERMES_HOME=/opt/data, which is also the hermes user's HOME: a
    venv or ~/.local/bin there belongs to the user. The first Docker judge run got
    `/usr/bin/python: No module named pytest` because /opt/data/checkenv/bin was moved last."""
    from baton_plugin import remote

    home = tmp_path / "data"
    venv, local_bin = home / "checkenv" / "bin", home / ".local" / "bin"
    tools = home / "tools" / "python-3.14" / "bin"
    for d in (venv, local_bin, tools):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), str(venv), str(local_bin), "/usr/bin"]))
    path = remote.check_env(tmp_path / "throwaway")["PATH"].split(os.pathsep)
    assert path == [str(venv), str(local_bin), "/usr/bin", str(tools)]
