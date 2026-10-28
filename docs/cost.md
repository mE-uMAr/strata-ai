# Cutting token cost

The brief makes eight cost requirements part of the definition of done (§5). Each is
listed here with how it is met and the number a simulation printed. Token counts in the
simulations come from fakes that count `len(text) // 4`; the arithmetic and the request
shapes are real, the absolute token counts are not a tokenizer's.

## 1. Cache before anything else

`ReplayCache` sits outside the router, keyed on the gateway's `input_hash`. A hit reaches
no adapter.

- Two identical requests: **1** provider call; the second reported 0 tokens and $0
  (`sim/s02`).
- 10 distinct requests asked 3 times each: **10** provider calls for 30 lookups; hit rate
  66.7% over the workload, **100%** on the repeats (`sim/s02`).
- A two-pass triage job through the translator's gateway: 6 of 12 requests from cache,
  100% of repeats (`sim/s07`).
- The report flags any repeated request that reached a model as a bug: with the cache
  off, the same request twice printed `BUG: 1 repeated request(s) missed the cache`
  (`sim/s07`).
- Only answers that satisfy the schema are cached, so a failure is never replayed free:
  two invalid answers → 2 provider calls, 0 writes (`sim/s02`).

## 2. Cap output hard, and ask for JSON only

- The API path sends `max_tokens` = the caller's `max_output_tokens` (2000 in
  `sim/s06`); a response cut off there is `Malformed`, earns a retry, and carries what it
  spent.
- Claude Code gets `CLAUDE_CODE_MAX_OUTPUT_TOKENS` set per call (`sim/s04`). Codex and
  generic CLIs are checked after the call against measured output tokens; see
  [open issues](open-issues.md).
- Every path appends a contract: one JSON value, no prose, no fences, no restated task.
  The live Claude Code call answered `{"sum":5}` in **9** output tokens
  (`sim/live_claude_code.py`).
- Prose around JSON is a failure, not something to cut out and accept, so a prompt that
  keeps producing it shows up as retries in the ledger rather than being hidden.

## 3. Use provider prompt caching; order stable-first

- The caller's prompt is sent first and unchanged; additions go after it. On all five
  translator templates the rendered prompt is an exact prefix of what is sent, and the
  head before the first data fence holds no document text (`sim/s08`).
- **API path:** the contract and schema go in the system prompt, and one cache
  breakpoint sits on the template's head, so system + schema + instructions are cached
  and the per-call document is never written to cache. Second call of the triage
  template with a different document: **1021 of 1074** input tokens read from cache,
  **$0.0000575 vs $0.000175** (67% less) (`sim/s06`).
- **Claude Code:** the CLI caches on its own; the adapter reads
  `cache_read_input_tokens`. Same template head, new document: **1029 of 1135** input
  tokens cached in the fake's emulation (`sim/s04`).
- `cached_input_tokens` is on every `Answer`, in the ledger, and summed in the report.

## 4. Never resend unchanged context

The protocol hands this module the full rendered prompt every time, so the first line
of defence is the cache: the same bundle in the same request is never sent twice
(§1 above). Below that, prompt caching makes the stable part a reference rather than a
re-billed copy (§3 above), and a retry appends to the previous request instead of
rewriting it, so the previous request is a cacheable prefix.

What this does not reach: the same document under two *different* templates. Templates
put instructions before the document, so the shared document is not a shared prefix.
See [open issues](open-issues.md).

## 5. Cheapest capable model first

Routes are ordered by the deployment, cheapest first, and the router escalates only on
a reason. In `sim/s05` the cheap step answered and the next step was **never started**
(0 calls); in `sim/s07`, 5 of 6 first-pass answers came from the cheapest step or the
next subscription step, and 1 from the metered key after both subscriptions reported
their limits.

## 6. Retries are earned

One retry per step by default, only on a parse, schema or ceiling failure, carrying the
reason. In `sim/s05`: prose then valid → **2** calls on the same step, no escalation;
two failures → escalated after **2** calls. A refusal → **1** call, raised, next step
never started. A rate limit → **1** request to the vendor (the SDK's own retries are
off; `sim/s06`).

## 7. Prefer the flat-rate path while it exists

Subscription steps go first in every shipped route, and a metered step cannot even be
configured without `AIPROVIDER_ALLOW_METERED` (`sim/s05`, `sim/s06`). On the CLI path:

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and every other credential-shaped variable are
  removed from the child's environment, because a CLI that finds one bills per token
  silently (`sim/s03`, `sim/s04`).
- Claude Code is **not** run with `--bare`: that mode authenticates only with an API key
  and never reads the subscription login.
- Claude Code runs with no tools (`--tools ""`), no MCP servers and no user hooks, and
  with a 48-token system prompt in place of its own (`sim/s04`, `sim/s08`). The live
  call still processed **952** input tokens for a request of roughly 120, so about 800
  tokens per call is overhead the CLI adds that these flags do not remove (the user's
  global memory file and environment context). It costs quota, not money.

## 8. Report the saving

`python -m provider.report <ledger>`; see the README for a full sample. From
`sim/s07`: 12 requests, 6 from cache, 5 by subscription, 1 metered; **$0.000175 spent**
against **$0.002791** for the same work all metered and uncached; saving **$0.002615
(94%)**. The gateway's own call records sum to the same $0.00017532 as the ledger, so
the budget the gateway enforces and the report agree.

## Schemas, compactly

Translator templates name their fields in prose but do not include the schema, so a
path without native structured output must be shown it. It is sent without whitespace
and without Pydantic's `title` keys, keeping descriptions. Across the five templates:
**20,663 → 11,390** estimated tokens (45% fewer); `StrategyIR` alone 17,363 → 9,501
(`sim/s08`).
