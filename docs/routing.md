# Routing

A route is a declared list of steps for one caller `model_id`, cheapest capable first:

```toml
[routes]
extraction = ["claude_code:haiku", "codex:gpt-5-mini", "claude_code:sonnet"]
```

Each step is `adapter:model`. The adapter is one of a fixed set (`claude_code`,
`codex`, `command.<name>`, `keys.<vendor>`); a route never names a module path, so
nothing is imported by name at run time, the same rule the translator's own config
follows.

A `model_id` with no route is refused. `"*"` matches any `model_id`, but only if a
config declares it: there is no implicit fallback route (`sim/s01`).

## When the router moves on

| What happened | Retry on the same step? | Escalate? | `sim/s05` |
| --- | --- | --- | --- |
| Not JSON, breaks the schema, or hit the output ceiling | Yes, up to `max_schema_retries` (default 1), with the reason appended | Yes, once retries are spent | 2 calls then answered; 2 failures then escalated |
| Adapter unavailable: missing binary, logged out, timed out, vendor down | No | Yes | — |
| Subscription limit reached (vendor message) | No | Yes | 1 call on the limited step, then the next |
| Subscription window spent (local quota count) | No, and not spawned | Yes | 2 calls allowed, the 3rd skipped without a spawn |
| The model refused | No | **No**, raised to the caller | 1 call; the next step never started |
| The account has no money left | No | **No**, raised to the caller | `sim/s06` |
| Any other failure | No | Yes | — |

**Escalation is on a reason, never on suspicion.** A small model whose answer
validates is the answer. Asking a larger model as well, because an answer looked
short, would double the cost to settle a doubt nobody measured.

**A refusal is not escalated.** Asking a different model the same thing until one
agrees is not a policy this module implements; the caller sees the refusal.

**An exhausted budget is not escalated.** The next metered step would spend from an
account the caller did not choose.

## Retries live here, not in adapters

The translator's rule is that a provider does not retry, and every adapter keeps it:
one call, one answer. The router is a policy layer above them, and its retries are
bounded, earned by a specific failure and each one a line in the ledger. That keeps the
translator's rule where it matters (no adapter hides a retry) while allowing the one
retry the brief asks for (§5.6: on a parse or schema failure, with a tightened
instruction, capped).

The retry appends the rejection after everything that was sent the first time, so the
first request is a prefix of the retry.

## What a request reports

The sum of every attempt. If a cheap step spent 251 input tokens on two rejected
answers and the next step answered, the answer reports all of it (355 in `sim/s05`),
because the gateway enforces budgets on these numbers and tokens spent on a rejected
answer were still spent.

## The metered gate

A route that names a metered step (any `keys.*` adapter, or a `command.*` tool declared
`path = "metered"`) **does not build** unless the environment sets
`AIPROVIDER_ALLOW_METERED=1`. The error names the step:

```text
routes name metered steps (extraction -> command.paid:fake-paid) but AIPROVIDER_ALLOW_METERED
is not set. A metered path is only reachable when the environment asks for it.
```

The switch is read from the environment only; a config file that sets it is refused,
so a file copied from a post-launch deployment cannot carry the permission to spend.

With the switch set, every time a metered step is about to be called the router logs a
`WARNING`:

```text
METERED PATH TAKEN: keys.anthropic:claude-haiku-5-5 is answering triage (sha256:9658e24e9d95)
and will be billed per token
```

With no logging configured, Python's last-resort handler prints it to stderr, so it is
visible even in a bare script (`sim/s06`, `sim/s07`). The ledger line is marked
`metered`, and the saving report flags every metered attempt.

## Quota

Subscriptions limit use per window and say so only by refusing once it is spent. The
router counts locally, from the ledger:

```toml
[quota.claude_code]
units = 200          # calls per window, from your plan
window_hours = 5
```

An adapter whose window is spent is skipped without being spawned; the ledger records
the skip, and the report shows units used and left per adapter. An adapter with no
quota table is stopped only by the vendor's own limit message, which escalates the
same way. The limit is configured, not discovered: no CLI reports its remaining quota
in its output.
