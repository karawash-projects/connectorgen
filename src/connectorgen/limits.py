"""Guards against schemas that are small on disk but explode when used.

A schema like `A: {allOf: [B, B, B, B]}`, `B: {allOf: [C, C, C, C]}`, ... makes a
validator evaluate 4^depth subschemas against every value, even an empty object. Such
schemas only come from hostile or broken specs, so the work is measured up front (cheaply,
with memoization per $ref) and the action is skipped instead of hanging the process.
"""

from __future__ import annotations

import contextvars
import functools
import json
import re
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError, best_match

DEFS_PREFIX = "#/$defs/"

# Subschemas evaluated in place for one value. Real APIs are in the tens or hundreds.
MAX_INPLACE_WORK = 50_000
_INPLACE_LISTS = ("allOf", "anyOf", "oneOf")
_INPLACE_ONE = ("not", "if", "then", "else")


class SchemaTooLarge(Exception):
    """Using this schema would take an unreasonable amount of work."""


# ---------------------------------------------------------------- bounded validation

# Keyword evaluations allowed for one validation: a base plus an allowance per value in the
# payload. Real APIs use ~10 steps per value (a full Stripe or GitHub object: ~2,000 steps);
# a hostile schema (allOf / unevaluatedProperties / dependentSchemas fan-out) needs billions
# even for `{}`. So big real payloads fit, and bombs stop within about a second.
VALIDATION_BASE_STEPS = 100_000
VALIDATION_STEPS_PER_VALUE = 200


def _size(instance: Any, cap: int = 10_000_000) -> int:
    n, stack = 0, [instance]
    while stack and n < cap:
        node = stack.pop()
        n += 1
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return n


_budget: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("validation_budget", default=None)


def _counted(fn: Any) -> Any:
    @functools.wraps(fn)
    def keyword(validator: Any, value: Any, instance: Any, schema: Any) -> Any:
        budget = _budget.get()
        if budget is not None:
            budget[0] -= 1
            if budget[0] < 0:
                raise SchemaTooLarge(f"validation needed more than {budget[1]:,} steps")
        return fn(validator, value, instance, schema)
    return keyword


# -- keywords whose cost isn't one step ---------------------------------------------
#
# A `pattern` can backtrack catastrophically, and jsonschema's uniqueItems / enum compare
# every pair. Inside a bounded validation, regexes run on the `regex` engine with a timeout,
# and uniqueItems / enum use hashing (keeping JSON Schema equality: 1 == 1.0, 1 != true).

REGEX_TIMEOUT = 0.25  # seconds per match


@functools.lru_cache(maxsize=4096)
def _compile(pattern: str) -> Any:
    import regex

    try:
        return regex.compile(pattern)
    except regex.error:
        return None


def _bounded_search(pattern: Any, string: str, flags: int = 0) -> Any:
    compiled = _compile(pattern) if isinstance(pattern, str) and not flags else None
    if compiled is None:
        return re.search(pattern, string, flags)
    try:
        return compiled.search(string, timeout=REGEX_TIMEOUT)
    except TimeoutError:
        raise SchemaTooLarge(f"the regex {pattern[:60]!r} takes too long to match") from None


class _ReShim:
    """Stands in for `re` inside jsonschema: bounded matching during our validations, the
    standard library everywhere else (so other users of jsonschema see no change)."""

    def __getattr__(self, name: str) -> Any:
        return getattr(re, name)

    @staticmethod
    def search(pattern: Any, string: str, flags: int = 0) -> Any:
        if _budget.get() is None:
            return re.search(pattern, string, flags)
        return _bounded_search(pattern, string, flags)


def _install_re_shim() -> None:
    from jsonschema import _keywords, _utils

    for module in (_keywords, _utils):
        if getattr(module, "re", None) is re:
            module.re = _ReShim()  # type: ignore[attr-defined]


def _canonical(value: Any) -> str:
    def norm(v: Any) -> Any:
        if isinstance(v, bool) or v is None or isinstance(v, str):
            return v
        if isinstance(v, (int, float)):
            return int(v) if isinstance(v, float) and v.is_integer() else v
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return repr(v)
    return json.dumps(norm(value), sort_keys=True, separators=(",", ":"), default=repr)


def _charge(n: int) -> None:
    budget = _budget.get()
    if budget is not None:
        budget[0] -= n
        if budget[0] < 0:
            raise SchemaTooLarge(f"validation needed more than {budget[1]:,} steps")


def _unique_items(validator: Any, unique: Any, instance: Any, schema: Any) -> Any:
    if unique and validator.is_type(instance, "array"):
        _charge(len(instance))
        seen: set[str] = set()
        for item in instance:
            key = _canonical(item)
            if key in seen:
                yield ValidationError(f"{instance!r} has non-unique elements")
                return
            seen.add(key)


_enum_sets: dict[int, tuple[Any, frozenset[str]]] = {}


def _enum(validator: Any, enums: Any, instance: Any, schema: Any) -> Any:
    cached = _enum_sets.get(id(enums))
    if cached is None or cached[0] is not enums:
        _charge(len(enums))
        cached = (enums, frozenset(_canonical(e) for e in enums))
        if len(_enum_sets) > 4096:
            _enum_sets.clear()
        _enum_sets[id(enums)] = cached
    if _canonical(instance) not in cached[1]:
        yield ValidationError(f"{instance!r} is not one of {enums!r}")


_install_re_shim()
# Built once: creating validator classes is slow and defeats jsonschema's caches.
_CountingValidator = validators.extend(
    Draft202012Validator,
    {**{name: _counted(fn) for name, fn in Draft202012Validator.VALIDATORS.items()},
     "uniqueItems": _counted(_unique_items), "enum": _counted(_enum)})


class BoundedValidator:
    """A Draft 2020-12 validator that gives up (SchemaTooLarge) instead of running for
    minutes or hours on a schema built to explode. Use it for schemas from specs, docs and
    MCP servers, i.e. anything a third party wrote."""

    def __init__(self, schema: Any, base_steps: int | None = None):
        self.base_steps = VALIDATION_BASE_STEPS if base_steps is None else base_steps
        self._validator = _CountingValidator(schema)

    def _run(self, instance: Any, consume: Any) -> Any:
        steps = self.base_steps + VALIDATION_STEPS_PER_VALUE * _size(instance)
        token = _budget.set([steps, steps])
        try:
            return consume(self._validator.iter_errors(instance))
        finally:
            _budget.reset(token)

    def errors(self, instance: Any) -> list[ValidationError]:
        return self._run(instance, list)

    def best_error(self, instance: Any) -> ValidationError | None:
        return self._run(instance, best_match)

    def is_valid(self, instance: Any) -> bool:
        return self.best_error(instance) is None


_defs_checked: list[Any] = [None, False]  # (the defs dict last checked, all cheap?)


def check_schema_work(schema: Any, defs: dict[str, Any], limit: int = MAX_INPLACE_WORK) -> None:
    """Raise SchemaTooLarge if checking a value against `schema` could take unreasonable work.
    Shared definitions are walked once per connector (cached), then each schema only needs
    its own subschemas checked."""
    if _defs_checked[0] is not defs:
        try:
            ok = inplace_work({"$defs-all": list(defs.values())}, defs, limit, follow_refs=False) >= 0
        except SchemaTooLarge:
            ok = False  # some definition is too costly: check each action's reachable ones
        _defs_checked[0], _defs_checked[1] = defs, ok
    inplace_work(schema, defs, limit, follow_refs=not _defs_checked[1])


def inplace_work(schema: Any, defs: dict[str, Any], limit: int = MAX_INPLACE_WORK, *,
                 follow_refs: bool = True) -> int:
    """How many subschemas a validator evaluates against one value: the schema itself plus
    everything reached through $ref / allOf / anyOf / oneOf / not / if-then-else, counted
    with repetition (that's what the validator does). Raises SchemaTooLarge past `limit`."""
    memo: dict[str, int] = {}
    active: set[str] = set()

    def cost(node: Any) -> int:
        if not isinstance(node, dict):
            return 1
        total = 1
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(DEFS_PREFIX):
            total += ref_cost(ref)
        for key in _INPLACE_LISTS:
            for sub in node.get(key) or []:
                total += cost(sub)
        for key in _INPLACE_ONE:
            if key in node:
                total += cost(node[key])
        if total > limit:
            raise SchemaTooLarge(f"validating one value would evaluate more than {limit:,} subschemas")
        return total

    def ref_cost(ref: str) -> int:
        if ref in memo:
            return memo[ref]
        if ref in active:  # recursion through in-place keywords: the validator would loop too
            return limit + 1
        active.add(ref)
        memo[ref] = cost(resolve(ref))
        active.discard(ref)
        return memo[ref]

    def resolve(ref: str) -> Any:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in ref[len(DEFS_PREFIX):].split("/")]
        node: Any = defs.get(parts[0])
        for p in parts[1:]:
            node = node.get(p) if isinstance(node, dict) else None
        return node

    # Every subschema reachable from `schema` (properties, items, referenced definitions) is
    # a place where a value gets checked; each must be cheap on its own. Iterative, with each
    # node visited once.
    worst, seen, stack = 0, set(), [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        worst = max(worst, cost(node))
        ref = node.get("$ref")
        if follow_refs and isinstance(ref, str) and ref.startswith(DEFS_PREFIX):
            stack.append(resolve(ref))
        stack.extend(v for k, v in node.items() if isinstance(v, (dict, list))
                     and (follow_refs or k not in ("$defs", "definitions")))  # shared defs: cached
    return worst
