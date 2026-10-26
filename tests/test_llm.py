import json

import anthropic
import httpx2  # the anthropic SDK (2.x) is built on httpx2, not httpx
import pytest

from connectorgen.enrich import passes
from connectorgen.ingest import docs
from connectorgen.llm import AnthropicLLM, CachedLLM, FakeLLM, LLMError, strict_object

# Structured outputs reject these (platform.claude.com/docs/en/build-with-claude/structured-outputs)
FORBIDDEN = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
             "minLength", "maxLength", "maxItems", "pattern", "$ref", "oneOf"}

SCHEMAS = {
    "pagination": passes._PAGINATION_SCHEMA,
    "auth": passes._AUTH_SCHEMA,
    "describe": passes._DESCRIBE_SCHEMA,
    "docs_extract": docs._EXTRACT_SCHEMA,
}


def _walk(schema, path="$"):
    yield path, schema
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k == "properties":
                for name, sub in v.items():
                    yield from _walk(sub, f"{path}.{name}")
            elif isinstance(v, dict):
                yield from _walk(v, f"{path}/{k}")
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, dict):
                        yield from _walk(item, f"{path}/{k}[{i}]")


@pytest.mark.parametrize("name", SCHEMAS)
def test_schemas_fit_structured_output_limits(name):
    unions = 0
    for path, node in _walk(SCHEMAS[name]):
        if not isinstance(node, dict):
            continue
        assert not FORBIDDEN & node.keys(), f"{name} {path}: unsupported {FORBIDDEN & node.keys()}"
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{name} {path}: needs additionalProperties false"
            assert set(node.get("required", [])) == set(node.get("properties", {})), \
                f"{name} {path}: all properties must be required"
        if isinstance(node.get("type"), list) or "anyOf" in node:
            unions += 1
        if "minItems" in node:
            assert node["minItems"] in (0, 1)
    assert unions <= 16, f"{name}: {unions} union-typed fields"


# ------------------------------------------------------------------ AnthropicLLM wiring


def _anthropic(handler):
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return anthropic.Anthropic(api_key="test-key", http_client=http, max_retries=0)


def _message(text, stop_reason="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 30}}


def test_anthropic_request_shape_and_parsing():
    seen = {}

    def handler(request: httpx2.Request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        # SDK 1.x sends x-api-key; 2.x sends Authorization: Bearer. Either is fine.
        seen["key"] = request.headers.get("x-api-key") or \
            request.headers.get("authorization", "").removeprefix("Bearer ")
        return httpx2.Response(200, json=_message('{"answer": "42"}'))

    llm = AnthropicLLM(model="claude-sonnet-5-5", client=_anthropic(handler))
    schema = strict_object({"answer": {"type": "string"}})
    assert llm.complete_json(system="sys", prompt="hi", schema=schema, max_tokens=500) == {"answer": "42"}

    body = seen["body"]
    assert seen["url"].endswith("/v1/messages")
    assert seen["key"] == "test-key"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "sys"
    assert body["max_tokens"] == 500
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert (llm.usage.calls, llm.usage.input_tokens, llm.usage.output_tokens) == (1, 120, 30)


def test_anthropic_truncation_is_an_error():
    llm = AnthropicLLM(client=_anthropic(
        lambda r: httpx2.Response(200, json=_message('{"a":', "max_tokens"))))
    with pytest.raises(LLMError, match="max_tokens"):
        llm.complete_json(system="", prompt="", schema=strict_object({}))


def test_anthropic_http_error_wrapped():
    llm = AnthropicLLM(client=_anthropic(lambda r: httpx2.Response(
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})))
    with pytest.raises(LLMError, match="AuthenticationError"):
        llm.complete_json(system="", prompt="", schema=strict_object({}))


# ------------------------------------------------------------------ cache + fake


def test_cache_answers_repeat_calls(tmp_path):
    fake = FakeLLM([{"x": "1"}])
    llm = CachedLLM(fake, tmp_path)
    schema = strict_object({"x": {"type": "string"}})
    assert llm.complete_json(system="s", prompt="p", schema=schema) == {"x": "1"}
    assert llm.complete_json(system="s", prompt="p", schema=schema) == {"x": "1"}
    assert len(fake.calls) == 1 and llm.usage.cache_hits == 1


def test_fake_rejects_off_schema_responses():
    fake = FakeLLM([{"x": 5}])
    with pytest.raises(AssertionError, match="violates schema"):
        fake.complete_json(system="", prompt="", schema=strict_object({"x": {"type": "string"}}))
