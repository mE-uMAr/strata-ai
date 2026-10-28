"""Never pay twice.

Content-addressed on ``request.input_hash``, which the gateway already computes over
model, prompt, rendered input and schema, so two requests share an entry exactly when
they would get the same answer.

**The on-disk shape is the translator's `ReplayProvider` shape**: ``root/<hash>.json``
with ``text``, ``input_tokens``, ``output_tokens`` and ``cost_usd``. A directory this
cache wrote can be handed to the translator's own offline provider and replayed with no
conversion. A nicer format was considered (one SQLite file, or a JSONL log) and
rejected: interoperability with the replay path is worth more than tidiness, and one
small file per answer is also what makes a cache entry easy to inspect, delete or ship.

The extra keys this cache writes (``adapter``, ``model``, ``path``, ``usage_source``,
``cached_input_tokens``, ``metered_equivalent_usd``) are ignored by `ReplayProvider`,
which reads only the keys it knows.

What a hit reports: zero tokens, zero cost, ``path="cache"``. Those are true numbers
for the call that just happened, which is what the gateway's budget needs. What the
original call cost is not lost; it is the hit's ``avoided_usd`` in the ledger.

Only answers that satisfy the request's schema are stored. Caching a bad answer would
make the failure free and permanent.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .accounting import Entry, Ledger
from .protocol import Answer, Completion, Provider, Request
from .validate import problems


@dataclass
class CacheStats:
    lookups: int = 0
    hits: int = 0
    writes: int = 0
    refused_writes: int = 0
    avoided_usd: float = 0.0
    avoided_input_tokens: int = 0
    avoided_output_tokens: int = 0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0


@dataclass
class ReplayCache:
    """Wraps any provider. A hit never reaches it."""

    root: Path
    inner: Provider
    ledger: Ledger | None = None
    name: str = "cache"
    stats: CacheStats = field(default_factory=CacheStats)

    def path_for(self, request: Request) -> Path:
        return self.root / f"{request.input_hash.removeprefix('sha256:')}.json"

    def complete(self, request: Request) -> Completion:
        self.stats.lookups += 1
        path = self.path_for(request)
        if path.is_file():
            return self._hit(request, json.loads(path.read_text(encoding="utf-8")))
        answer = self.inner.complete(request)
        if problems(answer.text, request.schema):
            self.stats.refused_writes += 1
        else:
            self._write(path, request, answer)
        return answer

    def _hit(self, request: Request, saved: dict) -> Answer:
        avoided = float(saved.get("cost_usd", 0.0)) + float(saved.get("metered_equivalent_usd", 0.0))
        self.stats.hits += 1
        self.stats.avoided_usd += avoided
        self.stats.avoided_input_tokens += int(saved.get("input_tokens", 0))
        self.stats.avoided_output_tokens += int(saved.get("output_tokens", 0))
        if self.ledger is not None:
            self.ledger.record(Entry(
                input_hash=request.input_hash, prompt_name=request.prompt_name, model_id=request.model_id,
                adapter="cache", model=str(saved.get("model", "")), path="cache", outcome="hit",
                avoided_usd=avoided, note=f"originally answered by {saved.get('adapter', 'unknown')}",
            ))
        return Answer(text=saved["text"], input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=0.0,
                      adapter="cache", model=str(saved.get("model", "")), path="cache", usage_source="measured")

    def _write(self, path: Path, request: Request, answer: Completion) -> None:
        record = {
            # The four keys ReplayProvider reads, plus the model_id it writes itself.
            "model_id": request.model_id,
            "text": answer.text,
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
            "cost_usd": answer.cost_usd,
        }
        if isinstance(answer, Answer):
            record.update(adapter=answer.adapter, model=answer.model, path=answer.path,
                          usage_source=answer.usage_source, cached_input_tokens=answer.cached_input_tokens,
                          metered_equivalent_usd=answer.metered_equivalent_usd)
        self.root.mkdir(parents=True, exist_ok=True)
        # Write then rename, so a crash mid-write never leaves a truncated entry that
        # a later run would treat as a hit.
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        os.replace(tmp, path)
        self.stats.writes += 1
