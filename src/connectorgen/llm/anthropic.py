"""Claude via the Anthropic API, using structured outputs (output_config.format).

Docs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
"""

from __future__ import annotations

import json
from typing import Any

from .base import LLMError, Usage

DEFAULT_MODEL = "claude-sonnet-5-5"


class AnthropicLLM:
    def __init__(self, model: str = DEFAULT_MODEL, *, api_key: str | None = None,
                 client: Any = None, max_retries: int = 3, timeout: float = 300.0):
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise LLMError("Install the AI extra: pip install 'connectorgen[ai]'") from e
            client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries,
                                         timeout=timeout)
        self.client = client
        self.model = model
        self.usage = Usage()
        base = getattr(client, "base_url", "") or ""
        self.cache_identity = f"anthropic:{base}:{model}"

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any],
                      max_tokens: int = 8000) -> dict[str, Any]:
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
