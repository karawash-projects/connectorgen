"""Any model behind an OpenAI-compatible API: OpenAI itself, OpenRouter, Groq,
Together, Mistral, DeepSeek, Gemini's compatibility endpoint, and local servers
such as Ollama, LM Studio, vLLM or a LiteLLM proxy.

Two wire protocols:
- "responses": OpenAI's Responses API (POST /responses), the default for OpenAI.
- "chat": Chat Completions (POST /chat/completions), what compatible servers speak.

Providers differ in how well they support structured output, so json_mode="auto"
starts with strict JSON Schema output and steps down when a provider rejects it:
schema -> json_object -> prompt-only. Whatever the mode, the reply is parsed and
validated against the schema, with one repair attempt if it doesn't match.
No extra dependency: requests go through httpx.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Literal

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from ..runtime.urls import same_origin
from .base import LLMError, Usage

# provider: (base URL, API key env var, wire protocol)
PRESETS: dict[str, tuple[str, str | None, str]] = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "responses"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "chat"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "chat"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY", "chat"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "chat"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "chat"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "chat"),
    "ollama": ("http://localhost:11434/v1", None, "chat"),
    "lmstudio": ("http://localhost:1234/v1", None, "chat"),
}

Mode = Literal["schema", "object", "prompt"]
_DOWNGRADE: dict[str, Mode] = {"schema": "object", "object": "prompt"}
_FORMAT_ERROR = re.compile(r"response_format|json_schema|json_object|text\.format|structured|"
                           r"\bschema\b|strict|not supported|unsupported|unrecognized|"
                           r"extra (?:fields|inputs)|unknown (?:field|parameter)", re.I)


def extract_json(text: str) -> Any:
    """Parse a model reply that should be JSON but may be wrapped in prose or code fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = esc = False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                esc = ch == "\\" and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("no JSON object found in the reply")


class OpenAICompatibleLLM:
    def __init__(
        self,
        model: str,
        *,
        provider: str | None = "openai",
        base_url: str | None = None,
        api_key: str | None = None,
        api: Literal["chat", "responses"] | None = None,
        json_mode: Literal["auto", "schema", "object", "prompt"] = "auto",
        timeout: float = 300.0,
        max_retries: int = 2,
        extra_headers: dict[str, str] | None = None,
        http: httpx.Client | None = None,
    ):
        preset = PRESETS.get(provider or "")
        if provider and preset is None:
            raise LLMError(f"Unknown provider '{provider}'. Known: {', '.join(PRESETS)}; "
                           "or pass base_url for any OpenAI-compatible server.")
        self.provider = provider or "custom"
        preset_url = preset[0] if preset else ""
        self.base_url = (base_url or preset_url).rstrip("/")
        if not self.base_url:
            raise LLMError("An OpenAI-compatible model needs base_url (e.g. http://localhost:8000/v1).")
        # A provider's own key (OPENAI_API_KEY, ...) only ever goes to that provider's own
        # host. With a custom base URL, pass api_key= or set CONNECTORGEN_LLM_API_KEY.
        own_host = bool(preset_url) and same_origin(self.base_url, preset_url)
        key_env = preset[1] if preset and own_host else None
        self.api_key = api_key or os.environ.get("CONNECTORGEN_LLM_API_KEY") or \
            (os.environ.get(key_env) if key_env else None)
        if key_env and not self.api_key:
            raise LLMError(f"No API key for {self.provider}: set {key_env} (or CONNECTORGEN_LLM_API_KEY).")
        self.api = api or (preset[2] if preset else "chat")
        self.model = model
        self.cache_identity = f"openai-compatible:{self.provider}:{self.base_url}:{self.api}:{model}"
        self.usage = Usage()
        self._mode: Mode = "schema" if json_mode == "auto" else json_mode  # type: ignore[assignment]
        self._auto = json_mode == "auto"
        self.max_retries = max_retries
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.http = http or httpx.Client(timeout=timeout, headers=headers)
        if http is not None:
            self.http.headers.update(headers)

    @property
    def json_mode(self) -> Mode:
        """The structured-output mode in use (may have stepped down in auto mode)."""
        return self._mode

    # ---------------------------------------------------------------- public

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any],
                      max_tokens: int = 8000) -> dict[str, Any]:
        validator = Draft202012Validator(schema)
        user = prompt
        last_problem = ""
        for attempt in range(2):  # one repair round if the reply doesn't match the schema
            text = self._complete(system, user, schema, max_tokens)
            try:
                data = extract_json(text)
            except (ValueError, json.JSONDecodeError) as e:
                last_problem = f"the reply wasn't valid JSON ({e})"
            else:
                error = best_match(validator.iter_errors(data))
                if error is None:
                    return data
                where = ".".join(map(str, error.path)) or "(root)"
                last_problem = f"at {where}: {error.message[:300]}"
            user = (f"{prompt}\n\nYour previous reply didn't match the required JSON Schema "
                    f"({last_problem}). Reply again with only a JSON object that matches it.")
        raise LLMError(f"{self.model} didn't return JSON matching the schema: {last_problem}")

    # ---------------------------------------------------------------- transport

    def _complete(self, system: str, prompt: str, schema: dict[str, Any], max_tokens: int) -> str:
        while True:
            body = self._body(system, prompt, schema, max_tokens)
            resp = self._post("/responses" if self.api == "responses" else "/chat/completions", body)
            if resp.status_code == 400 and self._auto and self._mode in _DOWNGRADE \
                    and _FORMAT_ERROR.search(resp.text):
                self._mode = _DOWNGRADE[self._mode]  # provider can't do this mode; step down
                continue
            if resp.is_error:
                raise LLMError(f"{self.provider} {self.model}: HTTP {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            self.usage.calls += 1
            return self._read_responses(data) if self.api == "responses" else self._read_chat(data)

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.http.post(self.base_url + path, json=body)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise LLMError(f"Can't reach {self.base_url}: {type(e).__name__}: {e}") from e
                time.sleep(min(8.0, 0.5 * 2 ** attempt))
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                wait = resp.headers.get("retry-after")
                try:
                    delay = min(30.0, float(wait)) if wait else min(8.0, 0.5 * 2 ** attempt)
                except ValueError:
                    delay = min(8.0, 0.5 * 2 ** attempt)
                time.sleep(delay)
                continue
            return resp
        raise AssertionError("unreachable")

    def _body(self, system: str, prompt: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        if self._mode != "schema":  # the schema has to travel in the prompt instead
            system = (f"{system}\n\nRespond with only a JSON object (no prose, no code fences) "
                      f"that matches this JSON Schema:\n{json.dumps(schema)}")
        fmt: dict[str, Any] | None = None
        if self.api == "responses":
            body: dict[str, Any] = {"model": self.model, "instructions": system, "input": prompt,
                                    "max_output_tokens": max_tokens}
            if self._mode == "schema":
                fmt = {"type": "json_schema", "name": "connectorgen_output", "schema": schema, "strict": True}
            elif self._mode == "object":
                fmt = {"type": "json_object"}
            if fmt:
                body["text"] = {"format": fmt}
            return body
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        # OpenAI deprecated max_tokens; most compatible servers only know max_tokens
        body["max_completion_tokens" if self.provider == "openai" else "max_tokens"] = max_tokens
        if self._mode == "schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "connectorgen_output", "schema": schema, "strict": True}}
        elif self._mode == "object":
            body["response_format"] = {"type": "json_object"}
        return body

    def _read_chat(self, data: dict[str, Any]) -> str:
        usage = data.get("usage") or {}
        self.usage.input_tokens += usage.get("prompt_tokens") or 0
        self.usage.output_tokens += usage.get("completion_tokens") or 0
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.model} returned no choices.")
        choice = choices[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise LLMError(f"{self.model} declined: {str(message['refusal'])[:200]}")
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.model} hit the token limit; output truncated. "
                           "Send less input per call or raise max_tokens.")
        content = message.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            raise LLMError(f"{self.model} returned an empty message.")
        return content

    def _read_responses(self, data: dict[str, Any]) -> str:
        usage = data.get("usage") or {}
        self.usage.input_tokens += usage.get("input_tokens") or 0
        self.usage.output_tokens += usage.get("output_tokens") or 0
        if data.get("status") not in (None, "completed"):
            reason = (data.get("incomplete_details") or {}).get("reason") or data.get("status")
            raise LLMError(f"{self.model} response incomplete ({reason}); output truncated or stopped.")
        texts = []
        for item in data.get("output") or []:
            for part in item.get("content") or [] if isinstance(item, dict) else []:
                if part.get("type") == "refusal":
                    raise LLMError(f"{self.model} declined: {str(part.get('refusal'))[:200]}")
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
        if not texts and isinstance(data.get("output_text"), str):
            texts.append(data["output_text"])
        if not texts:
            raise LLMError(f"{self.model} returned no text output.")
        return "".join(texts)
