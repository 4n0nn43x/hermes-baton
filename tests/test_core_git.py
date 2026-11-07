from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from helpers import git, make_repo, snapshot  # noqa: E402

from baton_plugin import capture, gitutil, ids  # noqa: E402
from baton_plugin.capture import ghost_commit  # noqa: E402
from baton_plugin.errors import BatonError  # noqa: E402


# ids ---------------------------------------------------------------------------------------------
def test_id_shape_and_validity():
    bid = ids.new_id()
    assert len(bid) == 26 and ids.is_valid(bid)
    assert not ids.is_valid("01ARZ3NDEKTSV4RRFFQ69G5FAI")  # I is not Crockford
    assert not ids.is_valid("short")


def test_ids_sort_by_time():
    a = ids.new_id(now_ms=1_000, randomness=b"\xff" * 10)
    b = ids.new_id(now_ms=2_000, randomness=b"\x00" * 10)
    assert a < b
    assert ids.timestamp_ms(b) == 2_000


def test_id_deterministic_inputs():
    assert ids.new_id(5, b"\x01" * 10) == ids.new_id(5, b"\x01" * 10)


def test_require_valid_rejects_path_tricks():
    with pytest.raises(ValueError):
        ids.require_valid("../../etc/passwd")


def test_ids_are_unique():
    assert len({ids.new_id() for _ in range(200)}) == 200


# gitutil -----------------------------------------------------------------------------------------
def test_git_env_is_sanitized(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("GIT_INDEX_FILE", "/elsewhere/index")
    env = gitutil.git_env()
    assert "GIT_DIR" not in env and "GIT_INDEX_FILE" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_AUTHOR_NAME"] == "baton"


def test_run_git_failure_is_baton_error(tmp_path):
    with pytest.raises(BatonError) as exc:
        gitutil.run_git(["rev-parse", "HEAD"], tmp_path)
    assert exc.value.code == "git_failed"


def test_commit_files_leaves_index_alone(tmp_path):
    repo = make_repo(tmp_path / "r")
    before = snapshot(repo)
    sha = gitutil.commit_files(repo, {"x/y.json": b"{}"}, "m", base="HEAD", parents=[gitutil.rev_parse(repo, "HEAD")])
    assert gitutil.show_file(repo, sha, "x/y.json") == b"{}"
    assert gitutil.show_file(repo, sha, "a.py") == b"x = 1\n"
    assert snapshot(repo) == before


# capture -----------------------------------------------------------------------------------------
def _dirty(repo: Path) -> None:
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.txt").write_text("hello\n")
    (repo / ".gitignore").write_text("ignored.log\n")
    (repo / "ignored.log").write_text("noise\n")


def test_capture_clean_repo(tmp_path):
    repo = make_repo(tmp_path / "r")
    res = ghost_commit(repo, "ID")
    assert res.files == ["a.py"] and res.untracked_included == [] and res.changed == []
    assert git(repo, "rev-parse", f"{res.project_commit}^") == git(repo, "rev-parse", "HEAD")
    assert git(repo, "log", "-1", "--format=%s", res.project_commit) == "baton: snapshot ID"


def test_capture_dirty_untracked_ignored(tmp_path):
    repo = make_repo(tmp_path / "r")
    _dirty(repo)
    res = ghost_commit(repo)
    assert sorted(res.files) == [".gitignore", "a.py", "new.txt"]
    assert sorted(res.untracked_included) == [".gitignore", "new.txt"]
    assert gitutil.show_file(repo, res.project_commit, "a.py") == b"x = 2\n"
    assert "ignored.log" not in res.files


def test_capture_is_byte_identical(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "x = 1\n", "b.py": "y = 1\n"})
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "stash", "-q")
    _dirty(repo)
    (repo / "a.py").write_text("x = 3\n")
    git(repo, "add", "a.py")
    (repo / "a.py").write_text("x = 4\n")  # staged and unstaged versions differ
    before = snapshot(repo)
    res = ghost_commit(repo)
    assert gitutil.show_file(repo, res.project_commit, "a.py") == b"x = 4\n"
    assert snapshot(repo) == before
    assert git(repo, "stash", "list").count("\n") == 0 and git(repo, "stash", "list")


def test_capture_staged_deletion(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "1\n", "gone.py": "2\n"})
    (repo / "gone.py").unlink()
    res = ghost_commit(repo)
    assert "gone.py" not in res.files


def test_capture_unborn_branch(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "1\n"}, commit=False)
    before = snapshot(repo)
    res = ghost_commit(repo)
    assert res.head is None and res.files == ["a.py"]
    assert git(repo, "log", "--format=%P", "-1", res.project_commit) == ""
    assert snapshot(repo) == before


def test_capture_branch_name(tmp_path):
    repo = make_repo(tmp_path / "r")
    git(repo, "checkout", "-qb", "feature/x")
    assert ghost_commit(repo).branch == "feature/x"


def test_capture_detached_head(tmp_path):
    repo = make_repo(tmp_path / "r")
    git(repo, "checkout", "-q", "--detach")
    res = ghost_commit(repo)
    assert res.branch is None and res.head


def test_capture_subdirectory_path(tmp_path):
    repo = make_repo(tmp_path / "r", {"pkg/m.py": "1\n"})
    res = ghost_commit(repo / "pkg")
    assert res.files == ["pkg/m.py"]


def test_capture_not_a_repo(tmp_path):
    with pytest.raises(BatonError) as exc:
        ghost_commit(tmp_path)
    assert exc.value.code == "not_a_repo"


def test_capture_submodule_refused(tmp_path):
    repo = make_repo(tmp_path / "r")
    (repo / ".gitmodules").write_text('[submodule "s"]\n\tpath = s\n\turl = ../s\n')
    with pytest.raises(BatonError) as exc:
        ghost_commit(repo)
    assert exc.value.code == "submodules_unsupported"


def test_capture_nested_repo_refused(tmp_path):
    repo = make_repo(tmp_path / "r")
    make_repo(repo / "inner")
    before = snapshot(repo)
    with pytest.raises(BatonError) as exc:
        ghost_commit(repo)
    assert exc.value.code == "submodules_unsupported"
    assert snapshot(repo)["index"] == before["index"]


def test_package_commit_adds_only_baton_dir(tmp_path):
    repo = make_repo(tmp_path / "r")
    res = ghost_commit(repo)
    bid = ids.new_id()
    pkg = capture.package_commit(repo, res.project_commit, bid, {"BATON.json": b"{}", "transcript.md": b"t"})
    changed = git(repo, "diff-tree", "-r", "--name-only", "--no-commit-id", res.project_commit, pkg).splitlines()
    assert sorted(changed) == [f".baton/{bid}/BATON.json", f".baton/{bid}/transcript.md"]
    assert not (repo / ".baton").exists()
