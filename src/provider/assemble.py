"""Turning `Settings` into a working `Provider`.

Kept apart from `config` so that reading configuration never imports an adapter, and
an adapter is only imported when a route names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from .config import Settings, load
from .protocol import Provider, RefusingProvider


def from_settings(settings: Settings) -> Provider:
    """No routes means no model: the translator's `RefusingProvider`, unchanged."""
    if not settings.routes:
        return RefusingProvider()
    raise NotImplementedError("routing is not built yet")


def from_environment(environ: Mapping[str, str] | None = None) -> Provider:
    return from_settings(load(environ))
