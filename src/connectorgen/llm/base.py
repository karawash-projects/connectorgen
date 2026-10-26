from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..errors import ConnectorError


class LLMError(ConnectorError):
    """The model call failed or returned something unusable."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0

    def __str__(self) -> str:
        return (f"{self.calls} model calls, {self.input_tokens:,} input / "
                f"{self.output_tokens:,} output tokens, {self.cache_hits} cache hits")


@runtime_checkable
class LLM(Protocol):
    """A model that returns JSON matching a schema.

    `schema` follows the structured-output subset: every object has
    additionalProperties false, no recursion, no numeric/length constraints.
    Use `strict_object` to build them. Free-form JSON Schemas (which may be
    recursive) travel as JSON strings inside the response instead.
    """

    model: str
    usage: Usage

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any],
                      max_tokens: int = 8000) -> dict[str, Any]: ...


def strict_object(properties: dict[str, Any], *, description: str = "") -> dict[str, Any]:
    """Object schema with every property required and no extras. Optional values
    are expressed as nullable (`{"type": ["string", "null"]}`) instead."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


def nullable(type_: str, description: str = "") -> dict[str, Any]:
    s: dict[str, Any] = {"type": [type_, "null"]}
    if description:
        s["description"] = description
    return s


def text(description: str = "") -> dict[str, Any]:
    return {"type": "string", "description": description} if description else {"type": "string"}


def enum(values: list[Any], description: str = "") -> dict[str, Any]:
    s: dict[str, Any] = {"enum": values}
    if description:
        s["description"] = description
    return s
