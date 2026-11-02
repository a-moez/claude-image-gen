# OpenAI GPT Image — the reference

Verified against `developers.openai.com` on **2026-09-16**. This area moves fast: GPT Image 2.5
shipped **2026-09-08**, eight days before this was written. When a model name is rejected, check
live docs rather than assuming this page is current.

## Models

| Alias | Model id | Use |
|---|---|---|
| `flare` | `gpt-image-2.5-flare` | **Default.** Fast, high quality; ~50% lower latency than GPT Image 2 |
| `sunburst` | `gpt-image-2.5-sunburst` | Editing precision, premium creative work; slower |
| `gpt2` | `gpt-image-2` | Previous generation (also `gpt-image-2-2026-04-21`) |
| `gpt15` | `gpt-image-1.5` | Older; slightly higher output rate ($32/1M) |
| `mini` | `gpt-image-1-mini` | Cheapest. No `input_fidelity` support |

`chatgpt-image-latest`, `gpt-image-1` and `dall-e-2`/`dall-e-3` also route by prefix. Avoid the
DALL·E models — different parameter set, no `usage` payload.

## The two entry points

`images.generate()` with no references; `images.edit()` with one or more. **The tool switches
automatically** the moment you pass a `--ref`.

`edit()` takes **up to 16 images** (png / webp / jpg, each under 50MB). An optional `mask` is a PNG
with an alpha channel, the same dimensions as the image; its **fully transparent areas are what gets
edited**, and it applies to the **first** image only.

Both return **`b64_json` only** for GPT image models — there is no URL to fetch.

## Parameters

| Parameter | Values | Notes |
|---|---|---|
| `size` | `1024x1024`, `1536x1024`, `1024x1536`, or custom `WxH` | Both edges multiples of 16; ratio 1:3–3:1; max edge 3840; total 655,360–8,294,400 px. **Above 2560x1440 is experimental.** |
| `quality` | `low` `medium` `high` `xhigh` `max` `auto` | `xhigh`/`max` arrived with the 2.5 family; older models reject them |
| `output_format` | `png` (default) `jpeg` `webp` | |
| `output_compression` | 0–100 | jpeg/webp only |
| `background` | `transparent` `opaque` `auto` | transparent requires png or webp output |
| `moderation` | `auto` (default) `low` | |
| `n` | 1–10 | Native — one call returns n images, unlike Gemini |
| `input_fidelity` | `high` `low` | **`edit()` only**, and **only on `gpt-image-1` / `gpt-image-1.5`.** GPT Image 2 and later always process inputs at high fidelity; passing it is an error there |
| `partial_images` | 0–3 | streaming previews; unused by this tool |

## Cost — the part that differs from Gemini

Billing is **token-based**, so the true cost of a call only exists after it returns.

**Rates ($ per 1M tokens):**

| Model | Text in | Image in | Output |
|---|---|---|---|
| 2.5-flare / 2.5-sunburst / gpt-image-2 | 5.00 | 8.00 | 30.00 |
| gpt-image-1.5 | 5.00 | 8.00 | 32.00 |
| gpt-image-1-mini | 2.00 | 2.50 | 8.00 |

**Output tokens for a 1024x1024 image**, which scale linearly with pixel count:

| quality | tokens | ≈ $ at 1024² (30/1M) |
|---|---|---|
| low | 272 | 0.008 |
| medium | 1,056 | 0.032 |
| high | 4,160 | 0.125 |
| xhigh | **no published figure** | ? |
| max | **no published figure** | ? |

A 1024x1536 image is 1.5× the pixels and costs about 1.5× — which is exactly the linear scaling the
tool applies.

**The response carries a `usage` object** (`input_tokens`, `output_tokens`, `total_tokens`, plus an
`input_tokens_details` breakdown of `text_tokens` / `image_tokens`). That is the authoritative
number, and it is what lands in the spend log. `imagegen.py --calibrate` reports measured $/image
back out — correct `TOKEN_ANCHORS` in `providers/openai_images.py` from that, never from a table
someone published.

### ⭐ Measured on gpt-image-2.5-flare, 2026-09-16 (first real runs)

Three findings, each paid for once:

**1. `low` costs far less on 2.5 than the published 1.x anchor.** A 896x1120 image returned
**157 output tokens** — normalised to 1024², **164**, against the table's **272**. The estimate is
therefore ~66% high for this tier. It errs in the safe direction (you approve more than you pay), so
the anchors are left alone rather than corrected from a single point: `medium` and `high` are still
**unmeasured on 2.5** and extrapolating one tier across three is exactly the invented number this
tool refuses to print. Two more images would settle it.

*Update 2026-11-02:* settled for `low` and `medium` from 322 logged calls. Output on 2.5 is near-flat
with size (`low` 157–211, `medium` 366–462 tokens per image), so the estimate now uses those measured
figures (`MEASURED_OUT_25` in `providers/openai_images.py`). `high` is still unmeasured on 2.5.

**2. ⚠️ References dominate the cost of an edit — they are not a rounding error.** Two 1440×1440
references added **3,042 image input tokens = $0.0243**, against $0.0047 of output. That is **82% of
the call**, and about **5× what the image itself cost**:

| call | output tok | input tok | $ |
|---|---|---|---|
| generate, 0 refs | 157 | 67 (text) | **0.0050** |
| edit, 2 refs | 157 | 3,149 (107 text + 3,042 image) | **0.0296** |

So ≈ **1,521 tokens ≈ $0.012 per reference** at that size. The "feed every clean angle" rule still
holds for quality, but on OpenAI each extra angle has a real price. Crop references tight; it cuts
tokens as well as improving fidelity.

*Update 2026-11-02:* the pre-call estimate now counts references. Across the log, each reference costs
a step by its size, and the steps add up: **768 tokens at ~0.5 MP, 1,032 at ~1 MP, 1,452–1,518 from
~2 MP up** (three refs = 1452 + 1452 + 768 = 3,672, exactly). Bigger than ~2 MP buys nothing more, so
downsizing a 12 MP phone photo saves upload time, not tokens. With `n>1`, the inputs are billed once
per image (an `n=2` call logged 650 text tokens for a ~325-token prompt).

**3. Custom solved sizes are accepted.** `--aspect 4:5 --res 1K` → `896x1120` was returned exactly.
The universal-vocabulary design is confirmed against the live API, not just the docs.

### Measured against Gemini Pro on a real product, 2026-10-25 (a beaded necklace)

Same prompt, same single reference: a handmade beaded necklace with irregular stones.

- **Sunburst `low`, 1K ($0.0196 actual; the estimate said $0.0078, and the reference was 1,452 of the 2,267 tokens):** right layout, but faceted beads came back smooth and round, and irregular stones came back tidied. It looked like a generated piece, not the real piece.
- **Sunburst `medium`, 2K, designed ad with three text strings ($0.0279 actual):** much closer to the real piece, and **all three strings exact, including a two-line Arabic headline**. Still slightly beautified, so the product needs a swap or a disclosure before it goes out as the brand's own.
- **Verdict:** `low` is fine for layout drafts, not for judging product fidelity. For a real product, generate with Gemini Pro and use Sunburst for the designed layout + text.

### Latency is the provider's, and it swings by minutes, 2026-10-27/29

The same 4 Sunburst `low` 1152x2048 transparent prompts were timed with `IMAGEGEN_TRACE=1`:

| When | How | Per-request time (sent → received) |
|---|---|---|
| 27 Oct, ~17:16–18:00 UTC | `--batch` | 21s · 29s · **2m18s · 2m54s**, and a 2-item batch with one call at **~7m** |
| 27 Oct, 18:04 UTC | 4 separate commands | 22–33s |
| 29 Oct, 12:37 UTC | `--batch` | 17–19s (all 4 sent within 5ms) |
| 29 Oct, 12:38 UTC | 4 separate commands | 18–22s |
| 29 Oct, 12:39 UTC | `--batch` | 15–20s |

**The batch is not slower than separate commands.** Measured side by side in the same minute, it was
marginally faster. The 27 Oct stalls happened while requests sat at OpenAI. They came in a window of
about 45 minutes and had cleared before the separate-command run, which is why that run *looked*
like the fix. A 2-hour gap is enough for this to swing from 20s to several minutes. Before changing
code over a slow run, rerun with `IMAGEGEN_TRACE=1`. If "sent" is immediate and "received" is late,
the wait is the provider's.

## When the SDK lags the API

The typed Python SDK trails the API by weeks. `providers/openai_images.py` inspects the installed
method signature and routes anything it does not recognise into `extra_body`, which the SDK forwards
verbatim. So a parameter introduced tomorrow works today, and there is no need to pin behaviour to
whatever version happens to be installed.

`openai>=2` is still required — 1.x predates the 2.5 models entirely.

## What OpenAI is good and bad at

**Good:** composing a designed layout; rendering on-image text, including multi-line non-Latin;
following a long, structured art-direction prompt.

**Bad, and this matters:** it **beautifies away from the real product**. Output is prettier than the
actual item — a maker recognises the difference instantly. So for a real brand, let it design the
*layout* with a fabricated stand-in, then swap the genuine product in afterwards (a Gemini scoped
swap, or a further `edit()` fed the real reference) and verify fidelity against the reference rather
than from memory.
