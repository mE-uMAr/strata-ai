"""Step 9: the Command Code adapter, against the fake CLI in the shape captured live.

Measures:

- an answer read from the NDJSON stream's result line, with ``inputTokens`` taken as
  the total and ``cacheReadTokens`` as part of it, and ``cost_usd`` zero on the
  subscription path;
- the flags that keep the call contained: one turn, no session, no skills or mods, no
  auto-update, the prompt on stdin, and no credential-shaped variable in its environment;
- a model that reaches for a tool hits the turn cap (exit 8), which is `Malformed` and
  earns one retry through the router;
- a usage limit is `QuotaExhausted`, a logged-out CLI `Unavailable`;
- ``path = "metered"`` puts the adapter behind AIPROVIDER_ALLOW_METERED like any key;
- the model catalog: 94 models, which of them see images, which are free;
- a route through Command Code from TOML, with its models named by full id.
"""

from __future__ import annotations

import json
import os

from common import ANSWER, claim, fake_binary, fake_env, finish, heading, request, scratch, seen

from provider import ConfigError, assemble, load
from provider.cli.command_code import FREE_MODELS, MODELS, VISION_MODELS, CommandCode
from provider.protocol import Malformed, QuotaExhausted, Unavailable

PARENT = {**os.environ, "ANTHROPIC_API_KEY": "placeholder", "OPENAI_API_KEY": "placeholder"}


def adapter(state, modes="ok", **table):
    return CommandCode({"binary": fake_binary("commandcode"), "timeout_s": 10,
                        "models": {"extraction": "deepseek/deepseek-v4-flash"},
                        "env": fake_env(state, modes, ANSWER), **table}, environ=PARENT)


heading("one answer")
state = scratch("s09")
answer = adapter(state).complete(request())
info = seen(state, 1)
claim("answer is the result line's finalText", answer.text == json.dumps(ANSWER), answer.text == json.dumps(ANSWER))
claim("path / cost_usd / quota units", f"{answer.path} / {answer.cost_usd} / {answer.quota_units}",
      (answer.path, answer.cost_usd, answer.quota_units) == ("subscription", 0.0, 1.0))
claim("input tokens (total) / cached part / source",
      f"{answer.input_tokens} / {answer.cached_input_tokens} / {answer.usage_source}",
      answer.input_tokens == 14300 + info["prompt_tokens"] and answer.cached_input_tokens == 5248
      and answer.usage_source == "measured")

heading("what the CLI was given")
argv = info["argv"]
claim("--max-turns", argv[argv.index("--max-turns") + 1], argv[argv.index("--max-turns") + 1] == "1")
flags = ["--no-session", "--no-skills", "--safe-mode", "--skip-onboarding", "--no-auto-update", "-t", "-p"]
claim("containment flags present", [f for f in flags if f not in argv] or "all", all(f in argv for f in flags))
claim("model by full id", argv[argv.index("-m") + 1], argv[argv.index("-m") + 1] == "deepseek/deepseek-v4-flash")
claim("prompt on stdin ends with the output contract", "JSON Schema" in info["prompt_tail"],
      "JSON Schema" in info["prompt_tail"])
claim("credential-shaped variables visible", info["credential_vars"], info["credential_vars"] == [])

heading("failures")
for mode, expected in (("toolcall", Malformed), ("limit", QuotaExhausted), ("logged_out", Unavailable)):
    try:
        adapter(scratch(f"s09-{mode}"), mode).complete(request())
        claim(f"{mode}: raised", "no", False)
    except expected as e:
        claim(f"{mode}: {type(e).__name__}", str(e)[:60], True)
    except Exception as e:  # noqa: BLE001 - reported as a failed claim
        claim(f"{mode}: raised {type(e).__name__}", str(e)[:60], False)

heading("the catalog")
claim("models listed / that see images / free", f"{len(MODELS)} / {len(VISION_MODELS)} / {len(FREE_MODELS)}",
      len(MODELS) == 94 and len(FREE_MODELS) == 3)
claim("the model verified live on an image sees images", "deepseek/deepseek-v4.1-flash" in VISION_MODELS,
      "deepseek/deepseek-v4.1-flash" in VISION_MODELS)
claim("the default text model does not", "deepseek/deepseek-v4-flash" in VISION_MODELS,
      "deepseek/deepseek-v4-flash" not in VISION_MODELS)

heading("a route through Command Code, from TOML")
root = scratch("s09-route")
env = fake_env(root / "cc", "toolcall,ok", ANSWER)
inline = ", ".join(f"{k} = {json.dumps(v)}" for k, v in env.items())
config = root / "route.toml"
config.write_text(f"""
[routes]
extraction = ["command_code:deepseek/deepseek-v4-flash"]

[command_code]
binary = {json.dumps(fake_binary("commandcode"))}
env = {{ {inline} }}
""", encoding="utf-8")
assembly = assemble(load({}, config_path=config))
answer = assembly.provider.complete(request())
claim("tool reach, then a retry that answered: attempts", answer.attempts, answer.attempts == 2)
claim("ledger outcomes", [e.outcome for e in assembly.ledger.entries],
      [e.outcome for e in assembly.ledger.entries] == ["schema", "ok"])
retry = seen(root / "cc", 2)["prompt_tail"]
claim("the retry says there is nothing to read", "nothing to read" in retry, "nothing to read" in retry)
config.write_text(config.read_text().replace("[command_code]\n", '[command_code]\npath = "metered"\n'),
                  encoding="utf-8")
try:
    assemble(load({}, config_path=config))
    claim("path = metered without AIPROVIDER_ALLOW_METERED", "built", False)
except ConfigError:
    claim("path = metered without AIPROVIDER_ALLOW_METERED", "ConfigError", True)

finish("s09")
