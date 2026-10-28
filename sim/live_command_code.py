"""Real calls through the installed Command Code CLI: one text, one image.

    python sim/live_command_code.py --live [--model M] [--vision-model V]

Not part of ``run_all.py``: it spends real usage (about 14K input tokens for the text
call and 29K for the image call, almost all of it the CLI's own agent overhead). It
checks what the fake cannot: that the real CLI still answers in the shape the adapter
reads, and that a vision model really reads the image it is given.
"""

from __future__ import annotations

import argparse
import io
import json

from common import claim, finish, heading, request

from PIL import Image as PILImage, ImageDraw, ImageFont
from provider.cli.command_code import CommandCode
from provider.protocol import Image, with_images

parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true")
parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
parser.add_argument("--vision-model", default="deepseek/deepseek-v4.1-flash")
args = parser.parse_args()
if not args.live:
    print("not run: pass --live to spend two real Command Code calls")
    raise SystemExit(0)

adapter = CommandCode({"timeout_s": 240})
schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}

heading(f"text, {args.model}")
answer = adapter.ask(request("What is 2 + 3? Put the result in `n`.", schema=schema, max_output_tokens=400), args.model)
print(f"  text: {answer.text}")
claim("answer validates", json.loads(answer.text), json.loads(answer.text) == {"n": 5})
claim("input / cached / output tokens", f"{answer.input_tokens} / {answer.cached_input_tokens} / {answer.output_tokens}",
      answer.usage_source == "measured" and answer.input_tokens > answer.cached_input_tokens)

heading(f"image, {args.vision_model}")
im = PILImage.new("RGB", (240, 120), "white")
try:
    font = ImageFont.truetype("arial.ttf", 72)
except OSError:
    font = ImageFont.load_default()
ImageDraw.Draw(im).text((40, 20), "417", fill="black", font=font)
buffer = io.BytesIO()
im.save(buffer, "PNG")
vision = with_images(request("What number is written in the image? Put it in `n`.", schema=schema,
                             max_output_tokens=400), [Image(buffer.getvalue(), "image/png")])
answer = adapter.ask(vision, args.vision_model)
print(f"  text: {answer.text}")
claim("read the number in the image", json.loads(answer.text), json.loads(answer.text) == {"n": 417})
claim("input / output tokens", f"{answer.input_tokens} / {answer.output_tokens}", answer.usage_source == "measured")
finish("live_command_code")
