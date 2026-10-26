"""Regression tests for the security review: each test pins one finding."""

import asyncio
import copy
import logging
import os
import stat
import sys

import httpx
import pytest
import respx
from typer.testing import CliRunner

import connectorgen.ingest.openapi as openapi_mod
from conftest import plain
from connectorgen import (
    ActionHTTPError,
    ConnectionFailedError,
    Connector,
    ConnectorClient,
    ConnectorError,
    Credentials,
    RetryPolicy,
    UnsupportedSpecError,
    connector_from_openapi,
)
from connectorgen.cli import app
from connectorgen.ingest.docs import _grounded, _safe_base_url, connector_from_docs, fetch_docs
from connectorgen.ingest.openapi import fetch_spec_text, parse_spec_text
from connectorgen.ir import McpServerConfig, Trigger, WebhookSignature
from connectorgen.llm import CachedLLM, FakeLLM, OpenAICompatibleLLM
from connectorgen.llm.cache import default_cache_dir
from connectorgen.mcp import AsyncMcpClient, connector_from_mcp
from connectorgen.repair import apply_proposal, check_for_drift
from connectorgen.repair.diff import diff_connectors
from connectorgen.triggers.polling import Poller
from connectorgen.triggers.webhooks import WebhookError, parse_webhook, sign_payload

BASE = "https://api.acme.test/v1"


def spec(key_in="header"):
    return {
        "openapi": "3.0.3",
        "info": {"title": "Acme", "version": "1"},
        "servers": [{"url": BASE}],
        "components": {"securitySchemes": {"key": {"type": "apiKey", "in": key_in,
                                                   "name": "X-API-Key" if key_in == "header" else "api_key"}}},
        "security": [{"key": []}],
        "paths": {
            "/items": {"get": {"operationId": "listItems", "parameters": [
                {"name": "cursor", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                        "next": {"type": "string"}}}}}}}}},
            "/items/{id}": {
                "get": {"operationId": "getItem", "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "ok"}}},
                "post": {"operationId": "updateItem", "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "ok"}}}},
        },
    }


def client_for(key_in="header", **creds):
    c = connector_from_openapi(spec(key_in))
    return ConnectorClient(c, Credentials(api_key="SECRET", **creds))


# -- pagination never sends credentials to another origin ----------------------------


@respx.mock
@pytest.mark.parametrize("key_in", ["header", "query"])
def test_link_header_to_other_host_is_refused(key_in):
    respx.get(f"{BASE}/items").respond(json={"data": [{"id": 1}]},
                                       headers={"Link": '<https://evil.test/steal>; rel="next"'})
    evil = respx.get(url__startswith="https://evil.test").respond(json={"data": []})
    with client_for(key_in) as client, pytest.raises(ConnectorError, match="another origin"):
        list(client.paginate("list_items"))
    assert not evil.called


@respx.mock
@pytest.mark.parametrize("next_url", ["https://evil.test/items?page=2", "http://api.acme.test/v1/items?p=2",
                                      "https://api.acme.test:8443/v1/items?p=2"])
def test_cursor_url_to_other_origin_is_refused(next_url):
    c = connector_from_openapi(spec("query"))
    act = c.action("list_items")
    act.pagination.style, act.pagination.items_path = "cursor", "data"
    act.pagination.request_param, act.pagination.response_cursor_path = "cursor", "next"
    respx.get(f"{BASE}/items").respond(json={"data": [{"id": 1}], "next": next_url})
    other = respx.route(url__startswith=next_url.split("?")[0]).respond(json={"data": []})
    with ConnectorClient(c, Credentials(api_key="SECRET")) as client, \
            pytest.raises(ConnectorError, match="another origin"):
        list(client.paginate("list_items"))
    assert not other.called


@respx.mock
def test_same_origin_link_still_works_with_query_key():
    respx.get(f"{BASE}/items", params={"page": "2"}).respond(json={"data": [{"id": 2}]})
    respx.get(f"{BASE}/items").respond(json={"data": [{"id": 1}]},
                                       headers={"Link": f'<{BASE}/items?page=2>; rel="next"'})
    with client_for("query") as client:
        assert [i["id"] for i in client.paginate("list_items")] == [1, 2]
    assert respx.calls.last.request.url.params["api_key"] == "SECRET"


# -- redirects --------------------------------------------------------------------------


@respx.mock
def test_cross_origin_redirect_drops_every_credential_header():
    respx.get(f"{BASE}/items/1").respond(302, headers={"Location": "https://cdn.other.test/file"})
    cdn = respx.get("https://cdn.other.test/file").respond(json={"ok": True})
    with client_for("header", token="T") as client:
        assert client.execute("get_item", id="1") == {"ok": True}
    sent = cdn.calls.last.request.headers
    assert "x-api-key" not in sent and "authorization" not in sent and "cookie" not in sent


@respx.mock
def test_same_origin_redirect_keeps_auth():
    respx.get(f"{BASE}/items/1").respond(301, headers={"Location": "/v1/items/one"})
    final = respx.get(f"{BASE}/items/one").respond(json={"ok": True})
    with client_for() as client:
        client.execute("get_item", id="1")
    assert final.calls.last.request.headers["x-api-key"] == "SECRET"


@respx.mock
def test_https_to_http_downgrade_is_refused():
    respx.get(f"{BASE}/items/1").respond(302, headers={"Location": "http://api.acme.test/v1/items/1"})
    plain = respx.get("http://api.acme.test/v1/items/1").respond(json={})
    with client_for() as client, pytest.raises(ConnectorError, match="https to http"):
        client.execute("get_item", id="1")
    assert not plain.called


@respx.mock
def test_write_is_not_replayed_to_another_origin():
    respx.post(f"{BASE}/items/1").respond(307, headers={"Location": "https://other.test/x"})
    other = respx.post("https://other.test/x").respond(json={})
    with client_for() as client, pytest.raises(ConnectorError, match="Refusing to replay a POST"):
        client.execute("update_item", id="1", body={"a": 1})
    assert not other.called


# -- secrets out of messages and logs ---------------------------------------------------


@respx.mock
def test_query_key_redacted_in_errors_and_httpx_logs(caplog):
    respx.get(f"{BASE}/items/1").respond(500, json={"e": 1})
    respx.get(f"{BASE}/items/2").mock(side_effect=httpx.ConnectError("boom"))
    with caplog.at_level(logging.INFO), client_for("query") as client:
        client.retry.max_attempts = 1
        with pytest.raises(ActionHTTPError) as e1:
            client.execute("get_item", id="1")
        with pytest.raises(ConnectionFailedError) as e2:
            client.execute("get_item", id="2")
    for text in (str(e1.value), e1.value.url, str(e2.value), caplog.text):
        assert "SECRET" not in text
    assert "api_key=%2A%2A%2A" in e1.value.url or "api_key=***" in e1.value.url


def test_repr_hides_secrets():
    from connectorgen.ir import OAuthFlow
    from connectorgen.runtime.oauth import OAuth2Session

    creds = Credentials(api_key="K-SECRET", token="T-SECRET", username="u", password="P-SECRET")
    assert "SECRET" not in repr(creds) and "SECRET" not in str(creds)
    flow = OAuthFlow(flow="authorization_code", authorization_url="https://a.test/auth",
                     token_url="https://a.test/token")
    req = OAuth2Session(flow, "cid").authorization_url("http://127.0.0.1:1/cb")
    assert req.code_verifier not in repr(req) and req.state not in repr(req)


# -- path parameters stay inside their segment ------------------------------------------


@respx.mock
@pytest.mark.parametrize("value", ["..", ".", "../admin", "a/b"])
def test_path_param_cannot_change_endpoint(value):
    route = respx.get(url__regex=r".*").respond(json={})
    with client_for() as client:
        client.execute("get_item", id=value)
    path = route.calls.last.request.url.raw_path.decode().split("?")[0]
    assert path.startswith("/v1/items/") and path.count("/") == 3, path


# -- check/apply: changes that move credentials or tighten inputs are never "safe" --------


def oauth_spec():
    s = spec()
    s["components"]["securitySchemes"]["oauth"] = {"type": "oauth2", "flows": {"clientCredentials": {
        "tokenUrl": "https://auth.acme.test/token", "scopes": {}}}}
    return s


def verdict(old, new):
    return check_for_drift(connector_from_openapi(old), spec=new).verdict


@pytest.mark.parametrize("mutate", [
    lambda s: s["components"]["securitySchemes"]["oauth"]["flows"]["clientCredentials"].update(
        tokenUrl="https://evil.test/token"),
    lambda s: s["components"]["securitySchemes"]["key"].update({"in": "query"}),
    lambda s: s["components"]["securitySchemes"].update(
        {"other": {"type": "apiKey", "in": "query", "name": "k"}}),
], ids=["token_url", "key_location", "new_scheme"])
def test_auth_changes_need_review(mutate):
    old = oauth_spec()
    new = copy.deepcopy(old)
    mutate(new)
    assert verdict(old, new) == "needs_review"


def _param(s):
    return s["paths"]["/items"]["get"]["parameters"][0]["schema"]


def _out_item(s):
    return s["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "properties"]["data"]["items"]


@pytest.mark.parametrize("mutate,expected", [
    (lambda s: _param(s).update(enum=["a", "b"]), "breaking"),
    (lambda s: _param(s).update(pattern="^[a-z]+$"), "breaking"),
    (lambda s: _param(s).update(maxLength=5), "breaking"),
    (lambda s: _param(s).update(format="uuid"), "breaking"),
    (lambda s: _out_item(s).update(properties={"id": {"type": "string"}}) or
        _out_item(s).update(properties={}), "info"),
], ids=["enum_added", "pattern_added", "limit_tightened", "format_added", "noop"])
def test_param_tightening_is_breaking(mutate, expected):
    old = spec()
    new = copy.deepcopy(old)
    mutate(new)
    d = diff_connectors(connector_from_openapi(old), connector_from_openapi(new))
    worst = min((e.severity for e in d.entries), key=["breaking", "review", "additive", "info"].index,
                default="info")
    assert worst == expected, d.to_text()


def test_nested_response_changes_are_classified():
    old = spec()
    _out_item(old).update(properties={"id": {"type": "string"}, "owner": {"type": "object", "properties": {
        "name": {"type": "string"}, "role": {"type": "string", "enum": ["admin", "member"]}}}})
    removed, retyped, new_enum = (copy.deepcopy(old) for _ in range(3))
    del _out_item(removed)["properties"]["owner"]["properties"]["name"]
    _out_item(retyped)["properties"]["owner"]["properties"]["name"]["type"] = "integer"
    _out_item(new_enum)["properties"]["owner"]["properties"]["role"]["enum"].append("guest")
    assert verdict(old, removed) == "needs_review"
    assert verdict(old, retyped) == "needs_review"
    assert verdict(old, new_enum) == "needs_review"
    added = copy.deepcopy(old)
    _out_item(added)["properties"]["owner"]["properties"]["email"] = {"type": "string"}
    assert verdict(old, added) == "safe_update"


def test_base_url_override_is_not_drift():
    old = spec()
    new = copy.deepcopy(old)
    new["paths"]["/items"]["get"]["summary"] = "List items"
    c = connector_from_openapi(old, base_url="https://staging.acme.test/v1")
    proposal = check_for_drift(c, spec=new)
    assert proposal.verdict == "safe_update", proposal.diff.to_text()
    assert proposal.candidate.base_url == "https://staging.acme.test/v1"
    again = check_for_drift(proposal.candidate, spec=new)  # after apply: still not drift
    assert again.verdict in ("no_change", "safe_update"), again.diff.to_text()


# -- docs ingest: base URL, fetching, grounding, destructive -----------------------------


@pytest.mark.parametrize("candidate,docs,ok", [
    ("https://api.acme.com/v1", "https://docs.acme.com/api", True),
    ("https://api.acme.co.uk", "https://docs.acme.co.uk/", True),
    ("https://attacker.readme.io/", "https://acme.readme.io/docs", False),
    ("https://evil.github.io/", "https://acme.github.io/docs", False),
    ("https://evil.gitbook.io/", "https://acme.gitbook.io/docs", False),
    ("https://acme.readme.io/api", "https://acme.readme.io/docs", True),       # exact host
    ("http://api.acme.com/v1", "https://docs.acme.com/api", False),            # downgrade
    ("https://api.acme.com:8443/v1", "https://docs.acme.com/api", False),      # other port
    ("https://10.0.0.6/", "https://10.0.0.5/docs", False),                     # other private IP
    ("https://169.254.169.254/", "https://docs.acme.com/", False),
    ("http://127.0.0.1:8000/api", "http://127.0.0.1:8000/docs", True),         # same local origin
    ("https://evil.co.uk/", "https://docs.acme.co.uk/", False),
])
def test_docs_base_url_policy(candidate, docs, ok):
    assert _safe_base_url(candidate, [docs]) is ok


@respx.mock
def test_docs_fetch_never_follows_redirects_off_site():
    respx.get("https://docs.acme.test/a").respond(302, headers={"Location": "http://169.254.169.254/meta"})
    respx.get("https://docs.acme.test/b").respond(302, headers={"Location": "https://www.acme.test/b"})
    respx.get("https://www.acme.test/b").respond(200, text="<p>GET /things</p>", headers={"content-type": "text/html"})
    metadata = respx.get(url__startswith="http://169.254.169.254").respond(200, text="SECRET CREDENTIALS")
    pages = fetch_docs(["https://docs.acme.test/a", "https://docs.acme.test/b"])
    assert not metadata.called
    assert [p.source for p in pages] == ["https://www.acme.test/b"]  # same-site https redirect is fine


@respx.mock
def test_docs_crawl_is_bounded_even_with_empty_pages():
    calls = []

    def endless(request):
        calls.append(request.url)
        n = int(request.url.params.get("p", "0"))
        return httpx.Response(200, html=f'<html><body><a href="?p={n + 1}"></a></body></html>')

    respx.get(url__startswith="https://docs.acme.test/docs/").mock(side_effect=endless)
    assert fetch_docs(["https://docs.acme.test/docs/"], crawl=True, max_pages=5) == []
    assert len(calls) == 5


@respx.mock
def test_docs_body_size_is_capped():
    respx.get("https://docs.acme.test/huge").respond(200, text="x" * 2_000, headers={"content-type": "text/plain"})
    assert fetch_docs(["https://docs.acme.test/huge"], max_bytes=1_000) == []


def test_grounding_ignores_the_source_url():
    chunk = "[source: https://docs.acme.test/admin/users/export]\n# Notes\nGET /notes lists notes."
    assert _grounded("/notes", chunk)
    assert not _grounded("/admin/users/export", chunk)
    assert not _grounded("/note", chunk)          # whole tokens, not substrings of 'notes'


@respx.mock
def test_docs_actions_with_destructive_paths_are_flagged():
    respx.get("https://docs.acme.test/api").respond(
        200, text="<p>POST /cache/flush-all purges everything. POST /cache/purge too.</p>",
        headers={"content-type": "text/html"})
    ep = {"name": "refresh_cache", "method": "POST", "path": "/cache/purge", "summary": "Refresh",
          "description": "", "params": [], "body_encoding": "none", "body_required": False,
          "body_schema_json": "", "response_schema_json": "", "evidence": "",
          "pagination": {"style": "none", "request_param": "", "limit_param": "", "items_path": "",
                         "response_cursor_path": "", "cursor_from_item_field": "", "has_more_path": ""}}
    out = {"api_title": "Acme", "base_url": "https://api.acme.test", "auth": [], "endpoints": [ep]}
    c, _ = connector_from_docs(["https://docs.acme.test/api"], FakeLLM([out]))
    assert c.actions[0].destructive


# -- hostile specs can't exhaust memory or crash -------------------------------------------


LAUGHS = "\n".join(['openapi: "3.0.3"', 'info: {title: x, version: "1"}', 'a0: &a0 ["lol","lol","lol","lol"]']
                   + [f'a{i}: &a{i} [' + ",".join([f"*a{i - 1}"] * 9) + ']' for i in range(1, 9)]
                   + ["paths: {}"])


def test_yaml_alias_bomb_is_refused_quickly(monkeypatch):
    monkeypatch.setattr(openapi_mod, "MAX_SPEC_NODES", 100_000)
    assert len(LAUGHS) < 600
    with pytest.raises(UnsupportedSpecError, match="expands to more than"):
        parse_spec_text(LAUGHS)


def test_ref_fanout_is_refused(monkeypatch):
    monkeypatch.setattr(openapi_mod, "MAX_SPEC_NODES", 50_000)
    bomb = {f"n{i}": {f"k{j}": {"$ref": f"#/x-bomb/n{i + 1}"} for j in range(4)} for i in range(16)}
    bomb["n16"] = {"description": "leaf"}
    s = {"openapi": "3.0.3", "info": {"title": "x", "version": "1"}, "servers": [{"url": "https://x.test"}],
         "paths": {"/a": {"get": {"responses": {"200": {"description": "ok",
                                                        "headers": {"h": {"$ref": "#/x-bomb/n0"}}}}}}},
         "x-bomb": bomb}
    with pytest.raises(UnsupportedSpecError, match="refs that fan out"):
        connector_from_openapi(s)


@pytest.mark.parametrize("text", [
    '{"openapi":"3.0.3","x":' + "[" * 100_000 + "]" * 100_000 + "}",
    'openapi: "3.0.3"\nx: ' + "[" * 5_000 + "]" * 5_000,
], ids=["json", "yaml"])
def test_deep_nesting_is_a_clear_error(text):
    with pytest.raises(UnsupportedSpecError, match="nested"):
        parse_spec_text(text)


@respx.mock
def test_spec_download_is_size_capped():
    respx.get("https://specs.test/huge.json").respond(200, content=b"{" + b" " * 5_000 + b"}")
    with pytest.raises(UnsupportedSpecError, match="larger than"):
        fetch_spec_text("https://specs.test/huge.json", max_bytes=1_000)


# -- from-mcp never writes secrets into the connector file ---------------------------------


@pytest.mark.parametrize("header", ["X-Auth-Token=abc", "PRIVATE-TOKEN=abc", "X-Goog-Api-Key=abc",
                                    "X-Custom=Bearer abc", "X-Thing=ghp_abcdefghijklmnop1234"])
def test_from_mcp_rejects_secret_headers(header, tmp_path):
    out = tmp_path / "c.json"
    result = CliRunner().invoke(app, ["from-mcp", "--url", "https://x.test/mcp", "--header", header,
                                      "-o", str(out)])
    assert result.exit_code != 0 and "Don't put credentials in --header" in plain(result.output)
    assert not out.exists()


@pytest.mark.parametrize("config", [
    McpServerConfig(transport="http", url="https://x.test/mcp?access_token=abc"),
    McpServerConfig(transport="stdio", command="server", args=["--api-key", "abc123"]),
    McpServerConfig(transport="stdio", command="server", args=["--token=abc123"]),
    McpServerConfig(transport="stdio", command="server", env={"SERVICE_TOKEN": "abc"}),
], ids=["url_query", "flag_value", "flag_eq", "static_env"])
def test_mcp_import_refuses_configs_holding_secrets(config):
    with pytest.raises(ConnectorError, match="would save what look like credentials"):
        connector_from_mcp(config)


def test_mcp_secret_check_allows_ordinary_configs():
    for cfg in [McpServerConfig(transport="stdio", command="npx", args=[
                    "-y", "@modelcontextprotocol/server-filesystem", "/home/u/projects/data-2024-0123456789abcdef"]),
                McpServerConfig(transport="stdio", command="srv", args=["--max-tokens", "100", "--verbose"]),
                McpServerConfig(transport="http", url="https://x.test/mcp?region=eu",
                                headers={"Accept": "application/json", "X-Tenant": "acme"})]:
        assert cfg.secret_problems() == []


# -- triggers: polling long lists, webhook retries ------------------------------------------


def _offset_list_connector(order):
    c = connector_from_openapi({
        "openapi": "3.0.3", "info": {"title": "Feed", "version": "1"}, "servers": [{"url": BASE}],
        "paths": {"/events": {"get": {"operationId": "listEvents", "parameters": [
            {"name": "offset", "in": "query", "schema": {"type": "integer"}},
            {"name": "limit", "in": "query", "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                "type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}}}}}}}}}},
        derive_triggers=False)
    act = c.action("list_events")
    act.pagination.style, act.pagination.request_param, act.pagination.limit_param = "offset", "offset", "limit"
    c.triggers.append(Trigger(name="new_event", kind="polling", source_action="list_events",
                              dedup_key="id", order=order))
    return c


def _serve(feed):
    def handler(request):
        off, lim = int(request.url.params.get("offset", 0)), int(request.url.params.get("limit", 10))
        return httpx.Response(200, json=[{"id": i} for i in feed[off:off + lim]])
    return handler


@respx.mock
def test_oldest_first_poller_sees_items_appended_to_a_long_list():
    feed = list(range(1, 2001))      # far longer than max_pages * page_size
    respx.get(f"{BASE}/events").mock(side_effect=_serve(feed))
    c = _offset_list_connector("oldest_first")
    with ConnectorClient(c) as client:
        poller = Poller(client, "new_event", max_pages=5, page_size=100)
        for _ in range(10):                  # the baseline keeps reading until the end of the list
            assert poller.poll().items == []
            if poller.store.load(poller.key)["initialized"]:
                break
        else:
            pytest.fail("baseline never reached the end of the list")
        assert poller.poll().items == []     # nothing new yet
        feed += [2001, 2002]
        result = poller.poll()
    assert [i["id"] for i in result.items] == [2001, 2002]


@respx.mock
def test_unknown_order_truncation_is_reported(caplog):
    respx.get(f"{BASE}/events").mock(side_effect=_serve(list(range(1, 1001))))
    c = _offset_list_connector("unknown")
    with ConnectorClient(c) as client:
        result = Poller(client, "new_event", max_pages=2, page_size=100).poll()
    assert result.complete is False and "can't be seen" in caplog.text


def _hook(with_sig=True):
    return Trigger(name="evt", kind="webhook", delivery_id_header="X-Delivery",
                   payload_schema={"type": "object", "required": ["id"]},
                   signature=WebhookSignature(header="X-Sig", format="plain", algorithm="sha256",
                                              encoding="hex", prefix="sha256=") if with_sig else None)


def test_webhook_retry_after_rejected_delivery_is_accepted():
    trig, seen = _hook(), set()
    bad = b'{"oops": 1}'
    with pytest.raises(WebhookError):   # fails schema validation
        parse_webhook(trig, {"X-Delivery": "d1", "X-Sig": sign_payload("s", bad, trig.signature)}, bad,
                      secret="s", seen_ids=seen, validate="raise")
    with pytest.raises(WebhookError):   # bad signature
        parse_webhook(trig, {"X-Delivery": "d1", "X-Sig": "sha256=00"}, b'{"id": 1}', secret="s", seen_ids=seen)
    good = b'{"id": 1}'
    headers = {"X-Delivery": "d1", "X-Sig": sign_payload("s", good, trig.signature)}
    assert parse_webhook(trig, headers, good, secret="s", seen_ids=seen, validate="raise") == {"id": 1}
    with pytest.raises(WebhookError, match="already processed"):
        parse_webhook(trig, headers, good, secret="s", seen_ids=seen)


@pytest.mark.parametrize("fmt", ["stripe", "plain"])
def test_non_ascii_signature_is_a_webhook_error(fmt):
    sig = WebhookSignature(header="Sig", format=fmt, algorithm="sha256", encoding="hex")
    trig = Trigger(name="evt", kind="webhook", signature=sig)
    import time as _t
    value = f"t={int(_t.time())},v1=é" if fmt == "stripe" else "sha256=é"
    with pytest.raises(WebhookError, match="mismatch"):
        parse_webhook(trig, {"Sig": value}, b"{}", secret="s")


# -- LLM: keys stay with their provider, the cache can't serve the wrong answer ---------------


def test_provider_key_is_not_sent_to_a_custom_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-provider-secret")
    monkeypatch.delenv("CONNECTORGEN_LLM_API_KEY", raising=False)
    custom = OpenAICompatibleLLM("m", provider="openai", base_url="https://llm.attacker.test/v1")
    assert custom.api_key is None and "authorization" not in custom.http.headers
    own = OpenAICompatibleLLM("m", provider="openai")
    assert own.api_key == "sk-provider-secret"
    explicit = OpenAICompatibleLLM("m", provider="openai", base_url="https://proxy.test/v1", api_key="k2")
    assert explicit.api_key == "k2"


class _Echo:
    def __init__(self, model, answer):
        self.model, self.answer = model, answer
        from connectorgen.llm.base import Usage
        self.usage = Usage()
        self.calls = 0

    def complete_json(self, **kw):
        self.calls += 1
        return self.answer


SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


def test_cache_key_includes_provider_and_endpoint(tmp_path):
    a = _Echo("same-model", {"x": 1})
    b = _Echo("same-model", {"x": 2})
    a.cache_identity, b.cache_identity = "openai:https://a.test:same-model", "custom:https://b.test:same-model"
    assert CachedLLM(a, tmp_path).complete_json(system="s", prompt="p", schema=SCHEMA) == {"x": 1}
    assert CachedLLM(b, tmp_path).complete_json(system="s", prompt="p", schema=SCHEMA) == {"x": 2}
    assert b.calls == 1


def test_planted_or_invalid_cache_entries_are_ignored(tmp_path):
    inner = _Echo("m", {"x": 5})
    cached = CachedLLM(inner, tmp_path)
    cached.complete_json(system="s", prompt="p", schema=SCHEMA)
    for f in tmp_path.glob("*.json"):
        f.write_text('{"x": "not an integer"}')
    assert cached.complete_json(system="s", prompt="p", schema=SCHEMA) == {"x": 5}
    assert inner.calls == 2


def test_cache_defaults_outside_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CONNECTORGEN_CACHE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert tmp_path not in default_cache_dir().parents


# -- smaller fixes ----------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="time.tzset is POSIX-only")
def test_retry_after_minus_0000_is_utc(monkeypatch):
    import time as _time

    monkeypatch.setenv("TZ", "America/Toronto")   # the bug only shows outside UTC
    _time.tzset()
    request_finalizer = _time.tzset
    try:
        _check_retry_after()
    finally:
        monkeypatch.delenv("TZ")
        request_finalizer()


def _check_retry_after():
    policy = RetryPolicy(clock=lambda: 1_700_000_000.0)   # 2023-11-14 22:13:20 UTC
    resp = httpx.Response(429, headers={"Retry-After": "Tue, 14 Nov 2023 22:14:20 -0000"})
    assert policy.server_wait(resp) == pytest.approx(60.0)


def test_mcp_connect_timeout_stays_a_timeout():
    hang = McpServerConfig(transport="stdio", command=sys.executable,
                           args=["-c", "import time; time.sleep(60)"])
    async def run():
        client = AsyncMcpClient(Connector(name="hang", mcp=hang))
        await asyncio.wait_for(client.__aenter__(), timeout=1.0)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(run())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_apply_keeps_permissions_and_every_backup(tmp_path):
    old = spec()
    path = tmp_path / "c.json"
    connector_from_openapi(old).save(path)
    os.chmod(path, 0o644)
    backups = set()
    for summary in ("one", "two"):           # two applies within the same second
        new = copy.deepcopy(old)
        new["paths"]["/items"]["get"]["summary"] = summary
        proposal = check_for_drift(Connector.load(path), spec=new, current_path=path)
        proposal.save(tmp_path / "proposal")
        backups.add(apply_proposal(tmp_path / "proposal", path))
    assert len(backups) == 2 and all(b.exists() for b in backups)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


# -- second review: bypasses of the first round of fixes ----------------------------------


@pytest.mark.parametrize("evil_path", ["@evil.test/items/{id}", ".evil.test/items/{id}", ":8080/items/{id}",
                                       "\\\\evil.test/items/{id}"])
def test_spec_paths_cannot_change_the_host(evil_path):
    s = spec()
    s["servers"] = [{"url": "https://api.acme.test"}]
    s["paths"][evil_path] = s["paths"].pop("/items/{id}")
    c = connector_from_openapi(s)
    assert all(a.path.startswith("/") for a in c.actions) and "Skipped 1 path" in " ".join(c.notes)
    old = connector_from_openapi(spec())
    assert check_for_drift(old, spec=s).verdict == "needs_review"   # the action disappeared


@respx.mock
@pytest.mark.parametrize("evil_path", ["@evil.test/x", ".evil.test/x", ":8080/x", "x"])
def test_runtime_keeps_requests_under_the_base_url(evil_path):
    c = connector_from_openapi(spec())
    c.base_url = "https://api.acme.test"
    c.action("get_item").path = evil_path          # e.g. a hand-edited or tampered connector file
    anything = respx.route().respond(json={})
    with ConnectorClient(c, Credentials(api_key="SECRET")) as client, \
            pytest.raises(ConnectorError, match="doesn't stay under the base URL"):
        client.execute("get_item", id="1")
    assert not anything.called


def test_deep_flow_yaml_does_not_crash_the_process(tmp_path):
    path = tmp_path / "deep.yaml"
    path.write_text('openapi: "3.0.3"\ninfo: {title: x, version: "1"}\npaths: {}\nx: ' + "[" * 30_000 + "]" * 30_000)
    code = ("from connectorgen.ingest.openapi import load_spec\n"
            "from connectorgen import UnsupportedSpecError\n"
            f"try:\n    load_spec({str(path)!r})\nexcept UnsupportedSpecError as e:\n    print('refused:', e)\n")
    import subprocess
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "refused" in out.stdout, (out.returncode, out.stderr[-300:])


def _fanout_spec(levels, width, how):
    schemas = {}
    for i in range(levels):
        nxt = {"$ref": f"#/components/schemas/L{i + 1}"}
        if how == "allOf":
            schemas[f"L{i}"] = {"allOf": [nxt] * width}
        else:
            props = {f"p{j}": nxt for j in range(width)}
            schemas[f"L{i}"] = {"type": "object", "properties": props,
                                **({"required": list(props)} if how == "required" else {})}
    schemas[f"L{levels}"] = {"type": "object"}
    body = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/L0"}}}}
    return {"openapi": "3.0.3", "info": {"title": "x", "version": "1"}, "servers": [{"url": "https://x.test"}],
            "paths": {"/a": {"post": {"operationId": "a", "requestBody": {**body, "required": True},
                                      "responses": {"200": {"description": "ok", **body}}}}},
            "components": {"schemas": schemas}}


@pytest.mark.parametrize("levels,width,how", [(12, 6, "required"), (8, 8, "allOf")])
def test_schema_ref_fanout_is_skipped_quickly_by_contract_tests(levels, width, how):
    import time as _time

    from connectorgen.validate import run_contract_tests
    c = connector_from_openapi(_fanout_spec(levels, width, how))
    started = _time.monotonic()
    report = run_contract_tests(c)
    assert report.skipped == 1 and _time.monotonic() - started < 20
    assert "too large" in " ".join(report.results[0].warnings)


def test_schema_ref_fanout_does_not_blow_up_the_diff():
    import time as _time
    old = _fanout_spec(7, 40, "optional")
    new = copy.deepcopy(old)
    new["info"]["version"] = "2"
    started = _time.monotonic()
    proposal = check_for_drift(connector_from_openapi(old), spec=new)
    assert _time.monotonic() - started < 30
    assert proposal.verdict == "needs_review"
    assert any(e.kind == "schema_too_large" for e in proposal.diff.entries)


def _body(s):
    return s["paths"]["/items/{id}"]["post"]["requestBody"]["content"]["application/json"]["schema"]


@pytest.mark.parametrize("mutate", [
    lambda s: _body(s).update(additionalProperties=False),
    lambda s: _body(s).update(minProperties=2),
    lambda s: _body(s).update(oneOf=[{"required": ["a"]}, {"required": ["b"]}]),
], ids=["additionalProperties", "minProperties", "oneOf"])
def test_top_level_body_constraints_are_not_safe(mutate):
    old = spec()
    _body(old).update(properties={"a": {"type": "string"}, "b": {"type": "string"}})
    new = copy.deepcopy(old)
    mutate(new)
    assert verdict(old, new) == "needs_review"


def test_top_level_response_type_change_is_breaking():
    old = spec()
    new = copy.deepcopy(old)
    new["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["type"] = "array"
    assert verdict(old, new) == "needs_review"


@pytest.mark.parametrize("config", [
    McpServerConfig(transport="http", url="https://mcp.test/mcp", headers={"Ocp-Apim-Subscription-Key": "0123456789abcdef0123"}),
    McpServerConfig(transport="http", url="https://mcp.test/mcp", headers={"X-Session-Id": "8f3a9c2b1d"}),
    McpServerConfig(transport="http", url="https://ghp_abcdefghijklmnop1234@mcp.test/mcp"),
    McpServerConfig(transport="http", url="https://mcp.test/mcp?k=sk_live_0123456789abcdef"),
    McpServerConfig(transport="http", url="https://mcp.test/s/ghp_abcdefghijklmnop1234/mcp"),
    McpServerConfig(transport="stdio", command="server-postgres", args=["postgresql://admin:hunter2@db/prod"]),
    McpServerConfig(transport="stdio", command="srv", args=["--connection-string", "Server=x;Password=hunter2"]),
    McpServerConfig(transport="stdio", command="srv", args=["--dsn", "mysql://root:pw@h/db"]),
    McpServerConfig(transport="stdio", command="docker", args=["run", "-e", "GITHUB_TOKEN=ghp_x", "img"]),
    McpServerConfig(transport="stdio", command="srv", args=["https://hooks.slack.com/services/T04QF2R9/B07HK3L1Z/q8Wk2vN4rT6yP1sD9fG3hJ5"]),
], ids=["subscription-key", "session-id", "userinfo", "query-value", "path-token", "dsn-arg",
        "conn-string", "dsn-flag", "env-assign", "slack-webhook"])
def test_mcp_secret_check_catches_more_shapes(config):
    assert config.secret_problems()


def test_check_does_not_read_local_files_named_by_a_connector(tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN PRIVATE KEY-----")
    c = Connector(name="d", source=str(secret), provenance={"connector": "llm:x"}, spec_hash="sha256:x")
    for a in c.actions:
        a.provenance = {"action": "llm:x"}
    from connectorgen.repair.check import _regenerate
    with pytest.raises(ConnectorError, match="pass them again explicitly"):
        _regenerate(c, "docs", spec=None, credentials=None, env=None, llm=FakeLLM([]))


@respx.mock
@pytest.mark.parametrize("link", ["https://xn--ap-acme-test.evil/", "https://аpi.acme.test/v1/items"])
def test_malformed_next_links_are_a_clear_error(link):
    respx.get(f"{BASE}/items").respond(json={"data": [{"id": 1}]},
                                       headers=[(b"Link", f'<{link}>; rel="next"'.encode())])
    with client_for() as client, pytest.raises(ConnectorError):
        list(client.paginate("list_items"))


@pytest.mark.parametrize("candidate", ["https://api.acme.com\t", "https://[::1].acme.com", "https://api.acme.com／v1",
                                       "https://evil.test\\@acme.com", "https://evil.test%2F@api.acme.com",
                                       "https://evil.test;.acme.com"])
def test_odd_docs_base_urls_are_refused_not_crashing(candidate):
    assert _safe_base_url(candidate, ["https://docs.acme.com/api"]) is False


@pytest.mark.parametrize("candidate,docs,ok", [
    ("https://evil.zendesk.com", "https://acme.zendesk.com/hc", False),
    ("https://evil.atlassian.net", "https://acme.atlassian.net/wiki", False),
    ("https://evil.wordpress.com", "https://acme.wordpress.com/", False),
    ("https://api.acme.com", "https://developer.acme.com/docs", True),
    ("https://api.acme.com/v2", "https://acme.com/docs", True),
    ("https://api.v2.acme.com", "https://acme.com/docs", False),    # deeper hosts: pass --base-url
    ("https://api.acme.com", "https://docs.api.acme.com/", False),   # nested docs host: pass --base-url
    ("https://api7.zendesk.com", "https://developer.zendesk.com/", False),
    ("https://rest.zendesk.com", "https://developer.zendesk.com/", False),
    ("https://api.zendesk.com", "https://docsacme.zendesk.com/hc", False),
    ("https://api.acme.com", "https://docs.attacker.acme.com/", False),
    ("https://attacker.my.salesforce.com", "https://developer.salesforce.com/docs/", False),
    ("https://attacker.zendesk.com", "https://developer.zendesk.com/api-reference/", False),
    ("https://script.google.com", "https://developers.google.com/drive/api", False),
    ("https://api.acme.com", "https://blog.acme.com/api", False),   # not a conventional docs host
])
def test_docs_hosts_only_vouch_for_their_own_domain(candidate, docs, ok):
    assert _safe_base_url(candidate, [docs]) is ok


@pytest.mark.parametrize("method,path,summary", [
    ("POST", "/cache/flush", ""), ("POST", "/db/reset", ""), ("POST", "/users/{id}/deactivate", ""),
    ("POST", "/users/{id}", "Deletes the user permanently"), ("PUT", "/config", ""),
    ("POST", "/accounts/{id}/close", ""), ("POST", "/jobs/{id}/kill", ""), ("PATCH", "/users/{id}", ""),
])
def test_docs_destructive_detection_is_conservative(method, path, summary):
    from connectorgen.ingest.docs import docs_action_destructive
    from connectorgen.ir import Action
    assert docs_action_destructive(Action(name="x", method=method, path=path, summary=summary))
    assert not docs_action_destructive(Action(name="create_note", method="POST", path="/notes",
                                              summary="Create a note"))


@respx.mock
def test_oldest_first_link_header_poller_sees_appended_items():
    feed = list(range(50))

    def handler(request):
        p = int(request.url.params.get("p", "0"))
        more = (p + 1) * 5 < len(feed)
        headers = {"Link": f'<{BASE}/items?p={p + 1}>; rel="next"'} if more else {}
        return httpx.Response(200, json={"data": [{"id": i} for i in feed[p * 5:(p + 1) * 5]]}, headers=headers)

    respx.get(url__startswith=f"{BASE}/items").mock(side_effect=handler)
    c = connector_from_openapi(spec())
    act = c.action("list_items")
    act.pagination.style, act.pagination.items_path = "link_header", "data"
    c.triggers = [Trigger(name="new_item", kind="polling", source_action="list_items",
                          order="oldest_first", dedup_key="id")]
    with ConnectorClient(c, Credentials(api_key="SECRET")) as client:
        poller = Poller(client, "new_item", max_pages=3)
        for _ in range(10):
            assert poller.poll().items == []
            if poller.store.load(poller.key)["initialized"]:
                break
        feed += [999]
        assert [i["id"] for i in poller.poll().items] == [999]


@respx.mock
def test_double_slash_paths_stay_on_the_api_host():
    c = connector_from_openapi(spec())
    c.base_url = "https://api.acme.test"
    c.action("get_item").path = "//evil.test/items/{id}"   # appended: https://api.acme.test//evil.test/...
    route = respx.route().respond(json={})
    with ConnectorClient(c, Credentials(api_key="SECRET")) as client:
        client.execute("get_item", id="1")
    assert route.calls.last.request.url.host == "api.acme.test"


@pytest.mark.parametrize("config", [
    McpServerConfig(transport="stdio", command="uvx", args=["mcp-server-fetch==2025.1.17"]),
    McpServerConfig(transport="stdio", command="srv", args=["--project-id", "0b5f6c1e-9d7a-4c3b-8f21-6a2d9e4b7c10",
                                                            "--model", "claude-sonnet-4-5-20250929"]),
    McpServerConfig(transport="stdio", command="srv", args=["--auth-server-url", "https://auth.acme.com",
                                                            "--tls-cert", "/etc/ssl/server.pem",
                                                            "--private-key-path", "~/.ssh/id_ed25519",
                                                            "--session-dir", "/tmp/x", "--auth", "none"]),
    McpServerConfig(transport="stdio", command="npx", args=["-y", "mcp-remote", "https://mcp.acme.com/sse",
                                                            "--header", "Authorization:${AUTH_HEADER}"]),
    McpServerConfig(transport="stdio", command="srv", env={"GOOGLE_APPLICATION_CREDENTIALS": "/home/me/sa.json",
                                                           "NODE_OPTIONS": "--max-old-space-size=4096",
                                                           "AUTH_MODE": "oauth"}),
    McpServerConfig(transport="http", url="https://mcp.acme.com/mcp?ws=acme&session_timeout=30m",
                    headers={"User-Agent": "connectorgen-client/1.2.3",
                             "X-Tenant-Id": "0b5f6c1e-9d7a-4c3b-8f21-6a2d9e4b7c10", "X-Auth-Mode": "oauth"}),
], ids=["pinned-version", "ids-and-models", "auth-ish-flags", "env-placeholder", "env-paths", "plain-headers"])
def test_mcp_secret_check_allows_ordinary_configs_too(config):
    assert config.secret_problems() == []


# -- third review --------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["- " * 100_000 + "a", '[ "]", ' * 100_000 + "1" + "]" * 100_000,
                                  "[ # ]\n" * 100_000 + "1" + "]" * 100_000, "? " * 100_000 + "a"],
                         ids=["dash-lists", "quoted-brackets", "commented-brackets", "explicit-keys"])
def test_deep_yaml_in_any_shape_is_refused_without_crashing(tmp_path, body):
    import subprocess
    path = tmp_path / "deep.yaml"
    path.write_text('openapi: "3.0.0"\ninfo: {title: t, version: "1"}\npaths: {}\nx:\n  ' + body)
    code = ("from connectorgen.ingest.openapi import load_spec\nfrom connectorgen import UnsupportedSpecError\n"
            f"try:\n    load_spec({str(path)!r})\nexcept UnsupportedSpecError as e:\n    print('refused:', e)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and "nested more than" in out.stdout, (out.returncode, out.stderr[-300:])


def _uneval_spec(levels=10):
    schemas = {f"S{i}": {"allOf": [{"$ref": f"#/components/schemas/S{i + 1}"}] * 2,
                         "unevaluatedProperties": False} for i in range(levels)}
    schemas[f"S{levels}"] = {"type": "object"}
    return {"openapi": "3.1.0", "info": {"title": "x", "version": "1"}, "servers": [{"url": "https://x.test"}],
            "paths": {"/a": {"post": {"operationId": "mk", "requestBody": {"required": True, "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/S0"}}}},
                "responses": {"200": {"description": "ok"}}}}},
            "components": {"schemas": schemas}}


def test_validation_of_hostile_schemas_is_bounded_everywhere():
    import time as _time

    from connectorgen.validate import run_contract_tests
    c = connector_from_openapi(_uneval_spec())
    started = _time.monotonic()
    assert run_contract_tests(c).skipped == 1
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with ConnectorClient(c, http=http, retry=RetryPolicy.disabled()) as client, \
            pytest.raises(ConnectorError, match="too complex to validate"):
        client.execute("mk", {"body": {}})
    assert _time.monotonic() - started < 30


def test_bounded_validator_handles_real_payloads():
    from connectorgen.limits import BoundedValidator
    schema = {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string"}}}}}
    big = [{"id": i, "tags": ["a", "b", "c"]} for i in range(20_000)]
    assert BoundedValidator(schema).best_error(big) is None


@pytest.mark.parametrize("mutate", [
    lambda s: _body(s).update(allOf=[{"additionalProperties": False}]),
    lambda s: _body(s).update(allOf=[{"maxProperties": 0}]),
    lambda s: _body(s).update(allOf=[{"not": {"required": ["a"]}}]),
], ids=["allOf-additionalProperties", "allOf-maxProperties", "allOf-not"])
def test_body_constraints_inside_allof_are_not_safe(mutate):
    old = spec()
    _body(old).update(properties={"a": {"type": "string"}})
    new = copy.deepcopy(old)
    mutate(new)
    assert verdict(old, new) == "needs_review"


def test_response_type_removed_or_wrapped_is_not_safe():
    old = spec()
    items = old["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    items["properties"]["count"] = {"type": "integer"}
    for change in ({"allOf": [{"type": "string"}]}, {"not": {"type": "integer"}}, {}):
        new = copy.deepcopy(old)
        new["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "properties"]["count"] = change
        assert verdict(old, new) == "needs_review", change


def test_deep_changes_are_seen():
    old = spec()
    node = _body(old)
    for _ in range(8):
        node["properties"] = {"k": {"type": "object"}}
        node = node["properties"]["k"]
    new = copy.deepcopy(old)
    deep = _body(new)
    for _ in range(8):
        deep = deep["properties"]["k"]
    deep.update(properties={"z": {"type": "string"}}, required=["z"])
    assert verdict(old, new) == "needs_review"


def test_endpoint_moves_need_review():
    old = spec()
    new = copy.deepcopy(old)
    new["paths"]["/items/{id}/purge"] = new["paths"].pop("/items/{id}")
    assert verdict(old, new) == "needs_review"
    renamed = copy.deepcopy(old)
    item = renamed["paths"].pop("/items/{id}")
    for op in item.values():
        for prm in op.get("parameters", []):
            prm["name"] = "itemId"
    renamed["paths"]["/items/{itemId}"] = item
    d = check_for_drift(connector_from_openapi(old), spec=renamed).diff
    assert [e.severity for e in d.entries if e.kind == "endpoint_moved"] == ["info", "info"]


@pytest.mark.parametrize("config", [
    McpServerConfig(transport="stdio", command="npx", args=["-y", "mcp-remote", "https://mcp.acme.com/mcp",
                                                            "--header", "Authorization:Bearer abc123def456ghi"]),
    McpServerConfig(transport="stdio", command="sh", args=["-c", "npx -y @acme/mcp --token abc123def456"]),
    McpServerConfig(transport="stdio", command="npx", args=["telegram-mcp", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"]),
    McpServerConfig(transport="http", url="https://mcp.acme.com/mcp#access_token=abc123def456"),
], ids=["header-arg", "nested-command", "telegram", "fragment"])
def test_mcp_secret_check_third_round(config):
    assert config.secret_problems()


def test_mcp_override_for_values_known_not_to_be_secret():
    cfg = McpServerConfig(transport="stdio", command=sys.executable,
                          args=[str(__import__("pathlib").Path(__file__).parent / "fixtures" / "mcp_server.py"),
                                "--token", "not-a-secret-just-a-name"])
    with pytest.raises(ConnectorError, match="allow-secret-like-values"):
        connector_from_mcp(cfg, env={"NOTES_TOKEN": "x"})
    assert connector_from_mcp(cfg, env={"NOTES_TOKEN": "x"}, allow_secret_like_values=True).actions


@respx.mock
@pytest.mark.parametrize("link", ["https://[::1/", "https://api.acme.test:abc/"])
def test_unparseable_links_are_a_clear_error(link):
    respx.get(f"{BASE}/items").respond(json={"data": [{"id": 1}]}, headers={"Link": f'<{link}>; rel="next"'})
    with client_for() as client, pytest.raises(ConnectorError):
        list(client.paginate("list_items"))


@respx.mock
def test_redirect_to_invalid_idn_host_is_a_clear_error():
    respx.get(f"{BASE}/items/1").respond(302, headers={"Location": "https://xn--/x"})
    with client_for() as client, pytest.raises(ConnectorError):
        client.execute("get_item", id="1")


@respx.mock
@pytest.mark.parametrize("max_pages,page_size", [(1, 5), (2, 5), (10, 10)])
@pytest.mark.parametrize("style", ["offset", "page"])
def test_oldest_first_offset_and_page_lists_always_progress(style, max_pages, page_size):
    feed = list(range(23))

    def handler(request):
        q = request.url.params
        if style == "offset":
            o = int(q.get("offset", 0))
        else:
            o = (int(q.get("offset", 1)) - 1) * page_size
        return httpx.Response(200, json=[{"id": i} for i in feed[o:o + page_size]])

    respx.get(f"{BASE}/events").mock(side_effect=handler)
    c = _offset_list_connector("oldest_first")
    act = c.action("list_events")
    act.pagination.style, act.pagination.limit_param = style, None
    with ConnectorClient(c) as client:
        poller = Poller(client, "new_event", max_pages=max_pages)
        for _ in range(30):
            poller.poll()
            if poller.store.load(poller.key)["initialized"]:
                break
        else:
            pytest.fail("baseline never finished")
        feed.append(100)
        got = [i["id"] for _ in range(3) for i in poller.poll().items]
    assert got == [100]


@pytest.mark.parametrize("resume", [{"offset": "https://evil.test/x"}, {"offset": -5}, {"x": 1},
                                    {"__url__": "https://evil.test\\\\@api.acme.test/v1/events"}])
def test_damaged_saved_state_restarts_safely(resume):
    c = _offset_list_connector("oldest_first")
    with respx.mock:
        route = respx.get(url__startswith=BASE).respond(json=[])
        with ConnectorClient(c) as client:
            poller = Poller(client, "new_event")
            poller.store.save(poller.key, {"seen": [], "initialized": True, "resume": resume})
            try:
                poller.poll()
            except ConnectorError:
                pass
    assert all(call.request.url.host == "api.acme.test" for call in route.calls)
    assert all("evil" not in str(call.request.url).split("?")[0] for call in route.calls)


@pytest.mark.parametrize("method,path,name,summary,expected", [
    ("POST", "/containers/prune", "prune", "", True),
    ("POST", "/conversations.kick", "kick", "", True),
    ("POST", "/keys/{id}/rotate", "rotate_key", "", True),
    ("POST", "/charges/{id}/refund", "refund", "", True),
    ("POST", "/items/bulkDelete", "bulk", "", True),
    ("POST", "/v1/query", "run_query", "Runs a SQL statement", True),
    ("POST", "/dropbox/files", "list_dropbox_files", "", False),
    ("POST", "/forms", "create_form", "Create a form with dropdown fields.", False),
    ("POST", "/search", "search", "Search items. Deleted items are excluded.", False),
    ("POST", "/orders", "create_order", "Create an order. Orders can be cancelled later.", False),
])
def test_docs_destructive_words(method, path, name, summary, expected):
    from connectorgen.ingest.docs import docs_action_destructive
    from connectorgen.ir import Action
    assert docs_action_destructive(Action(name=name, method=method, path=path, summary=summary)) is expected


def test_swapped_path_placeholders_need_review():
    old = spec()
    old["paths"]["/repos/{owner}/{repo}"] = {"get": {"operationId": "getRepo", "parameters": [
        {"name": "owner", "in": "path", "required": True, "schema": {"type": "string"}},
        {"name": "repo", "in": "path", "required": True, "schema": {"type": "string"}}],
        "responses": {"200": {"description": "ok"}}}}
    new = copy.deepcopy(old)
    new["paths"]["/repos/{repo}/{owner}"] = new["paths"].pop("/repos/{owner}/{repo}")
    assert verdict(old, new) == "needs_review"


# -- fourth review ---------------------------------------------------------------------------


@respx.mock
def test_catastrophic_pattern_in_a_spec_cannot_hang_execute():
    import time as _time
    s = spec()
    s["paths"]["/items"]["get"]["parameters"][0]["schema"] = {"type": "string", "pattern": r"^(a|aa)+$"}
    c = connector_from_openapi(s)
    respx.get(url__startswith=BASE).respond(json={"data": []})
    started = _time.monotonic()
    with ConnectorClient(c, Credentials(api_key="SECRET")) as client, \
            pytest.raises(ConnectorError, match="too complex|takes too long"):
        client.execute("list_items", cursor="a" * 60 + "b")
    assert _time.monotonic() - started < 10


@pytest.mark.parametrize("schema,instance,valid", [
    ({"type": "string", "pattern": r"^(\w+\s?)*$"}, "an ordinary looking cursor value that is long!", False),
    ({"type": "array", "uniqueItems": True}, [{"i": i} for i in range(20_000)], True),
    ({"type": "array", "items": {"enum": [f"v{i}" for i in range(20_000)]}}, [f"v{i}" for i in range(300)], True),
    ({"enum": [1]}, True, False), ({"enum": [1, "x"]}, 1.0, True),
    ({"type": "array", "uniqueItems": True}, [1, 1.0], False), ({"type": "array", "uniqueItems": True}, [1, True], True),
])
def test_bounded_validator_is_fast_and_keeps_json_schema_semantics(schema, instance, valid):
    import time as _time

    from connectorgen.limits import BoundedValidator
    started = _time.monotonic()
    assert (BoundedValidator(schema).best_error(instance) is None) is valid
    assert _time.monotonic() - started < 5


def test_plain_re_is_untouched_outside_bounded_validation():
    import re as _re

    from jsonschema import Draft202012Validator
    assert Draft202012Validator({"pattern": "^a+$"}).is_valid("aaa")
    assert _re.search("a+", "aaa")


@respx.mock
@pytest.mark.parametrize("style", ["page", "cursor", "link_header"])
def test_server_pages_bigger_than_max_items_lose_nothing(style):
    page_size = 50
    feed = list(range(70))

    def handler(request):
        q = request.url.params
        if style == "page":
            start = (int(q.get("cursor", 1)) - 1) * page_size
        else:
            start = int(q.get("cursor", q.get("o", 0)))
        chunk = feed[start:start + page_size]
        body, headers = {"data": [{"id": i} for i in chunk]}, {}
        more = start + page_size < len(feed)
        if style == "cursor" and more:
            body["next"] = str(start + page_size)
        if style == "link_header" and more:
            headers["Link"] = f'<{BASE}/items?o={start + page_size}>; rel="next"'
        return httpx.Response(200, json=body, headers=headers)

    respx.get(url__startswith=f"{BASE}/items").mock(side_effect=handler)
    c = connector_from_openapi(spec())
    act = c.action("list_items")
    act.pagination.style, act.pagination.items_path = style, "data"
    act.pagination.request_param = "cursor"
    act.pagination.response_cursor_path = "next" if style == "cursor" else None
    act.params[0].json_schema = {"type": ["string", "integer"]}   # page numbers are integers
    c.triggers = [Trigger(name="t", kind="polling", source_action="list_items",
                          order="oldest_first", dedup_key="id")]
    with ConnectorClient(c, Credentials(api_key="SECRET")) as client:
        poller = Poller(client, "t", max_items=10, max_pages=2)
        for _ in range(20):
            poller.poll()
            if poller.store.load(poller.key)["initialized"]:
                break
        feed.extend(range(1000, 1032))
        got = [i["id"] for _ in range(10) for i in poller.poll().items]
    assert sorted(got) == list(range(1000, 1032))


def test_yaml_merge_key_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(openapi_mod, "MAX_SPEC_NODES", 200_000)
    keys = "\n".join(f"  k{i}: v" for i in range(500))
    items = "\n".join("  - {<<: *m0}" for _ in range(2_000))
    text = f'openapi: "3.0.0"\ninfo: {{title: t, version: "1"}}\npaths: {{}}\nm: &m0\n{keys}\nlist:\n{items}\n'
    with pytest.raises(UnsupportedSpecError, match="merge keys|expands"):
        parse_spec_text(text)


def test_huge_enums_import_in_linear_time():
    import time as _time
    s = spec()
    s["paths"]["/items"]["get"]["parameters"][0]["schema"] = {"type": "string",
                                                               "enum": [f"v{i}" for i in range(100_000)] + ["v1"]}
    started = _time.monotonic()
    c = connector_from_openapi(s)
    assert _time.monotonic() - started < 10
    assert len(c.action("list_items").params[0].json_schema["enum"]) == 100_000


@pytest.mark.parametrize("config,secret", [
    (McpServerConfig(transport="stdio", command="docker",
                     args=["run", "--env=GITHUB_PERSONAL_ACCESS_TOKEN=ghp_abcdefghijklmnop1234", "img"]), True),
    (McpServerConfig(transport="stdio", command="npx", args=["@smithery/cli", "run", "x", "--config",
                                                             '{"apiKey": "a8F3kQ9zLm2Xr7Tp"}']), True),
    (McpServerConfig(transport="stdio", command="srv", env={"SSH_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc"}), True),
    (McpServerConfig(transport="stdio", command="srv", args=["--password", "correcthorse"]), True),
    (McpServerConfig(transport="stdio", command="srv", env={"BRAVE_API_KEY": "<YOUR_API_KEY>"}), False),
    (McpServerConfig(transport="stdio", command="srv", env={"AWS_SECRET_ACCESS_KEY": "YOUR_SECRET_ACCESS_KEY_HERE"}), False),
    (McpServerConfig(transport="stdio", command="srv", args=["--auth-token", "YOUR_SENTRY_TOKEN"]), False),
    (McpServerConfig(transport="stdio", command="srv", env={"SLACK_BOT_TOKEN": "xoxb-your-bot-token"}), False),
    (McpServerConfig(transport="stdio", command="srv", env={"API_KEY": "${env:API_KEY}", "API_KEY_SOURCE": "keychain"}), False),
    (McpServerConfig(transport="stdio", command="npx", args=["-y", "@modelcontextprotocol/server-postgres",
                                                             "postgresql://postgres@localhost:5432/mydb"]), False),
    (McpServerConfig(transport="stdio", command="npx", args=["-y", "@modelcontextprotocol/server-postgres",
                                                             "postgresql://user:password@localhost/db"]), False),
], ids=["env-flag-assign", "json-config", "pem", "password-word", "angle-placeholder", "here-placeholder",
        "your-placeholder", "your-bot-token", "vscode-env+keychain", "pg-user-only", "pg-readme-placeholder"])
def test_mcp_secret_check_fourth_round(config, secret):
    assert bool(config.secret_problems()) is secret
