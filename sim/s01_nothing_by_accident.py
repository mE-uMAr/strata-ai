"""Step 1: nothing reaches a model by accident.

Measures, with no network and no subscription:

- an empty environment assembles into the translator's own `RefusingProvider`, which
  raises on the first call;
- attached to the translator's real gateway, that refusal comes back as a logged call
  record with outcome ``provider_error`` and zero spend;
- a config file holding a credential, or holding the metered switch, is refused on load;
- a route for one ``model_id`` does not answer for another, because there is no
  implicit fallback route.
"""

from __future__ import annotations

from common import TRANSLATOR, claim, finish, heading, request, scratch

from provider import ConfigError, RefusingProvider, from_environment, load
from provider.protocol import ProviderError
from translator.extraction.models import TriageReport
from translator.gateway.config import GatewayConfig, build

heading("an empty environment")
provider = from_environment({})
claim("assembled provider type", type(provider).__name__, isinstance(provider, RefusingProvider))
try:
    provider.complete(request())
    claim("first call raised", "no", False)
except ProviderError as e:
    claim("first call raised ProviderError", type(e).__name__, True)

heading("attached to the translator's own gateway")
gateway = build(GatewayConfig(prompts=TRANSLATOR / "prompts"), provider=from_environment({}))
try:
    gateway.call(prompt="triage", variables={"document": "Buy when RSI < 30."}, schema=TriageReport,
                 tenant="sim", pass_name="triage")
    claim("gateway call raised", "no", False)
except ProviderError as e:
    record = e.record
    claim("call record outcome", record.outcome, record.outcome == "provider_error")
    claim("call record cost_usd", record.usage.cost_usd, record.usage.cost_usd == 0.0)
    claim("tenant spend after the refusal (calls)", gateway.spent("sim").calls, gateway.spent("sim").calls == 0)

heading("a config file is not a place for credentials")
root = scratch("s01")
cases = {
    "named api_key": '[keys.anthropic]\napi_key = "abc"\n',
    "token-shaped value": '[command.tool]\nbinary = ["tool", "sk-ant-abc123"]\n',
    "metered switch in the file": 'allow_metered = true\n',
}
for label, text in cases.items():
    path = root / f"{label.replace(' ', '_')}.toml"
    path.write_text(text, encoding="utf-8")
    try:
        load({}, config_path=path)
        claim(f"refused: {label}", "loaded", False)
    except ConfigError as e:
        claim(f"refused: {label}", "ConfigError", True)
        print(f"      {e}")

heading("no implicit fallback route")
path = root / "routes.toml"
path.write_text('[routes]\nextraction = ["claude_code:haiku"]\n', encoding="utf-8")
settings = load({}, config_path=path)
claim("routes declared", sorted(settings.routes), sorted(settings.routes) == ["extraction"])
claim("route for the gateway's default model_id 'unattached'", "unattached" in settings.routes,
      "unattached" not in settings.routes)
claim("metered allowed without AIPROVIDER_ALLOW_METERED", settings.allow_metered, settings.allow_metered is False)

finish("s01")
