"""Pick a model from one string, for the CLI and for apps that let users choose.

    llm_from_spec("claude-sonnet-5-5")                  # Anthropic (ANTHROPIC_API_KEY)
    llm_from_spec("anthropic:claude-haiku-5-5")
    llm_from_spec("openai:gpt-6-luna")                  # OpenAI (OPENAI_API_KEY)
    llm_from_spec("openrouter:some-org/some-model")     # OPENROUTER_API_KEY
    llm_from_spec("ollama:llama3.1:8b")                 # local, no key
    llm_from_spec("my-model", base_url="http://localhost:8000/v1")   # any compatible server

Environment: CONNECTORGEN_MODEL (default spec), CONNECTORGEN_LLM_BASE_URL,
CONNECTORGEN_LLM_API_KEY (overrides the provider's own key variable).
"""

from __future__ import annotations

import os
from typing import Any

from .base import LLM, LLMError

DEFAULT_MODEL = "claude-sonnet-5-5"


def llm_from_spec(spec: str | None = None, *, base_url: str | None = None,
                  api_key: str | None = None, **options: Any) -> LLM:
    spec = (spec or os.environ.get("CONNECTORGEN_MODEL") or DEFAULT_MODEL).strip()
    base_url = base_url or os.environ.get("CONNECTORGEN_LLM_BASE_URL") or None
    from .openai_compat import PRESETS, OpenAICompatibleLLM

    provider, _, rest = spec.partition(":")
    known = provider in PRESETS or provider in ("anthropic", "custom")
    if not (known and rest):  # no provider prefix (model names may contain ':', e.g. llama3.1:8b)
        provider, rest = "", spec

    if provider == "anthropic" or (not provider and rest.startswith("claude") and not base_url):
        from .anthropic import AnthropicLLM

        return AnthropicLLM(model=rest, api_key=api_key or os.environ.get("CONNECTORGEN_LLM_API_KEY"))
    if provider in PRESETS:
        return OpenAICompatibleLLM(rest, provider=provider, base_url=base_url, api_key=api_key, **options)
    if base_url:  # "custom:model" or a bare model name with a base URL
        return OpenAICompatibleLLM(rest, provider=None, base_url=base_url, api_key=api_key, **options)
    if not provider and not rest.startswith("claude"):
        return OpenAICompatibleLLM(rest, provider="openai", api_key=api_key, **options)
    raise LLMError(f"Can't tell which provider '{spec}' belongs to; use provider:model "
                   f"(anthropic, {', '.join(PRESETS)}) or pass a base URL.")
