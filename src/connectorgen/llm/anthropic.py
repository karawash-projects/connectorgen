"""Claude via the Anthropic API, using structured outputs (output_config.format).

Docs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..runtime.urls import insecure_http
from .base import LLMError, Usage

DEFAULT_MODEL = "claude-sonnet-5-5"


class AnthropicLLM:
    def __init__(self, model: str = DEFAULT_MODEL, *, api_key: str | None = None,
                 base_url: str | None = None, client: Any = None, max_retries: int = 3,
                 timeout: float = 300.0, allow_insecure: bool = False):
        """base_url: an Anthropic-compatible gateway. Its key comes from api_key or
        CONNECTORGEN_LLM_API_KEY; ANTHROPIC_API_KEY is only sent to Anthropic's own API."""
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise LLMError("Install the AI extra: pip install 'connectorgen[ai]'") from e
            if base_url:
                api_key = api_key or os.environ.get("CONNECTORGEN_LLM_API_KEY")
                if not api_key:
                    raise LLMError(f"No API key for {base_url}: pass api_key= or set "
                                   "CONNECTORGEN_LLM_API_KEY.")
                if insecure_http(base_url) and not allow_insecure:
                    raise LLMError(f"Refusing to send an API key over plain http to {base_url}; "
                                   "use https or pass allow_insecure=True.")
            client = anthropic.Anthropic(api_key=api_key, base_url=base_url,
                                         max_retries=max_retries, timeout=timeout)
        self.client = client
        self.model = model
        self.usage = Usage()
        base = getattr(client, "base_url", "") or ""
        self.cache_identity = f"anthropic:{base}:{model}"

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any],
                      max_tokens: int = 8000) -> dict[str, Any]:
        """The reply, checked against `schema` (one repair round if it doesn't match)."""
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import best_match

        validator = Draft202012Validator(schema)
        user, problem = prompt, ""
        for _ in range(2):
            data = self._complete(system, user, schema, max_tokens)
            error = best_match(validator.iter_errors(data))
            if error is None:
                return data
            where = ".".join(map(str, error.path)) or "(root)"
            problem = f"at {where}: {error.message[:300]}"
            user = (f"{prompt}\n\nYour previous reply didn't match the required JSON Schema "
                    f"({problem}). Reply again with only a JSON object that matches it.")
        raise LLMError(f"{self.model} didn't return JSON matching the schema: {problem}")

    def _complete(self, system: str, prompt: str, schema: dict[str, Any],
                  max_tokens: int) -> Any:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as e:
            raise LLMError(f"{self.model} call failed: {type(e).__name__}: {e}") from e

        self.usage.calls += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.usage.output_tokens += getattr(usage, "output_tokens", 0) or 0

        if getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMError(f"{self.model} hit max_tokens={max_tokens}; output truncated. "
                           "Send less input per call or raise max_tokens.")
        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMError(f"{self.model} declined the request.")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
        if text is None:
            raise LLMError(f"{self.model} returned no text block.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"{self.model} returned invalid JSON: {e}") from e
