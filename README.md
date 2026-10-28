# AIProvider

Strata's model-provider module. The translator defines one protocol,
`complete(Request) -> Completion`, and attaches whatever the platform hands it
([translator ADR 0010](../translator/docs/adr/0010-platform-supplies-the-model.md)).
This repository is what the platform hands it. Every vendor name, API key, CLI binary
path and price table in Strata lives here and nowhere else.

```text
translator gateway ──complete(Request)──▶ ReplayCache ──miss──▶ Router ──▶ claude_code    (subscription)
   (prompts, hash,                         root/<hash>.json      │  ladder    command_code   (subscription)
    budgets, schema)                       never pays twice      │  retries   codex          (subscription)
                                                                  │  ledger    command.<x>    (subscription, by config)
                                                                  │            keys.anthropic (metered, opt-in only)
                                                                  ▼
                                                         ledger.jsonl ──▶ saving report
```

**Before launch** every route runs through flat-rate subscription CLIs (Claude Code,
Command Code and its 94 models, Codex, and any other CLI declared in config). **After launch** a route can add a
metered API key; the caller cannot tell which answered except by the cost. Nothing
reaches a model unless configuration says so: with no configuration this module
assembles into the translator's own `RefusingProvider`.

**Requests can carry images.** A `VisionRequest` (built with `with_images`) is routed
only to steps whose model can see images; the others are skipped without being started.
See [docs/vision.md](docs/vision.md).

## Quickstart

```sh
python -m venv .venv
.venv/Scripts/pip install -e ".[keys,keychain]"   # POSIX: .venv/bin/pip; the extras are optional
python sim/run_all.py                              # every simulation, offline, no keys
```

The `translator` repository must sit next to this one (`../translator`): the protocol
is imported from it, not copied, and `pyproject.toml` references it by path.

## Attaching it to the translator

```python
from provider import from_environment
from translator.gateway.config import GatewayConfig, build
from translator.gateway.models import ModelSpec

provider = from_environment()          # reads AIPROVIDER_CONFIG, .env, the keychain
gateway = build(
    GatewayConfig(prompts=Path("../translator/prompts"),
                  model=ModelSpec(provider="aiprovider", model_id="extraction",
                                  temperature=0, max_output_tokens=16000, seed=None)),
    provider=provider,
)
```

`model_id` selects a route in the config. A `model_id` with no route is refused.

## Configuration

| Where | What |
| --- | --- |
| `AIPROVIDER_CONFIG` → a TOML file | Routes, adapter settings, quota windows, price overrides, generic CLI tools. See [`aiprovider.example.toml`](aiprovider.example.toml). Refused on load if it holds anything shaped like a credential. |
| `AIPROVIDER_ALLOW_METERED=1` | The only switch that lets a route name a metered step. Read from the environment only, so a copied config file cannot carry it. |
| `AIPROVIDER_ANTHROPIC_API_KEY`, or the OS keychain (service `strata-aiprovider`) | The metered key. Deliberately not `ANTHROPIC_API_KEY`, which the Claude Code CLI would also read and bill against. |
| `AIPROVIDER_CACHE_DIR`, `AIPROVIDER_LEDGER` | Override the config's cache directory and ledger file. |

[`.env.example`](.env.example) lists the names with empty values; `.env` is gitignored.

## The saving report

```sh
python -m provider.report .cache/aiprovider/ledger.jsonl
```

prints how many requests were served from cache (and the hit rate on repeats, flagged
as a bug below 100%), by a subscription and by a metered path, how many escalated, the
money spent, what the same work would have cost all metered and uncached, and quota used
per adapter. A sample from [`sim/s07_saving_report.py`](sim/s07_saving_report.py):

```text
Saving report
  requests answered             12
    from the cache              6  (50.0% of requests)
    repeated requests           6, served from cache: 6 (100.0%)
    by a subscription path      5
    by a metered path           1
    after an escalation         2
  model attempts                11  (not answered: limit 2, schema 3)
  money spent                   $0.000175
  the same work, all metered    $0.002791  ($0.001308 for calls made + $0.001483 avoided by the cache)
  saving                        $0.002615
  ! metered path taken on 1 attempt(s)
```

## Simulations

There are no unit tests, as everywhere in Strata. Behaviour is verified by simulations
under [`sim/`](sim/) that run the real code end to end and print what they measured,
each value next to the claim it supports. Every number in [`docs/`](docs/) comes from
one of them.

| Simulation | Shows |
| --- | --- |
| `s01_nothing_by_accident` | No config → `RefusingProvider`; a refusal logged by the real gateway at zero cost; credential-bearing configs refused |
| `s02_never_pay_twice` | A repeated request reaches no model; the cache is replayed by the translator's own `ReplayProvider` |
| `s03_generic_cli` | A CLI declared in TOML answers; isolation, credential scrub, no hangs, hard timeout, error mapping |
| `s04_claude_code_and_codex` | Both CLIs' real output shapes, measured usage, prompt-cache reads, flags that keep calls cheap |
| `s05_routing` | Cheapest first, earned retries, reasoned escalation, quota skips, refusals not retried, metered gate and loud log |
| `s06_metered_keys` | The Anthropic adapter through the official SDK against a loopback fake: request shape, prompt caching, error mapping |
| `s07_saving_report` | A two-pass job through the translator's gateway, and the report that accounts for it |
| `s08_prompt_shape` | The prompt shape on the translator's real templates; the example config |
| `s09_command_code` | Command Code in its captured output shape; turn-cap retries; the 94-model catalog |
| `s10_vision` | Images in the request's identity; routing around blind steps; image delivery per adapter |
| `live_claude_code` | Real subscription calls, text and an image (`--live` only; not in `run_all`) |
| `live_command_code` | Real Command Code calls, text and an image (`--live` only; not in `run_all`) |

They need no network, no subscription and no key: CLIs are played by
[`sim/fake_cli.py`](sim/fake_cli.py) and the API by
[`sim/fake_anthropic.py`](sim/fake_anthropic.py) on 127.0.0.1.

## Documents

| Document | Covers |
| --- | --- |
| [docs/design.md](docs/design.md) | The seam, the stack, the modules and the shape of an answer |
| [docs/routing.md](docs/routing.md) | Routes, retries, escalation, quota, and the metered gate |
| [docs/cost.md](docs/cost.md) | Each token-cost requirement of the brief, how it is met, and the number that shows it |
| [docs/vision.md](docs/vision.md) | Requests with images, which models see them, and how each tool receives them |
| [docs/decisions.md](docs/decisions.md) | Choices where an alternative was weighed, and why it lost |
| [docs/open-issues.md](docs/open-issues.md) | What is not done, not verified, or belongs to another repository |

There are no version numbers. Identity is the commit ([translator ADR 0009](../translator/docs/adr/0009-single-version.md)).
