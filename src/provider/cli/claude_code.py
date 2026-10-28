"""The Claude Code CLI, on a Claude subscription.

    claude -p --output-format json --model <m> --tools "" --system-prompt <short> ...

with the prompt on stdin. Print mode answers once and exits; the JSON result carries
the answer, measured usage (including prompt-cache reads and writes) and
``total_cost_usd``, which is what the same call would have cost on the API. That figure
becomes ``metered_equivalent_usd``; ``cost_usd`` is 0.0 because the subscription has
already been paid for.

Choices worth knowing about, with the alternative that was rejected:

- **Not ``--bare``.** It is the obvious "minimal mode", but it makes the CLI
  authenticate strictly with ``ANTHROPIC_API_KEY`` and never read the subscription's
  OAuth login. On this path that would turn every call into a metered one.
- **The credential scrub is what keeps this a subscription call.** If
  ``ANTHROPIC_API_KEY`` were in the child's environment the CLI would use it and bill
  per token, and nothing in its output would say so. `_process.child_env` removes it,
  and the simulation checks the variable never arrives.
- **``--tools ""``**: no tools at all, so the model cannot read files, run commands or
  fetch URLs, and no tool definitions are sent (they cost input tokens on every call).
  ``--strict-mcp-config`` with no MCP config keeps user MCP servers out for the same
  reasons, and ``--setting-sources project`` in an empty directory keeps user hooks out.
- **``--system-prompt``** replaces the default coding-agent system prompt, which
  describes tools this path never has, with two sentences.
- **The output ceiling** is ``CLAUDE_CODE_MAX_OUTPUT_TOKENS``, set per call from
  ``request.max_output_tokens``.
- **``--json-schema``** (native structured output) is available with
  ``structured = true``, but off by default: on Windows the CLI is a ``cmd.exe`` shim and
  a schema's quotes cannot be passed through one safely. Point ``binary`` at
  ``["node", "<path>/cli.js"]`` to use it there.
- Temperature and seed have no CLI flag. Extraction's determinism comes from repeated
  runs (translator ADR 0010), so this is recorded rather than worked around.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..accounting import price_for
from ..protocol import (
    Adapter, Answer, BudgetSpent, Malformed, ProviderError, QuotaExhausted, Refused, Request, Unavailable,
    estimate_tokens,
)
from ..shaping import SYSTEM, with_contract
from . import _process

LIMIT = ("usage limit", "limit reached", "rate limit", "rate_limit", "too many requests")
UNAVAILABLE = ("not logged in", "please run /login", "/login", "invalid api key", "oauth", "authentication",
               "overloaded", "api error: 5", "connection error")
BUDGET = ("credit balance is too low",)
CEILING = ("output token maximum", "max_tokens", "exceeded the")

QUIET_TRAFFIC = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class ClaudeCode(Adapter):
    path = "subscription"

    def __init__(self, table: Mapping[str, Any] | None = None, *, environ: Mapping[str, str] | None = None,
                 prices: Mapping[str, Mapping[str, float]] | None = None) -> None:
        table = dict(table or {})
        self.name = "claude_code"
        self.binary = list(table.get("binary", ["claude"]))
        self.timeout_s = float(table.get("timeout_s", 300))
        self.models = dict(table.get("models", {}))
        self.structured = bool(table.get("structured", False))
        self.system_prompt = str(table.get("system_prompt", SYSTEM))
        self.extra_args = list(table.get("extra_args", []))
        self.env = dict(table.get("env", {}))
        self.quota_units = float(table.get("quota_units", 1.0))
        self._environ = environ
        self._prices = dict(prices or {})

    def argv(self, model: str, request: Request) -> list[str]:
        args = ["-p", "--output-format", "json", "--model", model, "--tools", "", "--strict-mcp-config",
                "--setting-sources", "project", "--disable-slash-commands", "--no-session-persistence",
                "--system-prompt", self.system_prompt, *self.extra_args]
        if self.structured:
            args += ["--json-schema", json.dumps(request.schema, separators=(",", ":"))]
        return [*_process.resolve(self.binary), *args]

    def ask(self, request: Request, model: str, note: str = "") -> Answer:
        prompt = (request.prompt if self.structured else with_contract(request.prompt, request.schema)) + note
        env = _process.child_env(self._environ, extra={
            **QUIET_TRAFFIC, **self.env, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(request.max_output_tokens)})
        result = _process.run(self.argv(model, request), stdin_text=prompt, timeout_s=self.timeout_s, env=env)
        return self._read(request, model, prompt, result)

    def _read(self, request: Request, model: str, prompt: str, result: _process.Run) -> Answer:
        envelope = _result_object(result.stdout)
        message = str(envelope.get("result", "")) if envelope else ""
        usage = envelope.get("usage") if envelope else None
        # What a failed call spent, when the CLI says. A rejection with no usage (a
        # usage limit, a login failure) consumed nothing, so nothing is estimated for it.
        spent = self._answer(request, model, prompt, message, envelope, result) if usage else None

        if envelope is None or result.returncode != 0 or envelope.get("is_error") or envelope.get("subtype") != "success":
            detail = message or (envelope or {}).get("subtype", "")
            if _process.matches(BUDGET, detail, result.stderr):
                raise BudgetSpent(_process.describe_failure(self.name, result, detail), spent)
            if _process.matches(LIMIT, detail, result.stderr):
                raise QuotaExhausted(_process.describe_failure(self.name, result, detail), spent)
            if _process.matches(UNAVAILABLE, detail, result.stderr):
                raise Unavailable(_process.describe_failure(self.name, result, detail), spent)
            if _process.matches(CEILING, detail):
                raise Malformed(_process.describe_failure(self.name, result, detail), spent)
            raise ProviderError(_process.describe_failure(
                self.name, result, detail or "no result object in the output"))
        if spent is None:
            spent = self._answer(request, model, prompt, message, envelope, result)
        if envelope.get("stop_reason") == "refusal":
            raise Refused(f"{self.name}: the model declined", spent)
        if usage and spent.output_tokens > request.max_output_tokens:
            raise Malformed(f"{self.name}: {spent.output_tokens} output tokens, over the ceiling of "
                            f"{request.max_output_tokens}", spent)
        if not spent.text:
            raise ProviderError(_process.describe_failure(self.name, result, "the result is empty"))
        return spent

    def _answer(self, request: Request, model: str, prompt: str, message: str, envelope: dict,
                result: _process.Run) -> Answer:
        text = message.strip()
        if self.structured and envelope.get("structured_output") is not None:
            text = json.dumps(envelope["structured_output"], separators=(",", ":"))
        usage = envelope.get("usage") or {}
        if usage:
            read = int(usage.get("cache_read_input_tokens") or 0)
            input_tokens = (int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                            + read)
            output_tokens = int(usage.get("output_tokens") or 0)
            source = "measured"
        else:
            read, input_tokens, output_tokens, source = 0, estimate_tokens(prompt), estimate_tokens(text), "estimated"
        reported = envelope.get("total_cost_usd")
        if isinstance(reported, (int, float)):
            equivalent = float(reported)
        else:
            price = price_for(model, self._prices)
            equivalent = price.cost(input_tokens=input_tokens, output_tokens=output_tokens,
                                    cache_read_tokens=read) if price else 0.0
        answered_by = ",".join(sorted(envelope.get("modelUsage") or {})) or model
        return Answer(text=text, input_tokens=input_tokens, output_tokens=output_tokens,
                      latency_ms=result.latency_ms, cost_usd=0.0, adapter=self.name, model=answered_by,
                      path="subscription", usage_source=source, cached_input_tokens=read,
                      quota_units=self.quota_units, metered_equivalent_usd=equivalent)


def _result_object(stdout: str) -> dict | None:
    """The ``type: result`` object. Older CLIs printed a list of messages instead."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        for item in reversed(parsed):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
    return None
