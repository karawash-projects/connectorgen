"""Fixing connectors when the live API disagrees with an unchanged spec."""

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from conftest import plain
from connectorgen import Connector, ConnectorClient, Credentials, connector_from_openapi
from connectorgen.cli import app
from connectorgen.ir import McpServerConfig
from connectorgen.llm import FakeLLM
from connectorgen.mcp import connector_from_mcp
from connectorgen.repair import apply_proposal, check_for_drift
from connectorgen.repair.live import different_shape, propose_live_fixes, reconcile_output
from connectorgen.repair.observe import enum_findings, infer_shape, merge_shapes
from connectorgen.validate.smoke import run_smoke_tests

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://us.api.petstore.test/v1"
CREDS = Credentials(api_key="k")


def smoke(c, **kw):
    return run_smoke_tests(c, CREDS, sleep=lambda s: None, **kw)


def valid(c, action, data):
    from jsonschema import Draft202012Validator
    schema = {**c.action(action).output_schema, "$defs": c.schemas}
    return not list(Draft202012Validator(schema).iter_errors(data))


# ------------------------------------------------------------------ observation


def test_shapes_keep_no_values():
    data = {"id": "p-7", "email": "jane@example.com", "tags": ["vip"], "age": 41, "x": None}
    shape = infer_shape(data)
    dumped = json.dumps(shape)
    assert "jane" not in dumped and "p-7" not in dumped and "vip" not in dumped and "41" not in dumped
    assert shape["properties"]["age"] == {"type": "integer"}
    assert shape["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}


def test_merging_shapes_of_list_items():
    items = infer_shape([{"id": 1, "nick": "a"}, {"id": 2, "nick": None}, {"id": 3}])["items"]
    assert items["required"] == ["id"]                       # only fields present in every item
    assert items["properties"]["nick"]["type"] == ["null", "string"]
    assert merge_shapes({"type": "object"}, {"type": "null"})["type"] == ["null", "object"]


def test_enum_findings_keep_only_short_codes():
    schema = {"type": "object", "properties": {
        "status": {"enum": ["open", "closed"]}, "note": {"enum": ["a"]}}}
    found = enum_findings(schema, {"status": "pending_review", "note": "Call Jane at 555-0100 tomorrow"})
    assert found == [{"path": ["status"], "value": "pending_review"}]


# ------------------------------------------------------------------ reconciliation


SPEC = {"type": "object", "required": ["id", "name"], "properties": {
    "id": {"type": "integer"}, "name": {"type": "string"},
    "status": {"type": "string", "enum": ["open", "closed"]},
    "tags": {"type": "array", "items": {"type": "string"}}}}


def test_reconcile_widens_types_and_relaxes_required():
    observed = infer_shape({"id": "p-7", "status": "open", "tags": [1], "owner": {"id": 3}})
    new, changes = reconcile_output(SPEC, observed, [{"path": ["status"], "value": "archived"}])
    assert new["properties"]["id"]["type"] == ["integer", "string"]
    assert new["required"] == ["id"]                              # name wasn't returned
    assert "owner" in new["properties"] and "required" not in new["properties"]["owner"]
    assert new["properties"]["tags"]["items"]["type"] == ["integer", "string"]
    assert new["properties"]["status"]["enum"] == ["open", "closed", "archived"]
    assert any("name: not always present" in c for c in changes)
    assert reconcile_output(new, observed, [{"path": ["status"], "value": "archived"}])[1] == []


def test_reconcile_nullable_and_anyof():
    spec = {"anyOf": [{"type": "string"}, {"type": "object", "properties": {"id": {"type": "integer"}}}]}
    new, changes = reconcile_output(spec, {"type": "array", "items": {"type": "string"}})
    assert len(new["anyOf"]) == 3 and "also returned as array" in changes[0]
    new, changes = reconcile_output({"type": "integer"}, {"type": "null"})
    assert new["type"] == ["integer", "null"] and changes == ["response: can be null"]


def test_guard_refuses_different_shapes():
    assert "needs a person" in different_shape({"type": "array"}, infer_shape({"error": "x"}))
    envelope = infer_shape({"error": {"code": 500}})
    assert "required fields are missing" in different_shape(SPEC, envelope)
    assert different_shape(SPEC, infer_shape({"id": 1, "name": "x", "extra": True})) is None


# ------------------------------------------------------------------ end to end (HTTP)


@pytest.fixture
def connector_file(tmp_path):
    spec_path = tmp_path / "petstore.yaml"
    spec_path.write_text((FIXTURES / "petstore.yaml").read_text())
    c = connector_from_openapi(spec_path)
    path = tmp_path / "pets.json"
    c.save(path)
    return spec_path, path


LIVE_PET = {"id": "p-7", "name": "Rex", "tag": None, "owner": {"login": "jane"}}  # id is now a string


@respx.mock
def test_output_drift_fixed_from_smoke_report(connector_file, tmp_path):
    _, path = connector_file
    c = Connector.load(path)
    respx.get(f"{BASE}/pets/7").respond(json=LIVE_PET)
    report = smoke(c, actions=["get_pet"], global_params={"petId": 7})
    assert report.results[0].status == "drift"
    saved = json.dumps(report.to_dict())
    assert "jane" not in saved and "p-7" not in saved        # evidence carries no response data

    p = check_for_drift(c, smoke_report=report.to_dict(), fix_live=True, current_path=path,
                        used_actions=["get_pet"])
    assert p.verdict == "live_fix"
    fixed = p.candidate
    assert valid(fixed, "get_pet", LIVE_PET) and not valid(c, "get_pet", LIVE_PET)
    assert fixed.schemas["Pet"] == c.schemas["Pet"]          # shared schema untouched
    assert fixed.action("get_pet").provenance["output_schema"] == "observed (live API)"
    assert p.affected_used == ["get_pet"]
    assert "Fixes from the live API" in p.to_text()

    p.save(tmp_path / "repair")
    apply_proposal(tmp_path / "repair", path)
    with ConnectorClient(Connector.load(path), CREDS, validate_responses="raise") as client:
        assert client.execute("get_pet", petId=7)["id"] == "p-7"


@respx.mock
def test_verify_live_confirms_fix(connector_file):
    _, path = connector_file
    c = Connector.load(path)
    respx.get(f"{BASE}/pets/7").respond(json=LIVE_PET)
    report = smoke(c, actions=["get_pet"], global_params={"petId": 7})
    _, fixes = propose_live_fixes(c, report, verify_live=True, credentials=CREDS,
                                  global_params={"petId": 7})
    assert fixes.accepted[0].verified == "live"


@respx.mock
def test_error_envelope_with_200_is_not_auto_fixed(connector_file):
    _, path = connector_file
    c = Connector.load(path)
    respx.get(f"{BASE}/orders").respond(json={"error": "maintenance"})   # spec says array
    report = smoke(c, actions=["get_orders"])
    p = check_for_drift(c, smoke_report=report, fix_live=True)
    assert p.verdict == "live_drift"
    assert "needs a person" in p.live_fixes["fixes"][0]["reason"]


def region_fix(confidence="high", location="query"):
    return {"diagnosis": "The API now requires a region query parameter.", "confidence": confidence,
            "operations": [{"op": "add_param", "name": "region", "location": location,
                            "new_name": "", "required": True, "type": "string",
                            "enum_values": ["us", "eu"], "description": "Data region"}]}


def region_api():
    respx.get(f"{BASE}/pets/7").mock(side_effect=lambda r: httpx.Response(200, json={"id": 7, "name": "Rex"})
                                     if r.url.params.get("region") else
                                     httpx.Response(400, json={"error": "missing required parameter: region"}))


@respx.mock
def test_rejected_request_fixed_by_model_and_verified_live(connector_file):
    _, path = connector_file
    c = Connector.load(path)
    region_api()
    report = smoke(c, actions=["get_pet"], global_params={"petId": 7})
    assert report.results[0].http_status == 400 and "region" in report.results[0].error_body

    llm = FakeLLM([region_fix()])
    p = check_for_drift(c, smoke_report=report, fix_live=True, llm=llm, verify_live=True,
                        credentials=CREDS, global_params={"petId": 7, "region": "eu"})
    assert p.verdict == "live_fix"
    fix = p.live_fixes["fixes"][0]
    assert fix["accepted"] and fix["verified"] == "live"
    region = next(x for x in p.candidate.action("get_pet").params if x.name == "region")
    assert region.required and region.json_schema["enum"] == ["us", "eu"]
    assert "<error>" in llm.calls[0].prompt and "missing required parameter: region" in llm.calls[0].prompt


@pytest.mark.parametrize("proposal,reason", [
    (region_fix("low"), "confidence low"),
    (region_fix(location="path"), "can't add path param"),
    ({"diagnosis": "Bad id.", "confidence": "high", "operations": []}, "nothing to change"),
])
@respx.mock
def test_model_fixes_rejected(connector_file, proposal, reason):
    _, path = connector_file
    c = Connector.load(path)
    region_api()
    report = smoke(c, actions=["get_pet"], global_params={"petId": 7})
    _, fixes = propose_live_fixes(c, report, llm=FakeLLM([proposal]))
    assert not fixes.accepted and reason in fixes.fixes[0].reason


@respx.mock
def test_fix_that_fails_live_is_reverted(connector_file):
    _, path = connector_file
    c = Connector.load(path)
    respx.get(f"{BASE}/pets/7").respond(400, json={"error": "missing required parameter: region"})
    report = smoke(c, actions=["get_pet"], global_params={"petId": 7})
    patched, fixes = propose_live_fixes(c, report, llm=FakeLLM([region_fix()]), verify_live=True,
                                        credentials=CREDS, global_params={"petId": 7, "region": "eu"})
    assert fixes.fixes[0].verified == "failed" and not fixes.accepted
    assert patched.action("get_pet") == c.action("get_pet")


@respx.mock
def test_without_a_model_rejections_are_reported(connector_file):
    _, path = connector_file
    c = Connector.load(path)
    region_api()
    report = smoke(c, actions=["get_pet"], global_params={"petId": 7})
    _, fixes = propose_live_fixes(c, report)
    assert "pass a model to diagnose it" in fixes.skipped[0]


@respx.mock
def test_live_fixes_survive_regeneration(connector_file):
    spec_path, path = connector_file
    c = Connector.load(path)
    respx.get(f"{BASE}/pets/7").respond(json=LIVE_PET)
    report = smoke(c, actions=["get_pet"], global_params={"petId": 7})
    fixed = check_for_drift(c, smoke_report=report, fix_live=True).candidate

    spec = yaml.safe_load(spec_path.read_text())       # later, the spec changes elsewhere
    spec["paths"]["/toys"] = {"get": {"operationId": "listToys", "responses": {"200": {"description": "ok"}}}}
    spec_path.write_text(yaml.safe_dump(spec))
    p = check_for_drift(fixed)
    assert valid(p.candidate, "get_pet", LIVE_PET)      # fix re-applied onto the new version
    assert any("re-applied live response fixes" in n for n in p.notes)


def test_cli_fix_live_needs_report(connector_file):
    _, path = connector_file
    r = CliRunner().invoke(app, ["check", str(path), "--fix-live"])
    assert r.exit_code != 0 and "--smoke-report" in plain(r.output)


@respx.mock
def test_cli_fix_live(connector_file, tmp_path, monkeypatch):
    _, path = connector_file
    monkeypatch.setenv("CONNECTORGEN_API_KEY", "k")
    respx.get(f"{BASE}/pets/7").respond(json=LIVE_PET)
    runner = CliRunner()
    smoke_json = tmp_path / "smoke.json"
    runner.invoke(app, ["smoke", str(path), "-a", "get_pet", "--param", "petId=7",
                        "--json", str(smoke_json), "--delay", "0"])
    r = runner.invoke(app, ["check", str(path), "--smoke-report", str(smoke_json), "--fix-live",
                            "--verify-live", "--param", "petId=7", "-o", str(tmp_path / "repair")])
    assert r.exit_code == 20, r.output                  # live fixes always go to review
    assert "Fixes from the live API" in r.output and "(output, live)" in r.output


# ------------------------------------------------------------------ MCP: a real server that changed


def test_mcp_tool_rejection_fixed_against_real_server():
    config = McpServerConfig(transport="stdio", command=sys.executable,
                             args=[str(FIXTURES / "mcp_server.py")],
                             env_passthrough=["NOTES_TOKEN", "NOTES_VARIANT"])
    v1 = connector_from_mcp(config, env={"NOTES_TOKEN": "x", "NOTES_VARIANT": "v1"})
    v2_env = {"NOTES_TOKEN": "x", "NOTES_VARIANT": "v2"}
    # the server was upgraded in place, but its tool list... we only have the old connector
    report = run_smoke_tests(v1, None, actions=["add"], global_params={"a": 1, "b": 2},
                             env=v2_env, sleep=lambda s: None)
    assert report.results[0].status == "error" and "precision" in report.results[0].error_body

    fix = {"diagnosis": "add now requires an integer 'precision' argument.", "confidence": "high",
           "operations": [{"op": "add_param", "name": "precision", "location": "", "new_name": "",
                           "required": True, "type": "integer", "enum_values": [], "description": ""}]}
    patched, fixes = propose_live_fixes(v1, report, llm=FakeLLM([fix]), verify_live=True, env=v2_env,
                                        global_params={"a": 1, "b": 2, "precision": 0})
    assert fixes.accepted[0].verified == "live"
    assert patched.action("add").arguments_schema["required"] == ["a", "b", "precision"]
