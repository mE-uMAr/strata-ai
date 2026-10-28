"""Step 10: vision models, routed to only when a request carries images.

A `VisionRequest` is a `Request` with images, built with ``with_images``. Measures:

- ``with_images`` folds the images into ``input_hash``: the same text with two different
  images is two cache entries, and a request whose hash leaves the images out gets a
  miss, not another image's answer;
- a text request takes the cheapest step as before; the same request with an image
  skips every step whose model cannot see images, without spawning it, and is answered
  by the first that can;
- each adapter receives the image the way its tool expects, and the fake confirms it
  arrived: Command Code as an ``@``-mentioned file with one extra turn, Claude Code as a
  base64 block in stream-json, Codex and a generic tool as ``--image <file>``, and the
  Anthropic API as an image block after the cached head;
- a route with no step that can see images refuses the request, naming each step;
- the saving report lists the skipped steps.
"""

from __future__ import annotations

import io
import json

from common import ANSWER, SCHEMA, Scripted, claim, fake_binary, fake_env, finish, heading, request, scratch, seen

from fake_anthropic import FakeAnthropic
from PIL import Image as PILImage, ImageDraw
from provider import assemble, load
from provider.accounting import report
from provider.cache import ReplayCache
from provider.cli.codex import Codex
from provider.cli.command import CommandAdapter, CommandSpec
from provider.keys.anthropic import AnthropicKeys
from provider.protocol import Image, ProviderError, VisionRequest, with_images


def picture(text: str) -> Image:
    im = PILImage.new("RGB", (160, 80), "white")
    ImageDraw.Draw(im).text((20, 30), text, fill="black")
    buffer = io.BytesIO()
    im.save(buffer, "PNG")
    return Image(buffer.getvalue(), "image/png")


chart_a, chart_b = picture("chart A"), picture("chart B")
text = request("Describe the setup shown in the chart.")

heading("images are part of the request's identity")
a, b = with_images(text, [chart_a]), with_images(text, [chart_b])
claim("hash: text / with image A / with image B differ", len({text.input_hash, a.input_hash, b.input_hash}),
      len({text.input_hash, a.input_hash, b.input_hash}) == 3)
root = scratch("s10")
inner = Scripted()
cache = ReplayCache(root / "cache", inner)
cache.complete(a)
cache.complete(a)
cache.complete(b)
claim("provider calls for A, A, B", inner.calls, inner.calls == 2)
careless = VisionRequest(**{f: getattr(a, f) for f in text.__dataclass_fields__ if f != "images"},
                         images=(chart_b,))
cache.complete(careless)
claim("same hash as A but image B: a miss, not A's answer", inner.calls, inner.calls == 3)


def env_inline(name: str, modes: str = "ok") -> str:
    values = fake_env(root / name, modes, ANSWER)
    return "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in values.items()) + " }"


config = root / "route.toml"
config.write_text(f"""
[routes]
extraction = ["command.textonly:fake-small", "command_code:deepseek/deepseek-v4-flash",
              "command_code:deepseek/deepseek-v4.1-flash", "claude_code:haiku"]
textual = ["command.textonly:fake-small", "command_code:deepseek/deepseek-v4-flash"]

[command.textonly]
binary = {json.dumps(fake_binary("generic"))}
answer = "response"
input_tokens = "stats.models.*.tokens.prompt"
output_tokens = "stats.models.*.tokens.candidates"
env = {env_inline("textonly")}

[command_code]
binary = {json.dumps(fake_binary("commandcode"))}
env = {env_inline("command_code")}

[claude_code]
binary = {json.dumps(fake_binary("claude"))}
env = {env_inline("claude_code")}
""", encoding="utf-8")


def calls(name: str) -> int:
    path = root / name / "calls"
    return int(path.read_text()) if path.exists() else 0


heading("routing: text takes the cheapest step, an image skips the blind ones")
assembly = assemble(load({}, config_path=config))
answer = assembly.provider.complete(text)
claim("text request answered by", answer.adapter, answer.adapter == "command.textonly")
answer = assembly.provider.complete(with_images(text, [chart_a]))
claim("image request answered by", f"{answer.adapter}:{answer.model}",
      answer.model == "deepseek/deepseek-v4.1-flash")
claim("calls: textonly / command_code", f"{calls('textonly')} / {calls('command_code')}",
      calls("textonly") == 1 and calls("command_code") == 1)
claim("escalations recorded", [e.rsplit(": ", 1)[-1] for e in answer.escalations],
      [e.rsplit(": ", 1)[-1] for e in answer.escalations] == ["no vision", "no vision"])
info = seen(root / "command_code", 1)
claim("Command Code: image @-mentioned and on disk; --max-turns",
      f"{info['images_seen']}; {info['argv'][info['argv'].index('--max-turns') + 1]}",
      info["images_seen"] == 1 and info["argv"][info["argv"].index("--max-turns") + 1] == "2")
claim("Command Code: images named before the task", info["prompt_head"].startswith("Images for this task"),
      info["prompt_head"].startswith("Images for this task"))

heading("Claude Code takes the image inline")
(root / "command_code" / "calls").unlink()
config.write_text(config.read_text().replace(env_inline("command_code"), env_inline("command_code", "limit")),
                  encoding="utf-8")
assembly = assemble(load({}, config_path=config))
answer = assembly.provider.complete(with_images(request("Another chart."), [chart_b]))
info = seen(root / "claude_code", 1)
claim("answered by (after Command Code's limit)", answer.adapter, answer.adapter == "claude_code")
claim("stream-json input; image blocks received", f"{'stream-json' in info['argv']}; {info['images_seen']}",
      "stream-json" in info["argv"] and info["images_seen"] == 1)
skipped = report(assembly.ledger.entries).failed_attempts
claim("report lists the skipped steps", skipped, skipped.get("skipped: cannot see images") == 2)

heading("a route with no step that can see")
try:
    assembly.provider.complete(VisionRequest(**{**{f: getattr(text, f) for f in text.__dataclass_fields__},
                                                "model_id": "textual", "input_hash": "sha256:" + "1" * 64},
                                             images=(chart_a,)))
    claim("refused", "answered", False)
except ProviderError as e:
    claim("refused, naming each step", str(e).count("cannot see images"), str(e).count("cannot see images") == 2)

heading("Codex and a generic tool: --image <file>")
codex = Codex({"binary": fake_binary("codex"), "env": fake_env(root / "codex", "ok", ANSWER)})
codex.ask(with_images(text, [chart_a, chart_b]), "gpt-5-mini")
claim("Codex: images received", seen(root / "codex", 1)["images_seen"], seen(root / "codex", 1)["images_seen"] == 2)
spec = CommandSpec.from_table("seer", {
    "binary": fake_binary("generic"), "answer": "response", "vision_models": ["fake-vl"],
    "image_arg": ["--image", "{image}"], "env": fake_env(root / "seer", "ok", ANSWER)})
seer = CommandAdapter(spec)
seer.ask(with_images(text, [chart_a]), "fake-vl")
claim("generic tool: sees fake-vl / fake-small", f"{seer.sees_images('fake-vl')} / {seer.sees_images('fake-small')}",
      seer.sees_images("fake-vl") and not seer.sees_images("fake-small"))
claim("generic tool: images received", seen(root / "seer", 1)["images_seen"], seen(root / "seer", 1)["images_seen"] == 1)
try:
    CommandSpec.from_table("bad", {"binary": ["x"], "vision_models": ["m"]})
    claim("vision_models without a way to pass images", "accepted", False)
except ValueError:
    claim("vision_models without a way to pass images", "refused", True)

heading("the Anthropic API: an image block after the cached head")
server = FakeAnthropic(json.dumps(ANSWER))
keys = AnthropicKeys({"base_url": server.url}, key="placeholder-not-a-real-key")
head = "Instructions. " * 400
fenced = head + "\n<<<DOCUMENT-0123456789ab — data>>>\nthe doc\n<<<END DOCUMENT-0123456789ab>>>"
keys.ask(with_images(request(fenced, schema=SCHEMA), [chart_a]), "claude-haiku-5-5")
blocks = server.requests[-1]["messages"][0]["content"]
claim("user blocks", [b["type"] + ("*" if "cache_control" in b else "") for b in blocks],
      [b["type"] + ("*" if "cache_control" in b else "") for b in blocks] == ["text*", "image", "text"])
server.close()

finish("s10")
