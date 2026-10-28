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
