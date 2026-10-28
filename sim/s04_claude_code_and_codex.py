"""Step 4: the Claude Code and Codex adapters, against the fake CLI in their shapes.

Measures:

- Claude Code: usage read from the JSON result (cache writes and reads included),
  ``metered_equivalent_usd`` taken from the CLI's own ``total_cost_usd``, ``cost_usd``
  zero; a second call sharing the prompt's head reports ``cached_input_tokens``;
- the flags that keep the call cheap and contained are on the command line (no tools,
  a two-sentence system prompt, no ``--bare``), the output ceiling reached the CLI, and
  no API key reached it;
- a usage limit, a refusal and a logged-out CLI each map to their own error, and the
  refusal carries what it spent;
- Codex: usage from ``turn.completed``, the answer from the last ``agent_message``, a
  ``turn.failed`` usage limit as ``QuotaExhausted``, and missing usage marked estimated.
"""

from __future__ import annotations

import json
import os

from common import ANSWER, claim, fake_binary, fake_env, finish, heading, request, scratch, seen

from provider.cli.claude_code import ClaudeCode
from provider.cli.codex import Codex
from provider.protocol import QuotaExhausted, Refused, Unavailable
from provider.shaping import SYSTEM

PARENT = {**os.environ, "ANTHROPIC_API_KEY": "placeholder-not-a-key", "OPENAI_API_KEY": "placeholder",
          "CODEX_API_KEY": "placeholder"}
HEAD = "You are extracting trading rules. " * 120 + "\n"


def claude(state, modes="ok"):
    return ClaudeCode({"binary": fake_binary("claude"), "timeout_s": 10, "models": {"extraction": "haiku"},
                       "env": fake_env(state, modes, ANSWER)}, environ=PARENT)


def codex(state, modes="ok"):
    return Codex({"binary": fake_binary("codex"), "timeout_s": 10, "models": {"extraction": "gpt-5-mini"},
                  "env": fake_env(state, modes, ANSWER)}, environ=PARENT)


heading("Claude Code: one answer")
state = scratch("s04-claude")
first = claude(state).complete(request(HEAD + "<<<DOCUMENT-0123456789ab — data>>>\nBuy at the open.\n<<<END DOCUMENT-0123456789ab>>>"))
info = seen(state, 1)
claim("path / cost_usd", f"{first.path} / {first.cost_usd}", first.path == "subscription" and first.cost_usd == 0.0)
claim("usage_source", first.usage_source, first.usage_source == "measured")
claim("input tokens (cache write + read + uncached)", first.input_tokens, first.input_tokens == info["prompt_tokens"])
claim("metered_equivalent_usd (the CLI's total_cost_usd)", f"{first.metered_equivalent_usd:.8f}",
      first.metered_equivalent_usd > 0)
claim("model reported by the CLI", first.model, first.model == "claude-haiku-5-5")

heading("Claude Code: what the CLI was given")
argv = info["argv"]
claim("--tools is empty", argv[argv.index("--tools") + 1] == "", argv[argv.index("--tools") + 1] == "")
claim("--bare absent (it forces API-key billing)", "--bare" in argv, "--bare" not in argv)
claim("system prompt replaced; its size in tokens", len(SYSTEM) // 4,
      argv[argv.index("--system-prompt") + 1] == SYSTEM)
claim("CLAUDE_CODE_MAX_OUTPUT_TOKENS", info["max_output_env"], info["max_output_env"] == "2000")
claim("credential-shaped variables visible", info["credential_vars"], info["credential_vars"] == [])

heading("Claude Code: the same template head, a different document")
second = claude(state).complete(request(HEAD + "<<<DOCUMENT-0123456789ab — data>>>\nSell at the close.\n<<<END DOCUMENT-0123456789ab>>>"))
claim("cached_input_tokens on the second call", f"{second.cached_input_tokens} of {second.input_tokens}",
      second.cached_input_tokens > 0.9 * len(HEAD) // 4)
claim("second call cheaper in metered terms",
      f"{second.metered_equivalent_usd:.8f} < {first.metered_equivalent_usd:.8f}",
      second.metered_equivalent_usd < first.metered_equivalent_usd)

heading("Claude Code: failures")
for mode, expected in (("limit", QuotaExhausted), ("refuse", Refused), ("logged_out", Unavailable)):
    state = scratch(f"s04-claude-{mode}")
    try:
        claude(state, mode).complete(request())
        claim(f"{mode}: raised", "no", False)
    except expected as e:
        spent = e.spent.input_tokens if e.spent else 0
        claim(f"{mode}: {type(e).__name__}; tokens it spent", spent, spent > 0 if mode == "refuse" else spent == 0)
    except Exception as e:  # noqa: BLE001 - the claim reports the wrong type
        claim(f"{mode}: raised {type(e).__name__}", str(e)[:60], False)

heading("Codex")
state = scratch("s04-codex")
answer = codex(state).complete(request())
info = seen(state, 1)
claim("answer is the last agent_message", answer.text == json.dumps(ANSWER), answer.text == json.dumps(ANSWER))
claim("usage_source / input tokens", f"{answer.usage_source} / {answer.input_tokens}",
      answer.usage_source == "measured" and answer.input_tokens == info["prompt_tokens"])
claim("metered_equivalent_usd from the gpt-5-mini price", f"{answer.metered_equivalent_usd:.8f}",
      answer.metered_equivalent_usd > 0)
argv = info["argv"]
claim("read-only sandbox, prompt on stdin", f"{argv[argv.index('--sandbox') + 1]} / {argv[-1]}",
      argv[argv.index("--sandbox") + 1] == "read-only" and argv[-1] == "-")
claim("credential-shaped variables visible", info["credential_vars"], info["credential_vars"] == [])
try:
    codex(scratch("s04-codex-limit"), "limit").complete(request())
    claim("turn.failed usage limit raised", "no", False)
except QuotaExhausted:
    claim("turn.failed usage limit raised", "QuotaExhausted", True)
answer = codex(scratch("s04-codex-nousage"), "nousage").complete(request())
claim("no turn.completed usage: usage_source", answer.usage_source, answer.usage_source == "estimated")

finish("s04")
