"""AIProvider: Strata's model-provider module.

The translator defines one protocol, ``complete(Request) -> Completion``, and attaches
whatever the platform hands it (translator ADR 0010). This package is what the platform
hands it. ``from_environment()`` is the one entry point a deployment needs; with no
configuration it returns the translator's own `RefusingProvider`, so an unconfigured
deployment cannot reach a model.
"""

from __future__ import annotations

from .assemble import Assembly, assemble, from_environment, from_settings
from .config import ConfigError, Settings, load
from .protocol import Answer, ProviderError, RefusingProvider

__all__ = ["Answer", "Assembly", "ConfigError", "ProviderError", "RefusingProvider", "Settings", "from_environment",
           "assemble", "from_settings", "load"]
