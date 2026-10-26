"""MCP server -> connector, against a real stdio subprocess."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import plain
from connectorgen import Connector, ConnectorError, InputValidationError
from connectorgen.cli import app
from connectorgen.errors import McpToolError
from connectorgen.ir import McpServerConfig
from connectorgen.mcp import AsyncMcpClient, McpClient, aconnector_from_mcp, connector_from_mcp, open_client
from connectorgen.validate import run_contract_tests
from connectorgen.validate.smoke import run_smoke_tests

SERVER = Path(__file__).parent / "fixtures" / "mcp_server.py"
CONFIG = McpServerConfig(transport="stdio", command=sys.executable, args=[str(SERVER)],
                         env_passthrough=["NOTES_TOKEN"])
ENV = {"NOTES_TOKEN": "s3cret"}


@pytest.fixture(scope="module")
def notes():
    return connector_from_mcp(CONFIG, env=ENV)


@pytest.fixture
def client(notes):
    with McpClient(notes, env=ENV) as c:
        yield c


def test_server_metadata(notes):
    assert notes.kind == "mcp"
    assert (notes.name, notes.title, notes.api_version) == ("notes", "Notes", "1.2.0")
    assert notes.description == "A tiny notes service for tests."
    assert notes.spec_hash.startswith("sha256:")


def test_tools_become_actions_with_safety_flags(notes):
    by = {a.name: a for a in notes.actions}
    assert set(by) == {"add", "list_notes", "create_note", "delete_note", "whoami", "explode"}
    assert by["list_notes"].mcp_tool == "list-notes"           # original name kept for calls
    assert by["add"].summary == "Add numbers"
    assert by["add"].safe and not by["add"].destructive        # readOnlyHint
    assert not by["create_note"].safe and not by["create_note"].destructive  # destructiveHint false
    assert by["delete_note"].destructive                       # unannotated write: spec default
    assert by["add"].input_schema()["required"] == ["a", "b"]
    assert by["add"].output_schema["properties"]["sum"] == {"type": "integer"}


def test_no_secrets_in_saved_file(notes, tmp_path):
    path = tmp_path / "notes.json"
    notes.save(path)
    text = path.read_text()
    assert "s3cret" not in text and "NOTES_TOKEN" in text   # the name, never the value
    assert Connector.load(path) == notes


def test_execute_structured_and_text_results(client):
    assert client.execute("add", a=2, b=3) == {"sum": 5}                   # structured content
    assert [n["id"] for n in client.execute("list_notes", tag="work")] == ["n2"]  # JSON text
    assert client.execute("create_note", {"text": "hi"}) == "created: hi"   # plain text


def test_inputs_validated_before_calling(client):
    with pytest.raises(InputValidationError, match="'b' is a required property"):
        client.execute("add", a=1)
    with pytest.raises(InputValidationError, match="not one of"):
        client.execute("list_notes", tag="garden")


def test_tool_errors_raise(client):
    with pytest.raises(McpToolError, match="database is on fire"):
        client.execute("explode")
    assert client.execute("add", a=1, b=1) == {"sum": 2}   # session still usable after an error


def test_env_passthrough_is_explicit(notes, monkeypatch):
    monkeypatch.setenv("OTHER_SECRET", "x")
    with McpClient(notes, env=ENV) as c:
        assert c.execute("whoami") == {"has_secret": True, "leaked": False}


def test_missing_env_is_a_clear_error(notes, monkeypatch):
    monkeypatch.delenv("NOTES_TOKEN", raising=False)
    with pytest.raises(ConnectorError, match="needs environment variable.*NOTES_TOKEN"):
        McpClient(notes)


def test_bad_command_is_a_clear_error():
    broken = Connector(name="x", mcp=McpServerConfig(transport="stdio",
                                                     command="/nonexistent/server-binary"))
    with pytest.raises(ConnectorError, match="Couldn't connect to MCP server"):
        McpClient(broken)


def test_async_client():
    async def run():
        c = await aconnector_from_mcp(CONFIG, env=ENV)
        async with AsyncMcpClient(c, env=ENV) as client:
            return await client.execute("add", a=20, b=22)
    assert asyncio.run(run()) == {"sum": 42}


def test_open_client_picks_the_right_runtime(notes, petstore):
    with open_client(notes, env=ENV) as c:
        assert isinstance(c, McpClient)
    from connectorgen import ConnectorClient
    with open_client(petstore) as c:
        assert isinstance(c, ConnectorClient)


def test_contract_tests_cover_mcp_actions(notes):
    report = run_contract_tests(notes)
    assert report.ok and report.passed == len(notes.actions)


def test_smoke_calls_only_read_only_tools(notes):
    report = run_smoke_tests(notes, None, env=ENV, sleep=lambda s: None)
    statuses = {r.action: r.status for r in report.results}
    assert "create_note" not in statuses and "delete_note" not in statuses   # writes never run
    assert statuses["add"] == "skipped"            # needs a and b
    assert statuses["list_notes"] == "ok" and statuses["whoami"] == "ok"
    assert statuses["explode"] == "error"


def test_smoke_params_fill_mcp_arguments(notes):
    report = run_smoke_tests(notes, None, env=ENV, actions=["add"],
                             global_params={"a": 1, "b": 2}, sleep=lambda s: None)
    assert report.results[0].status == "ok"


def test_cli_from_mcp_inspect_call(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_TOKEN", "s3cret")
    out = tmp_path / "notes.json"
    cmd = f"{sys.executable} {SERVER}"
    runner = CliRunner()
    result = runner.invoke(app, ["from-mcp", "--command", cmd, "--env", "NOTES_TOKEN", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "6 tools (4 read-only, 1 destructive)" in result.output

    result = runner.invoke(app, ["inspect", str(out)])
    assert "MCP stdio" in result.output and "needs env: NOTES_TOKEN" in result.output

    result = runner.invoke(app, ["call", str(out), "add", "-i", "a=4", "-i", "b=5"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"sum": 9}

    result = runner.invoke(app, ["call", str(out), "delete_note", "-i", "id=n1"])
    assert result.exit_code == 1 and "--allow-destructive" in result.output


def test_cli_rejects_secrets_in_headers():
    result = CliRunner().invoke(app, ["from-mcp", "--url", "https://x.test/mcp",
                                      "--header", "Authorization=Bearer abc"])
    assert result.exit_code != 0 and "Don't put credentials in --header" in plain(result.output)
