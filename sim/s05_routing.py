"""Step 5: the router's escalation rules, end to end from a TOML config.

Every step here is a fake CLI with its own call counter, so "was it called" is a
measured number, not an inference. Measures:

- the cheapest step answers when it can, and the next step is never started;
- a schema failure earns one retry on the same step, carrying the rejection reason;
- two schema failures escalate, and the escalation is on the answer and in the ledger;
- a usage limit escalates at once, with no retry;
- a step whose local quota window is spent is skipped without being spawned;
- a refusal is raised to the caller: no retry, no escalation;
- the answer after an escalation reports the tokens of every attempt, not just the last;
- a route naming a metered step does not build without AIPROVIDER_ALLOW_METERED, and
  with it, taking that step logs a WARNING and marks the ledger line.
"""

from __future__ import annotations

import json
import logging

from common import ANSWER, claim, fake_binary, finish, heading, request, scratch

from provider import ConfigError, assemble, load
from provider.protocol import ProviderError, Refused

root = scratch("s05")
captured: list[str] = []


class Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        captured.append(f"{record.levelname}: {record.getMessage()}")


logging.getLogger("provider.routing").addHandler(Capture())
logging.getLogger("provider.routing").setLevel(logging.INFO)


def fake_table(name: str, state, modes: str, extra: str = "") -> str:
    env = {"FAKE_CLI_STATE": str(state), "FAKE_CLI_MODES": modes, "FAKE_CLI_ANSWER": json.dumps(ANSWER)}
    persona = "claude" if name == "claude_code" else "generic"
    usage = "" if persona == "claude" else (
        'answer = "response"\ninput_tokens = "stats.models.*.tokens.prompt"\n'
        'output_tokens = "stats.models.*.tokens.candidates"\n')
    inline = ", ".join(f"{k} = {json.dumps(v)}" for k, v in env.items())
    return (f"\n[{name}]\nbinary = {json.dumps(fake_binary(persona))}\ntimeout_s = 10\n{usage}{extra}"
            f"env = {{ {inline} }}\n")


def config(tag: str, small: str, claude: str = "ok", *, metered: str | None = None, quota: int | None = None,
           order: tuple[str, ...] = ("command.small:fake-small", "claude_code:haiku")):
    """A route over fake CLIs: command.small → claude_code [→ command.paid, a metered tool]."""
    states = {name: root / f"{tag}-{name}" for name in ("small", "claude", "paid")}
    steps = list(order) + (["command.paid:fake-paid"] if metered else [])
    text = f"max_schema_retries = 1\n\n[routes]\nextraction = {json.dumps(steps)}\n"
    text += fake_table("command.small", states["small"], small)
    text += fake_table("claude_code", states["claude"], claude)
    if metered:
        text += fake_table("command.paid", states["paid"], metered, 'path = "metered"\nprice_as = "claude-haiku-5-5"\n')
    if quota is not None:
        text += f'\n[quota."command.small"]\nunits = {quota}\nwindow_hours = 5\n'
    path = root / f"{tag}.toml"
    path.write_text(text, encoding="utf-8")
    return path, states


def calls(state) -> int:
    return int((state / "calls").read_text()) if (state / "calls").exists() else 0


def run(tag: str, small: str, claude: str = "ok", env: dict | None = None, **kw):
    path, states = config(tag, small, claude, **kw)
    return assemble(load(env or {}, config_path=path)), states


heading("the cheapest step answers")
assembly, states = run("cheap", "ok")
answer = assembly.provider.complete(request())
claim("answered by", answer.adapter, answer.adapter == "command.small")
claim("calls: small / claude", f"{calls(states['small'])} / {calls(states['claude'])}",
      calls(states["small"]) == 1 and calls(states["claude"]) == 0)

heading("a schema failure earns one retry on the same step")
assembly, states = run("retry", "prose,ok")
answer = assembly.provider.complete(request())
outcomes = [e.outcome for e in assembly.ledger.entries]
claim("answered by / attempts", f"{answer.adapter} / {answer.attempts}",
      answer.adapter == "command.small" and answer.attempts == 2)
claim("ledger outcomes", outcomes, outcomes == ["schema", "ok"])
tail = json.loads((states["small"] / "seen-2.json").read_text())["prompt_tail"]
claim("retry prompt carries the rejection", "rejected and discarded" in tail, "rejected and discarded" in tail)
claim("claude calls", calls(states["claude"]), calls(states["claude"]) == 0)

heading("two schema failures escalate")
assembly, states = run("escalate", "prose,invalid")
answer = assembly.provider.complete(request())
claim("answered by", answer.adapter, answer.adapter == "claude_code")
claim("escalations on the answer", list(answer.escalations), len(answer.escalations) == 1)
claim("calls: small / claude", f"{calls(states['small'])} / {calls(states['claude'])}",
      calls(states["small"]) == 2 and calls(states["claude"]) == 1)
small_spent = sum(e.input_tokens for e in assembly.ledger.entries if e.adapter == "command.small")
claim("input tokens reported = every attempt's", f"{answer.input_tokens} (small spent {small_spent})",
      answer.input_tokens > small_spent > 0)

heading("a usage limit escalates at once")
assembly, states = run("limit", "limit")
answer = assembly.provider.complete(request())
claim("calls: small / claude", f"{calls(states['small'])} / {calls(states['claude'])}",
      calls(states["small"]) == 1 and calls(states["claude"]) == 1)
claim("escalation reason", answer.escalations[0].rsplit(": ", 1)[-1], answer.escalations[0].endswith(": limit"))

heading("a spent local quota window is skipped without spawning")
assembly, states = run("quota", "ok", quota=2)
for i in range(3):
    answer = assembly.provider.complete(request(f"document {i}"))
claim("calls: small / claude after 3 requests", f"{calls(states['small'])} / {calls(states['claude'])}",
      calls(states["small"]) == 2 and calls(states["claude"]) == 1)
claim("third request escalation", answer.escalations[0].rsplit(": ", 1)[-1], "local quota" in answer.escalations[0])

heading("a refusal is not retried and not escalated")
assembly, states = run("refuse", "ok", "refuse", order=("claude_code:haiku", "command.small:fake-small"))
try:
    assembly.provider.complete(request())
    claim("refusal raised", "no", False)
except Refused as e:
    claim("refusal raised; tokens it spent", e.spent.input_tokens, e.spent is not None and e.spent.input_tokens > 0)
claim("calls: claude / small", f"{calls(states['claude'])} / {calls(states['small'])}",
      calls(states["claude"]) == 1 and calls(states["small"]) == 0)

heading("the metered path")
try:
    run("metered-denied", "limit", "limit", metered="ok")
    claim("built without AIPROVIDER_ALLOW_METERED", "yes", False)
except ConfigError as e:
    claim("built without AIPROVIDER_ALLOW_METERED", "ConfigError", True)
    print(f"      {e}")
captured.clear()
assembly, states = run("metered-allowed", "limit", "limit", env={"AIPROVIDER_ALLOW_METERED": "1"}, metered="ok")
answer = assembly.provider.complete(request())
warnings = [c for c in captured if c.startswith("WARNING") and "METERED PATH TAKEN" in c]
claim("answered by / path", f"{answer.adapter} / {answer.path}", answer.path == "metered")
claim("cost_usd charged (haiku-5-5 prices)", f"{answer.cost_usd:.8f}", answer.cost_usd > 0)
claim("WARNING lines logged", len(warnings), len(warnings) == 1)
print(f"      {warnings[0] if warnings else ''}")
claim("ledger line marked metered", assembly.ledger.entries[-1].note[:7],
      assembly.ledger.entries[-1].note.startswith("metered"))
claim("escalations before it", len(answer.escalations), len(answer.escalations) == 2)

heading("every step failing")
assembly, states = run("allfail", "logged_out", "logged_out")
try:
    assembly.provider.complete(request())
    claim("raised", "no", False)
except ProviderError as e:
    claim("raised ProviderError naming each step", str(e).count("|") + 1, str(e).count("|") == 1)

finish("s05")
