"""Configuration: what answers, in what order, and where credentials come from.

Two sources, with different jobs:

- **A TOML file** (path in ``AIPROVIDER_CONFIG``) holds structure: the routes, each
  adapter's settings, quota windows, price overrides, and the declarations of generic
  CLI tools. It is the kind of thing that can be reviewed and committed, so it is
  checked on load and **refused if it holds anything shaped like a credential**.
- **The environment and the OS keychain** hold everything secret, and the one switch
  that makes a metered path reachable, ``AIPROVIDER_ALLOW_METERED``. That switch lives
  in the environment rather than in the file so that a config file copied from one
  deployment to another cannot carry the permission to spend money with it.

With neither source present the result is a `Settings` with no routes, and
`provider.from_environment()` turns that into the translator's `RefusingProvider`.
Nothing reaches a model because a default fell through.

A ``.env`` file is read if present (``AIPROVIDER_ENV_FILE``, else ``./.env``), with the
real environment taking precedence. It is gitignored; ``.env.example`` lists the names
with empty values.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_PREFIX = "AIPROVIDER_"
KEYCHAIN_SERVICE = "strata-aiprovider"

ADAPTER_KINDS = ("claude_code", "codex", "command", "keys")
"""The fixed set of adapter families. A route names one of these, never a module
path: like the translator's own config, nothing here imports by name at run time."""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Rung:
    """One step of a route: an adapter and the vendor model it should ask for."""

    adapter: str
    model: str

    @classmethod
    def parse(cls, text: str) -> Rung:
        adapter, sep, model = text.partition(":")
        if not sep or not adapter or not model:
            raise ConfigError(f"a route step is 'adapter:model', got {text!r}")
        if adapter.split(".")[0] not in ADAPTER_KINDS:
            raise ConfigError(f"unknown adapter {adapter!r} in {text!r}; known kinds: {ADAPTER_KINDS}")
        if adapter in ("command", "keys"):
            raise ConfigError(f"{adapter!r} needs a name, as in '{adapter}.<name>:<model>'")
        return cls(adapter=adapter, model=model)

    def __str__(self) -> str:
        return f"{self.adapter}:{self.model}"


@dataclass(frozen=True)
class Settings:
    routes: Mapping[str, tuple[Rung, ...]] = field(default_factory=dict)
    """Caller ``model_id`` → steps, cheapest capable first. A ``model_id`` with no
    route is refused; ``"*"`` matches anything, but only if a config declares it."""
    allow_metered: bool = False
    max_schema_retries: int = 1
    cache_dir: Path | None = None
    ledger_path: Path | None = None
    adapters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """Per-adapter tables: ``claude_code``, ``codex``, ``command.<name>``, ``keys.<vendor>``."""
    quotas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    prices: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    environ: Mapping[str, str] = field(default_factory=dict, repr=False)
    source: str = "defaults"

    def adapter_config(self, name: str) -> Mapping[str, Any]:
        return self.adapters.get(name, {})

    def secret(self, name: str) -> str | None:
        """A credential by logical name, from the environment and then the keychain.

        The environment variable is ``AIPROVIDER_<NAME>``, deliberately prefixed: an
        unprefixed ``ANTHROPIC_API_KEY`` in the environment would also be picked up by
        the Claude Code CLI, which would then bill the API instead of the subscription.
        Keeping this module's keys under its own names, and scrubbing the vendor names
        from every CLI's environment, is what keeps the two paths apart.
        """
        value = self.environ.get(ENV_PREFIX + name.upper())
        if value:
            return value
        return _keychain(name)


def _keychain(name: str) -> str | None:
    try:
        import keyring  # optional: the `keychain` extra
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, name.lower()) or None
    except Exception:  # a locked or absent keychain is "no credential", not a crash
        return None


# -- loading ----------------------------------------------------------------

_CREDENTIAL_KEY = re.compile(r"(api_?key|token|secret|password|credential|cookie|session_?id)", re.IGNORECASE)
_CREDENTIAL_VALUE = re.compile(r"^(sk-|sk_|xox[abp]-|ghp_|gho_|github_pat_|AKIA|AIza|eyJ)")
_ALLOWED_KEYS = {"max_output_tokens", "input_tokens", "output_tokens", "cached_input_tokens", "max_tokens_env"}


def _reject_credentials(value: Any, where: str) -> None:
    """Refuse a config file that holds a credential, wherever in the tree it sits.

    The token-count path settings of a generic CLI legitimately contain "token" in
    their names, so those are allowed by exact name; everything else that looks like a
    secret by name or by shape is an error rather than a warning.
    """
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if (_CREDENTIAL_KEY.search(str(key)) and str(key) not in _ALLOWED_KEYS
                    and isinstance(inner, str) and inner):
                raise ConfigError(f"{where}.{key} looks like a credential; credentials come from the "
                                  f"environment ({ENV_PREFIX}*) or the OS keychain, never from a file")
            _reject_credentials(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            _reject_credentials(inner, f"{where}[{i}]")
    elif isinstance(value, str) and _CREDENTIAL_VALUE.match(value):
        raise ConfigError(f"{where} holds a value shaped like a credential; move it to the environment")


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and ``#`` comments ignored; optional quotes stripped."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip().removeprefix("export ").strip()] = value
    return values


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load(environ: Mapping[str, str] | None = None, *, config_path: Path | None = None,
         env_file: Path | None = None) -> Settings:
    """Settings from the environment, an optional ``.env`` and an optional TOML file."""
    real = dict(os.environ if environ is None else environ)
    env_path = env_file or (Path(real[ENV_PREFIX + "ENV_FILE"]) if real.get(ENV_PREFIX + "ENV_FILE") else Path(".env"))
    merged = {**(read_env_file(env_path) if env_path.is_file() else {}), **real}

    path = config_path or (Path(merged[ENV_PREFIX + "CONFIG"]) if merged.get(ENV_PREFIX + "CONFIG") else None)
    table: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"{ENV_PREFIX}CONFIG names {path}, which does not exist")
        table = tomllib.loads(path.read_text(encoding="utf-8"))
        _reject_credentials(table, path.name)
        if "allow_metered" in table:
            raise ConfigError(f"{path.name} sets allow_metered; that switch is only read from "
                              f"{ENV_PREFIX}ALLOW_METERED, so a copied config cannot carry it")
    base = path.parent if path is not None else Path.cwd()

    routes = {
        model_id: tuple(Rung.parse(step) for step in steps)
        for model_id, steps in table.get("routes", {}).items()
    }
    adapters: dict[str, Mapping[str, Any]] = {}
    for kind in ("claude_code", "codex"):
        if kind in table:
            adapters[kind] = table[kind]
    for kind in ("command", "keys"):
        for name, inner in table.get(kind, {}).items():
            adapters[f"{kind}.{name}"] = inner

    def where(key: str, env: str) -> Path | None:
        raw = merged.get(ENV_PREFIX + env) or table.get(key)
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else base / p

    return Settings(
        routes=routes,
        allow_metered=_flag(merged.get(ENV_PREFIX + "ALLOW_METERED")),
        max_schema_retries=int(table.get("max_schema_retries", 1)),
        cache_dir=where("cache_dir", "CACHE_DIR"),
        ledger_path=where("ledger", "LEDGER"),
        adapters=adapters,
        quotas=table.get("quota", {}),
        prices=table.get("prices", {}),
        environ=merged,
        source=str(path) if path is not None else "environment only",
    )
