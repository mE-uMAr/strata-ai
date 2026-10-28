"""The Codex CLI, for OpenAI models on a ChatGPT subscription.

    codex exec --json --skip-git-repo-check --sandbox read-only -m <m> -

with the prompt on stdin. ``--json`` prints one event per line; the answer is the last
``item.completed`` event whose item is an ``agent_message``, and usage is on
``turn.completed`` (``input_tokens``, which includes ``cached_input_tokens``, and
``output_tokens``). A ``turn.failed`` or ``error`` event carries the failure.

Choices worth knowing about:

- **Read-only sandbox in an empty directory.** Codex is an agent and may try to run a
  command before answering. It cannot be given zero tools from the command line, so
  the containment is the sandbox plus `_process`'s empty working directory: there is
  nothing to read and nothing it may write. A command it runs anyway costs tokens, and
  shows up in the usage numbers rather than being hidden.
- **The credential scrub keeps it on the subscription.** With ``OPENAI_API_KEY`` or
  ``CODEX_API_KEY`` in its environment the CLI can bill the API instead of the ChatGPT
  plan. Both are removed from the child's environment.
- **Reasoning effort** defaults to ``low`` (``-c model_reasoning_effort=low``):
  extraction against a schema rarely repays long reasoning, and reasoning tokens are
  billed as output. Configurable per deployment.
- **``--output-schema``** (native structured output) is used when ``structured = true``;
  the schema is written to a file in the call's directory, so nothing quoted crosses a
  shim's command line.
- **Images** are written into the call's directory and passed with ``--image <file>``,
  one flag per image. All the GPT-5 models Codex runs take images; narrow the set with
  ``vision_models`` if yours does not. Not yet checked against a live Codex install.
- **The output ceiling** is checked against the measured ``output_tokens`` after the
  call: the CLI's own setting for it is a config-file key, not a per-call flag, and an
  unverified key is worse than an honest after-the-fact check.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..accounting import price_for
from ..protocol import (
    Adapter, Answer, Malformed, ProviderError, QuotaExhausted, Request, Unavailable, estimate_tokens, images_of,
)
from ..shaping import image_files, with_contract
from . import _process

LIMIT = ("usage limit", "rate limit", "too many requests", "429", "quota")
UNAVAILABLE = ("not logged in", "login", "unauthorized", "401", "authentication", "stream disconnected",
               "connection")


class Codex(Adapter):
    path = "subscription"

    def __init__(self, table: Mapping[str, Any] | None = None, *, environ: Mapping[str, str] | None = None,
                 prices: Mapping[str, Mapping[str, float]] | None = None) -> None:
        table = dict(table or {})
        self.name = "codex"
        self.binary = list(table.get("binary", ["codex"]))
        self.timeout_s = float(table.get("timeout_s", 300))
        self.models = dict(table.get("models", {}))
        self.structured = bool(table.get("structured", False))
        self.reasoning_effort = table.get("reasoning_effort", "low")
        self.extra_args = list(table.get("extra_args", []))
        self.env = dict(table.get("env", {}))
        self.quota_units = float(table.get("quota_units", 1.0))
        self.vision = frozenset(table["vision_models"]) if "vision_models" in table else None
        self._environ = environ
        self._prices = dict(prices or {})

    def ask(self, request: Request, model: str, note: str = "") -> Answer:
        prompt = (request.prompt if self.structured else with_contract(request.prompt, request.schema)) + note
        executable = _process.resolve(self.binary)
        pictures = image_files(images_of(request))

        def argv(workdir) -> list[str]:
            args = ["exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
                    "-m", model]
            if self.reasoning_effort:
                args += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
            if self.structured:
                args += ["--output-schema", str(workdir / "schema.json")]
            for name in pictures:
                args += ["--image", str(workdir / name)]
            return [*executable, *args, *self.extra_args, "-"]

        env = _process.child_env(self._environ, extra=self.env)
        result = _process.run(argv, stdin_text=prompt, timeout_s=self.timeout_s, env=env,
                              files={"schema.json": json.dumps(request.schema), **pictures})
        return self._read(request, model, prompt, result)

    def _read(self, request: Request, model: str, prompt: str, result: _process.Run) -> Answer:
        events = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        failure = next((e for e in reversed(events) if e.get("type") in ("turn.failed", "error")), None)
        if failure is not None or result.returncode != 0:
            detail = ""
            if failure is not None:
                detail = str((failure.get("error") or {}).get("message") or failure.get("message") or "")
            if _process.matches(LIMIT, detail, result.stderr):
                raise QuotaExhausted(_process.describe_failure(self.name, result, detail))
            if _process.matches(UNAVAILABLE, detail, result.stderr):
                raise Unavailable(_process.describe_failure(self.name, result, detail))
            raise ProviderError(_process.describe_failure(self.name, result, detail))

        messages = [e["item"].get("text", "") for e in events
                    if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"]
        text = messages[-1].strip() if messages else ""
        if not text:
            raise ProviderError(_process.describe_failure(self.name, result, "no agent_message in the event stream"))

        usage = next((e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed" and e.get("usage")),
                     None)
        if usage:
            input_tokens = int(usage.get("input_tokens") or 0)
            cached = int(usage.get("cached_input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            source = "measured"
        else:
            input_tokens, cached, output_tokens, source = estimate_tokens(prompt), 0, estimate_tokens(text), "estimated"
        price = price_for(model, self._prices)
        equivalent = price.cost(input_tokens=input_tokens, output_tokens=output_tokens,
                                cache_read_tokens=cached) if price else 0.0
        answer = Answer(text=text, input_tokens=input_tokens, output_tokens=output_tokens,
                        latency_ms=result.latency_ms, cost_usd=0.0, adapter=self.name, model=model,
                        path="subscription", usage_source=source, cached_input_tokens=cached,
                        quota_units=self.quota_units, metered_equivalent_usd=equivalent)
        if usage and output_tokens > request.max_output_tokens:
            raise Malformed(f"{self.name}: {output_tokens} output tokens, over the ceiling of "
                            f"{request.max_output_tokens}", spent=answer)
        return answer
