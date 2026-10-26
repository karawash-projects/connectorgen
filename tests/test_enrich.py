import pytest

from connectorgen import connector_from_openapi
from connectorgen.enrich import enrich
from connectorgen.ir import Trigger
from connectorgen.llm import FakeLLM

# An API whose cursor lives in an unconventional field, so the parser can't settle it.
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Acme", "version": "1",
             "description": "Authenticate by sending your personal token as "
                            "`Authorization: Bearer <token>`."},
    "servers": [{"url": "https://api.acme.test"}],
    "paths": {
        "/widgets": {"get": {
            "operationId": "listWidgets",
            "summary": "List widgets",
            "parameters": [
                {"name": "after", "in": "query", "schema": {"type": "string"}},
                {"name": "size", "in": "query", "schema": {"type": "integer"}},
            ],
            "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "widgets": {"type": "array", "items": {"type": "object"}},
                    "paging": {"type": "object", "properties": {
                        "continuation": {"type": "string"}}},
                }}}}}},
        }},
        "/widgets/{id}/archive": {"post": {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"204": {"description": "ok"}},
        }},
        "/gadgets": {"get": {
            "operationId": "listGadgets",
            "responses": {"200": {"description": "ok"}},
        }},
    },
}

GOOD_PAGINATION = {
    "style": "cursor", "request_param": "after", "limit_param": "size", "items_path": "widgets",
    "response_cursor_path": "paging.continuation", "cursor_from_item_field": "",
    "has_more_path": "", "confidence": "high", "reasoning": "paging.continuation feeds 'after'",
}


@pytest.fixture
def acme():
    return connector_from_openapi(SPEC)


def by_schema(responses):
    """Route fake responses by which pass is asking (identified by schema keys)."""
    def responder(call):
        keys = set(call.schema["properties"])
        if "style" in keys:
            return responses["pagination"]
        if "schemes" in keys:
            return responses["auth"]
        return responses["describe"](call) if callable(responses["describe"]) else responses["describe"]
    return responder


def test_parser_left_a_note(acme):
    widgets = acme.action("list_widgets")
    assert widgets.pagination.style == "link_header"  # parser's fallback guess
    assert any("after" in n for n in widgets.notes)


def test_pagination_accepted_after_contract_test(acme):
    llm = FakeLLM(by_schema({"pagination": GOOD_PAGINATION}))
    out, report = enrich(acme, llm, passes=["pagination"])
    p = out.action("list_widgets").pagination
    assert (p.style, p.response_cursor_path, p.items_path) == ("cursor", "paging.continuation", "widgets")
    assert out.action("list_widgets").notes == []
    assert out.action("list_widgets").provenance["pagination"] == "llm:fake-model"
    assert report.accepted and not report.rejected
    # the input is never mutated
    assert acme.action("list_widgets").pagination.style == "link_header"
    # the prompt carried the evidence, wrapped as untrusted spec content
    prompt = llm.calls[0].prompt
    assert "<spec>" in prompt and "paging" in prompt and "after" in prompt


@pytest.mark.parametrize("override,reason", [
    ({"items_path": "results"}, "contract test failed"),
    ({"response_cursor_path": "paging.next"}, "contract test failed"),
    ({"request_param": "page_token"}, "unknown query parameter"),
    ({"confidence": "low"}, "confidence low"),
    ({"response_cursor_path": ""}, "needs response_cursor_path"),
])
def test_pagination_rejections(acme, override, reason):
    llm = FakeLLM(by_schema({"pagination": {**GOOD_PAGINATION, **override}}))
    out, report = enrich(acme, llm, passes=["pagination"])
    assert out.action("list_widgets").pagination == acme.action("list_widgets").pagination
    assert out.action("list_widgets").notes  # note stays for a human
    assert report.rejected and reason in report.rejected[0].reason


def test_auth_inferred_and_verified(acme):
    llm = FakeLLM(by_schema({"auth": {
        "schemes": [{"type": "bearer", "param_name": "", "location": "",
                     "description": "Personal token"}],
        "confidence": "high", "evidence": "Authorization: Bearer <token>"}}))
    out, report = enrich(acme, llm, passes=["auth"])
    assert [s.type for s in out.auth] == ["bearer"]
    assert out.provenance["auth"] == "llm:fake-model"
    assert "Bearer" in llm.calls[0].prompt


def test_auth_not_touched_when_spec_has_it(petstore):
    llm = FakeLLM([])
    out, _ = enrich(petstore, llm, passes=["auth"])
    assert out.auth == petstore.auth and llm.calls == []


@pytest.mark.parametrize("proposal,reason", [
    ({"schemes": [{"type": "bearer", "param_name": "", "location": "", "description": ""}],
      "confidence": "low", "evidence": "guess"}, "confidence low"),
    ({"schemes": [{"type": "api_key", "param_name": "", "location": "header", "description": ""}],
      "confidence": "high", "evidence": "x"}, "api_key scheme without param_name"),
])
def test_auth_rejections(acme, proposal, reason):
    out, report = enrich(acme, FakeLLM(by_schema({"auth": proposal})), passes=["auth"])
    assert out.auth == []
    assert reason in report.rejected[0].reason


def describe_responder(call):
    entries = {
        "post_widgets_by_id_archive": ("archive_widget", "Archive a widget", "Archives a widget."),
        "list_widgets": ("list_all_widgets", "Should not replace spec summary", "Lists widgets."),
        "list_gadgets": ("list_gadgets", "List gadgets", "Lists every gadget."),
    }
    return {"actions": [{"name": n, "new_name": e[0], "summary": e[1], "description": e[2]}
                        for n, e in entries.items() if f"name={n} " in call.prompt]}


def test_describe_fills_gaps_and_renames_only_invented_names(acme):
    acme.triggers.append(Trigger(name="t", kind="polling",
                                 source_action="post_widgets_by_id_archive"))
    out, report = enrich(acme, FakeLLM(by_schema({"describe": describe_responder})),
                         passes=["describe"])
    names = [a.name for a in out.actions]
    assert "archive_widget" in names                  # parser-invented name replaced
    assert "list_widgets" in names                    # spec operationId kept
    assert out.action("list_widgets").summary == "List widgets"     # spec text kept
    assert out.action("list_widgets").description == "Lists widgets."  # gap filled
    assert out.action("list_gadgets").summary == "List gadgets"
    assert out.action("archive_widget").provenance["name"].endswith("(was post_widgets_by_id_archive)")
    assert out.triggers[0].source_action == "archive_widget"


def test_describe_rejects_bad_and_duplicate_names(acme):
    def responder(call):
        out = []
        if "name=post_widgets_by_id_archive " in call.prompt:
            out.append({"name": "post_widgets_by_id_archive", "new_name": "list_widgets",
                        "summary": "Archive", "description": "d"})
        if "name=list_gadgets " in call.prompt:
            out.append({"name": "list_gadgets", "new_name": "Bad Name!", "summary": "s",
                        "description": "d"})
        out.append({"name": "ghost", "new_name": "ghost", "summary": "", "description": ""})
        return {"actions": out}

    out, report = enrich(acme, FakeLLM(by_schema({"describe": responder})), passes=["describe"],
                         rename_all=True)
    reasons = {c.target: c.reason for c in report.rejected}
    assert reasons["post_widgets_by_id_archive"] == "name already used by another action"
    assert reasons["list_gadgets"] == "not a valid snake_case name"
    assert any("unknown action 'ghost'" in e for e in report.errors)
    assert any("no proposal returned for 'list_widgets'" in e for e in report.errors)


def test_unknown_pass_rejected(acme):
    with pytest.raises(ValueError, match="Unknown passes"):
        enrich(acme, FakeLLM([]), passes=["magic"])


def test_model_failure_is_reported_not_raised(acme):
    out, report = enrich(acme, FakeLLM([]), passes=["pagination"])  # runs out of responses
    assert report.errors and "pagination pass stopped" in report.errors[0]
    assert out.action("list_widgets").pagination == acme.action("list_widgets").pagination
