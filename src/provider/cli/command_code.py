"""The Command Code CLI, on a Command Code account.

    cmd -p --output-format json -m <model> --max-turns 1 --no-session --no-skills
        --safe-mode --skip-onboarding --no-auto-update -t

with the prompt on stdin. ``--output-format json`` prints an NDJSON event stream that
ends in a result line::

    {"type": "result", "subtype": "success", "stopReason": "end_turn",
     "usage": {"inputTokens": 14417, "outputTokens": 9, "cacheReadTokens": 5248,
               "cacheWriteTokens": 0},
     "durationMs": 4063, "finalText": "{\\"sum\\": 5}"}

``inputTokens`` is the total. Two identical calls reported 14417 and 14411 while their
cache reads were 5248 and 4096, so ``cacheReadTokens`` is a part of ``inputTokens``,
not an addition to it. Usage is therefore measured, and the answer is ``finalText``.

Why each flag (all checked against ``cmd --help`` at command-code 1.84.0):

- ``--max-turns 1``: one model turn, so the agent answers instead of exploring. A text
  task that tries a tool hits the cap; the CLI exits 8 and that becomes `Malformed`,
  which earns one retry with a reminder that there is nothing to read.
- ``--no-session``, ``--no-skills``, ``--safe-mode``, ``--skip-onboarding``: nothing is
  persisted, and no skills, installed mods or onboarding run.
- ``--no-auto-update``: running the CLI otherwise updates it in place, which changed the
  installed version (1.73.2 to 1.84.0) on the first ``--help``. A provider must not
  change the tool it depends on as a side effect of answering.
- ``-t``: trusts the call's own empty directory, so no permission prompt appears.

**The overhead is large.** The CLI sends its own agent system prompt and tool
definitions with every call: about 14,400 input tokens for a 20-token question, of
which only part is read from cache. There is no flag to replace or drop them. Route
Command Code after paths with less overhead unless its models are the reason to use it.

**Images** are written into the call's directory and ``@``-mentioned before the task;
the agent reads them with its own read-only ``read_file`` tool, so a vision call needs
one more turn per image. Measured live with ``deepseek/deepseek-v4.1-flash``: it read
the image and answered correctly in 2 turns and 29,165 input tokens.

**Billing.** The adapter treats Command Code as a subscription (``path =
"subscription"``, one quota unit per call). If the account is pay-as-you-go instead,
set ``path = "metered"`` in ``[command_code]``, which puts it behind
``AIPROVIDER_ALLOW_METERED`` like any key. The CLI reports no cost, so the
metered-equivalent figure needs a price for the model (the Claude models are in the
table; others need ``[prices]``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..accounting import price_for
from ..protocol import (
    Adapter, Answer, Malformed, ProviderError, QuotaExhausted, Request, Unavailable, estimate_tokens, images_of,
)
from ..shaping import image_files, image_mentions, with_contract
from . import _process

LIMIT = ("usage limit", "rate limit", "limit reached", "too many requests", "429", "insufficient credit",
         "out of credits", "quota")
UNAVAILABLE = ("not logged in", "cmd login", "please log in", "unauthorized", "401", "authentication",
               "not authenticated", "connection", "econnrefused", "network")
TURN_CAP_EXIT = 8


MODELS: dict[str, dict[str, bool]] = {
    # From `cmd --list-models` at command-code 1.84.0. `vision` is true where the model's
    # own description says vision, multimodal, omni-modal or visual, and for the Claude,
    # Gemini and GPT families, which all take images. `free` marks models offered at no
    # charge; see docs/open-issues.md before sending an uploader's document to one.
    "deepseek/deepseek-v4-pro": {}, "deepseek/deepseek-v4-flash": {},
    "deepseek/deepseek-v4-flash-vision-exp": {"vision": True}, "deepseek/deepseek-v4-flash-fast": {},
    "deepseek/deepseek-v4.1-flash": {"vision": True}, "deepseek/deepseek-v4.1-flash-fast": {},
    "moonshotai/kimi-k3.1-pro": {}, "moonshotai/kimi-k3": {},
    "moonshotai/kimi-k2.7-code": {"vision": True}, "moonshotai/kimi-k2.7-code-highspeed": {"vision": True},
    "moonshotai/kimi-k2.6": {"vision": True}, "moonshotai/kimi-k2.5": {"vision": True},
    "z-ai/glm-5.3-flash": {}, "z-ai/glm-5.3-flashx": {}, "zai-org/glm-5.3": {}, "zai-org/glm-5.2": {},
    "zai-org/glm-5.2-fast": {}, "zai-org/glm-5.1": {}, "zai-org/glm-5": {},
    "minimax/minimax-m3.1-flash": {"vision": True}, "minimaxai/minimax-m3": {"vision": True},
    "minimaxai/minimax-m2.7": {}, "minimaxai/minimax-m2.5": {},
    "ornith/ornith-1.5-122b": {},
    "xiaomi/mimo-v2.6-pro": {"vision": True}, "xiaomi/mimo-v2.6-pro-ultraspeed": {"vision": True},
    "xiaomi/mimo-v2.6-flash": {"vision": True},
    "qwen/qwen3.8-omni-flash": {"vision": True}, "qwen/qwen3.8-max-prime": {}, "qwen/qwen3.8-max-0902": {},
    "qwen/qwen3.8-max": {}, "qwen/qwen3.8-27b": {"vision": True}, "qwen/qwen3.8-flash": {},
    "qwen/qwen3.7-max": {}, "qwen/qwen3.7-plus": {}, "qwen/qwen3.7-flash": {}, "qwen/qwen3.6-max-preview": {},
    "qwen/qwen3.6-plus": {},
    "meituan/longcat-2.0": {}, "stepfun/step-5-preview": {}, "stepfun/step-3.7-flash": {"vision": True},
    "stepfun/step-3.5-flash": {}, "tencent/hy3-paid": {}, "tencent/hy4-preview": {},
    "nvidia/nemotron-3-ultra-550b-a55b": {}, "nvidia/nemotron-3.5-super-vl": {"vision": True},
    "thinkingmachines/inkling": {"vision": True}, "thinkingmachines/inkling-small": {},
    "reflection/beam:free": {"free": True}, "poolside/laguna-s-2.1-free": {"free": True},
    "inclusionai/ling-3.1-flash": {}, "mistral/mistral-large-4": {"vision": True},
    "stealth/glyph-cluster:free": {"free": True},
    **{m: {"vision": True} for m in (
        "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5-1", "claude-fable-5",
        "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-haiku-5-5",
        "claude-haiku-4-5",
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
        "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5.4-mini",
        "google/gemini-4-argon", "google/gemini-3.8-flash", "google/gemini-3.7-flash", "google/gemini-3.6-flash",
        "google/gemini-3.5-flash", "google/gemini-3.5-flash-lite", "google/gemini-3.1-flash-lite",
        "meta/muse-spark-1.3", "meta/muse-spark-1.3-contributor")},
    "sakana/fugu-ultra": {}, "meta/muse-spark-1.1": {}, "meta/muse-spark-1.2": {},
    "meta/muse-spark-1.2-contributor": {}, "meta/muse-spark-open": {}, "meta/muse-wonder-1.0": {},
    "xai/grok-4.5": {}, "xai/grok-4.6": {}, "xai/grok-4.7": {},
}
"""Command Code's text models, with the ones that can see images marked. The CLI also
lists a headless "decision model" (``typesafe/jev``) with its own input format; it does
not answer prompts and is left out."""

VISION_MODELS = frozenset(m for m, caps in MODELS.items() if caps.get("vision"))
FREE_MODELS = frozenset(m for m, caps in MODELS.items() if caps.get("free"))


class CommandCode(Adapter):
    def __init__(self, table: Mapping[str, Any] | None = None, *, environ: Mapping[str, str] | None = None,
                 prices: Mapping[str, Mapping[str, float]] | None = None) -> None:
        table = dict(table or {})
        self.name = "command_code"
        self.path = table.get("path", "subscription")
        if self.path not in ("subscription", "metered"):
            raise ValueError(f"command_code.path must be 'subscription' or 'metered', not {self.path!r}")
        self.binary = list(table.get("binary", ["commandcode"]))
        self.timeout_s = float(table.get("timeout_s", 300))
        self.models = dict(table.get("models", {}))
        self.effort = table.get("effort")
        self.vision = frozenset(table["vision_models"]) if "vision_models" in table else VISION_MODELS
        self.extra_args = list(table.get("extra_args", []))
        self.env = dict(table.get("env", {}))
        self.quota_units = float(table.get("quota_units", 1.0))
        self._environ = environ
        self._prices = dict(prices or {})

    def ask(self, request: Request, model: str, note: str = "") -> Answer:
        images = images_of(request)
        files: dict[str, str | bytes] = image_files(images) if images else {}
        prompt = (image_mentions(files) if images else "") + with_contract(request.prompt, request.schema) + note
        turns = 1 + len(images)
        args = ["-p", "--output-format", "json", "-m", model, "--max-turns", str(turns), "--no-session",
                "--no-skills", "--safe-mode", "--skip-onboarding", "--no-auto-update", "-t"]
        if self.effort:
            args += ["--effort", str(self.effort)]
        argv = [*_process.resolve(self.binary), *args, *self.extra_args]
        env = _process.child_env(self._environ, extra=self.env)
        result = _process.run(argv, stdin_text=prompt, timeout_s=self.timeout_s, env=env, files=files)
        return self._read(request, model, prompt, result)

    def _read(self, request: Request, model: str, prompt: str, result: _process.Run) -> Answer:
        lines = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        final = next((e for e in reversed(lines) if e.get("type") == "result"), None)
        usage = (final or {}).get("usage") or {}
        spent = self._answer(model, prompt, final, usage, result) if usage else None

        if result.returncode == TURN_CAP_EXIT:
            raise Malformed(f"{self.name}: the model reached for a tool instead of answering (turn cap hit); "
                            "answer directly from the text given, there is nothing to read", spent)
        if final is None or result.returncode != 0 or final.get("subtype") != "success":
            detail = str((final or {}).get("error") or (final or {}).get("finalText") or (final or {}).get("subtype") or "")
            if _process.matches(LIMIT, detail, result.stderr):
                raise QuotaExhausted(_process.describe_failure(self.name, result, detail), spent)
            if _process.matches(UNAVAILABLE, detail, result.stderr):
                raise Unavailable(_process.describe_failure(self.name, result, detail), spent)
            raise ProviderError(_process.describe_failure(self.name, result, detail or "no result line in the output"))

        answer = spent or self._answer(model, prompt, final, usage, result)
        if not answer.text:
            raise ProviderError(_process.describe_failure(self.name, result, "the result has no finalText"))
        if usage and answer.output_tokens > request.max_output_tokens:
            raise Malformed(f"{self.name}: {answer.output_tokens} output tokens, over the ceiling of "
                            f"{request.max_output_tokens}", answer)
        return answer

    def _answer(self, model: str, prompt: str, final: dict | None, usage: dict, result: _process.Run) -> Answer:
        text = str((final or {}).get("finalText") or "").strip()
        if usage:
            input_tokens = int(usage.get("inputTokens") or 0)
            cached = int(usage.get("cacheReadTokens") or 0)
            written = int(usage.get("cacheWriteTokens") or 0)
            output_tokens = int(usage.get("outputTokens") or 0)
            source = "measured"
        else:
            input_tokens, cached, written, output_tokens, source = (estimate_tokens(prompt), 0, 0,
                                                                    estimate_tokens(text), "estimated")
        price = price_for(model, self._prices)
        equivalent = price.cost(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cached,
                                cache_write_tokens=written) if price else 0.0
        return Answer(text=text, input_tokens=input_tokens, output_tokens=output_tokens,
                      latency_ms=int((final or {}).get("durationMs") or result.latency_ms),
                      cost_usd=equivalent if self.path == "metered" else 0.0, adapter=self.name, model=model,
                      path=self.path, usage_source=source, cached_input_tokens=cached,
                      quota_units=self.quota_units if self.path == "subscription" else 0.0,
                      metered_equivalent_usd=equivalent)
