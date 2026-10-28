"""Turning `Settings` into a working `Provider`.

Kept apart from `config` so that reading configuration never imports an adapter: an
adapter module is imported only when a route names it, so an installation without the
``keys`` extra can still run every CLI path.

The stack, outermost first::

    ReplayCache  (if cache_dir is set)  — a hit stops here, before any model
      Router                            — walks the route, retries, escalates, ledgers
        adapters                        — one per step: a CLI, or (if allowed) a key

No routes means no stack: the translator's `RefusingProvider`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .accounting import Ledger, QuotaMeter
from .cache import ReplayCache
from .config import ConfigError, Settings, load
from .protocol import Adapter, Provider, RefusingProvider
from .routing import Router, Step


def build_adapter(name: str, settings: Settings, environ: Mapping[str, str] | None = None) -> Adapter:
    table = settings.adapter_config(name)
    prices = dict(settings.prices)
    if name == "claude_code":
        from .cli.claude_code import ClaudeCode
        return ClaudeCode(table, environ=environ, prices=prices)
    if name == "codex":
        from .cli.codex import Codex
        return Codex(table, environ=environ, prices=prices)
    if name.startswith("command."):
        from .cli.command import CommandAdapter, CommandSpec
        if not table:
            raise ConfigError(f"a route names {name} but the config has no [{name}] table")
        return CommandAdapter(CommandSpec.from_table(name.split(".", 1)[1], table), environ=environ, prices=prices)
    if name == "keys.anthropic":
        from .keys.anthropic import AnthropicKeys
        return AnthropicKeys(table, key=settings.secret("anthropic_api_key"), prices=prices)
    raise ConfigError(f"no adapter called {name}")


@dataclass
class Assembly:
    """The provider, and the parts a report needs to read."""

    provider: Provider
    ledger: Ledger | None = None
    router: Router | None = None
    cache: ReplayCache | None = None


def assemble(settings: Settings, environ: Mapping[str, str] | None = None) -> Assembly:
    if not settings.routes:
        return Assembly(provider=RefusingProvider())
    adapters: dict[str, Adapter] = {}
    routes: dict[str, list[Step]] = {}
    for model_id, rungs in settings.routes.items():
        steps = []
        for rung in rungs:
            if rung.adapter not in adapters:
                adapters[rung.adapter] = build_adapter(rung.adapter, settings, environ)
            steps.append(Step(adapters[rung.adapter], rung.model))
        routes[model_id] = steps
    ledger = Ledger(settings.ledger_path)
    router = Router(routes, ledger=ledger, allow_metered=settings.allow_metered,
                    max_schema_retries=settings.max_schema_retries,
                    quota=QuotaMeter(ledger, {k: dict(v) for k, v in settings.quotas.items()}))
    if settings.cache_dir is None:
        return Assembly(provider=router, ledger=ledger, router=router)
    cache = ReplayCache(settings.cache_dir, router, ledger)
    return Assembly(provider=cache, ledger=ledger, router=router, cache=cache)


def from_settings(settings: Settings, environ: Mapping[str, str] | None = None) -> Provider:
    return assemble(settings, environ).provider


def from_environment(environ: Mapping[str, str] | None = None) -> Provider:
    return from_settings(load(environ))
