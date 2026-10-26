"""Taskly: a small but realistic API for system tests, served over real sockets.

It serves its own OpenAPI spec and behaves like real APIs do: API-key or OAuth
(client credentials, short-lived tokens) auth, cursor and page+Link pagination,
a rate-limited endpoint, signed webhooks, a v2 with spec changes, and a "drift"
mode where behaviour changes but the spec doesn't.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import socket
import threading
import time
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

API_KEY = "taskly-key"
CLIENT_ID, CLIENT_SECRET = "taskly-client", "taskly-secret"
WEBHOOK_SECRET = "whsec_taskly"
TOKEN_TTL = 2  # seconds: short, so tests see refreshes


class State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.version = 1
        self.drift = False
        self.tasks: dict[int, dict[str, Any]] = {}
        self.next_id = 1
        self.tokens: dict[str, float] = {}
        self.tokens_issued = 0
        self.stats_calls = 0
        self.requests: list[str] = []


STATE = State()


def spec(base: str) -> dict[str, Any]:
    v2 = STATE.version >= 2
    task = {"type": "object", "required": ["id", "title", "priority"], "properties": {
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "priority": {"type": "string", "enum": ["normal", "high"] if v2 else ["low", "normal", "high"]},
        "estimate": {"type": "integer", "nullable": True, "description": "Hours"},
        "created_at": {"type": "string", "format": "date-time"},
        **({"status": {"type": "string"}} if v2 else {}),
    }}
    task_input = {"type": "object", "required": ["title"], "properties": {
        k: task["properties"][k] for k in ("title", "priority", "estimate")}}
    ok = lambda schema: {"description": "ok", "content": {"application/json": {"schema": schema}}}  # noqa: E731
    paths: dict[str, Any] = {
        "/tasks": {
            "get": {"operationId": "listTasks", "summary": "List tasks", "tags": ["tasks"], "parameters": [
                {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                {"name": "sort", "in": "query", "schema": {"type": "string", "enum": ["created", "updated"]}},
                {"name": "direction", "in": "query", "schema": {"type": "string", "enum": ["asc", "desc"]}}],
                "responses": {"200": ok({"type": "object", "properties": {
                    "tasks": {"type": "array", "items": {"$ref": "#/components/schemas/Task"}},
                    "next_cursor": {"type": "string", "nullable": True}}})}},
            "post": {"operationId": "createTask", "summary": "Create a task", "tags": ["tasks"],
                     "requestBody": {"required": True, "content": {"application/json": {
                         "schema": {"$ref": "#/components/schemas/TaskInput"}}}},
                     "responses": {"201": ok({"$ref": "#/components/schemas/Task"})}}},
        "/tasks/{task_id}": {
            "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "get": {"operationId": "getTask", "summary": "Get a task", "tags": ["tasks"],
                    "responses": {"200": ok({"$ref": "#/components/schemas/Task"})}},
            "delete": {"operationId": "deleteTask", "summary": "Delete a task", "tags": ["tasks"],
                       "responses": {"204": {"description": "deleted"}}}},
        "/projects": {"get": {"operationId": "listProjects", "summary": "List projects", "parameters": [
            {"name": "page", "in": "query", "schema": {"type": "integer"}},
            {"name": "per_page", "in": "query", "schema": {"type": "integer"}}],
            "responses": {"200": ok({"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"}, "name": {"type": "string"}}}})}}},
        "/stats": {"get": {"operationId": "getStats", "summary": "Usage stats",
                           "responses": {"200": ok({"type": "object", "properties": {"count": {"type": "integer"}}})}}},
        "/me": {"get": {"operationId": "getMe", "summary": "Who am I (OAuth only)",
                        "security": [{"oauth": ["tasks"]}],
                        "responses": {"200": ok({"type": "object", "properties": {"client_id": {"type": "string"}}})}}},
    }
    if v2:
        paths["/labels"] = {"get": {"operationId": "listLabels", "summary": "List labels",
                                    "responses": {"200": ok({"type": "array", "items": {"type": "string"}})}}}
    return {
        "openapi": "3.1.0" if v2 else "3.0.3",
        "info": {"title": "Taskly", "version": f"{STATE.version}.0"},
        "servers": [{"url": base}],
        "components": {
            "schemas": {"Task": task, "TaskInput": task_input},
            "securitySchemes": {
                "apiKey": {"type": "apiKey", "in": "header", "name": "X-Taskly-Key"},
                "oauth": {"type": "oauth2", "flows": {"clientCredentials": {
                    "tokenUrl": f"{base}/oauth/token", "scopes": {"tasks": "Read and write tasks"}}}}}},
        "security": [{"apiKey": []}, {"oauth": ["tasks"]}],
        "paths": paths,
        "webhooks" if v2 else "x-webhooks": {"task.created": {"post": {
            "summary": "A task was created",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "required": ["event", "task"], "properties": {
                    "event": {"type": "string"}, "task": {"$ref": "#/components/schemas/Task"}}}}}},
            "responses": {"200": {"description": "ok"}}}}},
    }


def _authed(request: Request, bearer_only: bool = False) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        expiry = STATE.tokens.get(auth[7:])
        return expiry is not None and expiry > time.time()
    return not bearer_only and request.headers.get("x-taskly-key") == API_KEY


def _deny() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


async def openapi(request: Request) -> JSONResponse:
    return JSONResponse(spec(str(request.base_url).rstrip("/")))


async def tasks(request: Request) -> Response:
    STATE.requests.append(f"{request.method} {request.url.path}?{request.url.query}")
    if not _authed(request):
        return _deny()
    if request.method == "POST":
        body = await request.json()
        allowed = ["normal", "high"] if STATE.version >= 2 else ["low", "normal", "high"]
        if not body.get("title"):
            return JSONResponse({"error": "title is required"}, status_code=422)
        if body.get("priority", "normal") not in allowed:
            return JSONResponse({"error": f"priority must be one of {allowed}"}, status_code=422)
        task = {"id": STATE.next_id, "title": body["title"], "priority": body.get("priority", "normal"),
                "estimate": body.get("estimate"), "created_at": f"2026-10-25T12:00:{STATE.next_id:02d}Z"}
        if STATE.version >= 2:
            task["status"] = "open"
        STATE.tasks[STATE.next_id] = task
        STATE.next_id += 1
        return JSONResponse(task, status_code=201)
    q = request.query_params
    items = sorted(STATE.tasks.values(), key=lambda t: t["id"],
                   reverse=q.get("sort") == "created" and q.get("direction") == "desc")
    start, limit = int(q.get("cursor") or 0), int(q.get("limit") or 50)
    page = items[start:start + limit]
    more = start + limit < len(items)
    return JSONResponse({"tasks": page, "next_cursor": str(start + limit) if more else None})


async def task(request: Request) -> Response:
    if not _authed(request):
        return _deny()
    tid = int(request.path_params["task_id"])
    if tid not in STATE.tasks:
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "DELETE":
        del STATE.tasks[tid]
        return Response(status_code=204)
    t = dict(STATE.tasks[tid])
    if STATE.drift:  # behaviour changed, spec didn't: estimate became a string, new field
        t["estimate"] = "3h"
        t["assignee"] = {"login": "jane", "email": "jane@example.com"}
    return JSONResponse(t)


async def projects(request: Request) -> Response:
    if not _authed(request):
        return _deny()
    if STATE.drift and not request.query_params.get("workspace"):
        return JSONResponse({"error": "missing required parameter: workspace"}, status_code=400)
    page, per = int(request.query_params.get("page") or 1), int(request.query_params.get("per_page") or 2)
    rows = [{"id": i, "name": f"Project {i}"} for i in range(1, 6)]
    chunk = rows[(page - 1) * per: page * per]
    headers = {}
    if page * per < len(rows):
        nxt = request.url.include_query_params(page=page + 1, per_page=per)
        headers["Link"] = f'<{nxt}>; rel="next"'
    return JSONResponse(chunk, headers=headers)


async def stats(request: Request) -> Response:
    if not _authed(request):
        return _deny()
    STATE.stats_calls += 1
    if STATE.stats_calls == 1:
        return JSONResponse({"error": "slow down"}, status_code=429, headers={"Retry-After": "1"})
    return JSONResponse({"count": len(STATE.tasks)})


async def labels(request: Request) -> Response:
    if not _authed(request):
        return _deny()
    if STATE.version < 2:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(["bug", "feature"])


async def me(request: Request) -> Response:
    if not _authed(request, bearer_only=True):
        return _deny()
    return JSONResponse({"client_id": CLIENT_ID})


async def token(request: Request) -> Response:
    form = dict(await request.form())
    cid, secret = form.get("client_id"), form.get("client_secret")
    basic = request.headers.get("authorization", "")
    if basic.startswith("Basic "):
        cid, _, secret = base64.b64decode(basic[6:]).decode().partition(":")
    if form.get("grant_type") != "client_credentials" or (cid, secret) != (CLIENT_ID, CLIENT_SECRET):
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    tok = secrets.token_urlsafe(16)
    STATE.tokens[tok] = time.time() + TOKEN_TTL
    STATE.tokens_issued += 1
    return JSONResponse({"access_token": tok, "token_type": "bearer", "expires_in": TOKEN_TTL})


async def admin(request: Request) -> Response:
    body = await request.json()
    if body.get("reset"):
        STATE.reset()
    STATE.version = body.get("version", STATE.version)
    STATE.drift = body.get("drift", STATE.drift)
    if body.get("send_webhook_to"):
        payload = json.dumps({"event": "task.created", "task": next(iter(STATE.tasks.values()))}).encode()
        sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient() as client:
            await client.post(body["send_webhook_to"], content=payload, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": sig,
                "X-Taskly-Delivery": "d-1"})
    return JSONResponse({"version": STATE.version, "drift": STATE.drift,
                         "tokens_issued": STATE.tokens_issued, "requests": STATE.requests})


app = Starlette(routes=[
    Route("/openapi.json", openapi),
    Route("/tasks", tasks, methods=["GET", "POST"]),
    Route("/tasks/{task_id:int}", task, methods=["GET", "DELETE"]),
    Route("/projects", projects),
    Route("/stats", stats),
    Route("/me", me),
    Route("/labels", labels),
    Route("/oauth/token", token, methods=["POST"]),
    Route("/admin", admin, methods=["POST"]),
])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(asgi_app: Any) -> tuple[str, uvicorn.Server, threading.Thread]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server, thread
