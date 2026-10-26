"""Model layer. Everything else depends only on the `LLM` protocol, so providers swap freely:
Anthropic (Claude) natively, and any OpenAI-compatible API (OpenAI, OpenRouter, Groq, Together,
Mistral, DeepSeek, Gemini, Ollama, LM Studio, vLLM, LiteLLM, ...). See `llm_from_spec`."""

from .base import LLM, LLMError, Usage, strict_object
from .cache import CachedLLM
from .factory import DEFAULT_MODEL, llm_from_spec
from .fake import FakeLLM
from .openai_compat import PRESETS, OpenAICompatibleLLM

__all__ = ["LLM", "LLMError", "Usage", "strict_object", "CachedLLM", "FakeLLM", "AnthropicLLM",
           "OpenAICompatibleLLM", "PRESETS", "llm_from_spec", "DEFAULT_MODEL"]


def __getattr__(name: str):  # lazy, so the anthropic package stays optional
    if name == "AnthropicLLM":
        from .anthropic import AnthropicLLM

        return AnthropicLLM
    raise AttributeError(name)
