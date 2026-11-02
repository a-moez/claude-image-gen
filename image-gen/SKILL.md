---
name: image-gen
description: >
  Use when generating, editing, restyling, or compositing IMAGES through a paid API — OpenAI
  GPT Image (gpt-image-2.5-flare / sunburst) or Nano Banana (Google Gemini). Covers text-to-image,
  editing an existing image, swapping a real product into a scene, multi-reference composites,
  masked inpainting, and batch runs. Also handles the money side: pricing a run before it happens,
  the spend cap, and reading back what has actually been spent. Triggers on "generate an image",
  "make a picture of", "restyle this photo", "edit this image", "swap X into this", "product shot",
  "image prompt", "nano banana", "gpt-image", "dall-e", "how much have I spent on images".
user-invocable: true
argument-hint: "[what to generate]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion
---

# Generating images

One CLI, two engines, one spend log — from any repo on this machine.

```
python "~/.claude/skills/image-gen/scripts/imagegen.py" \
    --prompt-file p.txt --ref product.jpg --model flare --aspect 4:5 -o out.png
```

On Windows, spell the path out: `C:/Users/<you>/.claude/skills/image-gen/scripts/imagegen.py`.

---

## ⛔ The money rule — read this before anything else

Real runs cost real money, and the risk is never one image, it is thirty.

**Before ANY run that is not `--dry-run`, you MUST call `AskUserQuestion`** summarising every
command (provider, model, size/quality, image count) and the **estimated total cost**, and wait for
approval. Assemble multiple generations into **one `--batch` run** so the user confirms once rather
than six times.

Two more layers back you up, and neither replaces the rule above:
- the script refuses more than `--max-images` (default 8) without `--force`
- a `PreToolUse` hook forces a harness confirmation on every real run, in every repo

`--dry-run` prints the full request and the cost estimate and spends nothing. Use it freely.

**A batch runs in parallel — 4 requests at a time by default.** That changes how long a run
takes, never what it costs, and the one approval still covers the whole batch. A Gemini `-n 4`
fans out the same way. `--parallel N` sets the width; `--parallel 1` is strictly sequential.
Only a rate-limit refusal (429) is retried, because it is never billed. If the provider keeps
throttling, lower `--parallel`. Each item prints as one block **when it finishes**, with its
elapsed time, so blocks arrive in completion order, tagged with their manifest index.

**A slow run is almost always the provider, not the batch.** OpenAI latency for the same request
has swung from 20 seconds to 8 minutes within one evening. A batch was measured side by side with
separate commands and was no slower (`reference/openai-images.md`). Before blaming the code, rerun
with `IMAGEGEN_TRACE=1`: it stamps every request as `sent` and `received`. Don't pipe a batch
into `tail`, because that hides every block until the whole run ends.

---

## Which engine

The model alias picks the provider. You rarely need `--provider`.

| Use | Model | Why |
|---|---|---|
| **Everyday high-quality generation** | `--model flare` | OpenAI's fast default (`gpt-image-2.5-flare`), ~50% lower latency than GPT Image 2 |
| **Precise editing, premium work** | `--model sunburst` | `gpt-image-2.5-sunburst` — built for editing precision; slower |
| **Cheap OpenAI** | `--model mini` | `gpt-image-1-mini` |
| **Preserving a real product exactly** | `--model pro` | Gemini `gemini-3-pro-image` — strong at keeping a real object faithful |
| **Cheap volume on Gemini** | `--model flash` / `lite` | the workhorse and the cheapest |

`--list-models` prints the full table. A full model id routes automatically by prefix
(`gpt-image-*`, `dall-e-*`, `chatgpt-image-*` → OpenAI; `gemini-*` → Gemini), so a model released
after this file was written still works without a code change.

**Pick by the job, not by habit.** OpenAI composes layouts and renders on-image text far better.
Gemini preserves a *real* product more faithfully and is usually cheaper. A piece that needs both —
a designed poster around a real product — wants a layout from one and the product truth from the
other.

---

## One vocabulary, two engines

`--aspect` and `--res` mean the same thing everywhere. Gemini takes them natively; the OpenAI
provider **solves** them into an exact pixel size (both edges multiples of 16, ratio reproduced
exactly, inside OpenAI's documented envelope).

| | `1K` | `2K` | `4K` |
|---|---|---|---|
| `--aspect 4:5` | 896x1120 | **1280x1600** | 1856x2320 |
| `--aspect 1:1` | 1024x1024 | 1456x1456 | 2048x2048 |
| `--aspect 16:9` | 1280x720 | 2048x1152 | 2816x1584 |

So a feed locked to 4:5 gets a true 4:5 from either engine. `--size 1536x864` bypasses the solver
when you want exact pixels. Anything outside 1:3–3:1 is refused, naming the constraint.

**Flags the chosen provider does not understand warn and continue** — so one batch manifest can mix
providers freely.

---

## The commands

```bash
# price it for free, first — always
imagegen.py --model flare --prompt-file p.txt -o out.png --dry-run

# generate
imagegen.py --model flare --prompt-file p.txt --aspect 4:5 --res 2K -o out.png --project acme

# edit / restyle / swap: >=1 --ref switches OpenAI to images.edit automatically
imagegen.py --model sunburst --prompt-file swap.txt \
    --ref scene-v1.png --ref real-product.jpg -o scene-v2.png

# masked inpainting (OpenAI; the mask's transparent areas are what gets edited)
imagegen.py --model sunburst --prompt-file p.txt --ref base.png --mask m.png -o out.png

# several generations, ONE approval, 4 at a time
imagegen.py --batch runs.json          # JSON array; keys mirror the flags
imagegen.py --batch runs.json --parallel 2   # narrower, if a provider throttles

# never spends
imagegen.py --usage [--project slug] [--repo name]   # totals across every repo
imagegen.py --calibrate                # measured $/image from real API usage
imagegen.py --check                    # keys + SDK versions
imagegen.py --list-models
```

**Output goes exactly where `-o` says** — resolved to an absolute path and echoed on every write, so
it is never ambiguous which repo a file landed in. A relative `-o` resolves against the current
directory, or against `--out-dir` when you pass one; an absolute `-o` ignores `--out-dir` entirely.
In a `--batch` run, `--out-dir` is the base for every item that has no `out_dir` of its own. The
order of precedence is: absolute `out` → the item's `out_dir` → `--out-dir` → the current directory.
So the same manifest can land in whatever folder the calling repo chooses.

**Every row is tagged twice**, because the log is shared across every repo on the machine:
`repo` is *where the command ran* (automatic, taken from the git root) and `--project` is *what the
spend was for* (manual). They diverge exactly when it matters — a client's asset generated from that
client's own build repo. `--usage` breaks down by both.

A batch item takes `prompt` or `prompt_file`, `out` (required), and any of `refs`, `model`,
`provider`, `n`, `aspect`, `res`, `quality`, `size`, `mask`, `background`, `thinking`,
`output_format`, `out_dir`, `project`, `overwrite`. Other CLI flags (`--model`, `--quality`,
`--project`…) do **not** carry into batch items, so set them per item; `--out-dir` is the one
run-wide exception. **Every item is validated before any item is billed**, so a typo in item 7
surfaces before items 1–6 cost anything. That includes a check that **no two items write the same
file** (counting the `-1/-2` names of an `n>1` item). Before parallel runs, that clash failed only
after the first copy had been paid for.

---

## Cost: estimated before, measured after

Gemini bills a flat rate per image, so its estimate is the final number.

**OpenAI bills tokens** ($30 per 1M output tokens), so the true cost only exists *after* the call.
The tool estimates beforehand, then reads the response's `usage`
payload and logs **what it actually cost**.

Two consequences worth knowing:
- `--quality xhigh` and `--quality max` have **no published token count**. The estimate prints
  `~$?` rather than a made-up number. The real figure is recorded after the run.
- **On GPT Image 2.5 at `low` and `medium`, the estimate uses measured rates:** output, the
  prompt's text, and every reference by its size. Back-tested on 322 real calls, it lands within
  ±15% on 81% of them. Elsewhere (`high`, older models, above 2048x1152) it falls back to the
  published output-only anchors, which ignore references.
- **`--calibrate` is the correction mechanism.** It prints measured $/image per (model, quality,
  size) from rows the API itself priced. If it disagrees with the estimate, fix
  `providers/openai_images.py`'s `MEASURED_OUT_25` / `TOKEN_ANCHORS` from that, not from a blog post.

Default quality is **`high`** — this tool exists for work where detail matters. Drop to `medium`
for drafts; it is roughly 4× cheaper.

---

## Naming outputs is the caller's job

The tool writes exactly to `-o` and never overwrites without `--overwrite`. Use a convention that
survives a multi-pass build:

```
<nn>-<slug>--<job>.png          job = reach | desire | conversion | ad
<concept>--plate.png → --v1.png → --v2.png → --v3-ad.png
```

Keep the trail. You will want to fall back to an earlier pass, and a swap that happened at v1 is
already one re-render older by v2.

---

## Getting good results

Detail lives in `reference/`. Load the one you need:

- **`reference/openai-images.md`** — models, every parameter, the size envelope, the cost model,
  when `input_fidelity` applies.
- **`reference/gemini-images.md`** — the measured Nano Banana behaviours: how references are
  interpreted, what degrades across passes, what a group shot does to a product's face, on-image
  text and Arabic.
- **`reference/architecture.md`** — ⚙️ **read this only when CHANGING the tool, not using it**: the
  module map, the provider contract and how to add a third engine, where the cost tables live, the
  invariants, and a free regression suite to paste after any edit.

Four rules that hold on both engines:

1. **Feed all clean angles of the SAME product** — extra angles give real 3D form. But **never a
   group shot as a fake extra angle**: the model blends in the neighbouring products. One good
   reference beats a good one plus a bad one. Crop each reference tight and upscale it.
2. **Concrete nouns beat abstract style.** "A brass tray with two ivory pillar candles and a sprig
   of dried olive" lands; "elegant styling" produces mush.
3. **Every full-frame pass degrades the whole image**, cumulatively, even when the prompt is scoped
   to one region. Plan to recover detail at the end rather than trying to avoid the loss.
4. **Read the output back** with the Read tool and hold it to the bar before showing anyone. You
   can see the result now — use that.

---

## Setup and troubleshooting

```bash
python "<skill>/scripts/setup_key.py"     # store keys once, for every repo
python "<skill>/scripts/imagegen.py" --check
```

Keys resolve in this order, first hit wins: **shell environment** → **the nearest `.env`, walking
up to the git root** → **`~/.config/claude-imagegen/.env`**. So a project that already has its own
`.env` keeps working, and a repo with none falls back to the machine-wide file. Keys are never
printed — only a masked tail.

| Symptom | What it means |
|---|---|
| `a network proxy refused it` | **NOT a bad key.** A sandbox (e.g. Claude Cowork) blocks the endpoint. Hand the user the exact command for their own terminal. |
| `OpenAI rejected the credential` | genuinely the key |
| `accepted the key but refused the request` | billing or model access, not the key |
| `openai 1.x is too old` | `pip install -U "openai>=2"` — 1.x predates the 2.5 models |
| `not installed for this interpreter` | a repo's own venv is active; the error prints the exact `pip install -r` line |

⚠️ **This area of the API moves fast** — GPT Image 2.5 shipped 2026-09-08. If a model name is
rejected, check live docs at `developers.openai.com` rather than assuming the table here is current.
Unknown parameters are forwarded via `extra_body`, so the tool does not have to wait for the SDK to
catch up.
