"""A stand-in for a subscription CLI, so every CLI path runs with no subscription.

    python sim/fake_cli.py --as generic|claude|codex [tool arguments...]

It reads the prompt the way the real tool would (stdin, or ``--prompt`` on argv for the
generic tool), and prints output in that tool's shape:

- ``generic``: ``{"response": ..., "stats": {"models": {<m>: {"tokens": {...}}}}}``
- ``claude``: Claude Code's ``--output-format json`` result object, with ``usage``,
  ``total_cost_usd`` and ``modelUsage``. Prompt caching is emulated: input shared with
  the previous call (same state directory) is reported as ``cache_read_input_tokens``.
- ``codex``: ``codex exec --json`` events, one per line, ending in ``turn.completed``
  with ``usage``.

Behaviour comes from the environment, which the adapter under test passes through its
configured ``env``:

- ``FAKE_CLI_MODES``: comma-separated, one per call (the last repeats): ``ok``,
  ``prose`` (JSON wrapped in chatter), ``invalid`` (JSON that breaks the schema),
  ``limit``, ``hang``, ``ask`` (stops to ask a question on stdin), ``crash``,
  ``logged_out``, ``refuse``, ``nousage``, ``long`` (more output than the ceiling).
- ``FAKE_CLI_ANSWER``: the JSON to answer with.
- ``FAKE_CLI_STATE``: a directory for the call counter, the cache emulation and a
  ``seen-<n>.json`` report of what each call saw (argv, cwd, which credential-shaped
  variables were visible), which the simulations check.

The fake never decides anything from the prompt beyond its length: it is a transport
stand-in, not a model.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def main() -> int:
    args = sys.argv[1:]
    persona = args[args.index("--as") + 1] if "--as" in args else "generic"
    state = Path(os.environ.get("FAKE_CLI_STATE", "."))
    state.mkdir(parents=True, exist_ok=True)
    counter = state / "calls"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    modes = (os.environ.get("FAKE_CLI_MODES") or "ok").split(",")
    mode = modes[min(n, len(modes)) - 1].strip()

    if "--prompt" in args:
        prompt = args[args.index("--prompt") + 1]
    else:
        prompt = sys.stdin.read()

    (state / f"seen-{n}.json").write_text(json.dumps({
        "argv": args, "cwd": os.getcwd(), "cwd_entries": sorted(os.listdir(".")),
        "credential_vars": sorted(k for k in os.environ if any(
            s in k.upper() for s in ("API_KEY", "AUTH_TOKEN", "ANTHROPIC", "AIPROVIDER"))),
        "max_output_env": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
        "prompt_tokens": tokens(prompt), "prompt_tail": prompt[-300:], "mode": mode,
    }, indent=2))

    if mode == "hang":
        time.sleep(3600)
    if mode == "ask":
        print("Do you trust the files in this folder? [y/N] ", end="", flush=True)
        try:
            input()
        except EOFError:
            print("error: no answer on stdin; refusing to continue", file=sys.stderr)
            return 2
        return 0
    if mode == "crash":
        print("Traceback (most recent call last): segmentation fault in renderer", file=sys.stderr)
        return 139
    if mode == "logged_out":
        print("Error: not logged in. Please log in with /login.", file=sys.stderr)
        return 1
    if mode == "limit":
        if persona == "codex":
            print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again in 3h."}}))
            return 1
        if persona == "claude":
            print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                              "result": "Claude AI usage limit reached|1767225600"}))
            return 1
        print("Error: 429 Too Many Requests: quota exhausted", file=sys.stderr)
        return 1

    answer = os.environ.get("FAKE_CLI_ANSWER") or '{"ok": true}'
    if mode == "prose":
        answer = f"Sure! Here is the JSON you asked for:\n```json\n{answer}\n```\nLet me know if you need more."
    elif mode == "invalid":
        answer = '{"rules": "not a list"}'
    elif mode == "refuse":
        answer = "I can't help with that."
    elif mode == "long":
        answer = json.dumps({"rules": ["x" * 400] * 200, "decision": "proceed"})

    in_tokens, out_tokens = tokens(prompt), tokens(answer)

    if persona == "generic":
        stats = {"models": {"fake-small": {"tokens": {"prompt": in_tokens, "candidates": out_tokens, "cached": 0}}}}
        print(json.dumps({"response": answer, **({} if mode == "nousage" else {"stats": stats})}))
        return 0

    if persona == "claude":
        model = args[args.index("--model") + 1] if "--model" in args else "default"
        previous = state / "last-prompt.txt"
        shared = 0
        if previous.exists():
            old = previous.read_text(encoding="utf-8")
            while shared < min(len(old), len(prompt)) and old[shared] == prompt[shared]:
                shared += 1
        previous.write_text(prompt, encoding="utf-8")
        read = tokens(prompt[:shared]) if shared else 0
        write = in_tokens - read
        if mode == "refuse":
            print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": answer,
                              "stop_reason": "refusal", "usage": {"input_tokens": in_tokens, "output_tokens": 6}}))
            return 0
        cost = round((write * 1.25 * 0.10 + read * 0.01 + out_tokens * 0.50) / 1e6, 8)
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": False, "duration_ms": 900, "num_turns": 1,
            "result": answer, "session_id": "00000000-0000-0000-0000-000000000000", "total_cost_usd": cost,
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": write,
                      "cache_read_input_tokens": read, "output_tokens": out_tokens},
            "modelUsage": {f"claude-{model}-5-5": {"inputTokens": 0, "outputTokens": out_tokens,
                                                   "cacheReadInputTokens": read,
                                                   "cacheCreationInputTokens": write, "costUSD": cost}},
        }))
        return 0

    # codex
    print(json.dumps({"type": "thread.started", "thread_id": "fake"}))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}}))
    print(json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": answer}}))
    if mode != "nousage":
        print(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": in_tokens, "cached_input_tokens": 0, "output_tokens": out_tokens}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
