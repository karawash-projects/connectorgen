import copy
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from connectorgen import Connector, ConnectorError, connector_from_openapi
from connectorgen.cli import app
from connectorgen.ir import McpServerConfig, Pagination, WebhookSignature
from connectorgen.mcp import connector_from_mcp
from connectorgen.repair import (
    RepairProposal,
    apply_proposal,
    carry_over,
    check_for_drift,
    diff_connectors,
)

FIXTURES = Path(__file__).parent / "fixtures"
SPEC = yaml.safe_load((FIXTURES / "petstore.yaml").read_text())


def spec(mutate=None):
    s = copy.deepcopy(SPEC)
    if mutate:
        mutate(s)
    return s


def kinds(diff, severity=None):
    return {(e.target, e.kind) for e in diff.entries if severity is None or e.severity == severity}


@pytest.fixture
def v1():
    return connector_from_openapi(spec())


# ------------------------------------------------------------------ diff


def test_identical_specs_have_no_diff(v1):
    assert diff_connectors(v1, connector_from_openapi(spec())).empty


def test_new_required_param_is_breaking_optional_is_additive(v1):
    def mutate(s):
        params = s["paths"]["/pets/{petId}"]["get"].setdefault("parameters", [])
        params.append({"name": "fields", "in": "query", "required": True, "schema": {"type": "string"}})
        s["paths"]["/pets"]["get"]["parameters"].append(
            {"name": "q", "in": "query", "schema": {"type": "string"}})
    d = diff_connectors(v1, connector_from_openapi(spec(mutate)))
    assert ("get_pet", "param_added_required") in kinds(d, "breaking")
    assert ("list_pets", "param_added") in kinds(d, "additive")


def test_removed_and_added_actions(v1):
    def mutate(s):
        del s["paths"]["/pets/{petId}"]["delete"]
        s["paths"]["/toys"] = {"get": {"operationId": "listToys",
                                       "responses": {"200": {"description": "ok"}}}}
    d = diff_connectors(v1, connector_from_openapi(spec(mutate)))
    assert ("delete_pet", "action_removed") in kinds(d, "breaking")
    assert ("list_toys", "action_added") in kinds(d, "additive")


def test_schema_field_removed_breaks_inputs_and_outputs(v1):
    d = diff_connectors(v1, connector_from_openapi(spec(
        lambda s: s["components"]["schemas"]["Pet"]["properties"].pop("tag"))))
    breaking = kinds(d, "breaking")
    assert ("create_pet", "body_field_removed") in breaking
    assert ("get_pet", "output_field_removed") in breaking


def test_type_change_and_new_output_field(v1):
    def mutate(s):
        props = s["components"]["schemas"]["Pet"]["properties"]
        props["id"] = {"type": "string"}
        props["born"] = {"type": "string", "format": "date"}
    d = diff_connectors(v1, connector_from_openapi(spec(mutate)))
    assert ("get_pet", "output_field_type_changed") in kinds(d, "breaking")
    assert ("get_pet", "output_field_added") in kinds(d, "additive")


def test_operation_rename_on_same_endpoint_is_matched(v1):
    v2 = connector_from_openapi(spec(
        lambda s: s["paths"]["/pets/{petId}"]["get"].update(operationId="fetchPet")))
    d = diff_connectors(v1, v2)
    assert ("get_pet", "fetch_pet") in d.matched
    assert ("get_pet", "renamed_upstream") in kinds(d, "info")
    assert not d.breaking
    merged = carry_over(v1, v2, d)
    assert merged.action("get_pet").name == "get_pet"           # published name kept
    assert merged.action("fetch_pet").name == "get_pet"         # new name works as alias
    assert merged.action("get_pet").operation_id == "fetchPet"  # upstream truth adopted


def test_becoming_destructive_needs_review(v1):
    v2 = v1.model_copy(deep=True)
    v2.action("get_pet").destructive = True
    assert ("get_pet", "became_destructive") in kinds(diff_connectors(v1, v2), "review")


# ------------------------------------------------------------------ merge


def test_merge_carries_local_customizations(v1):
    current = v1.model_copy(deep=True)
    current.base_url = "https://staging.petstore.test/v1"                 # local override
    cats = current.action("list_categories")               # spec gives it no pagination...
    cats.pagination = Pagination(style="link_header")      # ...enrichment added some
    cats.provenance["pagination"] = "llm:test"
    events = current.action("list_events")                 # spec already paginates this one:
    events.pagination = Pagination(style="link_header", items_path="events")
    events.provenance["pagination"] = "llm:test"           # the spec's answer should win
    current.action("get_orders").summary = ""                            # parser left it empty...
    orders = current.action("get_orders")
    orders.description = "Lists orders."                                  # ...enrichment filled it
    orders.provenance["description"] = "llm:test"
    trig = next(t for t in current.triggers if t.kind == "polling")
    trig.inputs = {"limit": 50}

    candidate = connector_from_openapi(spec(lambda s: s["paths"]["/pets"]["get"].update(
        operationId="listAllPets")))
    candidate.action("get_orders").description = ""
    merged = carry_over(current, candidate, diff_connectors(current, candidate))

    assert merged.base_url == "https://staging.petstore.test/v1"
    assert merged.action("list_categories").pagination.style == "link_header"
    assert merged.action("list_events").pagination.style == "cursor"
    assert merged.action("get_orders").description == "Lists orders."
    assert merged.action("list_pets").aliases == ["list_all_pets"]
    t = next(t for t in merged.triggers if t.kind == "polling")
    assert (t.name, t.source_action, t.inputs) == (trig.name, "list_pets", {"limit": 50})


def test_merge_keeps_webhook_signing_config():
    s31 = {"openapi": "3.1.0", "info": {"title": "H", "version": "1"},
           "servers": [{"url": "https://h.test"}], "paths": {},
           "webhooks": {"thing.created": {"post": {"requestBody": {"content": {
               "application/json": {"schema": {"type": "object"}}}},
               "responses": {"200": {"description": "ok"}}}}}}
    current = connector_from_openapi(s31)
    current.triggers[0].signature = WebhookSignature.github()
    current.triggers[0].delivery_id_header = "X-Delivery"
    candidate = connector_from_openapi(s31)
    merged = carry_over(current, candidate, diff_connectors(current, candidate))
    assert merged.triggers[0].signature == WebhookSignature.github()
    assert merged.triggers[0].delivery_id_header == "X-Delivery"


# ------------------------------------------------------------------ check + apply


@pytest.fixture
def connector_file(tmp_path):
    spec_path = tmp_path / "petstore.yaml"
    spec_path.write_text(yaml.safe_dump(SPEC))
    c = connector_from_openapi(spec_path)
    path = tmp_path / "pets.json"
    c.save(path)
    return spec_path, path


def test_unchanged_source_is_no_change(connector_file):
    spec_path, path = connector_file
    p = check_for_drift(Connector.load(path), current_path=path)
    assert p.verdict == "no_change" and p.candidate is None


def test_additive_change_is_safe_update(connector_file):
    spec_path, path = connector_file
    s = spec(lambda s: s["paths"].update({"/toys": {"get": {
        "operationId": "listToys", "responses": {"200": {"description": "ok"}}}}}))
    spec_path.write_text(yaml.safe_dump(s))
    p = check_for_drift(Connector.load(path), current_path=path)
    assert p.verdict == "safe_update"
    assert p.contract_after["failed"] == 0 and not p.new_failures
    assert "list_toys" in [a.name for a in p.candidate.actions]


def test_breaking_change_needs_review_and_flags_used_actions(connector_file):
    spec_path, path = connector_file
    spec_path.write_text(yaml.safe_dump(spec(
        lambda s: s["components"]["schemas"]["Pet"]["properties"].pop("tag"))))
    p = check_for_drift(Connector.load(path), current_path=path,
                        used_actions=["get_pet", "list_orders_typo"])
    assert p.verdict == "needs_review"
    assert "get_pet" in p.affected_used and "create_pet" not in p.affected_used
    assert any("list_orders_typo" in n for n in p.notes)


def test_live_drift_without_spec_change(connector_file):
    _, path = connector_file
    smoke = {"results": [{"action": "get_pet", "status": "drift",
                          "detail": "response doesn't match schema at id"}]}
    p = check_for_drift(Connector.load(path), smoke_report=smoke, used_actions=["get_pet"])
    assert p.verdict == "live_drift" and p.affected_used == ["get_pet"]


def test_apply_with_backup_and_stale_guard(connector_file, tmp_path):
    spec_path, path = connector_file
    spec_path.write_text(yaml.safe_dump(spec(lambda s: s["paths"]["/pets"]["get"]["parameters"].append(
        {"name": "q", "in": "query", "schema": {"type": "string"}}))))
    p = check_for_drift(Connector.load(path), current_path=path)
    p.save(tmp_path / "repair")
    loaded = RepairProposal.load(tmp_path / "repair")
    assert loaded.verdict == "safe_update" and loaded.diff.counts() == p.diff.counts()

    original = path.read_text()
    path.write_text(original.replace('"title": "Pet Store"', '"title": "Edited"'))  # someone edits
    with pytest.raises(ConnectorError, match="changed after this proposal"):
        apply_proposal(tmp_path / "repair", path)
    path.write_text(original)
    backup = apply_proposal(tmp_path / "repair", path)
    assert backup.read_text() == original
    assert "q" in [x.name for x in Connector.load(path).action("list_pets").params]


def test_nothing_to_apply(connector_file, tmp_path):
    _, path = connector_file
    p = check_for_drift(Connector.load(path), smoke_report={"results": []})
    p.verdict = "live_drift"
    p.save(tmp_path / "r")
    with pytest.raises(ConnectorError, match="Nothing to apply"):
        apply_proposal(tmp_path / "r", path)


def test_cli_exit_codes_and_apply(connector_file, tmp_path):
    spec_path, path = connector_file
    runner = CliRunner()
    out = tmp_path / "repair"
    assert runner.invoke(app, ["check", str(path), "-o", str(out)]).exit_code == 0

    spec_path.write_text(yaml.safe_dump(spec(lambda s: s["paths"]["/pets"]["get"]["parameters"].append(
        {"name": "q", "in": "query", "schema": {"type": "string"}}))))
    r = runner.invoke(app, ["check", str(path), "-o", str(out)])
    assert r.exit_code == 10, r.output
    assert "connectorgen apply" in r.output

    r = runner.invoke(app, ["apply", str(out), str(path), "--yes"])
    assert r.exit_code == 0 and "Previous version saved" in r.output

    spec_path.write_text(yaml.safe_dump(spec(lambda s: s["paths"].pop("/orders"))))
    r = runner.invoke(app, ["check", str(path), "-o", str(out), "--used", "get_orders"])
    assert r.exit_code == 20 and "<- used" in r.output


def test_cli_auto_apply_only_when_safe(connector_file, tmp_path):
    spec_path, path = connector_file
    spec_path.write_text(yaml.safe_dump(spec(lambda s: s["paths"].pop("/orders"))))
    r = CliRunner().invoke(app, ["check", str(path), "-o", str(tmp_path / "r"), "--auto-apply"])
    assert r.exit_code == 20                                     # breaking: not applied
    assert "get_orders" in [a.name for a in Connector.load(path).actions]


def test_cli_diff(v1, tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    v1.save(a)
    connector_from_openapi(spec(lambda s: s["paths"].pop("/orders"))).save(b)
    r = CliRunner().invoke(app, ["diff", str(a), str(b)])
    assert r.exit_code == 20 and "BREAKING get_orders: action no longer exists" in r.output


# ------------------------------------------------------------------ MCP servers change too

SERVER = FIXTURES / "mcp_server.py"


def test_mcp_server_change_detected_and_merged():
    config = McpServerConfig(transport="stdio", command=sys.executable, args=[str(SERVER)],
                             env_passthrough=["NOTES_TOKEN", "NOTES_VARIANT"])
    v1 = connector_from_mcp(config, env={"NOTES_TOKEN": "x", "NOTES_VARIANT": "v1"})
    unchanged = check_for_drift(v1, env={"NOTES_TOKEN": "x", "NOTES_VARIANT": "v1"})
    assert unchanged.verdict == "no_change"

    p = check_for_drift(v1, env={"NOTES_TOKEN": "x", "NOTES_VARIANT": "v2"}, used_actions=["add"])
    assert p.verdict == "needs_review"
    k = kinds(p.diff)
    assert ("add", "argument_field_added_required") in k            # breaking
    assert ("explode", "action_removed") in k                       # breaking
    assert ("archive_note", "action_added") in k                    # additive
    assert ("whoami", "renamed_upstream") in k                      # matched by args+description
    assert p.affected_used == ["add"]
    merged = p.candidate
    assert merged.action("whoami").mcp_tool == "who-am-i"          # old name -> new tool
    assert merged.action("who_am_i").name == "whoami"               # new name is an alias


# ------------------------------------------------------------------ nested classification

from connectorgen.repair.diff import classify_nested  # noqa: E402

OBJ = {"type": "object", "properties": {"mode": {"type": "string", "enum": ["a", "b"]},
                                        "name": {"type": "string", "maxLength": 10}}}


def worst(old, new):
    found = classify_nested(old, new, "f")
    return min((s for s, _ in found), key=["breaking", "review", "additive"].index) if found else None


@pytest.mark.parametrize("change,expected", [
    (lambda o: o["properties"]["mode"]["enum"].append("c"), "additive"),        # widened enum
    (lambda o: o["properties"]["mode"]["enum"].remove("b"), "breaking"),        # narrowed enum
    (lambda o: o["properties"].update(extra={"type": "string"}), "additive"),   # new optional field
    (lambda o: (o["properties"].update(extra={"type": "string"}),
                o.update(required=["extra"])), "breaking"),                     # new required field
    (lambda o: o["properties"].pop("name"), "breaking"),                        # field removed
    (lambda o: o["properties"]["name"].update(maxLength=50), "additive"),       # looser limit
    (lambda o: o["properties"]["name"].update(maxLength=5), "breaking"),        # tighter limit
    (lambda o: o["properties"]["name"].update(type=["string", "null"]), "additive"),  # nullable now
    (lambda o: o["properties"]["name"].update(type="integer"), "breaking"),
    (lambda o: o["properties"]["name"].update({"x-vendor": True}), None),       # not compared
])
def test_nested_input_changes(change, expected):
    from connectorgen.repair.diff import _expand
    new = copy.deepcopy(OBJ)
    change(new)
    assert worst(_expand(OBJ, {}), _expand(new, {})) == expected


def test_widened_output_type_is_review_not_breaking(v1):
    v2 = v1.model_copy(deep=True)
    v2.schemas["Pet"] = {**v2.schemas["Pet"], "properties": {
        **v2.schemas["Pet"]["properties"], "id": {"type": ["integer", "string"]}}}
    d = diff_connectors(v1, v2)
    assert ("get_pet", "output_field_type_widened") in kinds(d, "review")
    assert not [e for e in d.breaking if e.target == "get_pet"]
