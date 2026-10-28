"""Step 6: the metered path holds the same protocol, through the real SDK.

The Anthropic adapter runs against a fake Messages API on 127.0.0.1, so what is
checked is the request the official SDK actually builds. Measures:

- an answer with measured usage and ``cost_usd`` from the price table;
- the request: ``max_tokens`` equal to the caller's ceiling, no sampling parameters,
  the contract and schema in the system prompt, one cache breakpoint on the template's
  head and none on the document;
- a second call of the same template with a different document reads the head from the
  prompt cache, reports ``cached_input_tokens``, and costs less;
- each vendor error maps to its reason, and refusals and truncations carry their spend;
- with no key, the adapter is unavailable and no request is made;
- through the translator's gateway, a route that falls from a (fake) subscription CLI to
  this key produces a call record the gateway cannot tell apart from any other, except
  by its cost, and only with AIPROVIDER_ALLOW_METERED set.
"""

from __future__ import annotations

import json

from common import TRANSLATOR, TRIAGE_ANSWER, claim, fake_binary, fake_env, finish, heading, scratch

from fake_anthropic import FakeAnthropic
from provider import ConfigError, assemble, load
from provider.keys.anthropic import AnthropicKeys
from provider.protocol import BudgetSpent, Malformed, QuotaExhausted, Refused, Unavailable
from translator.extraction.models import TriageReport
from translator.gateway.gateway import ModelGateway
from translator.gateway.prompts import PromptLibrary
from translator.gateway.providers import Request

PLACEHOLDER = "placeholder-not-a-real-key"
server = FakeAnthropic(json.dumps(TRIAGE_ANSWER))
library = PromptLibrary(TRANSLATOR / "prompts")
template = library.get("triage")
schema = TriageReport.model_json_schema()


def triage_request(document: str, max_output_tokens: int = 2000) -> Request:
    rendered = library.render(template, {"document": document}, nonce=template.template_hash[7:19])
    return Request(prompt=rendered, prompt_name="triage", model_id="extraction", temperature=0.0,
                   max_output_tokens=max_output_tokens, seed=None, schema=schema,
                   input_hash="sha256:" + "0" * 64)


adapter = AnthropicKeys({"base_url": server.url, "models": {"extraction": "claude-haiku-5-5"}}, key=PLACEHOLDER)

heading("one call")
first = adapter.complete(triage_request("Buy EURUSD on the hourly when the 9 EMA crosses the 21 EMA."))
body = server.requests[-1]
claim("path / usage_source", f"{first.path} / {first.usage_source}",
      first.path == "metered" and first.usage_source == "measured")
claim("cost_usd (haiku-5-5, cache write included)", f"{first.cost_usd:.8f}", first.cost_usd > 0)
claim("max_tokens sent = caller's ceiling", body["max_tokens"], body["max_tokens"] == 2000)
claim("sampling parameters sent", [k for k in ("temperature", "top_p", "top_k") if k in body],
      not any(k in body for k in ("temperature", "top_p", "top_k")))
claim("schema in the system prompt", "JSON Schema" in body["system"][0]["text"], "JSON Schema" in body["system"][0]["text"])
marks = [("cache_control" in b) for b in body["messages"][0]["content"]]
claim("cache breakpoints per user block (head, document)", marks, marks == [True, False])
claim("document block starts at the data fence", body["messages"][0]["content"][1]["text"][:12],
      body["messages"][0]["content"][1]["text"].startswith("<<<DOCUMENT-"))
claim("effort sent", body.get("output_config"), body.get("output_config") == {"effort": "low"})
claim("key sent as a header, not in the body", PLACEHOLDER not in json.dumps(body),
      PLACEHOLDER not in json.dumps(body) and any(PLACEHOLDER in v for v in server.headers[-1].values()))

heading("the same template, another document")
second = adapter.complete(triage_request("Sell XAUUSD at the London open if the Asian range is broken."))
claim("cached_input_tokens / input_tokens", f"{second.cached_input_tokens} / {second.input_tokens}",
      second.cached_input_tokens > 0.5 * second.input_tokens)
claim("cost: second vs first", f"{second.cost_usd:.8f} vs {first.cost_usd:.8f}", second.cost_usd < first.cost_usd)

heading("vendor errors")
for mode, expected in (("429", QuotaExhausted), ("529", Unavailable), ("401", Unavailable), ("credit", BudgetSpent),
                       ("refusal", Refused), ("max_tokens", Malformed)):
    server.script = [mode]
    before = len(server.requests)
    try:
        adapter.complete(triage_request("Any document."))
        claim(f"{mode}: raised", "no", False)
    except expected as e:
        spent = e.spent.input_tokens if e.spent else 0
        carries = spent > 0 if mode in ("refusal", "max_tokens") else spent == 0
        claim(f"{mode}: {type(e).__name__}; one request; tokens spent", f"{len(server.requests) - before}; {spent}",
              carries and len(server.requests) - before == 1)
    except Exception as e:  # noqa: BLE001 - reported as a failed claim
        claim(f"{mode}: raised {type(e).__name__}", str(e)[:50], False)
server.script = ["ok"]

heading("no key")
before = len(server.requests)
try:
    AnthropicKeys({"base_url": server.url, "models": {"extraction": "claude-haiku-5-5"}}, key=None).complete(
        triage_request("Any document."))
    claim("no key: raised", "no", False)
except Unavailable:
    claim("no key: Unavailable; requests made", len(server.requests) - before, len(server.requests) == before)

heading("through the translator's gateway: a subscription CLI at its limit, then the key")
root = scratch("s06")
config = root / "route.toml"
config.write_text(f"""
[routes]
extraction = ["claude_code:haiku", "keys.anthropic:claude-haiku-5-5"]

[claude_code]
binary = {json.dumps(fake_binary("claude"))}
env = {{ {", ".join(f"{k} = {json.dumps(v)}" for k, v in fake_env(root / "claude", "limit").items())} }}

[keys.anthropic]
base_url = "{server.url}"
""", encoding="utf-8")
try:
    assemble(load({"AIPROVIDER_ANTHROPIC_API_KEY": PLACEHOLDER}, config_path=config))
    claim("route with a key step, metered not allowed", "built", False)
except ConfigError:
    claim("route with a key step, metered not allowed", "ConfigError", True)
assembly = assemble(load({"AIPROVIDER_ANTHROPIC_API_KEY": PLACEHOLDER, "AIPROVIDER_ALLOW_METERED": "1"},
                         config_path=config))
gateway = ModelGateway(prompts=library, provider=assembly.provider, cache=None)
gateway.model = gateway.model.model_copy(update={"provider": "aiprovider", "model_id": "extraction"})
result = gateway.call(prompt="triage", variables={"document": "Buy EURUSD when RSI(14) < 30; stop 20 pips."},
                      schema=TriageReport, tenant="sim", pass_name="triage")
record = result.record
claim("gateway outcome / decision", f"{record.outcome} / {result.value.decision}", record.outcome == "ok")
claim("gateway record cost_usd (metered, true)", f"{record.usage.cost_usd:.8f}", record.usage.cost_usd > 0)
entries = [(e.adapter, e.outcome) for e in assembly.ledger.entries]
claim("ledger: the subscription limit, then the key", entries,
      entries == [("claude_code", "limit"), ("keys.anthropic", "ok")])

server.close()
finish("s06")
