import subprocess
import sys

from typer.testing import CliRunner

from connectorgen import Connector
from connectorgen.cli import app
from connectorgen.ir import Action, Pagination, Param
from connectorgen.validate import check_action, run_contract_tests


def test_petstore_all_pass(petstore):
    report = run_contract_tests(petstore)
    assert report.ok
    assert report.passed == len(petstore.actions)


def test_detects_wrong_items_path(petstore):
    petstore.action("list_pets").pagination.items_path = "results"  # real field is "data"
    result = check_action(petstore, "list_pets")
    assert result.status == "fail"
    assert "items_path 'results'" in result.failures[0]


def test_detects_wrong_cursor_path(petstore):
    petstore.action("list_pets").pagination.response_cursor_path = "meta.next"
    result = check_action(petstore, "list_pets")
    assert any("response_cursor_path 'meta.next'" in f for f in result.failures)


def test_detects_unfillable_path_placeholder():
    c = Connector(name="t", base_url="https://x.test", actions=[
        Action(name="get", method="GET", path="/items/{item_id}",
               params=[Param(name="id", location="path", required=True,
                             json_schema={"type": "string"})])])
    result = check_action(c, "get")
    assert result.status == "fail"
    assert "unfilled path placeholders ['{item_id}']" in result.failures[0]


def test_detects_invalid_schema():
    c = Connector(name="t", base_url="https://x.test", actions=[
        Action(name="get", method="GET", path="/x", output_schema={"type": "nope"})])
    result = check_action(c, "get")
    assert result.status == "fail"
    assert "not valid JSON Schema" in result.failures[0]


def test_skips_when_input_cannot_be_synthesized():
    c = Connector(name="t", base_url="https://x.test", actions=[
        Action(name="get", method="GET", path="/x", params=[
            Param(name="q", location="query", required=True,
                  json_schema={"type": "string", "pattern": r"(a)\1"})])])
    result = check_action(c, "get")
    assert result.status == "skip" and result.ok


def test_empty_nested_form_values_are_sent():
    c = Connector(name="t", base_url="https://x.test", actions=[
        Action(name="post", method="POST", path="/x", body_encoding="form", body_required=True,
               body_schema={"type": "object", "required": ["features"],
                            "properties": {"features": {"type": "object"}}})])
    assert check_action(c, "post").status == "pass"


def test_cli_test_and_emitted_pytest(petstore, tmp_path):
    connector_path = tmp_path / "pets.json"
    petstore.save(connector_path)
    out = tmp_path / "generated" / "test_pets_contract.py"
    result = CliRunner().invoke(app, ["test", str(connector_path), "--emit-pytest", str(out)])
    assert result.exit_code == 0, result.output
    assert "8 passed, 0 failed" in result.output

    run = subprocess.run([sys.executable, "-m", "pytest", "-q", str(out)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "8 passed" in run.stdout


def test_cli_test_exits_nonzero_on_failure(petstore, tmp_path):
    petstore.action("list_pets").pagination = Pagination(style="offset", request_param="offset",
                                                         items_path="nope")
    path = tmp_path / "bad.json"
    petstore.save(path)
    result = CliRunner().invoke(app, ["test", str(path)])
    assert result.exit_code == 1
    assert "FAIL list_pets" in result.output
