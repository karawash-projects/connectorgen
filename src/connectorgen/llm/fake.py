from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from .base import LLMError, Usage


@dataclass
class FakeCall:
    system: str
    prompt: str
    schema: dict[str, Any]


class FakeLLM:
    """Scripted model for tests. `responder` maps a call to a response dict; it can
    also be a list of responses returned in order. Responses are checked against the
    schema, so a test can't pass with output the real API would never produce."""

    def __init__(self, responder: Callable[[FakeCall], dict[str, Any]] | list[dict[str, Any]],
                 model: str = "fake-model"):
        self.model = model
        self.usage = Usage()
        self.calls: list[FakeCall] = []
        self._queue = list(responder) if isinstance(responder, list) else None
        self._fn = None if isinstance(responder, list) else responder

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any],
                      max_tokens: int = 8000) -> dict[str, Any]:
        call = FakeCall(system, prompt, schema)
        self.calls.append(call)
        self.usage.calls += 1
        if self._queue is not None:
            if not self._queue:
                raise LLMError("FakeLLM ran out of scripted responses")
            result = self._queue.pop(0)
        else:
            result = self._fn(call)  # type: ignore[misc]
        errors = list(Draft202012Validator(schema).iter_errors(result))
        if errors:
            raise AssertionError(f"FakeLLM response violates schema: {errors[0].message}")
        return result
