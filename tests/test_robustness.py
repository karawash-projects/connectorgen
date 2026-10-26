"""Regression tests for quirks found by running hundreds of real-world specs
(scripts/corpus_check.py over a folder of public API specs)."""

import pytest
import respx

from connectorgen import ConnectorClient, ConnectorError, UnsupportedSpecError, connector_from_openapi
from connectorgen.ingest.openapi import load_spec, normalize_schema, parse_spec_text
from connectorgen.validate import run_contract_tests


def spec_yaml(tmp_path, text):
    path = tmp_path / "openapi.yaml"
    path.write_text(text)
    return path


def base(paths: dict, **extra) -> dict:
    return {"openapi": "3.0.3", "info": {"title": "Q", "version": "1"},
            "servers": [{"url": "https://q.test"}], "paths": paths, **extra}


def get_op(op_id: str, **op) -> dict:
    return {"get": {"operationId": op_id, "responses": {"200": {"description": "ok"}}, **op}}


# ------------------------------------------------------------------ YAML loading


def test_yaml_dates_stay_strings_and_keys_stay_as_written():
    data = parse_spec_text("""
openapi: 3.0.0
example: 2024-01-31
when: 2024-01-31T10:00:00Z
codes:
  200: ok
  18_24: young adults
  on: lights
""")
    assert data["example"] == "2024-01-31" and data["when"] == "2024-01-31T10:00:00Z"
    assert set(data["codes"]) == {"200", "18_24", "on"}


def test_yaml_unknown_tags_and_control_characters():
    assert parse_spec_text("a: !custom thing\nb: !seq [1, 2]\n") == {"a": "thing", "b": [1, 2]}
    assert parse_spec_text("a: hi\x07there\n") == {"a": "hithere"}


def test_malformed_yaml_says_where():
    with pytest.raises(UnsupportedSpecError, match=r"line 3, column 1"):
        parse_spec_text("openapi: 3.0.0\npaths:\n\tbad: tab\n")


def test_large_yaml_uses_fast_loader(tmp_path):
    import time
    paths = "\n".join(f"  /r{i}:\n    get:\n      operationId: r{i}\n      responses:\n"
                      f"        '200': {{description: ok}}" for i in range(3000))
    path = spec_yaml(tmp_path, f"openapi: 3.0.0\ninfo: {{title: big, version: '1'}}\npaths:\n{paths}\n")
    start = time.perf_counter()
    assert len(load_spec(path)["paths"]) == 3000
    assert time.perf_counter() - start < 5


# ------------------------------------------------------------------ $ref pointers


def test_percent_encoded_and_list_index_refs():
    s = base({
        "/users/{id}": {"get": {"operationId": "getUser",
                                "parameters": [{"name": "id", "in": "path", "required": True,
                                                "schema": {"type": "integer"}}],
                                "responses": {"200": {"description": "ok"}}}},
        "/again/{id}": {"get": {"operationId": "again",
                                "parameters": [{"name": "id", "in": "path", "required": True,
                                                "schema": {"$ref": "#/paths/~1users~1%7Bid%7D/get/parameters/0/schema"}}],
                                "responses": {"200": {"description": "ok"}}}},
    })
    c = connector_from_openapi(s)
    assert c.action("again").params[0].json_schema == {"type": "integer"}


def test_schema_names_with_special_characters():
    page_ref = {"$ref": "#/components/schemas/Page%20Of%20Items"}
    op = get_op("x", responses={"200": {"description": "ok",
                                        "content": {"application/json": {"schema": page_ref}}}})
    s = base({"/x": op}, components={"schemas": {"Page Of Items": {
        "type": "object", "properties": {"id": {"type": "integer"}}}}})
    c = connector_from_openapi(s)
    assert "Page Of Items" in c.schemas
    assert len(c.actions) == 1 and run_contract_tests(c).passed == 1


# ------------------------------------------------------------------ schema quirks


@pytest.mark.parametrize("raw,expected", [
    ({"type": "integer", "minimum": 0, "exclusiveMinimum": True}, {"type": "integer", "exclusiveMinimum": 0}),
    ({"type": "integer", "maximum": 9, "exclusiveMaximum": False}, {"type": "integer", "maximum": 9}),
    ({"enum": ["a", "a", "b"]}, {"enum": ["a", "b"]}),
    ({"type": "string", "pattern": "/^[a-z]+$/"}, {"type": "string", "pattern": "^[a-z]+$"}),
    ({"type": "string", "pattern": "^\\p{L}+$"}, {"type": "string", "x-pattern": "^\\p{L}+$"}),
    ({"type": "application/json"}, {"x-type": "application/json"}),
])
def test_schema_normalization(raw, expected):
    assert normalize_schema(raw) == expected


def test_wrongly_typed_defaults_are_not_used_as_examples():
    s = base({"/x": {"post": {"operationId": "x", "requestBody": {"required": True, "content": {
        "application/json": {"schema": {"type": "object", "required": ["size", "flag"], "properties": {
            "size": {"type": "integer", "default": "100"},
            "flag": {"type": "boolean", "default": "false"}}}}}},
        "responses": {"200": {"description": "ok"}}}}})
    assert run_contract_tests(connector_from_openapi(s)).ok


# ------------------------------------------------------------------ parameters


def test_runtime_owned_headers_and_duplicate_names():
    s = base({"/lists/{list_id}": {"patch": {
        "operationId": "patchList",
        "parameters": [
            {"name": "list_id", "in": "path", "required": True, "schema": {"type": "integer"}},
            {"name": "list_id", "in": "query", "schema": {"type": "integer"}},
            {"name": "Content-Type", "in": "header", "schema": {"type": "string"}}],
        "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
        "responses": {"200": {"description": "ok"}}}}})
    act = connector_from_openapi(s).action("patch_list")
    assert [(p.name, p.location) for p in act.params] == [("list_id", "path")]
    assert any("Duplicate parameter names" in n for n in act.notes)


def test_query_string_inside_path_template():
    s = base({"/export?filename={filename}": {"get": {
        "operationId": "export", "parameters": [{"name": "filename", "in": "path", "required": True,
                                                 "schema": {"type": "string"}}],
        "responses": {"200": {"description": "ok"}}}}})
    act = connector_from_openapi(s).action("export")
    assert act.path == "/export" and act.params[0].location == "query"


@respx.mock
def test_pre_encoded_form_body_is_sent_as_is():
    s = base({"/detect": {"post": {"operationId": "detect", "requestBody": {"content": {
        "application/x-www-form-urlencoded": {"schema": {"type": "string"}}}},
        "responses": {"200": {"description": "ok"}}}}})
    route = respx.post("https://q.test/detect").respond(json={})
    with ConnectorClient(connector_from_openapi(s)) as client:
        client.execute("detect", body="text=I like tacos")
    req = route.calls.last.request
    assert req.content == b"text=I like tacos"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"


# ------------------------------------------------------------------ servers and auth


def test_relative_server_url_is_explained():
    s = base({"/x": get_op("x")})
    s["servers"] = [{"url": "/api/v1"}]
    c = connector_from_openapi(s)
    assert any("relative" in n for n in c.notes)
    assert len(c.actions) == 1 and run_contract_tests(c).passed == 1                       # offline tests still run
    with pytest.raises(ConnectorError, match="relative.*https://your-host/api/v1"):
        ConnectorClient(c)
    with ConnectorClient(c, base_url="https://real.test/api/v1"):
        pass


def test_unsupported_auth_is_explained_not_a_failure():
    s = base({"/x": get_op("x", security=[{"hmac": []}])},
             components={"securitySchemes": {"hmac": {"type": "http", "scheme": "acme-hmac"}}})
    c = connector_from_openapi(s)
    assert "acme-hmac" in c.auth[0].description
    report = run_contract_tests(c)
    assert len(c.actions) == 1 and report.skipped == 1 and report.ok
    with ConnectorClient(c) as client, pytest.raises(ConnectorError, match="can't perform yet"):
        client.execute("x")


def test_yaml_follows_1_2_core_schema():
    data = parse_spec_text("required: [url, on, yes, no]\nflag: true\nnum: 1_000\noct: 0777\nf: 1.5\nn: 12\n")
    assert data["required"] == ["url", "on", "yes", "no"]          # not booleans
    assert data["flag"] is True and data["num"] == "1_000" and data["oct"] == 777
    assert data["f"] == 1.5 and data["n"] == 12


def test_undeclared_path_placeholder_is_added():
    act = connector_from_openapi(base({"/v2/{apikey}/places": get_op("places")})).action("places")
    assert [(p.name, p.location, p.required) for p in act.params] == [("apikey", "path", True)]
    assert run_contract_tests(connector_from_openapi(base({"/v2/{apikey}/places": get_op("places")}))).ok


def test_items_list_means_any_of():
    assert normalize_schema({"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}) == \
        {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}


def test_server_url_placeholders_without_defaults():
    s = base({"/places": get_op("places")})
    s["servers"] = [{"url": "https://api.test/v2/{apikey}"}]
    c = connector_from_openapi(s)
    assert any("placeholders {apikey}" in n for n in c.notes)
    assert run_contract_tests(c).passed == 1
    with pytest.raises(ConnectorError, match="unfilled placeholders"):
        ConnectorClient(c)
