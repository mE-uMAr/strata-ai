"""The generic CLI adapter: a command-line tool described entirely by configuration.

Adding a CLI tool to Strata is a config entry, not a module. A ``[command.<name>]``
table in the TOML file says how to run the tool and where its answer is::

    [command.gemini]
    binary = ["gemini"]
    argv = ["--model", "{model}", "--output-format", "json"]
    prompt_via = "stdin"                       # or "argv", with {prompt} in argv
    output = "json"                            # "json", "jsonl" or "text"
    answer = "response"                        # where the answer text is
    input_tokens = "stats.models.*.tokens.prompt"
    output_tokens = "stats.models.*.tokens.candidates"
    cached_input_tokens = "stats.models.*.tokens.cached"
    error = "error.message"
    price_as = "gemini-2.5-flash"              # for the metered-equivalent figure
    models = { extraction = "gemini-2.5-flash" }

Placeholders in ``argv``: ``{model}``, ``{prompt}`` (argv mode only),
``{max_output_tokens}``, ``{workdir}`` (the call's empty temporary directory) and
``{schema_file}`` (the request's schema, written into that directory).

**Path expressions** pick values out of the tool's output. Segments are separated by
dots; ``*`` fans out over a list or an object's values; an integer indexes a list
(``-1`` is the last element); ``[key=value]`` keeps the list elements whose ``key``
equals ``value``. For ``jsonl`` output the root is the list of parsed lines, so
``[type=turn.completed].usage.input_tokens`` reads the usage event. The answer is the
last string matched (an object or list is re-serialised as JSON); a token count is the
sum of every number matched, so a wildcard over several models adds them up.

Where a token count has no path, or the path matches nothing, the count is estimated
from text length and the answer is marked ``usage_source="estimated"``. A count is
never reported as zero to mean "unknown".

Every tool that is not Claude Code or Codex goes through here. Those two have their own
modules only because they report more than a path expression can describe cleanly
(Claude Code's API-equivalent cost; Codex's event stream); they do not import this one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ..accounting import price_for
from ..protocol import Adapter, Answer, Malformed, ProviderError, QuotaExhausted, Request, Unavailable, estimate_tokens
from ..shaping import with_contract
from . import _process

DEFAULT_LIMIT_PATTERNS = ("usage limit", "rate limit", "quota", "limit reached", "too many requests", "429")
DEFAULT_UNAVAILABLE_PATTERNS = ("not logged in", "please log in", "please login", "unauthorized", "authenticate")


@dataclass(frozen=True)
class CommandSpec:
    name: str
    binary: tuple[str, ...]
    argv: tuple[str, ...] = ()
    prompt_via: Literal["stdin", "argv"] = "stdin"
    output: Literal["json", "jsonl", "text"] = "json"
    answer: str | None = None
    input_tokens: str | None = None
    output_tokens: str | None = None
    cached_input_tokens: str | None = None
    error: str | None = None
    limit_patterns: tuple[str, ...] = DEFAULT_LIMIT_PATTERNS
    unavailable_patterns: tuple[str, ...] = DEFAULT_UNAVAILABLE_PATTERNS
    timeout_s: float = 300.0
    path: Literal["subscription", "metered", "local"] = "subscription"
    """What a call through this tool costs. A tool that bills per token must say
    ``metered``, which puts it behind ``AIPROVIDER_ALLOW_METERED`` like any key."""
    quota_units: float = 1.0
    price_as: str | None = None
    models: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    """Static, non-secret variables. Checked by the config loader like the rest."""
    pass_env: tuple[str, ...] = ()
    """Variables to pass through although they look like credentials. Each is named."""
    max_tokens_env: str | None = None
    """An environment variable the tool reads as its output ceiling, if it has one."""
    output_contract: bool = True
    """Append the JSON-only instruction and the compact schema. Turn off only for a
    tool given the schema some other way (``{schema_file}``)."""

    @classmethod
    def from_table(cls, name: str, table: Mapping[str, Any]) -> CommandSpec:
        known = set(cls.__dataclass_fields__) - {"name"}
        unknown = set(table) - known
        if unknown:
            raise ValueError(f"command.{name}: unknown settings {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key, value in table.items():
            values[key] = tuple(value) if isinstance(value, list) else value
        if "binary" not in values:
            raise ValueError(f"command.{name}: 'binary' is required")
        if values.get("prompt_via") == "argv" and not any("{prompt}" in a for a in values.get("argv", ())):
            raise ValueError(f"command.{name}: prompt_via = 'argv' needs a {{prompt}} placeholder in argv")
        return cls(name=name, **values)


# -- path expressions -------------------------------------------------------

_SEGMENT = re.compile(r"\[([^\]=]+)=([^\]]*)\]|([^.\[\]]+)")


def _same(value: object, text: str) -> bool:
    return (value if isinstance(value, str) else json.dumps(value)) == text


def select(document: object, expression: str) -> list[object]:
    current = [document]
    for m in _SEGMENT.finditer(expression):
        key, wanted, name = m.groups()
        found: list[object] = []
        for node in current:
            if key is not None:
                if isinstance(node, list):
                    found += [x for x in node if isinstance(x, dict) and key in x and _same(x[key], wanted)]
            elif name == "*":
                if isinstance(node, list):
                    found += node
                elif isinstance(node, dict):
                    found += list(node.values())
            elif isinstance(node, list) and re.fullmatch(r"-?\d+", name):
                i = int(name)
                if -len(node) <= i < len(node):
                    found.append(node[i])
            elif isinstance(node, dict) and name in node:
                found.append(node[name])
        current = found
    return current


def _text_at(document: object, expression: str) -> str | None:
    for value in reversed(select(document, expression)):
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
    return None


def _count_at(document: object, expression: str | None) -> int | None:
    if expression is None:
        return None
    numbers = [v for v in select(document, expression) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return int(sum(numbers)) if numbers else None


def _parse(output: str, kind: str) -> object:
    if kind == "json":
        return json.loads(output)
    if kind == "jsonl":
        lines = []
        for line in output.splitlines():
            line = line.strip()
            if line.startswith(("{", "[")):
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return lines
    return output


# -- the adapter ------------------------------------------------------------

class CommandAdapter(Adapter):
    def __init__(self, spec: CommandSpec, *, environ: Mapping[str, str] | None = None,
                 prices: Mapping[str, Mapping[str, float]] | None = None) -> None:
        self.spec = spec
        self.name = f"command.{spec.name}"
        self.path = spec.path
        self.models = dict(spec.models)
        self._environ = environ
        self._prices = dict(prices or {})

    def ask(self, request: Request, model: str, note: str = "") -> Answer:
        spec = self.spec
        prompt = (with_contract(request.prompt, request.schema) if spec.output_contract else request.prompt) + note
        executable = _process.resolve(spec.binary)
        if spec.prompt_via == "argv" and _process.is_shim(executable[0]):
            raise ProviderError(f"{self.name}: refusing to put the prompt on the argv of a cmd.exe shim; "
                                "set prompt_via = 'stdin'")

        def argv(workdir) -> list[str]:
            # Configured values first, the prompt last: text inside the prompt that
            # happens to look like a placeholder must reach the tool unchanged.
            values = {"{model}": model, "{max_output_tokens}": str(request.max_output_tokens),
                      "{workdir}": str(workdir), "{schema_file}": os.path.join(workdir, "schema.json")}
            out = []
            for arg in spec.argv:
                for key, value in values.items():
                    arg = arg.replace(key, value)
                out.append(arg.replace("{prompt}", prompt))
            return [*executable, *out]

        extra = dict(spec.env)
        if spec.max_tokens_env:
            extra[spec.max_tokens_env] = str(request.max_output_tokens)
        env = _process.child_env(self._environ, extra=extra, keep=spec.pass_env)

        result = _process.run(argv,
                              stdin_text=prompt if spec.prompt_via == "stdin" else None,
                              timeout_s=spec.timeout_s, env=env,
                              files={"schema.json": json.dumps(request.schema)})
        return self._read(request, model, prompt, result)

    def _read(self, request: Request, model: str, prompt: str, result: _process.Run) -> Answer:
        spec = self.spec
        try:
            document = _parse(result.stdout, spec.output)
        except json.JSONDecodeError:
            document = None
        message = _text_at(document, spec.error) if (spec.error and document is not None) else None
        if result.returncode != 0 or message:
            detail = message or ""
            if _process.matches(spec.limit_patterns, detail, result.stderr, result.stdout):
                raise QuotaExhausted(_process.describe_failure(self.name, result, detail))
            if _process.matches(spec.unavailable_patterns, detail, result.stderr):
                raise Unavailable(_process.describe_failure(self.name, result, detail))
            raise ProviderError(_process.describe_failure(self.name, result, detail))
        if document is None:
            raise ProviderError(_process.describe_failure(self.name, result, f"output is not valid {spec.output}"))

        text = document if spec.output == "text" else (_text_at(document, spec.answer) if spec.answer else None)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(_process.describe_failure(
                self.name, result, f"no answer at {spec.answer!r} in the tool's output"))
        text = text.strip()

        measured_in = _count_at(document, spec.input_tokens) if spec.output != "text" else None
        measured_out = _count_at(document, spec.output_tokens) if spec.output != "text" else None
        cached = (_count_at(document, spec.cached_input_tokens) if spec.output != "text" else None) or 0
        input_tokens = measured_in if measured_in is not None else estimate_tokens(prompt)
        output_tokens = measured_out if measured_out is not None else estimate_tokens(text)
        source = "measured" if measured_in is not None and measured_out is not None else "estimated"

        price = price_for(spec.price_as or model, self._prices)
        equivalent = price.cost(input_tokens=input_tokens, output_tokens=output_tokens,
                                cache_read_tokens=cached) if price else 0.0
        answer = Answer(
            text=text, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=result.latency_ms,
            cost_usd=equivalent if spec.path == "metered" else 0.0,
            adapter=self.name, model=model, path=spec.path, usage_source=source, cached_input_tokens=cached,
            quota_units=spec.quota_units if spec.path == "subscription" else 0.0,
            metered_equivalent_usd=equivalent,
        )
        if measured_out is not None and measured_out > request.max_output_tokens:
            raise Malformed(f"{self.name} produced {measured_out} output tokens, over the ceiling of "
                            f"{request.max_output_tokens}", spent=answer)
        return answer
