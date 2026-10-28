"""What each call actually cost.

Two kinds of cost, kept apart because they are different things:

- **Money.** The metered path charges per token. ``cost_usd`` is that charge, from the
  price table below, and it is what the translator's gateway enforces budgets on.
- **Quota.** A subscription call costs no marginal money, but it uses a slice of a
  window the subscription allows. Reporting that as dollars would be a fiction, and
  reporting it as zero would hide the one resource that actually runs out, so it is
  its own number: ``quota_units``, counted against a window per adapter.

Every attempt, every cache hit and every escalation is one line in the ledger, an
append-only JSONL file. The saving report (§5.8 of the brief) is computed from those
lines and nothing else, so each number in it can be traced to the calls behind it.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Price:
    """USD per million tokens.

    ``cache_read`` is what a token served from the vendor's prompt cache costs, and
    ``cache_write`` what writing one costs (Anthropic charges 1.25x input for a
    five-minute entry; OpenAI caches automatically and charges no write premium). Above
    ``long_context_from`` input tokens a request is billed at the ``long_*`` rates where
    the vendor has such a tier.
    """

    input: float
    output: float
    cache_read: float
    cache_write: float
    long_context_from: int | None = None
    long_input: float | None = None
    long_output: float | None = None

    def cost(self, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
             cache_write_tokens: int = 0) -> float:
        """``input_tokens`` is the total input, cached or not, as `Answer` defines it."""
        long = self.long_context_from is not None and input_tokens > self.long_context_from
        rate_in = self.long_input if long and self.long_input is not None else self.input
        rate_out = self.long_output if long and self.long_output is not None else self.output
        scale = rate_in / self.input if self.input else 1.0
        uncached = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
        return round((uncached * rate_in + cache_read_tokens * self.cache_read * scale
                      + cache_write_tokens * self.cache_write * scale + output_tokens * rate_out) / 1e6, 8)


PRICES: dict[str, Price] = {
    # Anthropic first-party API rates, as published in October 2026.
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.125, long_context_from=100_000, long_input=0.50, long_output=2.50),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-fable-5-1": Price(10.00, 50.00, 0.25, 12.50),
    "claude-sonnet-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-opus-5": Price(5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-4-6": Price(3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": Price(1.00, 5.00, 0.10, 1.25),
    # OpenAI rates for the models the Codex CLI fronts. Not re-verified when this table
    # was written: check them against the vendor's page before trusting a saving figure
    # built on them, and override them in the config's [prices] table.
    "gpt-5": Price(1.25, 10.00, 0.125, 1.25),
    "gpt-5-codex": Price(1.25, 10.00, 0.125, 1.25),
    "gpt-5-mini": Price(0.25, 2.00, 0.025, 0.25),
    "gpt-5-nano": Price(0.05, 0.40, 0.005, 0.05),
}
"""Overridden or extended per deployment by the config's ``[prices."<model>"]`` tables."""

ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
"""The Claude Code CLI's short model names, for pricing what it would have cost metered."""


def price_for(model: str, overrides: dict[str, dict[str, float]] | None = None) -> Price | None:
    model = ALIASES.get(model, model)
    if overrides and model in overrides:
        return Price(**overrides[model])
    return PRICES.get(model)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class Entry:
    """One line of the ledger: an attempt, a cache hit, or a refusal before any call."""

    input_hash: str
    prompt_name: str
    model_id: str
    adapter: str
    model: str
    path: str
    outcome: str
    """``ok``, ``hit``, or the reason an attempt failed (``schema``, ``limit``,
    ``unavailable``, ``refusal``, ``budget``, ``error``)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    usage_source: str = "measured"
    cost_usd: float = 0.0
    metered_equivalent_usd: float = 0.0
    avoided_usd: float = 0.0
    """For a cache hit: what the original call cost on its path, or would have cost
    metered if it ran on a subscription. Zero otherwise."""
    quota_units: float = 0.0
    latency_ms: int = 0
    rung: int = 0
    attempt: int = 0
    note: str = ""
    at: str = field(default_factory=_now)
    epoch: float = field(default_factory=time.time)


@dataclass
class Ledger:
    """Append-only. In memory always; on disk too when ``path`` is set."""

    path: Path | None = None
    entries: list[Entry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.path is not None and self.path.is_file():
            known = set(Entry.__dataclass_fields__)
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    # Fields this commit does not know are ignored, as Strata's formats require.
                    self.entries.append(Entry(**{k: v for k, v in json.loads(line).items() if k in known}))
        self._loaded = len(self.entries)

    def record(self, entry: Entry) -> Entry:
        with self._lock:
            self.entries.append(entry)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
        return entry

    def this_run(self) -> list[Entry]:
        """Entries recorded by this process, as opposed to ones read back from disk."""
        return self.entries[self._loaded:]

    def since(self, epoch: float) -> list[Entry]:
        return [e for e in self.entries if e.epoch >= epoch]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(e) for e in self.entries]


@dataclass
class QuotaMeter:
    """Counts subscription quota per adapter over a rolling window, from the ledger.

    Subscriptions limit usage per window (a number of messages every few hours) and
    say so only by failing once it is gone. Counting locally lets the router step past
    an adapter it knows is spent *without* spawning it, and lets the report say how much
    of each window a run used. The limit is configured, not discovered:

        [quota.claude_code]
        units = 200          # what one window allows, in calls
        window_hours = 5

    An adapter with no ``[quota.<name>]`` table is only stopped by the vendor's own
    limit message, which the adapter maps to `QuotaExhausted` either way.
    """

    ledger: Ledger
    windows: dict[str, dict[str, float]] = field(default_factory=dict)
    clock: Any = time.time

    def used(self, adapter: str) -> float:
        window = self.windows.get(adapter)
        hours = float(window.get("window_hours", 5)) if window else 5.0
        since = self.clock() - hours * 3600
        return sum(e.quota_units for e in self.ledger.since(since) if e.adapter == adapter)

    def remaining(self, adapter: str) -> float | None:
        window = self.windows.get(adapter)
        if not window or "units" not in window:
            return None
        return float(window["units"]) - self.used(adapter)

    def check(self, adapter: str) -> str | None:
        """A reason to skip the adapter, or None if its window has room."""
        left = self.remaining(adapter)
        if left is not None and left <= 0:
            window = self.windows[adapter]
            return (f"{adapter}: {self.used(adapter):g} of {float(window['units']):g} quota units used in the "
                    f"last {float(window.get('window_hours', 5)):g}h (counted locally)")
        return None
