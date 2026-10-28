"""Does a piece of text satisfy the request's schema?

The gateway validates every answer itself, against its Pydantic model. This module
checks the same thing earlier, against the JSON Schema the request carries, for two
callers that cannot wait for the gateway:

- the **router**, which escalates on a schema failure and so has to see it;
- the **cache**, which must never store an answer the gateway will reject, or a bad
  answer would be replayed for ever at no cost and no visibility.

Strict on purpose. An answer wrapped in prose or a code fence is a failure, not
something to cut out and accept: the translator's G5 decision is that repairing output
silently would hide a prompt regression, and that holds one layer down too. The retry
it earns is counted in the ledger, so a prompt that keeps producing prose shows up as
a cost rather than disappearing into a regex.

Problems are described from the **schema's** side only (path and the rule broken),
never by quoting the answer, because they are fed back into the retry prompt and the
model's output must not become instructions to the next call.
"""

from __future__ import annotations

import json
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


@lru_cache(maxsize=64)
def _validator(schema_text: str) -> Draft202012Validator:
    schema = json.loads(schema_text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _brief(value: object, limit: int = 80) -> str:
    text = json.dumps(value, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def problems(text: str, schema: dict, limit: int = 3) -> list[str]:
    """Empty when ``text`` is one JSON value valid against ``schema``."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        return [f"the answer is not a single JSON value (parse failed at line {e.lineno}, column {e.colno})"]
    try:
        validator = _validator(json.dumps(schema, sort_keys=True))
    except SchemaError as e:
        return [f"the request's schema is itself invalid: {e.message}"]
    found = sorted(validator.iter_errors(value), key=lambda e: (len(e.path), list(map(str, e.path))))
    return [
        f"{'/'.join(map(str, e.path)) or '(root)'}: breaks '{e.validator}' {_brief(e.validator_value)}"
        for e in found[:limit]
    ]
