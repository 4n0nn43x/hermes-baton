"""HTTP client for a remote Hermes gateway API server (Runs API), stdlib urllib only.

Verified against Hermes v0.23.0: gateway/platforms/api_server.py (sessions, capabilities, auth) and
gateway/platforms/api_server_runs.py (runs, idempotency). The bearer key is only ever sent to the
configured origin: a redirect to another origin is refused.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import BatonError

USER_AGENT = "hermes-baton"


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urllib.parse.urlsplit(url)
    port = p.port or {"http": 80, "https": 443}.get(p.scheme)
    return p.scheme, (p.hostname or "").lower(), port


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: tuple) -> None:
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != self.origin:
            raise BatonError("bad_response", "the peer redirected to another origin; refused (key not sent)")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_prompt(baton_id: str, task: str, done_when_argv: list[str]) -> str:
    """The fixed English instruction sent as the run input. Contains the task, never secrets."""
    check = " ".join(done_when_argv)
    return (
        f"You received baton {baton_id}, a coding task handed over from another machine.\n"
        "Load the `baton:baton-protocol` skill and follow it exactly:\n"
        f"1. Call `baton_receive` with id `{baton_id}`; it returns a worktree path, the task and the check.\n"
        "2. Work only inside that worktree.\n"
        f"3. Call `baton_check` (it runs `{check}`) until it passes.\n"
        f"4. Finish with `baton_return` (id `{baton_id}`, a short summary, status `verified`). If you cannot make "
        "the check pass, call `baton_return` with status `failed` and the reason.\n\n"
        f"Task:\n{task.strip()}\n"
    )


class RunsClient:
    def __init__(self, url: str, key: str, timeout: float = 30) -> None:
        self.base = url.rstrip("/")
        self._key = key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SameOriginRedirects(_origin(self.base)))

    def __repr__(self) -> str:  # never show the key
        return f"RunsClient({self.base!r})"

    def _request(self, method: str, path: str, body: dict | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = {"Authorization": f"Bearer {self._key}", "Accept": "application/json", "User-Agent": USER_AGENT}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status, resp_headers, raw = resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:
            status, resp_headers, raw = exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise BatonError("peer_unreachable", f"cannot reach {self.base}: {reason}") from None
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = None
        return status, {k.lower(): v for k, v in resp_headers.items()}, payload

    @staticmethod
    def _error_code(payload: Any) -> str:
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return str(payload["error"].get("code") or "")
        return ""

    @staticmethod
    def _error_message(payload: Any) -> str:
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return str(payload["error"].get("message") or "")[:300]
        return ""

    def _raise_for(self, status: int, payload: Any, what: str) -> None:
        code, msg = self._error_code(payload), self._error_message(payload)
        detail = f"{what}: HTTP {status}" + (f" {code}" if code else "") + (f": {msg}" if msg else "")
        if status in (401, 403):
            raise BatonError("auth_failed", f"{what}: the peer refused the API key (HTTP {status})")
        if status == 409 and code == "idempotency_key_conflict":
            raise BatonError("idempotency_conflict", detail)
        if status in (429, 502, 503, 504):
            raise BatonError("peer_unreachable", detail + " (retry later)")
        if 400 <= status < 500:
            raise BatonError("run_rejected", detail)
        raise BatonError("bad_response", detail)

    def _json(self, method: str, path: str, what: str, body: dict | None = None,
              ok: tuple[int, ...] = (200,)) -> tuple[int, dict, dict]:
        status, headers, payload = self._request(method, path, body)
        if status not in ok:
            self._raise_for(status, payload, what)
        if not isinstance(payload, dict):
            raise BatonError("bad_response", f"{what}: the peer did not return a JSON object")
        return status, headers, payload

    def capabilities(self) -> dict:
        _, _, caps = self._json("GET", "/v1/capabilities", "capabilities")
        feature = (caps.get("features") or {}).get("runs_idempotency")
        if not isinstance(feature, dict) or not feature.get("supported"):
            raise BatonError("run_rejected", "the peer does not support idempotent runs (Hermes >= 0.23.0 needed)")
        return caps

    def create_session(self, title: str, session_id: str | None = None) -> str:
        """POST /api/sessions. With an explicit id the call is retry-safe: 409 session_exists = reuse."""
        body: dict[str, Any] = {"title": title, "source": "api_server"}
        if session_id:
            body["id"] = session_id
        status, _, payload = self._request("POST", "/api/sessions", body)
        if status == 409 and session_id and self._error_code(payload) == "session_exists":
            return session_id
        if status not in (200, 201):
            self._raise_for(status, payload, "create session")
        if not isinstance(payload, dict):
            raise BatonError("bad_response", "create session: non-JSON response")
        row = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        sid = str(row.get("id") or row.get("session_id") or "")
        if not sid:
            raise BatonError("bad_response", "create session: no session id in the response")
        return sid

    def start_run(self, prompt: str, idempotency_key: str, session_id: str | None = None) -> dict:
        body: dict[str, Any] = {"input": prompt}
        if session_id:
            body["session_id"] = session_id
        status, headers, payload = self._request("POST", "/v1/runs", body, {"Idempotency-Key": idempotency_key})
        if status not in (200, 202):
            self._raise_for(status, payload, "start run")
        if not isinstance(payload, dict) or not payload.get("run_id"):
            raise BatonError("bad_response", "start run: no run_id in the response")
        replayed = headers.get("idempotency-replayed", "").lower() == "true" or payload.get("replayed") is True
        return {"run_id": str(payload["run_id"]), "status": payload.get("status", ""), "replayed": replayed}

    def get_run(self, run_id: str) -> dict:
        path = f"/v1/runs/{urllib.parse.quote(run_id, safe='')}"
        return self._json("GET", path, "get run")[2]

    def stop_run(self, run_id: str) -> dict:
        path = f"/v1/runs/{urllib.parse.quote(run_id, safe='')}/stop"
        return self._json("POST", path, "stop run", body={})[2]

    def resolve_unknown(self, run_id: str) -> dict:
        """Acknowledge a run left `unknown` by a peer restart, so the session can take new work."""
        run = self.get_run(run_id)
        if run.get("status") != "unknown":
            return run
        body = {"admission_id": run.get("admission_id"), "execution_generation": run.get("execution_generation")}
        path = f"/v1/runs/{urllib.parse.quote(run_id, safe='')}/resolve-unknown"
        return self._json("POST", path, "resolve unknown run", body=body)[2]
