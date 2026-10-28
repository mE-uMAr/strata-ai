"""Shared pieces for the simulations.

Each simulation prints what it measured. ``claim`` prints a value next to the claim it
supports and remembers whether the claim held; ``finish`` exits non-zero if any did not,
so ``sim/run_all.py`` can say which numbers in the docs are currently true.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from translator.gateway.providers import Request  # noqa: E402

TRANSLATOR = ROOT.parent / "translator"
_failures: list[str] = []


def heading(text: str) -> None:
    print(f"\n== {text}")


def claim(label: str, value: object, holds: bool) -> None:
    mark = "ok" if holds else "FAILED"
    print(f"  {label:<58} {value!s:<24} [{mark}]")
    if not holds:
        _failures.append(label)


def finish(name: str) -> None:
    if _failures:
        print(f"\n{name}: {len(_failures)} claim(s) failed: {_failures}")
        raise SystemExit(1)
    print(f"\n{name}: every claim held")


def scratch(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"aiprovider-sim-{prefix}-"))


SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "decision": {"enum": ["proceed", "reject"]},
    },
    "required": ["rules", "decision"],
    "additionalProperties": False,
}
ANSWER = {"rules": ["enter when the fast EMA crosses above the slow EMA"], "decision": "proceed"}


def request(prompt: str = "Extract the rules.\n\nThe document follows.", *, model_id: str = "extraction",
            schema: dict = SCHEMA, max_output_tokens: int = 2000, name: str = "rule_inventory") -> Request:
    body = json.dumps({"model": model_id, "prompt": prompt, "schema": schema}, sort_keys=True)
    return Request(prompt=prompt, prompt_name=name, model_id=model_id, temperature=0.0,
                   max_output_tokens=max_output_tokens, seed=None, schema=schema,
                   input_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest())
