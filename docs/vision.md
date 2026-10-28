# Vision

Some of what Strata reads is only partly text: a strategy PDF with a chart of the setup,
a screenshot of an indicator, a scanned page. A vision model can read those. This module
routes requests that carry images to models that can see them, and only to those.

## A request with images

```python
from provider.protocol import Image, with_images

request = with_images(text_request, [Image(png_bytes, "image/png")])
answer = provider.complete(request)
```

`with_images` returns a `VisionRequest`: a `Request` with an `images` field, in the same
way an `Answer` is a `Completion` with accounting fields. **The `Provider` protocol is
unchanged.** Any provider accepts a `VisionRequest`, and one that cannot see images
reads the text and ignores the rest. Supported types are PNG, JPEG, WebP and GIF.

`with_images` folds the images' digests into `input_hash`, so the same question about
two different charts is two cache entries. The cache also stores the digests and checks
them on a hit, so a caller that hashed without the images gets a miss rather than
another image's answer (`sim/s10`).

## Routing

A step whose model cannot see images is **skipped without being started** when the
request carries images, and the skip is recorded (`outcome: skipped`, note `cannot see
images`) and listed in the saving report. A route with no step that can see refuses the
request and names every step. Text requests route exactly as before (`sim/s10`).

A deployment can mix both on one route, or keep a separate one:

```toml
[routes]
extraction = ["claude_code:haiku", "command_code:deepseek/deepseek-v4-flash", "codex:gpt-5-mini"]
vision = ["claude_code:haiku", "command_code:deepseek/deepseek-v4.1-flash", "claude_code:sonnet"]
```

## Which models see, and how images reach them

| Adapter | Models that see images | How the image is delivered | Measured |
| --- | --- | --- | --- |
| `claude_code` | Every Claude model | Inline: a base64 block in a stream-json user message, one turn, no tool | Live: correct, **997** input tokens |
| `command_code` | 49 of the 94 listed (`cli/command_code.py`, `VISION_MODELS`) | Written to the call's directory and `@`-mentioned; the agent reads it with its read-only `read_file` tool, one extra turn per image | Live with `deepseek/deepseek-v4.1-flash`: correct, **29,325** input tokens |
| `codex` | The GPT-5 family it runs (narrow with `vision_models`) | `--image <file>`, once per image | Fake only: Codex is not installed here |
| `command.<name>` | Those listed in `vision_models` | `image_arg` (e.g. `["--image", "{image}"]`) or `image_mention = true` | Fake |
| `keys.anthropic` | Every Claude model | Image blocks after the cached head, before the document | Fake API |

**Prefer Claude Code for images.** The two live figures above are for the same 240×120
image and the same question. Command Code reads images through its agent, so it pays
its ~14K-token agent prompt twice (once per turn), and its vision route is worth taking
for its models, not its cost.

### Command Code's vision list

The list is taken from the CLI's own `--list-models` descriptions at command-code
1.84.0: a model counts as seeing images where its description says vision, multimodal,
omni-modal or visual, and every Claude, Gemini and GPT model counts because those
families all take images. Only `deepseek/deepseek-v4.1-flash` has been checked live. A
deployment can replace the list with `vision_models` in `[command_code]`.

## What is not connected yet

**The translator does not send images.** Its gateway builds a plain `Request` from
rendered text, so today a `VisionRequest` comes only from a caller that builds one
itself. Using vision in extraction (rendering a PDF page that holds a chart and asking
about it) needs a translator change: a pass that attaches page images and folds them
into its input hash. The provider side is ready for it. See
[open issues](open-issues.md).
