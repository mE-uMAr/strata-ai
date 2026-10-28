"""The seam, as the translator defines it, and what this module adds on top of it.

`Request`, `Completion`, `Provider` and `ProviderError` are imported from the
translator rather than copied. The protocol is the translator's to define (ADR 0010);
a copy here would drift silently, while an import fails loudly the day it changes.

What this module adds never changes the protocol:

- `Answer` is a `Completion` with extra fields. The gateway reads the five fields it
  knows and ignores the rest, so every adapter here is still a plain `Provider`. The
  extra fields carry what the brief requires a reader to be able to see: which path
  answered, whether the token counts were measured or estimated, how many input tokens
  the vendor served from its prompt cache, how much subscription quota the call used,
  and what the metered path would have charged.
- The errors are all `ProviderError` subclasses, so the gateway handles them as it
  already does. The subclass is the *reason*, which is what the router escalates on.
- Every error carries what the failed attempt spent (`spent`), because a refusal or a
  truncated answer still consumed tokens, and budgets must see true numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from translator.gateway.models import ProviderError
from translator.gateway.providers import Completion, Provider, RefusingProvider, Request, estimate_tokens

__all__ = [
    "Adapter", "Answer", "BudgetSpent", "Completion", "Malformed", "Provider", "ProviderError",
    "QuotaExhausted", "Refused", "RefusingProvider", "Request", "Unavailable", "estimate_tokens",
]

Path = Literal["cache", "subscription", "metered", "local"]
"""How an answer was paid for. `local` is a stub or a fake: no model, no money."""

UsageSource = Literal["measured", "estimated"]
"""`measured` means the tool or vendor reported the number. `estimated` means this
module computed it from text length, because the tool did not say."""


@dataclass(frozen=True)
class Answer(Completion):
    """A `Completion`, plus what the caller needs to tell a measured cost from a guess.

    Token semantics, fixed here so every adapter means the same thing:

    - ``input_tokens`` is every input token the model processed for this request,
      including the ones the vendor served from its prompt cache. The vendor may bill
      cached tokens at a tenth of the price, but they were still read.
    - ``cached_input_tokens`` is the part of ``input_tokens`` served from the vendor's
      prompt cache. Zero where the path does not report it.
    - ``cost_usd`` is marginal money. A subscription call fits in a flat fee, so it is
      0.0 *as a known value*, never as "unknown"; its real cost is ``quota_units``.
    - ``metered_equivalent_usd`` is what the same tokens would have cost on the
      metered API. It is what makes the saving report possible.
    """

    adapter: str = ""
    model: str = ""
    path: Path = "local"
    usage_source: UsageSource = "estimated"
    cached_input_tokens: int = 0
    quota_units: float = 0.0
    metered_equivalent_usd: float = 0.0
    attempts: int = 1
    escalations: tuple[str, ...] = ()


class AdapterError(ProviderError):
    """A failure with a reason the router can act on, and what it cost to find out."""

    reason: str = "error"

    def __init__(self, message: str, spent: Answer | None = None) -> None:
        super().__init__(message)
        self.spent = spent


class Unavailable(AdapterError):
    """The adapter cannot answer at all: no binary, no credential, not logged in,
    timed out, or the vendor is down. Escalate."""

    reason = "unavailable"


class QuotaExhausted(AdapterError):
    """The subscription's window is used up, or the vendor rate-limited the key. Escalate."""

    reason = "limit"


class Malformed(AdapterError):
    """The answer is not JSON, does not fit the schema, or was cut off at the output
    ceiling. Earns a retry with a tightened instruction, then escalates."""

    reason = "schema"


class Refused(AdapterError):
    """The model declined. Never retried and never escalated: asking a different model
    the same thing until one agrees is not a policy this module will implement."""

    reason = "refusal"


class BudgetSpent(AdapterError):
    """The account has no money or credit left. Never retried: the next call fails the
    same way, and escalating to another metered path would spend from a second account
    the caller did not choose."""

    reason = "budget"


class Adapter:
    """What every adapter in `cli/` and `keys/` provides.

    ``ask`` takes the vendor model explicitly so the router can walk a ladder of
    models through one adapter, and an optional ``note`` the router appends on a retry
    (the reason the previous answer was rejected), which goes after everything else
    so the previous request stays a cacheable prefix. ``complete`` makes the adapter a `Provider` on its own:
    it maps the caller's ``model_id`` through ``models`` and refuses one it was not
    told about, rather than falling back to some default model.
    """

    name: str
    path: Path
    models: dict[str, str]

    @property
    def metered(self) -> bool:
        return self.path == "metered"

    def ask(self, request: Request, model: str, note: str = "") -> Answer:  # pragma: no cover - interface
        raise NotImplementedError

    def complete(self, request: Request) -> Answer:
        model = self.models.get(request.model_id)
        if model is None:
            raise ProviderError(
                f"{self.name} has no model configured for model_id {request.model_id!r}; "
                f"configured: {sorted(self.models)}"
            )
        return self.ask(request, model)
