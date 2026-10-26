"""End-to-end system test: the real CLI against a real API over real sockets.

No mocks. Steps run in order and build on each other, like a user would:
generate -> test -> call -> paginate -> retry -> OAuth -> triggers -> webhooks ->
serve over MCP -> smoke -> upstream change -> check/apply -> live drift -> fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from connectorgen import Connector, Credentials
from connectorgen.ir import McpServerConfig, WebhookSignature
from connectorgen.llm import FakeLLM
from connectorgen.mcp import McpClient, connector_from_mcp
from connectorgen.repair.live import propose_live_fixes
from connectorgen.triggers import parse_webhook
from connectorgen.triggers.webhooks import DuplicateDeliveryError, WebhookError

from . import taskly_server as taskly

pytestmark = pytest.mark.e2e


class Env:
    base: str
    dir: Path
    servers: list


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env()
    e.dir = tmp_path_factory.mktemp("system")
    taskly.STATE.reset()
    e.base, server, thread = taskly.start(taskly.app)
    e.servers = [(server, thread)]
    yield e
    for server, thread in e.servers:
        server.should_exit = True
        thread.join(10)


def cg(e: Env, *args: str, expect: int | None = 0, key: bool = True, **extra_env: str):
    """Run the connectorgen CLI as a subprocess."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith("CONNECTORGEN_")}
    if key:
        environ["CONNECTORGEN_API_KEY"] = taskly.API_KEY
    environ.update(extra_env)
    proc = subprocess.run([sys.executable, "-m", "connectorgen.cli", *args], cwd=e.dir, env=environ,
                          capture_output=True, text=True, timeout=120)
    if expect is not None:
        assert proc.returncode == expect, f"exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return proc


def admin(e: Env, **body):
    return httpx.post(f"{e.base}/admin", json=body).json()


@pytest.mark.usefixtures("env")
class TestSystem:
    def test_01_generate_from_live_spec_url(self, env):
        out = cg(env, "generate", f"{env.base}/openapi.json", "-o", "taskly.json").stdout
        assert "7 actions" in out and "auth: api_key, oauth2" in out
        c = Connector.load(env.dir / "taskly.json")
        assert c.base_url == env.base
        assert c.action("get_me").security == ["oauth"]               # per-operation security
        assert c.action("list_tasks").security == ["apiKey", "oauth"]
        assert c.action("list_tasks").pagination.style == "cursor"
        assert c.action("list_projects").pagination.style == "page"
        new_tasks = next(t for t in c.triggers if t.name == "new_tasks")
        assert new_tasks.order == "newest_first"
        assert any(t.kind == "webhook" and t.event == "task.created" for t in c.triggers)

    def test_02_offline_contract_tests(self, env):
        assert "7 passed, 0 failed" in cg(env, "test", "taskly.json").stdout

    def test_03_create_get_and_validation(self, env):
        body = env.dir / "task.json"
        for title in ("Write spec", "Ship it", "Celebrate"):
            body.write_text(json.dumps({"title": title, "priority": "low"}))
            created = json.loads(cg(env, "call", "taskly.json", "create_task", "--body", str(body)).stdout)
        assert created["id"] == 3
        got = json.loads(cg(env, "call", "taskly.json", "get_task", "-i", "task_id=2").stdout)
        assert got["title"] == "Ship it"
        bad = cg(env, "call", "taskly.json", "get_task", "-i", "task_id=abc", expect=1)
        assert "is not of type 'integer'" in bad.stderr               # caught before any request
        missing = cg(env, "call", "taskly.json", "get_task", "-i", "task_id=99", expect=1)
        assert "HTTP 404" in missing.stderr
        unauth = cg(env, "call", "taskly.json", "get_task", "-i", "task_id=1", key=False, expect=1)
        assert "requires apiKey or oauth credentials" in unauth.stderr   # explained, nothing sent

    def test_04_destructive_needs_flag(self, env):
        assert "--allow-destructive" in cg(env, "call", "taskly.json", "delete_task", "-i", "task_id=3",
                                           expect=1).stderr
        cg(env, "call", "taskly.json", "delete_task", "-i", "task_id=3", "--allow-destructive")
        assert 3 not in taskly.STATE.tasks

    def test_05_cursor_and_link_header_pagination(self, env):
        body = env.dir / "task.json"
        for i in range(4):
            body.write_text(json.dumps({"title": f"Bulk {i}"}))
            cg(env, "call", "taskly.json", "create_task", "--body", str(body))
        tasks = json.loads(cg(env, "call", "taskly.json", "list_tasks", "-i", "limit=2", "--paginate").stdout)
        assert len(tasks) == 6 and len({t["id"] for t in tasks}) == 6     # all pages, no duplicates
        projects = json.loads(cg(env, "call", "taskly.json", "list_projects", "-i", "per_page=2",
                                 "--paginate").stdout)
        assert [p["id"] for p in projects] == [1, 2, 3, 4, 5]          # followed Link headers

    def test_06_rate_limit_retry(self, env):
        start = time.monotonic()
        out = json.loads(cg(env, "call", "taskly.json", "get_stats").stdout)
        assert out == {"count": 6} and time.monotonic() - start >= 1.0  # waited out Retry-After

    def test_07_oauth_client_credentials_and_refetch(self, env):
        cg(env, "auth", "taskly.json", "--client-id", taskly.CLIENT_ID, "--client-secret",
           taskly.CLIENT_SECRET, "--flow", "client_credentials", "--token-file", "tok.json", key=False)
        assert oct((env.dir / "tok.json").stat().st_mode & 0o777) == "0o600"
        creds = dict(CONNECTORGEN_CLIENT_ID=taskly.CLIENT_ID, CONNECTORGEN_CLIENT_SECRET=taskly.CLIENT_SECRET)
        me = json.loads(cg(env, "call", "taskly.json", "get_me", "--token-file", "tok.json", key=False,
                           **creds).stdout)
        assert me == {"client_id": taskly.CLIENT_ID}
        issued = taskly.STATE.tokens_issued
        time.sleep(taskly.TOKEN_TTL + 0.5)                             # token expires
        cg(env, "call", "taskly.json", "get_me", "--token-file", "tok.json", key=False, **creds)
        assert taskly.STATE.tokens_issued == issued + 1                # fetched a new one

    def test_08_polling_trigger(self, env):
        first = cg(env, "poll", "taskly.json", "new_tasks")
        assert first.stdout == "" and "Baseline recorded" in first.stderr
        (env.dir / "task.json").write_text(json.dumps({"title": "Fresh"}))
        cg(env, "call", "taskly.json", "create_task", "--body", "task.json")
        lines = cg(env, "poll", "taskly.json", "new_tasks").stdout.strip().splitlines()
        assert [json.loads(x)["title"] for x in lines] == ["Fresh"]
        assert cg(env, "poll", "taskly.json", "new_tasks").stdout == ""       # nothing new

    def test_09_signed_webhook_delivery(self, env):
        received: list[tuple[dict, bytes]] = []

        async def hook(request: Request) -> Response:
            received.append((dict(request.headers), await request.body()))
            return Response("ok")

        url, server, thread = taskly.start(Starlette(routes=[Route("/hook", hook, methods=["POST"])]))
        env.servers.append((server, thread))
        admin(env, send_webhook_to=f"{url}/hook")
        headers, body = received[0]
        c = Connector.load(env.dir / "taskly.json")
        trigger = next(t for t in c.triggers if t.kind == "webhook")
        trigger.signature = WebhookSignature.github()
        trigger.delivery_id_header = "X-Taskly-Delivery"
        seen: set[str] = set()
        payload = parse_webhook(trigger, headers, body, secret=taskly.WEBHOOK_SECRET,
                                schemas=c.schemas, seen_ids=seen, validate="raise")
        assert payload["event"] == "task.created"
        with pytest.raises(DuplicateDeliveryError):
            parse_webhook(trigger, headers, body, secret=taskly.WEBHOOK_SECRET, seen_ids=seen)
        with pytest.raises(WebhookError, match="mismatch"):
            parse_webhook(trigger, headers, body.replace(b"task", b"tusk"), secret=taskly.WEBHOOK_SECRET)

    def test_10_serve_over_mcp_and_use_it(self, env):
        port = taskly.free_port()
        environ = {**os.environ, "CONNECTORGEN_API_KEY": taskly.API_KEY}
        proc = subprocess.Popen([sys.executable, "-m", "connectorgen.cli", "serve", "taskly.json",
                                 "--transport", "http", "--port", str(port), "--tag", "tasks"],
                                cwd=env.dir, env=environ, stderr=subprocess.PIPE, text=True)
        try:
            url = f"http://127.0.0.1:{port}/mcp"
            for _ in range(100):
                try:
                    httpx.get(url, timeout=0.5)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            remote = connector_from_mcp(McpServerConfig(transport="http", url=url))
            names = {a.name for a in remote.actions}
            assert names == {"list_tasks", "create_task", "get_task"}         # delete excluded
            with McpClient(remote) as client:
                created = client.execute("create_task", body={"title": "Via MCP"})
                assert client.execute("get_task", task_id=created["id"])["title"] == "Via MCP"
        finally:
            proc.terminate()
            proc.wait(10)

    def test_11_live_smoke(self, env):
        out = cg(env, "smoke", "taskly.json", "--delay", "0", "--json", "smoke-ok.json", key=True)
        report = json.loads((env.dir / "smoke-ok.json").read_text())
        statuses = {r["action"]: r["status"] for r in report["results"]}
        assert statuses["get_task"] == "ok"                     # id chained from list_tasks
        assert statuses["get_me"] == "skipped"                  # OAuth-only; no OAuth creds given
        assert "create_task" not in statuses and "delete_task" not in statuses
        assert "0 drift, 0 http errors" in out.stdout

    def test_12_upstream_change_check_and_apply(self, env):
        assert cg(env, "check", "taskly.json").returncode == 0      # unchanged
        admin(env, version=2)
        r = cg(env, "check", "taskly.json", "--used", "create_task", "-o", "repair", expect=20)
        assert "no longer accepts ['low']" in r.stdout and "<- used" in r.stdout
        assert "list_labels" in r.stdout                            # additive change
        cg(env, "apply", "repair", "taskly.json", "--yes")
        c = Connector.load(env.dir / "taskly.json")
        assert c.action("list_labels") and list((env.dir).glob("taskly.json.*.bak"))
        assert "8 passed, 0 failed" in cg(env, "test", "taskly.json").stdout

    def test_13_live_drift_found_and_fixed(self, env):
        admin(env, drift=True)                                       # behaviour changes, spec doesn't
        cg(env, "smoke", "taskly.json", "--delay", "0", "--json", "smoke.json", expect=1)
        report = json.loads((env.dir / "smoke.json").read_text())
        by = {r["action"]: r for r in report["results"]}
        assert by["get_task"]["status"] == "drift"
        assert by["list_projects"]["http_status"] == 400
        raw = (env.dir / "smoke.json").read_text()
        assert "jane" not in raw and '"3h"' not in raw                # no response data saved

        r = cg(env, "check", "taskly.json", "--smoke-report", "smoke.json", "--fix-live",
               "--verify-live", "-o", "fix", expect=20)
        assert "+ get_task (output, live)" in r.stdout
        assert "pass a model to diagnose it" in r.stdout                # input fix needs a model
        cg(env, "apply", "fix", "taskly.json", "--yes")

        # the request-side fix, with a scripted model, verified against the live server
        c = Connector.load(env.dir / "taskly.json")
        llm = FakeLLM([{"diagnosis": "listProjects now requires a workspace query parameter.",
                        "confidence": "high", "operations": [{
                            "op": "add_param", "name": "workspace", "location": "query", "new_name": "",
                            "required": True, "type": "string", "enum_values": [], "description": ""}]}])
        patched, fixes = propose_live_fixes(c, report, llm=llm, verify_live=True,
                                            credentials=Credentials(api_key=taskly.API_KEY),
                                            global_params={"workspace": "acme"})
        assert [(f.action, f.verified) for f in fixes.accepted] == [("list_projects", "live")]
        patched.save(env.dir / "taskly.json")

        after = cg(env, "smoke", "taskly.json", "--delay", "0", "--param", "workspace=acme")
        assert "0 drift, 0 http errors, 0 errors" in after.stdout
