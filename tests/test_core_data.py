from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from baton_plugin import ids, manifest, peers, transcript  # noqa: E402
from baton_plugin.errors import BatonError  # noqa: E402
from baton_plugin.store import BatonRecord, BatonStore, can_transition  # noqa: E402

GHP = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


# manifest ----------------------------------------------------------------------------------------
def _manifest(bid: str, transcript_bytes: bytes = b"# t\n") -> dict:
    return manifest.build(
        baton_id=bid, peer="box", repo_name="r", head="a" * 40, branch="main", project_commit="b" * 40,
        task="fix it", done_when_argv=["python", "-m", "pytest", "-q"], transcript=transcript_bytes,
        cli_redact=True, extra_hits=0)


def test_manifest_shape():
    bid = ids.new_id()
    m = _manifest(bid)
    assert m["schema"] == 1 and m["return"] == {"branch": f"baton/{bid}", "result_ref": f"refs/baton/{bid}/result"}
    assert m["done_when"] == {"argv": ["python", "-m", "pytest", "-q"], "timeout_s": 900, "expect_exit": 0}
    assert m["transcript"]["path"] == f".baton/{bid}/transcript.md" and m["ttl_hours"] == 24
    assert m["created_at"].endswith("Z")


def test_manifest_roundtrip_verifies():
    bid = ids.new_id()
    files = manifest.package_files(_manifest(bid), b"# t\n")
    assert manifest.verify(files, bid) == []
    assert manifest.parse(files["BATON.json"])["id"] == bid


def test_manifest_detects_tampered_transcript():
    bid = ids.new_id()
    files = manifest.package_files(_manifest(bid), b"# t\n")
    files["transcript.md"] = b"# evil\n"
    assert any("sha256 mismatch: transcript.md" in p for p in manifest.verify(files, bid))


def test_manifest_detects_tampered_argv_even_with_new_sums():
    bid = ids.new_id()
    m = _manifest(bid)
    m["done_when"]["argv"] = "rm -rf /"
    files = manifest.package_files(m, b"# t\n")
    assert any("argv" in p for p in manifest.verify(files, bid))


def test_manifest_missing_file_and_schema():
    bid = ids.new_id()
    files = manifest.package_files(_manifest(bid), b"# t\n")
    del files["SHA256SUMS"]
    assert manifest.verify(files, bid) == ["missing file: SHA256SUMS"]
    m = _manifest(bid)
    m["schema"] = 2
    assert any("schema" in p for p in manifest.verify(manifest.package_files(m, b"# t\n"), bid))


def test_manifest_id_mismatch():
    bid = ids.new_id()
    files = manifest.package_files(_manifest(bid), b"# t\n")
    assert any("id mismatch" in p for p in manifest.verify(files, ids.new_id()))


# transcript --------------------------------------------------------------------------------------
def _fake_runner(md: str, code: int = 0):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out_dir = Path(argv[argv.index("export") + 1])
        if code == 0:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "s.md").write_text(md)
        return subprocess.CompletedProcess(argv, code, b"", b"Session not found" if code else b"")
    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_transcript_without_session_is_task_only():
    res = transcript.export(None, task="fix the parser")
    assert res.text.startswith("# Baton task") and "fix the parser" in res.text
    assert res.cli_redact is False and res.extra_hits == 0 and res.bytes == len(res.data)


def test_transcript_cli_argv_and_redaction():
    md = f"## Messages\n### User\nuse {GHP}\n### Assistant\nok\n"
    runner = _fake_runner(md)
    res = transcript.export("sess1", ["hermes"], task="t", runner=runner)
    argv = runner.calls[0]
    assert argv[:3] == ["hermes", "sessions", "export"] and argv[-6:] == [
        "--session-id", "sess1", "--format", "md", "--redact"][-6:] or "--redact" in argv
    assert "--redact" in argv and "--session-id" in argv and argv[argv.index("--format") + 1] == "md"
    assert res.cli_redact and res.extra_hits == 1 and GHP not in res.text and "[REDACTED]" in res.text


def test_transcript_missing_session_degrades():
    res = transcript.export("nope", ["hermes"], task="the task", runner=_fake_runner("", code=1))
    assert "the task" in res.text and not res.cli_redact and "unavailable" in res.note


def test_transcript_keeps_last_turns_and_cuts_tools():
    parts = ["## Messages\n", "### System\nsecret system prompt\n"]
    for i in range(30):
        parts.append(f"### User\nquestion {i}\n")
        parts.append("### Tool \u2192 terminal\n" + "o" * 5000 + "\n")
        parts.append(f"### Assistant\nanswer {i}\n")
    out = transcript.trim("".join(parts))
    assert "question 29" in out and "answer 29" in out and "question 5\n" not in out
    assert "system prompt" not in out and "bytes cut" in out
    assert out.count("### User") + out.count("### Assistant") == 20


def test_transcript_cap():
    big = "## Messages\n" + "".join(f"### User\n{'z' * 3000}\n" for _ in range(20)) * 3
    res = transcript.export("s", ["h"], task="keep me", runner=_fake_runner(big), cap=8 * 1024)
    assert res.bytes <= 8 * 1024 + 64 and "keep me" in res.text and "size cap" in res.text


# peers -------------------------------------------------------------------------------------------
CFG = {"bot_peers": {"box": {"url": "http://box.lan:8642/", "note": "vps"}, "my-vps": {"url": "https://v.example"}}}


def test_peer_key_env_name_matches_hermes():
    assert peers.key_env_name("my-vps") == "HERMES_PEER_MY_VPS_KEY"


def test_peer_from_bot_peers_record():
    p = peers.resolve("box", CFG, env={"HERMES_PEER_BOX_KEY": " sekrit-key-value "})
    assert (p.name, p.url, p.key, p.source) == ("box", "http://box.lan:8642", "sekrit-key-value", "bot_peers")
    assert "sekrit" not in repr(p)


def test_peer_secret_getter_wins():
    p = peers.resolve("my-vps", CFG, env={}, secret_getter=lambda n: "from-scope" if n == "HERMES_PEER_MY_VPS_KEY" else "")
    assert p.key == "from-scope"


def test_peer_profile_target():
    p = peers.resolve("box/Coder", CFG, env={"HERMES_PEER_BOX_KEY": "k"})
    assert p.url == "http://box.lan:8642/p/coder"


def test_peer_missing_key():
    with pytest.raises(BatonError) as exc:
        peers.resolve("box", CFG, env={})
    assert exc.value.code == "peer_key_missing" and "HERMES_PEER_BOX_KEY" in exc.value.message


def test_peer_unknown_and_settings_fallback():
    with pytest.raises(BatonError) as exc:
        peers.resolve("ghost", CFG, env={})
    assert exc.value.code == "peer_unknown"
    settings = {"peers": {"ghost": {"url": "http://127.0.0.1:9", "key_env": "GHOST_KEY"}}}
    p = peers.resolve("ghost", CFG, settings, env={"GHOST_KEY": "g"})
    assert p.source == "settings" and p.key == "g"


def test_peer_invalid_name_or_url():
    with pytest.raises(BatonError):
        peers.resolve("../x", CFG, env={})
    with pytest.raises(BatonError):
        peers.resolve("f", {"bot_peers": {"f": {"url": "file:///etc"}}}, env={"HERMES_PEER_F_KEY": "k"})


# store -------------------------------------------------------------------------------------------
def _record(bid: str, state: str = "PREPARED") -> BatonRecord:
    return BatonRecord(id=bid, state=state, repo="/r", peer="box", task="t", done_when={"argv": ["pytest"]},
                       remote_git="/g")


def test_store_roundtrip_and_list(tmp_path):
    store = BatonStore(tmp_path)
    a, b = ids.new_id(now_ms=1), ids.new_id(now_ms=2)
    store.save(_record(b))
    store.save(_record(a))
    assert [r.id for r in store.list()] == [a, b]
    assert store.get(a).task == "t" and store.get("bad id") is None
    raw = json.loads((tmp_path / "batons" / f"{a}.json").read_text())
    assert raw["state"] == "PREPARED"


@pytest.mark.parametrize("path", [
    ["PUSHED", "TRIGGERED", "RECEIVED", "RUNNING", "VERIFIED", "RETURNED", "LANDED"],
    ["PUSHED", "RETURNED", "LANDED"],
    ["PUSHED", "TRIGGERED", "FAILED"],
    ["CANCELLED"],
])
def test_store_legal_paths(tmp_path, path):
    store = BatonStore(tmp_path)
    rec = store.save(_record(ids.new_id()))
    for state in path:
        rec = store.transition(rec, state)
    assert rec.state == path[-1] and [h["state"] for h in rec.history] == path


@pytest.mark.parametrize("start,target", [
    ("PREPARED", "LANDED"), ("LANDED", "RUNNING"), ("CANCELLED", "PUSHED"), ("RETURNED", "RUNNING"),
    ("EXPIRED", "RETURNED"), ("PUSHED", "PREPARED"),
])
def test_store_illegal_transitions(tmp_path, start, target):
    store = BatonStore(tmp_path)
    rec = store.save(_record(ids.new_id(), start))
    with pytest.raises(BatonError) as exc:
        store.transition(rec, target)
    assert exc.value.code == "illegal_transition"
    assert store.get(rec.id).state == start


def test_store_same_state_is_noop():
    assert can_transition("RUNNING", "RUNNING")


def test_store_remote_records(tmp_path):
    store = BatonStore(tmp_path)
    bid = ids.new_id()
    store.save_remote(bid, {"id": bid, "state": "RUNNING"})
    assert store.get_remote(bid)["state"] == "RUNNING" and len(store.list_remote()) == 1


def test_store_require_unknown(tmp_path):
    with pytest.raises(BatonError) as exc:
        BatonStore(tmp_path).require(ids.new_id())
    assert exc.value.code == "unknown_baton"
