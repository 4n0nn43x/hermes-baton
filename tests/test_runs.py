from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fakegateway import KEY, FakeGateway  # noqa: E402

from baton_plugin.errors import BatonError  # noqa: E402
from baton_plugin.runs import RunsClient, build_prompt  # noqa: E402


@pytest.fixture
def gw():
    g = FakeGateway().start()
    yield g
    g.stop()


def test_capabilities_ok(gw):
    caps = RunsClient(gw.url, KEY).capabilities()
    assert caps["features"]["runs_idempotency"]["supported"] is True
    assert gw.requests[-1]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_capabilities_without_idempotency_refused():
    g = FakeGateway(idempotency=False).start()
    try:
        with pytest.raises(BatonError) as exc:
            RunsClient(g.url, KEY).capabilities()
        assert exc.value.code == "run_rejected"
    finally:
        g.stop()


def test_auth_failed_401(gw):
    with pytest.raises(BatonError) as exc:
        RunsClient(gw.url, "wrong-key-wrong-key").capabilities()
    assert exc.value.code == "auth_failed" and "wrong-key" not in str(exc.value)


def test_create_session_body_and_id(gw):
    sid = RunsClient(gw.url, KEY).create_session("Baton X", session_id="baton-x")
    assert sid == "baton-x"
    body = gw.requests[-1]["body"]
    assert body == {"title": "Baton X", "source": "api_server", "id": "baton-x"}


def test_create_session_retry_reuses_existing(gw):
    c = RunsClient(gw.url, KEY)
    assert c.create_session("Baton X", session_id="baton-x") == c.create_session("Baton X", session_id="baton-x")


def test_create_session_generated_id(gw):
    assert RunsClient(gw.url, KEY).create_session("t").startswith("api_")


def test_start_run_then_replay(gw):
    c = RunsClient(gw.url, KEY)
    first = c.start_run("do it", "01KEY", session_id="s")
    again = c.start_run("do it", "01KEY", session_id="s")
    assert first["replayed"] is False and again["replayed"] is True and first["run_id"] == again["run_id"]
    req = gw.requests[-1]
    assert req["headers"]["Idempotency-Key"] == "01KEY" and req["body"] == {"input": "do it", "session_id": "s"}


def test_idempotency_conflict_409(gw):
    c = RunsClient(gw.url, KEY)
    c.start_run("one", "K2")
    with pytest.raises(BatonError) as exc:
        c.start_run("two", "K2")
    assert exc.value.code == "idempotency_conflict"


def test_busy_peer_maps_to_unreachable(gw):
    gw.fail_runs_with = 429
    with pytest.raises(BatonError) as exc:
        RunsClient(gw.url, KEY).start_run("x", "K3")
    assert exc.value.code == "peer_unreachable"


def test_get_run_and_not_found(gw):
    c = RunsClient(gw.url, KEY)
    run = c.start_run("x", "K4")
    assert c.get_run(run["run_id"])["status"] == "running"
    with pytest.raises(BatonError) as exc:
        c.get_run("run_missing")
    assert exc.value.code == "run_rejected"


def test_resolve_unknown(gw):
    c = RunsClient(gw.url, KEY)
    run_id = c.start_run("x", "K5")["run_id"]
    gw.runs[run_id].update(status="unknown", admission_id="adm_1", execution_generation=7)
    out = c.resolve_unknown(run_id)
    assert out["outcome"] == "interrupted"
    assert gw.requests[-1]["body"] == {"admission_id": "adm_1", "execution_generation": 7}


def test_unreachable_peer():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(BatonError) as exc:
        RunsClient(f"http://127.0.0.1:{port}", KEY, timeout=3).capabilities()
    assert exc.value.code == "peer_unreachable"


def test_cross_origin_redirect_refused(gw):
    other = FakeGateway().start()
    try:
        gw.redirect_to = other.url + "/v1/capabilities"
        c = RunsClient(gw.url, KEY)
        with pytest.raises(BatonError) as exc:
            c._request("GET", "/redirect")
        assert exc.value.code == "bad_response"
        assert other.requests == []  # the key never reached the other origin
    finally:
        other.stop()


def test_same_origin_redirect_followed(gw):
    gw.redirect_to = gw.url + "/health"
    status, _, payload = RunsClient(gw.url, KEY)._request("GET", "/redirect")
    assert status == 200 and payload == {"status": "ok"}


def test_client_repr_hides_key():
    assert KEY not in repr(RunsClient("http://x", KEY))


def test_prompt_template():
    p = build_prompt("01ID", "fix the parser", ["python", "-m", "pytest", "-q"])
    assert "baton:baton-protocol" in p and "baton_receive" in p and "baton_check" in p
    assert "baton_return" in p and "failed" in p and "fix the parser" in p and "python -m pytest -q" in p
