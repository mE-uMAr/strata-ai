"""Step 3: the generic CLI adapter, driven by configuration, against a fake CLI.

Measures, with no subscription:

- a tool declared as a TOML table answers, with usage read from its output by path
  expressions and ``usage_source="measured"``;
- the same tool with no usage in its output is marked ``estimated``, and the counts are
  not zero;
- the call ran in an empty temporary directory, not the repository, and that directory
  is gone afterwards;
- credential-shaped variables in the parent's environment did not reach the tool;
- a tool that stops to ask a question on stdin fails at once instead of hanging;
- a tool that hangs is killed at the timeout, and the call returns close to it;
- a usage-limit message becomes ``QuotaExhausted``, a logged-out tool ``Unavailable``,
  and stderr is in the error;
- the prompt on argv works for a real executable, and is refused for a cmd.exe shim.
"""

from __future__ import annotations

import os
import sys
import time
import tomllib
from pathlib import Path

from common import ROOT, claim, fake_binary, fake_env, finish, heading, request, scratch, seen

from provider.cli.command import CommandAdapter, CommandSpec, select
from provider.protocol import ProviderError, QuotaExhausted, Unavailable

state = scratch("s03")
CONFIG = f"""
[command.fake]
binary = {fake_binary("generic")!r}
argv = []
prompt_via = "stdin"
output = "json"
answer = "response"
input_tokens = "stats.models.*.tokens.prompt"
output_tokens = "stats.models.*.tokens.candidates"
cached_input_tokens = "stats.models.*.tokens.cached"
timeout_s = 3
models = {{ extraction = "fake-small" }}
""".replace("'", '"')


def adapter(modes: str = "ok", **overrides) -> CommandAdapter:
    table = tomllib.loads(CONFIG)["command"]["fake"]
    table.update(overrides)
    table["env"] = fake_env(state, modes)
    parent = {**os.environ, "ANTHROPIC_API_KEY": "placeholder-not-a-key", "OPENAI_API_KEY": "placeholder",
              "AIPROVIDER_ANTHROPIC_API_KEY": "placeholder"}
    return CommandAdapter(CommandSpec.from_table("fake", table), environ=parent)


def calls() -> int:
    return int((state / "calls").read_text())


heading("a tool declared in config answers")
answer = adapter().complete(request())
info = seen(state, calls())
claim("answer adapter / path", f"{answer.adapter} / {answer.path}", answer.path == "subscription")
claim("usage_source", answer.usage_source, answer.usage_source == "measured")
claim("input / output tokens (read from the tool's stats)", f"{answer.input_tokens} / {answer.output_tokens}",
      answer.input_tokens == info["prompt_tokens"])
claim("cost_usd on a subscription path", answer.cost_usd, answer.cost_usd == 0.0)
claim("quota_units used", answer.quota_units, answer.quota_units == 1.0)
claim("prompt sent ends with the output contract", "validate against this JSON Schema" in info["prompt_tail"],
      "validate against this JSON Schema" in info["prompt_tail"])

heading("isolation")
claim("ran outside the repository", not info["cwd"].startswith(str(ROOT)), not info["cwd"].startswith(str(ROOT)))
claim("working directory held only the request's schema", info["cwd_entries"], info["cwd_entries"] == ["schema.json"])
claim("working directory removed afterwards", Path(info["cwd"]).exists(), not Path(info["cwd"]).exists())
claim("credential-shaped variables visible to the tool", info["credential_vars"], info["credential_vars"] == [])

heading("usage the tool does not report")
answer = adapter("nousage").complete(request())
claim("usage_source", answer.usage_source, answer.usage_source == "estimated")
claim("estimated input / output tokens (never zero)", f"{answer.input_tokens} / {answer.output_tokens}",
      answer.input_tokens > 0 and answer.output_tokens > 0)

heading("failures that must not hang or be swallowed")
for mode, expected in (("ask", ProviderError), ("hang", Unavailable), ("limit", QuotaExhausted),
                       ("logged_out", Unavailable), ("crash", ProviderError)):
    started = time.perf_counter()
    try:
        adapter(mode).complete(request())
        claim(f"{mode}: raised", "no", False)
    except ProviderError as e:
        elapsed = time.perf_counter() - started
        ok = type(e) is expected and (elapsed < 2.0 if mode != "hang" else 2.5 < elapsed < 8.0)
        claim(f"{mode}: {type(e).__name__} after", f"{elapsed:.2f}s", ok)
        print(f"      {str(e)[:150]}")

heading("the prompt on argv")
answer = adapter("ok", prompt_via="argv", argv=["--prompt", "{prompt}"]).complete(
    request("Prompt mentioning {model} and {workdir} literally"))
info = seen(state, calls())
claim("argv mode answered", answer.usage_source, answer.usage_source == "measured")
claim("placeholder-shaped text in the prompt reached the tool unchanged",
      "{model} and {workdir}" in info["argv"][-1], "{model} and {workdir}" in info["argv"][-1])
if sys.platform == "win32":
    shim = state / "tool.cmd"
    shim.write_text("@echo off\r\necho {}\r\n", encoding="utf-8")
    try:
        adapter("ok", binary=[str(shim)], prompt_via="argv", argv=["--prompt", "{prompt}"]).complete(request())
        claim("prompt on a .cmd shim's argv refused", "ran", False)
    except ProviderError as e:
        claim("prompt on a .cmd shim's argv refused", type(e).__name__, "shim" in str(e))
else:
    print("  (cmd.exe shims exist only on Windows; nothing to check here)")

heading("path expressions")
events = [{"type": "item.completed", "item": {"type": "agent_message", "text": "a"}},
          {"type": "item.completed", "item": {"type": "agent_message", "text": "b"}},
          {"type": "turn.completed", "usage": {"input_tokens": 7}}]
claim("[type=item.completed].item.text", select(events, "[type=item.completed].item.text"),
      select(events, "[type=item.completed].item.text") == ["a", "b"])
claim("-1.usage.input_tokens", select(events, "-1.usage.input_tokens"), select(events, "-1.usage.input_tokens") == [7])

finish("s03")
