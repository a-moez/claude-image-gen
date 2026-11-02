# Nano Banana (Gemini image generation) — the reference

These are **measured behaviours**, not guesses — each was paid for at least once. They were
established on the 2.5-era model through the web app and re-confirmed in part on the 3.x API; where
a finding has not been re-verified on 3.x, it says so.

## Models

| Alias | Model id | ≈ $/image (1K / 2K / 4K) |
|---|---|---|
| `flash` | `gemini-3.1-flash-image` | .067 / .101 / .151 — default workhorse |
| `pro` | `gemini-3-pro-image` | .134 / .134 / .24 — unforgiving jobs |
| `lite` | `gemini-3.1-flash-lite-image` | .034 — cheapest, 1K only |
| `legacy` | `gemini-2.5-flash-image` | **retires 2026-10-02** — do not start new work on it |

On Pro, 2K costs the same as 1K, so there is no reason to ask for 1K.

Reserve `pro` for the unforgiving jobs: apparel on a model, critical text, hero conversion shots,
ad creatives.

## Stateless, and what that implies

Every call is `prompt + 0..N references → image`. There is no session. **An edit or a swap is just a
generate with the previous version fed back as the first reference** — which is why the version
trail (`--plate` → `--v1` → `--v2`) matters: it *is* the state.

`n` runs **N separate billed calls**. The API does not honour a requested image count.

## References: the findings that cost the most to learn

- **Feed every clean angle of the SAME product.** A single flat reference leaves the model guessing
  at unseen geometry and it will invent a malformed shape. Two angles of one item preserved the form
  correctly where one did not.
- ⚠️ **A group shot is NOT a valid extra angle.** Adding a photo containing other products alongside
  a good solo reference made the product's face markedly *worse* — the model blended in features
  from the neighbours. A distant shot fails the same way. **One good reference beats a good one plus
  a bad one.**
- **Crop tight and upscale** before feeding. A tight crop reads far better than the same photo with
  the product small in frame.
- ⚠️ **A reference's camera angle and lighting get imported unless you forbid them explicitly.**
  Most product references are shot slightly from above, flat-lit against white. Ask to "replace X
  with the product from image 2" and the product comes back oversized, seen from the reference's
  angle, evenly lit, not sitting in the scene. Name both mismatches out loud — *"image 2 was
  photographed from above and lit flat against a white sheet; do NOT copy either"* — and anchor the
  light to a **named neighbouring subject already in the frame**, because abstract descriptions
  ("keep the warm sunlight") are not strong enough.
- ⛔ **Never name a target colour.** "The same warm tan as the object to its left" makes the model
  treat colour as a target and overshoot. Describe the *light*; the colour follows.
- **Reference coverage decides which product you sample.** A thin reference set does not merely
  weaken preservation — it invites the model to invent geometry (a slit that does not exist, a
  longer hem, a different material), and an owner spots invented detail on their own product instantly.
  Ask for more angles before generating; that is a zero-friction request up front and an excuse
  after a failure.

## Passes degrade the image, cumulatively

**Measured:** across a 3-pass build, centre-frame detail fell ~50% (variance-of-Laplacian 1272 →
617). Every generative pass re-renders the whole frame and softens high-frequency micro-detail —
fabric weave, crochet stitch, food texture, the exact things that prove a product is real.

**A scoped prompt limits *what changes*, not *whether the frame is re-rendered*.** A tight "replace
ONLY X, change nothing else" keeps the composition and the other products intact — genuinely
valuable — but untouched areas still come back softer.

So: **never add composition instructions to a pass whose real job is a local edit.** They buy
nothing and cost detail everywhere. In one test "gently pull back the top of the frame" did not even
change the framing — it re-cropped slightly inward while halving the texture.

The fix is not fewer passes. It is harvesting each pass for only the region it contributed and
compositing that back onto the pristine first version.

## Swaps

- **Targeted single-element replacement works well.** *"Replace ONLY \<X\> with the exact product
  from image 2 — match its size, placement, and the scene's light; change nothing else."*
- **Swap success scales with element size.** Large hero items swap faithfully; small background
  trinkets frequently come back mangled. Spend passes on heroes; remove or leave the small stuff.
- **Feed the newest version forward**, always.

## Text

- **Short, exact strings render reliably.** A wordmark, a tagline, one offer line. Quote each string
  exactly, name a simple type treatment, forbid extra words, and **verify at 100% zoom**.
- **Hero text holds; background text drifts.** Keep whatever text matters in sharp focus and let the
  rest fall into depth-of-field blur.
- **Hand-script drifts more than anything.** Preserve line breaks explicitly.
- **Arabic renders legibly** — a real differentiator, since most models garble it. A two-line Arabic
  headline plus a separate offer line came back correctly joined in one pass. Use **Latin numerals**
  inside Arabic strings (`100`, `10%`); Arabic-Indic numerals garble more. Verify with a native eye,
  and **fix typography in a design tool rather than regenerating** — re-rolling risks losing correct
  letterforms you already won.

## Prompting

- **Negation works.** "NOT 50%", "do not redraw", "never minimal" break the model's defaults. This
  coexists with Google's own advice to describe scenes positively ("an empty, deserted street"
  rather than "no cars") — use positive scene description *and* explicit preservation negatives.
- **Repetition reinforces.** An important preservation instruction can appear twice: once up front,
  once near the end.
- **Plain backgrounds mean the scene was not vivid enough.** The fix is more specific named objects,
  not more abstract style words.
- **The model has no inherent sense of scale.** Anchor it explicitly — an absolute size, a stated
  proportion to a named piece of furniture, or a metaphor ("a piece you could hold in two hands").
  Left alone it sizes the hero to dominate the frame, which makes small artisan pieces look like
  wall-spanning installations.
- **"Brighten-only" drift.** If the reference is already decent, the model tends to just clean and
  brighten it. Force elevation explicitly: "re-stage and re-light; do NOT reproduce the reference
  composition."

## Watermarks

- Every API image carries an **invisible SynthID watermark** (metadata). Nothing to fix.
- The visible four-point **corner sparkle is a WEB-APP artifact**, not an API one. Images from this
  tool are clean and never need de-sparkling. Telling the web app "no sparkle" in-prompt does not
  work — it ignores the negation.
- ⚠️ If you ever do clean a web-app image: **the sparkle's position is not constant.** It was once
  recorded as near-fixed (~0.87 across, 0.90 down); a later batch put it at ~0.95 / 0.93. A tool
  that searched only near the assumed spot and healed there anyway destroyed ~8,500 px of a
  mock-up while leaving the real sparkle untouched — and printed *"cleaned"*. Search wide, and prefer
  refusing over guessing.

## ⚠️ Gemini's aspect ratios are approximate; OpenAI's are exact

**Measured 2026-09-16.** `--aspect 4:5 --res 1K` on `gemini-3.1-flash-image` returned
**928×1152 = 0.8056:1**, not 0.8. The same request on OpenAI returned **896×1120 = 0.8000:1**,
because there the tool *computes* the size and rounds to an exact ratio.

So the two engines disagree by ~0.7% on the same nominal aspect. Visually negligible in isolation —
but if a feed is locked to 4:5 and you mix engines, frames will not be pixel-identical in shape.
When that matters (a grid mock-up, a fixed-slot layout), crop deterministically after the fact
rather than trusting either engine's framing.

## Measured on real jewelry, 2026-10-25 (`gemini-3-pro-image`, 2K, one reference each)

- **Bead-level preservation is strong.** A beaded strand kept its bead order and its irregular, worn beads. A mirrored pair kept which bead sits where on each piece, with the instruction stated twice.
- ⚠️ **Small hardware gets duplicated.** A single small tag beside a clasp came back as two, one on each side. Say "exactly ONE tag, on one side only".
- ⚠️ **Close skin on worn shots posterises.** Two of three close portraits (ear/jaw, chin) had blotchy flat patches or white specks, and one put a halo round a backlit tree. Crop below the face where you can; a re-run is not a reliable fix.
- **A crop prompt can be ignored:** "no eyes" still produced an eye in frame once.

## Still to re-verify on the 3.x API

Arabic-join reliability and multi-pass swap fidelity. Already confirmed on 3.x: no corner sparkle,
4:5 honoured natively, and Pro preservation is strong (only mild embroidered-face drift).
