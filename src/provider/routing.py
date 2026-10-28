"""Which adapter answers.

A route is a declared list of steps for one caller ``model_id``, cheapest capable
first. The router walks it for each request and moves to the next step only for a
*reason*:

| What happened | Retry on the same step? | Escalate? |
| --- | --- | --- |
| The answer is not JSON, breaks the schema, or hit the output ceiling | Yes, up to ``max_schema_retries``, with the reason appended | Yes, once retries are spent |
| The adapter is unavailable (missing binary, logged out, timed out, vendor down) | No | Yes |
| The subscription's limit is reached (vendor message or the local quota count) | No | Yes |
| The model refused | No | **No**: raised to the caller |
| The account has no money left | No | **No**: raised to the caller |
| Any other failure | No | Yes |
| The request carries images and this step's model cannot see them | Not called | Yes, without spawning it |

Escalating on *suspicion* (a short answer, a low-confidence look) is not on the list.
A small model whose answer validates is the answer; asking a bigger one as well would
double the cost to settle a doubt nobody measured.

**Retries live here, not in adapters.** The translator's rule is that a provider does
not retry, and each adapter keeps it: one call, one answer. The router is the policy
layer above them, and its retries are bounded, earned by a specific failure, and each
one is a line in the ledger. A retry appends the rejection after the previous request,
so the previous request is a cacheable prefix of the retry.

**The metered path is never a fallback by accident.** A route may name a metered
adapter only if ``AIPROVIDER_ALLOW_METERED`` is set; otherwise building the router
fails, naming the step. A route that is allowed to reach it logs a ``WARNING`` every
time it does, and the ledger line says so. The case this exists to prevent is a quiet
fall-through from a subscription to a pay-per-token key, so it is made impossible
without consent and impossible to miss with it.

**What a request cost is the sum of its attempts.** If the first step burned tokens on
a malformed answer and the second step answered, the returned `Answer` reports both,
because the gateway enforces budgets on these numbers and tokens spent on a rejected
answer were still spent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .accounting import Entry, Ledger, QuotaMeter
from .config import ConfigError
from .protocol import (
    Adapter, AdapterError, Answer, BudgetSpent, Malformed, ProviderError, Refused, Request, images_of,
)
from .shaping import rejection
from .validate import problems

log = logging.getLogger("provider.routing")


@dataclass(frozen=True)
class Step:
    adapter: Adapter
    model: str

    def __str__(self) -> str:
        return f"{self.adapter.name}:{self.model}"


@dataclass
class Router:
    routes: Mapping[str, Sequence[Step]]
    ledger: Ledger = field(default_factory=Ledger)
    allow_metered: bool = False
    max_schema_retries: int = 1
    quota: QuotaMeter | None = None
    name: str = "router"

    def __post_init__(self) -> None:
        if not self.allow_metered:
            metered = [f"{model_id} -> {step}" for model_id, steps in self.routes.items()
                       for step in steps if step.adapter.metered]
            if metered:
                raise ConfigError(
                    f"routes name metered steps ({'; '.join(metered)}) but AIPROVIDER_ALLOW_METERED is not set. "
                    "A metered path is only reachable when the environment asks for it."
                )
        if self.quota is None:
            self.quota = QuotaMeter(self.ledger)

    # -- one request --------------------------------------------------------

    def complete(self, request: Request) -> Answer:
        steps = self.routes.get(request.model_id) or self.routes.get("*")
        if not steps:
            raise ProviderError(f"no route for model_id {request.model_id!r}; routes exist for {sorted(self.routes)}")

        trail = _Trail()
        for index, step in enumerate(steps):
            outcome = self._walk_step(request, index, step, trail)
            if isinstance(outcome, Answer):
                return trail.combine(outcome)
            if index + 1 < len(steps):
                trail.escalations.append(f"{step} -> {steps[index + 1]}: {outcome}")
                log.info("escalating %s -> %s for %s (%s)", step, steps[index + 1], request.prompt_name, outcome)
        raise ProviderError("every step of the route failed: " + " | ".join(trail.failures))

    def _walk_step(self, request: Request, index: int, step: Step, trail: _Trail) -> Answer | str:
        """Try one step, with its earned retries. The winning `Answer`, or the reason to move on."""
        adapter = step.adapter
        if images_of(request) and not adapter.sees_images(step.model):
            self._record(request, step, index, 0, "skipped", note="cannot see images")
            trail.failures.append(f"{step}: cannot see images")
            return "no vision"
        note = ""
        for attempt in range(self.max_schema_retries + 1):
            skip = self.quota.check(adapter.name) if adapter.path == "subscription" else None
            if skip is not None:
                self._record(request, step, index, attempt, "limit", note=skip)
                trail.failures.append(f"{step}: {skip}")
                return "limit (local quota count)"
            if adapter.metered:
                log.warning("METERED PATH TAKEN: %s is answering %s (%s) and will be billed per token",
                            step, request.prompt_name, request.input_hash[:19])
            trail.calls += 1
            try:
                answer = adapter.ask(request, step.model, note)
            except (Refused, BudgetSpent) as e:
                self._failed(request, step, index, attempt, e, trail)
                raise type(e)(f"{step}: {e}", trail.combine(None)) from None
            except Malformed as e:
                self._failed(request, step, index, attempt, e, trail)
                note = rejection([str(e).split(": ", 1)[-1]])
                continue
            except AdapterError as e:
                self._failed(request, step, index, attempt, e, trail)
                return e.reason
            except ProviderError as e:
                self._record(request, step, index, attempt, "error", note=str(e)[:500])
                trail.failures.append(f"{step}: {e}")
                return "error"

            trail.parts.append(answer)
            found = problems(answer.text, request.schema)
            if not found:
                self._record(request, step, index, attempt, "ok", answer)
                return answer
            self._record(request, step, index, attempt, "schema", answer, note="; ".join(found))
            trail.failures.append(f"{step}: {'; '.join(found)}")
            note = rejection(found)
        return "schema"

    # -- bookkeeping --------------------------------------------------------

    def _failed(self, request: Request, step: Step, index: int, attempt: int, error: AdapterError,
                trail: _Trail) -> None:
        if error.spent is not None:
            trail.parts.append(error.spent)
        self._record(request, step, index, attempt, error.reason, error.spent, note=str(error)[:500])
        trail.failures.append(f"{step}: {error}")

    def _record(self, request: Request, step: Step, index: int, attempt: int, outcome: str,
                answer: Answer | None = None, note: str = "") -> None:
        a = answer
        if step.adapter.metered:
            note = ("metered " + note).strip()
        self.ledger.record(Entry(
            input_hash=request.input_hash, prompt_name=request.prompt_name, model_id=request.model_id,
            adapter=step.adapter.name, model=a.model if a else step.model, path=step.adapter.path, outcome=outcome,
            input_tokens=a.input_tokens if a else 0, output_tokens=a.output_tokens if a else 0,
            cached_input_tokens=a.cached_input_tokens if a else 0,
            usage_source=a.usage_source if a else "measured", cost_usd=a.cost_usd if a else 0.0,
            metered_equivalent_usd=a.metered_equivalent_usd if a else 0.0,
            quota_units=a.quota_units if a else 0.0, latency_ms=a.latency_ms if a else 0,
            rung=index, attempt=attempt, note=note,
        ))



@dataclass
class _Trail:
    """Everything one request spent on its way to an answer, or to failing."""

    parts: list[Answer] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    calls: int = 0

    def combine(self, winner: Answer | None) -> Answer | None:
        if winner is None:
            if not self.parts:
                return None
            winner = self.parts[-1]
        every = self.parts
        return Answer(
            text=winner.text,
            input_tokens=sum(p.input_tokens for p in every),
            output_tokens=sum(p.output_tokens for p in every),
            latency_ms=sum(p.latency_ms for p in every),
            cost_usd=round(sum(p.cost_usd for p in every), 8),
            adapter=winner.adapter, model=winner.model,
            path="metered" if any(p.path == "metered" for p in every) else winner.path,
            usage_source="estimated" if any(p.usage_source == "estimated" for p in every) else "measured",
            cached_input_tokens=sum(p.cached_input_tokens for p in every),
            quota_units=sum(p.quota_units for p in every),
            metered_equivalent_usd=round(sum(p.metered_equivalent_usd for p in every), 8),
            attempts=self.calls,
            escalations=tuple(self.escalations),
        )
