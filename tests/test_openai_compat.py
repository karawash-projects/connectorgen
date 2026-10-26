"""OpenAI-compatible provider against a mock server over real sockets."""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from typer.testing import CliRunner

from connectorgen import connector_from_openapi
from connectorgen.cli import app
from connectorgen.enrich import enrich
from connectorgen.llm import (
    AnthropicLLM,
    LLMError,
    OpenAICompatibleLLM,
    llm_from_spec,
    strict_object,
)
from connectorgen.llm.openai_compat import extract_json

from e2e.taskly_server import start

SCHEMA = strict_object({"answer": {"type": "string"}, "n": {"type": "integer"}})


class Mock:
    def __init__(self) -> None:
        self.modes = {"schema", "object", "prompt"}   # what this "provider" supports
        self.replies: list[str] = []
        self.requests: list[dict] = []
        self.fail_first: int | None = None              # e.g. 429 once
        self.finish = "stop"
        self.refusal: str | None = None

    def mode_of(self, body: dict) -> str:
        fmt = body.get("response_format") or (body.get("text") or {}).get("format") or {}
        return {"json_schema": "schema", "json_object": "object"}.get(fmt.get("type"), "prompt")


MOCK = Mock()


async def handle(request: Request) -> JSONResponse:
    body = await request.json()
    MOCK.requests.append({"path": request.url.path, "auth": request.headers.get("authorization"), **body})
    if MOCK.fail_first:
        status, MOCK.fail_first = MOCK.fail_first, None
        return JSONResponse({"error": {"message": "slow down"}}, status_code=status, headers={"Retry-After": "0"})
    mode = MOCK.mode_of(body)
    if mode not in MOCK.modes:
        return JSONResponse({"error": {"message": f"response_format type {mode} is not supported"}},
                            status_code=400)
    text = MOCK.replies.pop(0)
    if request.url.path.endswith("/responses"):
        content = [{"type": "refusal", "refusal": MOCK.refusal}] if MOCK.refusal else \
            [{"type": "output_text", "text": text}]
        return JSONResponse({"status": "completed" if MOCK.finish == "stop" else "incomplete",
                             "incomplete_details": None if MOCK.finish == "stop" else {"reason": "max_output_tokens"},
                             "output": [{"type": "message", "content": content}],
                             "usage": {"input_tokens": 11, "output_tokens": 7}})
    return JSONResponse({"choices": [{"finish_reason": MOCK.finish, "message": {
        "role": "assistant", "content": text, "refusal": MOCK.refusal}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7}})


@pytest.fixture(scope="module")
def server():
    url, srv, thread = start(Starlette(routes=[
        Route("/v1/chat/completions", handle, methods=["POST"]),
        Route("/v1/responses", handle, methods=["POST"])]))
    yield url + "/v1"
    srv.should_exit = True
    thread.join(10)


@pytest.fixture(autouse=True)
def reset():
    MOCK.__init__()


def llm(server, **kw):
    return OpenAICompatibleLLM("test-model", provider=None, base_url=server, api_key="sk-test", **kw)


def test_chat_schema_mode_request_and_parsing(server):
    MOCK.replies = ['{"answer": "42", "n": 1}']
    model = llm(server)
    assert model.complete_json(system="sys", prompt="hi", schema=SCHEMA, max_tokens=300) == {"answer": "42", "n": 1}
    req = MOCK.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer sk-test"
    assert req["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert req["max_tokens"] == 300
    assert req["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "connectorgen_output", "schema": SCHEMA, "strict": True}}
    assert (model.usage.calls, model.usage.input_tokens, model.usage.output_tokens) == (1, 11, 7)


def test_responses_api_for_openai(server):
    MOCK.replies = ['{"answer": "ok", "n": 2}']
    model = OpenAICompatibleLLM("gpt-x", provider="openai", base_url=server, api_key="sk")
    assert model.complete_json(system="sys", prompt="hi", schema=SCHEMA, max_tokens=99)["n"] == 2
    req = MOCK.requests[0]
    assert req["path"] == "/v1/responses"
    assert (req["instructions"], req["input"], req["max_output_tokens"]) == ("sys", "hi", 99)
    assert req["text"]["format"] == {"type": "json_schema", "name": "connectorgen_output",
                                     "schema": SCHEMA, "strict": True}


def test_openai_chat_uses_max_completion_tokens(server):
    MOCK.replies = ['{"answer": "a", "n": 0}']
    OpenAICompatibleLLM("gpt-x", provider="openai", base_url=server, api_key="sk", api="chat") \
        .complete_json(system="", prompt="", schema=SCHEMA, max_tokens=5)
    assert MOCK.requests[0]["max_completion_tokens"] == 5 and "max_tokens" not in MOCK.requests[0]


def test_steps_down_to_json_object_then_prompt(server):
    MOCK.modes = {"object", "prompt"}
    MOCK.replies = ['{"answer": "obj", "n": 1}']
    model = llm(server)
    assert model.complete_json(system="s", prompt="p", schema=SCHEMA)["answer"] == "obj"
    assert model.json_mode == "object"
    assert "JSON Schema" in MOCK.requests[-1]["messages"][0]["content"]   # schema moved into the prompt

    MOCK.modes = {"prompt"}
    MOCK.replies = ['Sure! Here you go:\n```json\n{"answer": "fenced", "n": 3}\n```']
    assert model.complete_json(system="s", prompt="p", schema=SCHEMA)["answer"] == "fenced"
    assert model.json_mode == "prompt" and "response_format" not in MOCK.requests[-1]


def test_repairs_once_then_gives_up(server):
    MOCK.replies = ['{"answer": 5}', '{"answer": "five", "n": 5}']
    model = llm(server)
    assert model.complete_json(system="", prompt="q", schema=SCHEMA) == {"answer": "five", "n": 5}
    assert "didn't match the required JSON Schema" in MOCK.requests[-1]["messages"][1]["content"]
    MOCK.replies = ["not json at all", "still not json"]
    with pytest.raises(LLMError, match="didn't return JSON matching the schema"):
        model.complete_json(system="", prompt="q", schema=SCHEMA)


def test_retries_rate_limits_and_reports_errors(server):
    MOCK.fail_first, MOCK.replies = 429, ['{"answer": "x", "n": 1}']
    assert llm(server).complete_json(system="", prompt="", schema=SCHEMA)["answer"] == "x"
    MOCK.fail_first = 401
    with pytest.raises(LLMError, match="HTTP 401"):
        llm(server, max_retries=0).complete_json(system="", prompt="", schema=SCHEMA)


@pytest.mark.parametrize("api", ["chat", "responses"])
def test_truncation_and_refusal_are_errors(server, api):
    MOCK.finish, MOCK.replies = "length", ['{"answer"']
    with pytest.raises(LLMError, match="truncated"):
        llm(server, api=api).complete_json(system="", prompt="", schema=SCHEMA)
    MOCK.finish, MOCK.refusal, MOCK.replies = "stop", "I can't help with that", ["{}"]
    with pytest.raises(LLMError, match="declined"):
        llm(server, api=api).complete_json(system="", prompt="", schema=SCHEMA)


def test_extract_json():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('text {"a": {"b": "}"}} more') == {"a": {"b": "}"}}
    with pytest.raises(ValueError):
        extract_json("nothing here")


def test_spec_parsing(server, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k2")
    assert isinstance(llm_from_spec("claude-haiku-5-5"), AnthropicLLM)
    assert isinstance(llm_from_spec("anthropic:claude-haiku-5-5"), AnthropicLLM)
    m = llm_from_spec("openai:gpt-6-luna")
    assert (m.provider, m.api, m.model, m.base_url) == ("openai", "responses", "gpt-6-luna", "https://api.openai.com/v1")
    m = llm_from_spec("ollama:llama3.1:8b")                   # model names can contain ':'
    assert (m.provider, m.model, m.base_url, m.api_key) == ("ollama", "llama3.1:8b", "http://localhost:11434/v1", None)
    m = llm_from_spec("llama3.1:8b", base_url=server)          # bare model + custom server
    assert (m.provider, m.model, m.base_url) == ("custom", "llama3.1:8b", server)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match="set GROQ_API_KEY"):
        llm_from_spec("groq:some-model")


def test_enrichment_runs_on_an_openai_compatible_model(server):
    spec = {"openapi": "3.0.3", "info": {"title": "W", "version": "1"}, "servers": [{"url": "https://w.test"}],
            "paths": {"/w": {"get": {"operationId": "listW", "parameters": [
                {"name": "after", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}},
                                                     "paging": {"type": "object", "properties": {
                                                         "token": {"type": "string"}}}}}}}}}}}}}
    MOCK.replies = [json.dumps({"style": "cursor", "request_param": "after", "limit_param": "",
                                "items_path": "items", "response_cursor_path": "paging.token",
                                "cursor_from_item_field": "", "has_more_path": "",
                                "confidence": "high", "reasoning": "paging.token feeds after"})]
    out, report = enrich(connector_from_openapi(spec), llm(server), passes=["pagination"])
    assert report.accepted and out.action("list_w").pagination.response_cursor_path == "paging.token"
    assert out.action("list_w").provenance["pagination"] == "llm:test-model"


def test_cli_with_custom_server(server, tmp_path, monkeypatch, petstore):
    monkeypatch.setenv("CONNECTORGEN_LLM_API_KEY", "sk-cli")
    path = tmp_path / "pets.json"
    petstore.save(path)
    MOCK.replies = [json.dumps({"actions": [{"name": a.name, "new_name": a.name, "summary": "",
                                             "description": "Does it."} for a in petstore.actions
                                            if not a.description]})]
    r = CliRunner().invoke(app, ["enrich", str(path), "--passes", "describe", "--dry-run", "--no-cache",
                                 "--model", "my-local-model", "--llm-base-url", server])
    assert r.exit_code == 0, r.output
    assert MOCK.requests[0]["model"] == "my-local-model" and MOCK.requests[0]["auth"] == "Bearer sk-cli"
