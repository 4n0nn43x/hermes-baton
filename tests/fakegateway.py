"""A fake Hermes gateway Runs API (aiohttp) on 127.0.0.1, run in a background thread.

Shapes follow Hermes v0.23.0 (gateway/platforms/api_server.py, api_server_runs.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid

from aiohttp import web

KEY = "k" * 32


def _err(message: str, status: int, code: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response({"error": {"message": message, "type": err_type, "param": None, "code": code}},
                             status=status)


class FakeGateway:
    def __init__(self, key: str = KEY, idempotency: bool = True) -> None:
        self.key = key
        self.idempotency = idempotency
        self.requests: list[dict] = []
        self.sessions: dict[str, dict] = {}
        self.idem: dict[str, tuple[str, str]] = {}
        self.runs: dict[str, dict] = {}
        self.redirect_to = ""
        self.fail_runs_with: int | None = None
        self._loop = asyncio.new_event_loop()
        self._runner: web.AppRunner | None = None
        self.port = 0

    def _authed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {self.key}"

    async def _log(self, request: web.Request) -> dict:
        body = await request.read()
        entry = {"method": request.method, "path": request.path, "headers": dict(request.headers),
                 "body": json.loads(body) if body else None}
        self.requests.append(entry)
        return entry

    async def handle(self, request: web.Request) -> web.StreamResponse:
        entry = await self._log(request)
        path = request.path
        if path == "/redirect":
            raise web.HTTPFound(self.redirect_to)
        if path == "/health":
            return web.json_response({"status": "ok"})
        if not self._authed(request):
            return web.json_response({"error": {"message": "Invalid gateway API key (API_SERVER_KEY)",
                                                "type": "gateway_auth_error", "code": "gateway_auth_failed"}},
                                     status=401)
        if path == "/v1/capabilities":
            features = {"run_submission": True}
            if self.idempotency:
                features["runs_idempotency"] = {"supported": True, "durable": True, "retention_seconds": 86400}
            return web.json_response({"object": "hermes.api_server.capabilities", "features": features})
        if path == "/api/sessions" and request.method == "POST":
            body = entry["body"] or {}
            sid = body.get("id") or f"api_{uuid.uuid4().hex[:8]}"
            if sid in self.sessions:
                return _err(f"Session already exists: {sid}", 409, "session_exists")
            self.sessions[sid] = {"id": sid, "title": body.get("title"), "source": body.get("source")}
            return web.json_response({"object": "hermes.session", "session": self.sessions[sid]}, status=201)
        if path == "/v1/runs" and request.method == "POST":
            if self.fail_runs_with:
                return _err("Too many concurrent runs (max 1)", self.fail_runs_with, "rate_limited")
            body = entry["body"] or {}
            if not body.get("input"):
                return _err("Missing 'input' field", 400, None)
            key = request.headers.get("Idempotency-Key", "")
            fp = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if key and key in self.idem:
                old_fp, run_id = self.idem[key]
                if old_fp != fp:
                    return _err("Idempotency-Key was already used with a different request payload", 409,
                                "idempotency_key_conflict")
                return web.json_response({"run_id": run_id, "status": "queued", "replayed": True}, status=202,
                                         headers={"Idempotency-Replayed": "true"})
            run_id = f"run_{uuid.uuid4().hex}"
            if key:
                self.idem[key] = (fp, run_id)
            self.runs[run_id] = {"object": "hermes.run", "run_id": run_id, "status": "running",
                                 "session_id": body.get("session_id")}
            return web.json_response({"run_id": run_id, "status": "started", "replayed": False}, status=202)
        if path.startswith("/v1/runs/"):
            parts = path.split("/")
            run_id = parts[3]
            run = self.runs.get(run_id)
            if run is None:
                return _err(f"Run not found: {run_id}", 404, "run_not_found")
            if len(parts) == 4:
                return web.json_response(run)
            if parts[4] == "resolve-unknown":
                body = entry["body"] or {}
                if body.get("admission_id") != run.get("admission_id"):
                    return _err("stale", 409, "stale_generation")
                run["status"] = "interrupted"
                return web.json_response({"run_id": run_id, "outcome": "interrupted"})
            if parts[4] == "stop":
                run["status"] = "cancelled"
                return web.json_response({"run_id": run_id, "status": "stopping"})
        return _err("not found", 404, "not_found")

    def start(self) -> "FakeGateway":
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        ready = threading.Event()

        async def _setup():
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(_setup())
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(10)
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        async def _cleanup():
            if self._runner:
                await self._runner.cleanup()
        fut = asyncio.run_coroutine_threadsafe(_cleanup(), self._loop)
        fut.result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
