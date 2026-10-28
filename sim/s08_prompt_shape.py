"""Step 8: what this module adds to a prompt, measured on the translator's real templates.

Measures:

- the caller's prompt is an unchanged prefix of what is sent, so a vendor prompt cache
  keyed on the template still matches;
- the stable head split off for a cache breakpoint holds the template's instructions
  and none of the document;
- the compact schema against an indented one, per translator output schema, in
  estimated tokens (``len // 4``);
- the system prompt this module sends in place of a coding CLI's own;
- the shipped example config loads, holds no credential, names no metered step, and
  assembles into a router without starting anything.
"""

from __future__ import annotations

import json

from common import ROOT, TRANSLATOR, claim, finish, heading

from provider import assemble, load
from provider.protocol import estimate_tokens
from provider.shaping import SYSTEM, compact_schema, split_stable, with_contract
from translator.extraction import models as extraction_models
from translator.gateway.prompts import PromptLibrary
from translator.ir.strategy import StrategyIR


def output_schema(name: str) -> dict:
    # Templates name their schema; most live in extraction.models, the IR in ir.strategy.
    model = StrategyIR if name == "StrategyIR" else getattr(extraction_models, name)
    return model.model_json_schema()


library = PromptLibrary(TRANSLATOR / "prompts")

heading("the caller's prompt stays a prefix; the head holds no document")
document = "UNIQUE-DOCUMENT-TEXT: buy when the 9 EMA crosses the 21 EMA."
for name in library.names():
    template = library.get(name)
    variables = {v: (document if v in template.data_variables else f"<{v}>") for v in template.placeholders}
    rendered = library.render(template, variables, nonce=template.template_hash[7:19])
    schema = output_schema(template.output_schema)
    sent = with_contract(rendered, schema)
    head, tail = split_stable(rendered)
    ok = sent.startswith(rendered) and "UNIQUE-DOCUMENT-TEXT" not in head
    claim(f"{name}: prefix kept / head tokens / document in head", f"{sent.startswith(rendered)} / "
          f"{estimate_tokens(head)} / {'UNIQUE-DOCUMENT-TEXT' in head}", ok)

heading("the schema, compact against indented (estimated tokens)")
total_indented = total_compact = 0
for name in library.names():
    template = library.get(name)
    schema = output_schema(template.output_schema)
    indented = estimate_tokens(json.dumps(schema, indent=2))
    compact = estimate_tokens(compact_schema(schema))
    total_indented += indented
    total_compact += compact
    claim(f"{template.output_schema}", f"{indented} -> {compact} ({1 - compact / indented:.0%} fewer)", compact < indented)
claim("all templates", f"{total_indented} -> {total_compact} ({1 - total_compact / total_indented:.0%} fewer)",
      total_compact < total_indented)

heading("the system prompt sent to a CLI that accepts one")
claim("SYSTEM, estimated tokens", estimate_tokens(SYSTEM), estimate_tokens(SYSTEM) < 100)

heading("the example config")
settings = load({}, config_path=ROOT / "aiprovider.example.toml")
claim("routes", {k: [str(s) for s in v] for k, v in settings.routes.items()}, "extraction" in settings.routes)
claim("metered steps named", [str(s) for v in settings.routes.values() for s in v if s.adapter.startswith("keys.")],
      not any(s.adapter.startswith("keys.") for v in settings.routes.values() for s in v))
assembly = assemble(settings)
claim("assembles into", type(assembly.provider).__name__, type(assembly.provider).__name__ == "ReplayCache")

finish("s08")
