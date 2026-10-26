import json
from pathlib import Path

import pytest
import respx
from typer.testing import CliRunner

import connectorgen.cli as cli
from connectorgen import UnsupportedSpecError
from connectorgen.ingest.docs import chunk_pages, connector_from_docs, fetch_docs, html_to_text
from connectorgen.llm import FakeLLM
from connectorgen.validate import run_contract_tests

DOCS = Path(__file__).parent / "fixtures" / "docs_page.html"
DOCS_URL = "https://docs.notes.test/docs/v2/index.html"
NO_PAGINATION = {"style": "none", "request_param": "", "limit_param": "", "items_path": "",
                 "response_cursor_path": "", "cursor_from_item_field": "", "has_more_path": ""}
NOTE_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"},
                                                "tags": {"type": "array", "items": {"type": "string"}}}}


def endpoint(**kw):
    base = {"name": "", "method": "GET", "path": "/", "summary": "", "description": "",
            "params": [], "body_encoding": "none", "body_required": False,
            "body_schema_json": "", "response_schema_json": "", "pagination": NO_PAGINATION,
            "evidence": ""}
    return {**base, **kw}


def param(name, location="query", type_="string", required=False):
    return {"name": name, "location": location, "required": required, "type": type_,
            "description": "", "enum_values": []}


def extraction(base_url="https://api.notes.test/v2", extra=()):
    return {
        "api_title": "Acme Notes API",
        "base_url": base_url,
        "auth": [{"type": "api_key", "param_name": "X-Notes-Key", "location": "header",
                  "description": "Send your key in X-Notes-Key"}],
        "endpoints": [
            endpoint(name="list_notes", path="/notes", summary="List notes",
                     params=[param("page_token"), param("tag")],
                     response_schema_json=json.dumps({"type": "object", "properties": {
                         "notes": {"type": "array", "items": NOTE_SCHEMA},
                         "next_page": {"type": ["string", "null"]}}}),
                     pagination={**NO_PAGINATION, "style": "cursor", "request_param": "page_token",
                                 "items_path": "notes", "response_cursor_path": "next_page"},
                     evidence="GET /notes"),
            endpoint(name="get_note", path="/notes/:note_id", summary="Get a note",
                     response_schema_json=json.dumps(NOTE_SCHEMA), evidence="GET /notes/:note_id"),
            endpoint(name="create_note", method="POST", path="/notes", summary="Create a note",
                     body_encoding="json", body_required=True,
                     body_schema_json=json.dumps({"type": "object", "required": ["text"],
                                                  "properties": NOTE_SCHEMA["properties"]}),
                     response_schema_json=json.dumps(NOTE_SCHEMA), evidence="POST /notes"),
            endpoint(name="delete_note", method="DELETE", path="/notes/{note_id}",
                     params=[param("note_id", "path", required=True)],
                     evidence="DELETE /notes/:note_id"),
            *extra,
        ],
    }


@pytest.fixture
def docs_http():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(DOCS_URL).respond(200, text=DOCS.read_text(),
                                   headers={"content-type": "text/html"})
        mock.get("https://docs.notes.test/docs/v2/webhooks.html").respond(
            200, text="<h1>Webhooks</h1><p>POST /hooks registers a webhook.</p>",
            headers={"content-type": "text/html"})
        mock.get("https://docs.notes.test/pricing").respond(200, text="<p>pricing</p>")
        yield mock


def test_html_to_text_keeps_structure_drops_chrome():
    text, links = html_to_text(DOCS.read_text())
    assert "# Acme Notes API" in text and "## List notes" in text
    assert '```\n{\n  "notes"' in text            # code blocks kept verbatim
    assert "| page_token | string |" in text       # table rows kept
    assert "analytics" not in text and "Pricing" not in text and "©" not in text
    assert "/docs/v2/webhooks.html" in links


def test_builds_working_connector(docs_http):
    llm = FakeLLM([extraction()])
    c, report = connector_from_docs([DOCS_URL], llm)
    assert c.title == "Acme Notes API"
    assert c.base_url == "https://api.notes.test/v2"   # same registrable domain as docs
    assert c.auth[0].param_name == "X-Notes-Key"
    assert [a.name for a in c.actions] == ["list_notes", "get_note", "create_note", "delete_note"]
    get_note = c.action("get_note")
    assert get_note.path == "/notes/{note_id}"          # :note_id normalized
    assert get_note.params[0].name == "note_id"         # undocumented placeholder added
    assert any("had no documented param" in n for n in get_note.notes)
    assert c.action("delete_note").destructive
    assert c.action("list_notes").pagination.response_cursor_path == "next_page"
    assert c.action("list_notes").provenance == {"action": "llm:fake-model"}
    assert report.kept == 4 and not report.dropped
    assert run_contract_tests(c).ok
    # docs went to the model marked as untrusted
    assert llm.calls[0].prompt.startswith("<docs>") and "untrusted" in llm.calls[0].system


def test_drops_endpoints_not_in_docs(docs_http):
    invented = endpoint(name="export_all", path="/admin/export", evidence="")
    c, report = connector_from_docs([DOCS_URL], FakeLLM([extraction(extra=[invented])]))
    assert "export_all" not in [a.name for a in c.actions]
    assert "GET /admin/export" in report.dropped[0]


def test_rejects_base_url_on_foreign_domain(docs_http):
    c, report = connector_from_docs([DOCS_URL], FakeLLM([extraction("https://evil.example/v2")]))
    assert c.base_url == ""
    assert any("isn't on the docs' own domain" in n for n in c.notes)


def test_explicit_base_url_wins(docs_http):
    c, _ = connector_from_docs([DOCS_URL], FakeLLM([extraction("https://evil.example")]),
                               base_url="https://api.notes.test/v2")
    assert c.base_url == "https://api.notes.test/v2"


def test_local_files_need_explicit_base_url():
    c, _ = connector_from_docs([str(DOCS)], FakeLLM([extraction()]))
    assert c.base_url == ""   # no domain to check a model-provided URL against


def test_invalid_model_schema_dropped(docs_http):
    bad = extraction()
    bad["endpoints"][1]["response_schema_json"] = '{"type": "nope"}'
    c, _ = connector_from_docs([DOCS_URL], FakeLLM([bad]))
    assert c.action("get_note").output_schema is None
    assert any("invalid and was dropped" in n for n in c.action("get_note").notes)


def test_crawl_stays_in_docs_directory(docs_http):
    pages = fetch_docs([DOCS_URL], crawl=True)
    assert [p.source for p in pages] == [DOCS_URL, "https://docs.notes.test/docs/v2/webhooks.html"]


def test_openapi_url_redirects_user_to_generate(tmp_path):
    spec = tmp_path / "openapi.yaml"
    spec.write_text("openapi: 3.0.0\ninfo: {title: x, version: '1'}\npaths: {}\n")
    with pytest.raises(UnsupportedSpecError, match="connectorgen generate"):
        fetch_docs([str(spec)])


def test_same_endpoint_across_chunks_is_merged(docs_http):
    first = extraction()
    second = {"api_title": "", "base_url": "", "auth": [], "endpoints": [
        endpoint(name="list_notes", path="/notes", params=[param("limit", type_="integer")],
                 description="Returns notes newest first, 50 per page.", evidence="GET /notes")]}
    pages = fetch_docs([DOCS_URL])
    assert len(chunk_pages(pages, max_chars=600)) > 1
    c, report = connector_from_docs([DOCS_URL], FakeLLM([first, second] + [
        {"api_title": "", "base_url": "", "auth": [], "endpoints": []}] * 10), chunk_chars=600)
    notes = c.action("list_notes")
    assert {p.name for p in notes.params} == {"page_token", "tag", "limit"}
    assert notes.description == "Returns notes newest first, 50 per page."


def test_cli_from_docs_and_enrich(docs_http, tmp_path, monkeypatch):
    scripted = [[extraction()], [{"actions": [
        {"name": n, "new_name": n, "summary": "", "description": f"Does {n}."}
        for n in ("list_notes", "get_note", "create_note", "delete_note")]}]]
    monkeypatch.setattr(cli, "_make_llm", lambda *args, **kw: FakeLLM(scripted.pop(0)))
    out = tmp_path / "notes.json"
    result = CliRunner().invoke(cli.app, ["from-docs", DOCS_URL, "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "contract tests: 4 passed, 0 failed" in result.output

    result = CliRunner().invoke(cli.app, ["enrich", str(out), "--passes", "describe", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output and "changes accepted" in result.output


def test_incomplete_docs_pagination_is_dropped(docs_http):
    bad = extraction()
    bad["endpoints"][0]["pagination"] = {**NO_PAGINATION, "style": "offset", "items_path": "notes"}
    c, _ = connector_from_docs([DOCS_URL], FakeLLM([bad]))
    notes = c.action("list_notes")
    assert notes.pagination.style == "none"
    assert any("needs a request_param" in n for n in notes.notes)
