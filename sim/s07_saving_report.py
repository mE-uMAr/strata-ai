"""Step 7: a whole run, and the report that says what it cost and what it saved.

A triage job over six documents goes through the translator's real gateway (its own
cache off, so this module's cache is the one being measured), twice: the second pass is
the same job re-run, which is exactly the repeated work the cache must absorb.

The route is a cheap generic CLI, then Claude Code, then the metered key (allowed for
this run). The fakes are scripted so the first pass meets every case once: a clean
answer, a prose answer that earns a retry, a schema failure that escalates, and a usage
limit on both subscriptions that ends on the metered path.

Measures:

- every number in the saving report (§5.8 of the brief): requests, cache hits and the
  hit rate on repeated requests, answers by subscription and by metered path,
  escalations, money spent, what the same work would have cost metered, the saving,
  and quota used per adapter;
- the gateway's own call records add up to the same money as the ledger, so the budget
  the gateway enforces and the report agree;
- the report rebuilt from the ledger file in a fresh process, through
  ``python -m provider.report``, prints the same text.
"""

from __future__ import annotations

import json
import subprocess
import sys

from common import ROOT, TRANSLATOR, TRIAGE_ANSWER, claim, fake_binary, finish, heading, request, scratch

from fake_anthropic import FakeAnthropic
from provider import assemble, load
from provider.accounting import Ledger, report
from translator.extraction.models import TriageReport
from translator.gateway.gateway import ModelGateway
from translator.gateway.prompts import PromptLibrary

root = scratch("s07")
server = FakeAnthropic(json.dumps(TRIAGE_ANSWER))
answer = json.dumps(TRIAGE_ANSWER)


def env(name: str, modes: str) -> str:
    values = {"FAKE_CLI_STATE": str(root / name), "FAKE_CLI_MODES": modes, "FAKE_CLI_ANSWER": answer}
    return "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in values.items()) + " }"


config = root / "aiprovider.toml"
config.write_text(f"""
cache_dir = "cache"
ledger = "ledger.jsonl"
max_schema_retries = 1

[routes]
extraction = ["command.small:fake-small", "claude_code:haiku", "keys.anthropic:claude-haiku-5-5"]

[command.small]
binary = {json.dumps(fake_binary("generic"))}
answer = "response"
input_tokens = "stats.models.*.tokens.prompt"
output_tokens = "stats.models.*.tokens.candidates"
price_as = "claude-haiku-5-5"
env = {env("small", "ok,prose,ok,ok,invalid,invalid,ok,limit")}

[claude_code]
binary = {json.dumps(fake_binary("claude"))}
env = {env("claude", "ok,limit")}

[keys.anthropic]
base_url = "{server.url}"

[quota.claude_code]
units = 50
window_hours = 5
""", encoding="utf-8")

assembly = assemble(load({"AIPROVIDER_ANTHROPIC_API_KEY": "placeholder-not-a-real-key", "AIPROVIDER_ALLOW_METERED": "1"},
                         config_path=config))
gateway = ModelGateway(prompts=PromptLibrary(TRANSLATOR / "prompts"), provider=assembly.provider, cache=None)
gateway.model = gateway.model.model_copy(update={"provider": "aiprovider", "model_id": "extraction"})
documents = [
    "Buy EURUSD on the hourly when the 9 EMA crosses above the 21 EMA. Stop 20 pips, target 40.",
    "Short GBPJPY on 4H when RSI(14) > 70 and price closes below the prior candle's low.",
    "Go long XAUUSD at the London open if the Asian range high breaks; stop at the range low.",
    "Buy SPY at the close when it is down three days in a row; sell at the next up close.",
    "Fade USDCAD moves of more than 1.5 ATR on the 15-minute chart; exit at the 20 SMA.",
    "Enter BTCUSD long on a daily close above the 50-day high; trail a 3 ATR stop.",
]

heading("two passes of the same six-document triage job")
for job in ("pass_1", "pass_2"):
    for document in documents:
        gateway.call(prompt="triage", variables={"document": document}, schema=TriageReport, tenant="sim",
                     pass_name="triage", job=job)
summary = report(assembly.ledger.entries, assembly.router.quota)
print()
print(summary.render())
print()
claim("requests answered", summary.requests, summary.requests == 12)
claim("served from the cache", summary.cache_hits, summary.cache_hits == 6)
claim("hit rate on repeated requests", f"{summary.repeated_hit_rate:.1%}", summary.repeated_hit_rate == 1.0)
claim("answered by a subscription / metered path",
      f"{summary.answered_by_subscription} / {summary.answered_by_metered}",
      (summary.answered_by_subscription, summary.answered_by_metered) == (5, 1))
claim("answered after an escalation", summary.answered_after_escalation, summary.answered_after_escalation == 2)
claim("money spent = the one metered answer", f"${summary.spent_usd:.8f}", 0 < summary.spent_usd)
claim("the same work all metered, without the cache", f"${summary.would_have_cost_usd:.8f}",
      summary.would_have_cost_usd > summary.spent_usd)
claim("saving", f"${summary.saving_usd:.8f} ({summary.saving_usd / summary.would_have_cost_usd:.0%})",
      summary.saving_usd > 0)
# Claude Code answered once and was turned away once at its usage limit. A turned-away
# call consumed nothing, so one unit is the true figure.
claim("quota used: claude_code (of 50 per 5h)", summary.quota_used.get("claude_code"),
      summary.quota_used.get("claude_code") == 1.0)
claim("no BUG flag", [f for f in summary.flags if f.startswith("BUG")], not any(f.startswith("BUG") for f in summary.flags))
claim("metered path flagged", [f for f in summary.flags if "metered" in f], any("metered" in f for f in summary.flags))

heading("the gateway and the ledger agree on money")
gateway_cost = sum(r.usage.cost_usd for r in gateway.records)
claim("gateway records: calls / cost_usd", f"{len(gateway.records)} / ${gateway_cost:.8f}",
      abs(gateway_cost - summary.spent_usd) < 1e-12)

heading("the report from the ledger file, in a fresh process")
ledger_path = root / "ledger.jsonl"
fresh = subprocess.run([sys.executable, "-m", "provider.report", str(ledger_path)], capture_output=True, text=True,
                       encoding="utf-8", cwd=ROOT / "src")
reloaded = report(Ledger(ledger_path).entries)
claim("ledger lines on disk", len(ledger_path.read_text(encoding="utf-8").splitlines()),
      len(ledger_path.read_text(encoding="utf-8").splitlines()) == len(assembly.ledger.entries))
same = fresh.stdout.strip() == reloaded.render().strip()
claim("python -m provider.report prints the same report", same, same and fresh.returncode == 0)
claim("money in the reloaded report", f"${reloaded.spent_usd:.8f}", reloaded.spent_usd == summary.spent_usd)

heading("the same request twice with the cache switched off")
nocache = (config.read_text(encoding="utf-8")
           .replace('cache_dir = "cache"\n', "")
           .replace('ledger = "ledger.jsonl"', 'ledger = "ledger-nocache.jsonl"')
           .replace(', "keys.anthropic:claude-haiku-5-5"]', "]")
           .replace('"ok,prose,ok,ok,invalid,invalid,ok,limit"', '"ok"'))
(root / "nocache.toml").write_text(nocache, encoding="utf-8")
bare = assemble(load({}, config_path=root / "nocache.toml"))
for _ in range(2):
    bare.provider.complete(request(documents[0], schema=TriageReport.model_json_schema()))
flags = report(bare.ledger.entries).flags
claim("repeated work that reached a model is flagged as a bug", [f for f in flags if f.startswith("BUG")],
      any(f.startswith("BUG") for f in flags))

server.close()
finish("s07")
