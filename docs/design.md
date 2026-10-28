# Design

## The seam

The translator owns the protocol (`translator/src/translator/gateway/providers.py`):

```python
class Provider(Protocol):
    name: str
    def complete(self, request: Request) -> Completion: ...
```

This module imports `Request`, `Completion`, `Provider`, `RefusingProvider` and
`ProviderError` from there rather than copying them. A copy would drift without notice;
an import breaks loudly the day the translator changes the seam, which is the day this
module needs to know.

Nothing here changes the protocol. What this module needs to say beyond it travels in
two ways the gateway already tolerates:

- **`Answer` subclasses `Completion`.** The gateway reads `text`, `input_tokens`,
  `output_tokens`, `latency_ms` and `cost_usd` and ignores the rest. The extra fields
  are the ones the brief requires a reader to be able to see: `path` (cache,
  subscription, metered, local), `usage_source` (measured or estimated),
  `cached_input_tokens`, `quota_units`, `metered_equivalent_usd`, `attempts` and
  `escalations`.
- **Every error is a `ProviderError` subclass**, so the gateway handles it as it already
  does. The subclass is the reason (`Unavailable`, `QuotaExhausted`, `Malformed`,
  `Refused`, `BudgetSpent`), which is what the router acts on, and each carries `spent`:
  what the failed attempt consumed.

### What the numbers mean

| Field | Meaning |
| --- | --- |
| `input_tokens` | Every input token the model processed, including those served from the vendor's prompt cache |
| `cached_input_tokens` | The part of `input_tokens` the vendor served from its prompt cache |
| `cost_usd` | Marginal money. 0.0 on a subscription path *as a known value*: the fee is already paid |
| `quota_units` | The subscription's real cost: a slice of its window (one call, by default) |
| `metered_equivalent_usd` | What the same tokens would have cost on the metered API |
| `usage_source` | `measured` if the tool reported the counts; `estimated` (text length / 4) if it did not. Never 0 for "unknown" |

A cache hit reports zero tokens and zero cost, because that is what the call that just
happened consumed; the original call's cost is the hit's `avoided_usd` in the ledger.

## The stack

```text
ReplayCache            if cache_dir is set; a hit stops here
  Router               one route per caller model_id; retries, escalation, ledger, quota
    adapters           cli/claude_code, cli/codex, cli/command (any CLI by config), keys/anthropic
```

`provider.from_environment()` builds it from configuration. With no routes it returns
the translator's `RefusingProvider` unchanged, so "no model was called" stays the
default rather than an assumption (`sim/s01`).

Every adapter is also a `Provider` on its own (`complete` maps the caller's `model_id`
through the adapter's `models` table and refuses one it was not told about), so a
deployment can attach a single adapter without a router if it wants to.

## Modules

| Module | Does |
| --- | --- |
| `protocol.py` | The imported seam, `Answer`, the error reasons, the `Adapter` base |
| `config.py` | TOML for structure, environment and keychain for secrets; refuses credentials in files |
| `assemble.py` | Settings → cache → router → adapters. Imports an adapter only when a route names it |
| `cache.py` | Content-addressed on `input_hash`, in the translator's `ReplayProvider` shape |
| `routing.py` | Walks a route; earned retries; escalation on a reason; the metered gate |
| `accounting.py` | Price tables, the ledger, the quota meter, the saving report |
| `shaping.py` | What is appended to a prompt (the JSON-only contract, the compact schema, a rejection note) and where |
| `validate.py` | Schema checking for the router and the cache; strict, and describes failures from the schema's side |
| `cli/_process.py` | The rules every CLI shares: closed stdin, tree-killing timeout, empty working directory, scrubbed environment, shim guard |
| `cli/command.py` | The generic CLI adapter, driven by a `[command.<name>]` table |
| `cli/claude_code.py` | `claude -p --output-format json` on a Claude subscription |
| `cli/codex.py` | `codex exec --json` on a ChatGPT subscription |
| `keys/anthropic.py` | The Messages API through the official SDK; the metered path |
| `report.py` | `python -m provider.report <ledger>` |

No adapter imports another. `cli/_process.py`, `shaping.py`, `validate.py` and
`accounting.py` are shared machinery, not adapters.

## The CLI path

Every CLI call follows the same rules (`cli/_process.py`, measured in `sim/s03`):

- **stdin is the prompt, then closed**, or the null device. A tool that stops to ask
  a question reads end-of-file and fails: 0.15 s in `sim/s03`, against a 3 s timeout.
- **A hard timeout that kills the process tree.** On Windows the CLIs are `.cmd` shims
  that start node; killing the shim alone would leave node running and holding the
  pipes. A hanging fake was killed and the call returned in 3.3 s with a 3 s timeout.
- **An empty temporary working directory**, removed afterwards. The tool cannot read
  the repository it was launched from. `sim/s03` checks the tool saw only the schema
  file this module wrote, and that the directory is gone.
- **Credential-shaped variables are removed from the child's environment.** A CLI that
  finds `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` uses it and bills per token while still
  looking like a subscription call from outside; scrubbing them is what keeps the
  subscription path a subscription path. `sim/s03` and `sim/s04` plant keys in the
  parent's environment and check none arrives.
- **Untrusted text never goes on a `cmd.exe` shim's argv**, because `cmd.exe` re-parses
  it. An argument holding a metacharacter is refused before the process starts.
- **stderr is kept** in the error raised on failure.

## The model's output is data

The router and the cache parse an answer as JSON and check it against the request's
schema. Nothing else is done with it: no field selects an action, no text is executed,
and the rejection note fed back on a retry describes the schema rule that was broken
(`rules: breaks 'type' "array"`), never the answer's own text, so a completion cannot
write the next prompt (`validate.py`).
