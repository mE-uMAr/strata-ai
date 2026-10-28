"""One real call through the installed Claude Code CLI, on its subscription login.

    python sim/live_claude_code.py --live [--model haiku]

Not part of ``run_all.py``: the suite is offline by default, and this spends a slice of
a real subscription's quota. It exists to check the one thing the fake cannot, which is
that the real CLI's output still has the shape the adapter reads. It prints what came
back and the numbers the adapter extracted.

Nothing is sent but a one-line synthetic task; no repository content, no document.
"""

from __future__ import annotations

import argparse
import json

from common import claim, finish, heading, request

from provider.cli.claude_code import ClaudeCode

parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true", help="actually call the CLI")
parser.add_argument("--model", default="haiku")
args = parser.parse_args()
if not args.live:
    print("not run: pass --live to spend one real subscription call")
    raise SystemExit(0)

schema = {"type": "object", "properties": {"sum": {"type": "integer"}}, "required": ["sum"],
          "additionalProperties": False}
heading(f"claude -p, model {args.model}")
adapter = ClaudeCode({"models": {"extraction": args.model}, "timeout_s": 180})
answer = adapter.complete(request("What is 2 + 3? Put the result in `sum`.", schema=schema, max_output_tokens=200))
print(f"  text: {answer.text}")
claim("answer parses and validates", json.loads(answer.text).get("sum"), json.loads(answer.text) == {"sum": 5})
claim("path / cost_usd", f"{answer.path} / {answer.cost_usd}", answer.path == "subscription" and answer.cost_usd == 0)
claim("usage_source", answer.usage_source, answer.usage_source == "measured")
claim("input / cached / output tokens", f"{answer.input_tokens} / {answer.cached_input_tokens} / {answer.output_tokens}",
      answer.input_tokens > 0 and answer.output_tokens > 0)
claim("metered equivalent (the CLI's total_cost_usd)", f"${answer.metered_equivalent_usd:.6f}",
      answer.metered_equivalent_usd > 0)
claim("model(s) that answered", answer.model, bool(answer.model))
claim("latency", f"{answer.latency_ms} ms", answer.latency_ms > 0)
finish("live_claude_code")
