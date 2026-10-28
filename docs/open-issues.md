# Open issues

What is not done, not verified, or belongs to another repository. Each says what was
observed and what would close it.

## Belongs to the translator

**Self-consistency runs share one `input_hash`.** The rule-inventory, binding and
synthesis passes call the gateway three times with the same prompt, variables and
model, and `run` is not part of the hashed request body. Any cache keyed on the hash
(the translator's own `MemoryCache`, which `build()` attaches by default, and this
module's `ReplayCache`) answers runs 2 and 3 with run 1's answer, so the agreement the
runs exist to measure is trivially 100%. This module follows the brief (a repeated
request must cost nothing) and cannot see `run`, because `Request` does not carry it.
Closing it is a translator change: put the run number (or a per-run nonce) into the
hashed request, so each run is its own cacheable request.

**The gateway records zero usage on a provider error.** When `complete` raises, the
gateway logs `Usage` of zero, even though a refusal or a truncated answer consumed
tokens. This module attaches the spend to the error (`error.spent`) and writes it to its
own ledger, so the saving report is right, but the gateway's budget does not see it.
Closing it is a translator change: read `spent` from the error when it is there.

**The translator cannot send images yet.** Its gateway builds a plain `Request` from
rendered text, so vision is reachable only by a caller that builds a `VisionRequest`
itself. Using it in extraction needs a pass that renders the page images it wants
looked at, attaches them with `with_images`, and includes them in the hash it logs
([vision](vision.md)).

## Not verified against a live tool

- **Codex.** The CLI is not installed where this was built. The adapter is written to
  `codex exec --json`'s documented event stream and checked against the fake only.
  Run a call by hand (`codex exec --json --skip-git-repo-check -m <model> -`) and
  compare the events with `sim/fake_cli.py --as codex` before routing to it.
- **The Gemini entry** in `aiprovider.example.toml` is commented out for the same
  reason: its flags and output paths are not checked against an installed version.
- **OpenAI prices** in `accounting.PRICES` were not re-verified when written; they feed
  only the metered-equivalent figure for Codex calls. Override them in `[prices]`.
- **Claude Code** was checked live (`sim/live_claude_code.py --live`), text and an inline
  image: the envelope, usage, cost and model fields read as expected, and the image was
  read correctly.
- **Command Code** was checked live (`sim/live_command_code.py --live`), text with
  `deepseek/deepseek-v4-flash` and an image with `deepseek/deepseek-v4.1-flash`. Its error
  shapes (usage limit, logged out) were not seen live; the adapter matches them by
  message, as for the other CLIs.
- **Command Code's vision list** comes from the models' own descriptions. Only
  `deepseek/deepseek-v4.1-flash` has read an image here.
- **Command Code's billing** is assumed to be a plan (`path = "subscription"`). The CLI
  reports no cost, and no prices are configured for its non-Claude models, so their
  metered-equivalent figure is 0 until `[prices]` entries are added.

## Limits of the CLI path

- **Output ceilings on Codex and generic CLIs are checked after the call**, against
  measured output tokens, because neither has a per-call flag this module could verify.
  An answer over the ceiling is rejected and its spend recorded, but the tokens were
  already generated. Where a tool has an environment variable for it, set
  `max_tokens_env` in its `[command.<name>]` table.
- **Temperature and seed** cannot be set on any CLI path, and the current API version
  rejects them too. Recorded in [decisions](decisions.md), A4.
- **Claude Code adds about 800 input tokens per call** that the flags here do not remove
  (952 processed for a request of about 120 in the live check): the user's global memory
  file and environment context. It costs quota, not money. `--bare` would remove it but
  forces API-key billing (X1).
- **Codex cannot be given zero tools** from the command line; it is contained by a
  read-only sandbox in an empty directory instead. A command it runs anyway costs
  tokens, which show up in its usage.
- **Quota limits are configured, not discovered.** No CLI reports its remaining window;
  the `[quota.<adapter>]` numbers must be set to the plan's allowance.

## Free models and uploaders' documents

Command Code lists three free models (`reflection/beam:free`, `poolside/laguna-s-2.1-free`,
`stealth/glyph-cluster:free`; `FREE_MODELS`). A free or stealth model may be offered in
exchange for the data sent to it. None is in the example routes; check the terms before
routing an uploader's document to one.

## Not built

- **Cross-template document reuse.** The same document under two templates is not a
  shared prefix, because templates put instructions first. Reordering would mean
  editing the caller's prompt (see decisions S2), and the protocol gives no reference
  to a previously sent bundle.
- **Metered adapters for other vendors.** `keys/` has Anthropic only, which is what
  the brief asked for to prove the protocol on the metered path. Another vendor is
  another module under `keys/` and one line in `assemble.build_adapter`.
- **Concurrency limits.** The router runs one request at a time per call; a deployment
  that calls it from many threads gets as many concurrent CLI processes. A per-adapter
  semaphore would belong in `routing.py`.
