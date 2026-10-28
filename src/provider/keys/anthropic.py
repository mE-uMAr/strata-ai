"""Anthropic's Messages API, billed per token. The post-launch path.

Uses the official SDK (the ``keys`` extra), with its own retries switched off
(``max_retries=0``): the translator's rule is that a provider does not retry, and the
router above this adapter decides what is worth another call.

The request is shaped for prompt caching, because on this path every input token is
money:

    system:   SYSTEM + the output contract and compact schema   ┐ the same for every call
    user[0]:  the template's fixed instructions (the "head")    ┘ of one template: cached
    user[1…]: the request's images, if it is a `VisionRequest`
    user[n]:  the fenced document and anything after it
    user[2]:  the rejection note, on a retry only

One ``cache_control`` breakpoint sits on the head block, so the system prompt, the
schema and the template instructions are written to the cache once and read at a
tenth of the price on every later call of the same template. Nothing after the
breakpoint is marked: the document differs per call, and marking it would pay the
1.25x cache-write premium on bytes that are never read back. When the system and head
together are shorter than the model's minimum cacheable prefix the breakpoint is left
off, since it would be silently ignored anyway.

The schema goes in the system prompt here rather than after the document as on the CLI
paths: the API takes a system prompt as structured data, so there is no shim to quote it
through, and putting it before the document is what makes it part of the cached prefix.

Not sent, and why:

- ``temperature`` and ``seed``: the current API version rejects sampling parameters on
  every model. Determinism comes from repeated runs (translator ADR 0010).
- ``thinking``: left at the model's default. Effort is set instead (``low`` unless
  configured), because effort is the control the current models honour and thinking
  tokens are billed as output.

Native structured output (``output_config.format``) is used when ``structured = true``.
Off by default: it accepts a subset of JSON Schema, and a translator schema that steps
outside it would fail every call; the contract-in-the-prompt path works for any schema.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping
from typing import Any

from ..accounting import price_for
from ..protocol import (
    Adapter, Answer, BudgetSpent, Malformed, ProviderError, QuotaExhausted, Refused, Request, Unavailable,
    estimate_tokens, images_of,
)
from ..shaping import SYSTEM, contract, split_stable

MIN_CACHEABLE = {"claude-haiku-4-5": 4096, "claude-sonnet-4-6": 1024, "claude-opus-4-8": 1024}
"""Minimum cacheable prefix in tokens, where it is not the 512 of the current models."""


class AnthropicKeys(Adapter):
    path = "metered"
    vision = None  # every Claude model takes images

    def __init__(self, table: Mapping[str, Any] | None = None, *, key: str | None,
                 prices: Mapping[str, Mapping[str, float]] | None = None) -> None:
        table = dict(table or {})
        self.name = "keys.anthropic"
        self.models = dict(table.get("models", {}))
        self.timeout_s = float(table.get("timeout_s", 120))
        self.base_url = table.get("base_url")
        self.effort = table.get("effort", "low")
        self.structured = bool(table.get("structured", False))
        self.cache_ttl = table.get("cache_ttl")  # None means the default five minutes
        self._key = key
        self._prices = dict(prices or {})
        self._client = None

    def __repr__(self) -> str:  # never let the key reach a log line
        return f"AnthropicKeys(models={self.models!r}, base_url={self.base_url!r}, key={'set' if self._key else 'unset'})"

    def client(self):
        if self._client is None:
            if not self._key:
                raise Unavailable(f"{self.name}: no key; set AIPROVIDER_ANTHROPIC_API_KEY or store "
                                  "'anthropic_api_key' in the keychain under strata-aiprovider")
            try:
                import anthropic
            except ImportError:
                raise Unavailable(f"{self.name}: the anthropic SDK is not installed (pip install -e '.[keys]')") from None
            self._client = anthropic.Anthropic(api_key=self._key, base_url=self.base_url, max_retries=0,
                                               timeout=self.timeout_s)
        return self._client

    def body(self, request: Request, model: str, note: str = "") -> dict[str, Any]:
        system = SYSTEM if self.structured else f"{SYSTEM}\n\n{contract(request.schema)}"
        head, tail = split_stable(request.prompt)
        marker: dict[str, Any] = {"type": "ephemeral", **({"ttl": self.cache_ttl} if self.cache_ttl else {})}
        cacheable = estimate_tokens(system + head) >= MIN_CACHEABLE.get(model, 512)
        content: list[dict[str, Any]] = [{"type": "text", "text": head,
                                          **({"cache_control": marker} if cacheable else {})}]
        for image in images_of(request):
            content.append({"type": "image", "source": {"type": "base64", "media_type": image.media_type,
                                                        "data": base64.b64encode(image.data).decode("ascii")}})
        if tail:
            content.append({"type": "text", "text": tail})
        if note:
            content.append({"type": "text", "text": note.strip()})
        output_config: dict[str, Any] = {}
        if self.effort:
            output_config["effort"] = self.effort
        if self.structured:
            output_config["format"] = {"type": "json_schema", "schema": request.schema}
        return {"model": model, "max_tokens": request.max_output_tokens,
                "system": [{"type": "text", "text": system}],
                "messages": [{"role": "user", "content": content}],
                **({"output_config": output_config} if output_config else {})}

    def ask(self, request: Request, model: str, note: str = "") -> Answer:
        client = self.client()
        import anthropic  # present: client() would have raised Unavailable otherwise
        started = time.perf_counter()
        try:
            response = client.messages.create(**self.body(request, model, note))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise Unavailable(f"{self.name}: the key was rejected ({e.status_code})") from None
        except anthropic.RateLimitError as e:
            raise QuotaExhausted(f"{self.name}: rate limited ({_message(e)})") from None
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            raise Unavailable(f"{self.name}: {type(e).__name__}") from None
        except anthropic.APIStatusError as e:
            message = _message(e)
            if e.status_code >= 500:
                raise Unavailable(f"{self.name}: vendor error {e.status_code} ({message})") from None
            if "credit balance" in message.lower() or e.status_code == 402:
                raise BudgetSpent(f"{self.name}: {message}") from None
            raise ProviderError(f"{self.name}: {e.status_code} {message}") from None
        latency = int((time.perf_counter() - started) * 1000)

        usage = response.usage
        read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        input_tokens = int(usage.input_tokens) + read + written
        output_tokens = int(usage.output_tokens)
        price = price_for(model, self._prices)
        if price is None:
            raise ProviderError(f"{self.name}: no price for {model}; add [prices.\"{model}\"] so cost_usd is true")
        cost = price.cost(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=read,
                          cache_write_tokens=written)
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()
        answer = Answer(text=text, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency,
                        cost_usd=cost, adapter=self.name, model=response.model or model, path="metered",
                        usage_source="measured", cached_input_tokens=read, metered_equivalent_usd=cost)
        if response.stop_reason == "refusal":
            raise Refused(f"{self.name}: the model declined", answer)
        if response.stop_reason == "max_tokens":
            raise Malformed(f"{self.name}: the answer was cut off at the {request.max_output_tokens}-token ceiling",
                            answer)
        if not text:
            raise ProviderError(f"{self.name}: the response held no text (stop_reason {response.stop_reason})")
        return answer


def _message(error: Exception) -> str:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])
    return str(getattr(error, "message", "") or error)
