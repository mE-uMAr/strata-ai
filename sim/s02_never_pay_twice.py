"""Step 2: a repeated request costs nothing, and the cache speaks the replay format.

Measures:

- a second identical request is served without reaching the provider behind the cache,
  and reports zero tokens and zero cost;
- the file the cache wrote is replayed by the translator's own `ReplayProvider` with
  no conversion;
- an answer that fails the schema is not cached, so it cannot be replayed for free;
- the hit rate over a workload with repeats, and the hit rate on the repeats alone;
- through the translator's real gateway (its own cache switched off, so only this one
  is in play), the second call of a triage reaches no model and is charged nothing.
"""

from __future__ import annotations

import json

from common import ANSWER, TRANSLATOR, TRIAGE_ANSWER, Scripted, claim, finish, heading, request, scratch

from provider.accounting import Ledger
from provider.cache import ReplayCache
from translator.extraction.models import TriageReport
from translator.gateway.gateway import ModelGateway
from translator.gateway.prompts import PromptLibrary
from translator.gateway.providers import ReplayProvider

heading("the same request twice")
root = scratch("s02")
inner = Scripted()
ledger = Ledger()
cache = ReplayCache(root / "cache", inner, ledger)
req = request()
first = cache.complete(req)
second = cache.complete(req)
claim("provider calls after two identical requests", inner.calls, inner.calls == 1)
claim("first answer: input tokens (estimated by the stub)", first.input_tokens, first.input_tokens > 0)
claim("second answer: input + output tokens", second.input_tokens + second.output_tokens,
      second.input_tokens + second.output_tokens == 0)
claim("second answer: cost_usd", second.cost_usd, second.cost_usd == 0.0)
claim("second answer: path", second.path, second.path == "cache")
claim("second answer text identical", second.text == first.text, second.text == first.text)
claim("ledger outcome for the second request", ledger.entries[-1].outcome, ledger.entries[-1].outcome == "hit")

heading("the translator's ReplayProvider reads what the cache wrote")
written = cache.path_for(req)
print(f"  file: <cache>/{written.name}")
print(f"  keys: {sorted(json.loads(written.read_text(encoding='utf-8')))}")
replayed = ReplayProvider(root=root / "cache").complete(req)
claim("ReplayProvider text equals the cached answer", replayed.text == first.text, replayed.text == first.text)
claim("ReplayProvider input_tokens equals the original call", replayed.input_tokens,
      replayed.input_tokens == first.input_tokens)

heading("an answer that fails the schema is not cached")
bad = Scripted(lambda r: "Sure! Here are the rules: " + json.dumps(ANSWER))
bad_cache = ReplayCache(root / "bad", bad)
bad_req = request("A different prompt")
bad_cache.complete(bad_req)
bad_cache.complete(bad_req)
claim("provider calls for two identical invalid answers", bad.calls, bad.calls == 2)
claim("writes refused", bad_cache.stats.refused_writes, bad_cache.stats.refused_writes == 2)
claim("entry on disk", bad_cache.path_for(bad_req).exists(), not bad_cache.path_for(bad_req).exists())

heading("a workload: 10 distinct requests, each asked 3 times")
work = Scripted()
work_cache = ReplayCache(root / "work", work)
for round_ in range(3):
    for i in range(10):
        work_cache.complete(request(f"document {i}"))
stats = work_cache.stats
repeats = stats.lookups - 10
claim("lookups / hits / provider calls", f"{stats.lookups} / {stats.hits} / {work.calls}", work.calls == 10)
claim("hit rate over the workload", f"{stats.hit_rate:.1%}", abs(stats.hit_rate - 20 / 30) < 1e-9)
claim("hit rate on repeated requests only", f"{stats.hits / repeats:.1%}", stats.hits == repeats)

heading("through the translator's gateway, with its own cache off")
triage = Scripted(lambda r: json.dumps(TRIAGE_ANSWER))
gateway = ModelGateway(prompts=PromptLibrary(TRANSLATOR / "prompts"),
                       provider=ReplayCache(root / "gateway", triage), cache=None)
document = "Buy EURUSD on the hourly when the 9 EMA crosses above the 21 EMA. Stop 20 pips, target 40."
results = [gateway.call(prompt="triage", variables={"document": document}, schema=TriageReport,
                        tenant="sim", pass_name="triage") for _ in range(2)]
claim("model calls for two identical triage calls", triage.calls, triage.calls == 1)
claim("second call record: input tokens", results[1].record.usage.input_tokens,
      results[1].record.usage.input_tokens == 0)
claim("second call record: cost_usd", results[1].record.usage.cost_usd, results[1].record.usage.cost_usd == 0.0)
claim("second call validated to the same decision", results[1].value.decision,
      results[1].value == results[0].value)

finish("s02")
