# image-gen — how it's built

**Read this when you are changing the tool, not using it.** To *use* it, `SKILL.md` is enough; for
per-engine craft, `openai-images.md` / `gemini-images.md`. This file exists so that a session told
*"add a third provider"* or *"the cost numbers look wrong"* does not have to reverse-engineer twelve
files first.

Every script also carries its own `IF YOU CHANGE THIS FILE` block in its header docstring. Those hold
the local traps; this holds the shape.

---

## Module map

```
scripts/
├── imagegen.py           the CLI. Parses flags, routes the no-spend modes, hands a dict to core.batch.
│                         Knows NO provider names except in --provider's choices.
├── setup_key.py          one-time interactive key writer (getpass → ~/.config/claude-imagegen/.env)
├── spend_gate.py         the PreToolUse hook. Standalone: imports nothing from core/.
├── core/
│   ├── request.py        GenRequest / GenResult — the vocabulary both providers speak. No logic.
│   ├── config.py         paths · key-resolution chain · find_git_root · the provider REGISTRY + router
│   ├── io_utils.py       prompts in, references checked, output paths resolved, image bytes out
│   ├── spend.py          the cap · the global log (locked) · --usage · --calibrate
│   ├── concurrency.py    the process-wide request semaphore · per-thread stdout · 429-only retry
│   │                     · IMAGEGEN_TRACE=1 sent/received stamps per HTTP call
│   └── batch.py          build → preview → run (up to --parallel at once) → log. ONE path for
│                         both a single call and a manifest.
└── providers/
    ├── base.py           the ImageProvider contract + the three error types
    ├── gemini.py         Nano Banana. Flat per-image pricing.
    └── openai_images.py  GPT Image. Token pricing + the (aspect,res) → WxH solver.
```

**The dependency rule:** `core/` may not import from `providers/` except through
`config.load_provider()`, and `providers/` may not import each other. If you find yourself wanting an
`if provider.name == "openai"` in `core/`, the contract is missing a method — add it to `base.py`
instead.

---

## The provider contract

A provider is class attributes for routing plus four methods. From `providers/base.py`:

| Attribute | Purpose |
|---|---|
| `name` | registry key, and what lands in the log |
| `aliases` | `{"flare": "gpt-image-2.5-flare"}` — an alias owned by exactly one provider routes on its own |
| `id_prefixes` | `("gpt-image-", …)` — routes a full model id, so a model released tomorrow works untouched |
| `env_keys` | acceptable env-var names, in priority order |
| `default_model` | used when `--model` is omitted |
| `max_refs` | enforced centrally by `io_utils.check_refs` — **must be a class attribute, not a module constant** |
| `key_url` | printed when the key is missing |

| Method | Contract |
|---|---|
| `validate(req)` | offline, free. Raise `ProviderError` naming the *constraint that failed*. Return a list of warning strings — a flag this engine doesn't understand **warns and continues**, so one manifest can mix providers. |
| `describe(req)` | offline. What the request becomes in this engine's own terms, for the dry-run line. |
| `estimate_cost(req)` | `-> (dollars_or_None, note_or_None)`. `None` means genuinely unknown — the caller prints `?`. |
| `generate(req)` | the paid call. Returns `[GenResult]`. Raises `AuthError` / `BlockedError` / `ProviderError`. |

### To add a third engine

1. New file in `providers/`, subclass `ImageProvider`, fill the attributes above.
2. One line in `config._REGISTRY`. **Nothing else changes** — `imagegen.py`, `batch.py` and `spend.py`
   never name a provider.
3. Check your aliases don't collide with an existing provider's (the router raises if two claim one,
   which is a correct failure, not a bug to paper over).
4. Import the SDK **lazily, inside the method that needs it** — `--model pro` must keep working on a
   machine with no `openai` installed, and vice versa.
5. Translate `--aspect` / `--res` into whatever your engine wants. Those two flags are universal and
   every provider must honour them; everything else may warn.

---

## Where the money lives

Two engines, two billing models, and the difference is load-bearing.

| | Gemini | OpenAI |
|---|---|---|
| Billed by | flat rate per image | **tokens** |
| Table | `gemini.COST[model][res]` | `openai_images.MEASURED_OUT_25` (2.5, low/medium) or `TOKEN_ANCHORS`, + `RATES` |
| Known before the call? | yes — the estimate is the final number | **no** — on 2.5 low/medium, within ±15% on 81% of logged calls |
| `GenResult.estimated` | `True` | `False` once a `usage` payload backed it |

`spend.log_run()` writes whichever number it has. `--usage` reports both and says how much is
API-measured.

⭐ **`--calibrate` is the correction mechanism, and the only legitimate one.** It prints measured
$/image per (model, quality, size) from rows the API itself priced. If it disagrees with the estimate,
fix `TOKEN_ANCHORS` **from that**, never from a published table — and never invent a number for a tier
that has none (`xhigh`, `max`), because a wrong cost figure is one nobody re-checks.

---

## Invariants — breaking any of these is a defect, not a style change

1. **Validation happens before billing.** `batch.run()` builds and previews *every* item before it
   generates *any*. A typo in item 7 must not surface after items 1–6 have been paid for.
2. **A key value is never printed.** Everything that surfaces one goes through `config.mask()`.
3. **A proxy block is never reported as an auth failure.** `_classify()` in each provider separates
   them by name. Conflating them once already sent a user to regenerate a working key.
4. **An unknown cost prints `?`.** Never a guess.
5. **Output paths are resolved once,** in `batch.build()` via `io_utils.resolve_out()`, and are
   absolute from there on. The log is app-wide; a relative path in it is unresolvable later.
6. **`-o` is authoritative.** `--out-dir` rebases a *relative* `-o` only. Nothing silently redirects
   output. In `--batch`, `main()` copies `--out-dir` onto every item that has no `out_dir` of its
   own, before `build()`. The precedence is: absolute `out` → the item's `out_dir` → `--out-dir` →
   the current directory.
7. **The key chain stops at the git root.** A stray `.env` in the home directory must never supply a
   key to an unrelated project.
8. **At most `--parallel` requests are in flight, process-wide.** Batch workers and a Gemini `n>1`
   fan-out draw from ONE semaphore (`concurrency.in_flight()`), so width never multiplies. A
   provider wraps each API call in `concurrency.call_with_retry()`, and holds a slot only while
   it is actually on the wire.
9. **No two items may write the same path.** `batch.duplicate_outputs()` checks this before
   billing, expanding `-1/-2` names and ignoring case (Windows).
10. **Only `RateLimitedError` is ever retried.** A 429 is refused before any work, so it is never
    billed. A timeout or 5xx may already have been billed, so it is never retried. That is also why
    the OpenAI client is built with `max_retries=0`. A quota/billing 429 (`insufficient_quota`) is
    classified as `AuthError` *before* the rate-limit branch.
11. **A paid image that landed is logged even if a sibling call failed.** Gemini's `n>1` fan-out
    returns its successes and raises only when nothing landed.
12. **A provider that retries must be able to re-send its inputs.** OpenAI's `call()` rewinds its
    reference file handles; the failed attempt has already read them to EOF.

⚠️ **Threads, not processes, and that was measured, not assumed** (2026-10-29). A slow batch on
2026-10-27 looked like the threading was serializing calls, and a rewrite to one process per image
was planned. A same-minute A/B said otherwise: the batch sent all 4 requests within 5ms and finished
in 25s, against 28s for 4 separate processes. The stall was OpenAI's latency. Don't reopen this
without an `IMAGEGEN_TRACE=1` run that shows a late *sent*, rather than a late *received*.

---

## The free regression suite

None of this spends anything. Run it after any change to routing, geometry or the guards.

```bash
S="$HOME/.claude/skills/image-gen/scripts/imagegen.py"

python "$S" --check                     # keys (masked) + SDK versions
python "$S" --list-models               # every alias, both providers
python "$S" --model flare --prompt x -o /tmp/a.png --dry-run   # → openai, solved size, estimate
python "$S" --model pro   --prompt x -o /tmp/b.png --dry-run   # → gemini, native aspect/res
python "$S" --model gpt-image-2.5-sunburst --prompt x -o /tmp/c.png --dry-run   # routes by prefix

# batch + --out-dir: item 1's out must show under /tmp/od/, item 2 under its own out_dir /tmp/own/,
# and item 3 (absolute out) must stay exactly /tmp/abs.png
echo '[{"prompt":"x","out":"a.png","model":"flare"},
       {"prompt":"x","out":"b.png","model":"flare","out_dir":"/tmp/own"},
       {"prompt":"x","out":"/tmp/abs.png","model":"flare"}]' > /tmp/m.json
python "$S" --batch /tmp/m.json --out-dir /tmp/od --dry-run | grep "out:"

# must REFUSE, each naming its constraint:
python "$S" --model flare --aspect 9:1 --prompt x -o /tmp/d.png --dry-run
python "$S" --model flare --mask m.png --prompt x -o /tmp/e.png --dry-run
python "$S" --model flare -n 50         --prompt x -o /tmp/f.png --dry-run
python "$S" --model pro   -n 9          --prompt x -o /tmp/g.png            # cap, exit 2

# concurrency: TOTAL must show parallel=3, blocks arrive in completion order, and a manifest
# whose items collide (two "a.png", or an n:2 "a.png" beside an "a-1.png") must exit 2 naming the path
python "$S" --batch /tmp/m.json --out-dir /tmp/od --dry-run --parallel 3
python "$S" --model flare --prompt x -o /tmp/h.png --dry-run --parallel 0   # argparse refuses

# the hook: paid → ask, everything else → silent
echo '{"tool_input":{"command":"python imagegen.py -o a.png"}}'            | python spend_gate.py
echo '{"tool_input":{"command":"python imagegen.py -o a.png --dry-run"}}'  | python spend_gate.py
```

**The geometry assertions** — the one piece of real arithmetic. For every aspect × tier, both edges
must be multiples of 16, the ratio must be reproduced **exactly**, and the result must sit inside
OpenAI's envelope (edge ≤ 3840; total 655,360–8,294,400 px):

```python
import sys; sys.path.insert(0, "<scripts>")
from providers.openai_images import solve_size
for aspect in ["1:1","4:5","2:3","3:2","16:9","9:16","21:9","3:4"]:
    for res in ["1K","2K","4K"]:
        w, h, warn = solve_size(aspect, res)
        a, b = (int(x) for x in aspect.split(":"))
        assert w % 16 == 0 and h % 16 == 0, (aspect, res, w, h)
        assert abs(w/h - a/b) < 1e-9, f"ratio drift {aspect} {res} -> {w}x{h}"
        assert 655_360 <= w*h <= 8_294_400 and max(w,h) <= 3840
```

⚠️ Rounding width and height *independently* silently drifts the ratio (4:5 @ 1K lands on 912x1152 =
0.792:1). `_exact_pair()` exists to prevent that, and the assertion above is what catches a
regression — an eyeballed size looks fine.

---

## Unverified territory

- **The Gemini findings in `gemini-images.md` were measured on the 2.5-era model through the web app.**
  Re-verify on 3.x rather than assuming parity. Still open: Arabic-join reliability, multi-pass swap
  fidelity.
- **OpenAI is newly measured.** GPT Image 2.5 shipped 2026-09-08; anything learned about how it
  behaves belongs in `openai-images.md`.
- **The SDK moves faster than its types.** `openai_images._split_params()` inspects the installed
  signature and routes anything unrecognised into `extra_body`. As of openai 3.14.1 every parameter we
  send is native, so that path is currently unexercised insurance — if you change it, re-check it
  against a parameter the SDK does *not* know.
