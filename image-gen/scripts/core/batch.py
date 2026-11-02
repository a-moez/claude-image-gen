"""
The run pipeline: build a request, preview it, spend the money, log the result.

A single `-o out.png` invocation and a `--batch manifest.json` run take the SAME path — a
single call is just a batch of one. That is deliberate: the preview, the cap, the warning
surface and the log format can never drift apart between the two.

Per-item routing means one manifest may MIX PROVIDERS — a Gemini restyle and an OpenAI
poster in the same approved run. The cap applies to the run's total image count.

ORDER MATTERS: every item is built and validated (offline, free) BEFORE any item is
generated (paid). A typo in item 7 must not surface after items 1-6 have been billed.

CONCURRENCY: once validated, items run up to `parallel` at a time (default 4; 1 restores
strictly sequential, live-printing behaviour). Cost is identical either way. Each item's
output is buffered and printed as one block when it finishes, so blocks arrive in
COMPLETION order, each tagged with its manifest index. See core/concurrency.py.

IF YOU CHANGE THIS FILE: preview() is what the human reads before approving spend. If you
add a parameter that affects cost, it has to appear there or the approval is uninformed.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import concurrency, io_utils, spend
from core.config import route
from core.request import GenRequest
from providers.base import ProviderError

_PASSTHROUGH = (
    "model", "n", "aspect", "res", "quality", "size", "mask", "background",
    "moderation", "thinking", "output_format", "out_dir", "project", "overwrite",
)


def build(item: dict) -> tuple:
    """dict -> (GenRequest, provider, prompt_file). Offline; raises on anything unserveable."""
    if "out" not in item:
        raise ValueError("item is missing the required 'out' key")

    prompt, prompt_file = io_utils.read_prompt(item.get("prompt"), item.get("prompt_file"))
    kwargs = {k: item[k] for k in _PASSTHROUGH if item.get(k) is not None}
    if "n" in kwargs:
        # A hand-written manifest may quote it ("n": "2"); coerce rather than fail later.
        try:
            kwargs["n"] = int(kwargs["n"])
        except (TypeError, ValueError):
            raise ValueError(f"'n' must be a whole number, got {kwargs['n']!r}")
    req = GenRequest(
        prompt=prompt,
        out=item["out"],
        refs=list(item.get("refs") or []),
        extra=dict(item.get("extra") or {}),
        **kwargs,
    )
    # Resolve the output to an absolute path ONCE, here, so every downstream consumer —
    # the overwrite guard, the "wrote" line, the spend log — agrees on where it landed.
    req.out = io_utils.resolve_out(req.out, req.out_dir)
    provider = route(model=req.model or None, provider=item.get("provider"))
    if not req.model:
        req.model = provider.default_model
    return req, provider, prompt_file


def preview(req, provider, index=None) -> tuple:
    """Print what this item will do and what it should cost. -> (est_cost, warnings).

    This is the text a human approves spend against, so it names every cost-bearing
    decision: model, geometry, quality, image count.
    """
    # check_refs first: a missing file is cheaper to report than a bad parameter combo,
    # and provider.validate() assumes the references exist.
    warnings = list(io_utils.check_refs(req.refs, provider.max_refs))
    warnings.extend(provider.validate(req))
    est, note = provider.estimate_cost(req)

    tag = f"[{index}] " if index else ""
    detail = provider.describe(req)
    money = f"~${est}" if est is not None else "~$? (unpriced tier)"
    print(f"{tag}→ {provider.name}  " + "  ".join(f"{k}={v}" for k, v in detail.items()))
    print(f"    n={req.n}  refs={len(req.refs)}  est={money}")
    if note:
        print(f"    note: {note}")
    print(f"    prompt: {req.head()}")
    for path in req.refs:
        print(f"    ref:    {path}")
    print(f"    out:    {', '.join(io_utils.output_paths(req.out, req.n))}")
    for w in warnings:
        print(f"    ⚠️  {w}")
    return est, warnings


def run_one(req, provider, prompt_file=None, dry_run=False, log_path=None) -> dict:
    """Execute one prepared item. Returns a summary dict for the run report."""
    paths = io_utils.output_paths(req.out, req.n)
    io_utils.guard_overwrite(paths, req.overwrite)

    if dry_run:
        print("    [dry-run] no API call made, nothing spent.\n")
        return {"ok": True, "dry_run": True, "outputs": paths, "cost": None}

    started = time.monotonic()
    results = provider.generate(req)
    if not results:
        print("    FAILED: no images produced.\n")
        return {"ok": False, "outputs": [], "cost": None}

    spend.log_run(req, results, prompt_file=prompt_file, log_path=log_path)
    cost = sum(r.cost for r in results if r.cost is not None) or None
    measured = any(r.estimated is False for r in results)
    # The elapsed time is what tells "the provider was slow" apart from "we were slow".
    took = f"  in {time.monotonic() - started:.0f}s"
    if cost is not None:
        label = "actual" if measured else "estimated"
        print(f"    cost: ${cost:.4f} ({label}){took}")
    else:
        print(f"    done{took}")
    print()
    return {"ok": True, "outputs": [r.path for r in results], "cost": cost}


def load_manifest(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        raise ValueError("a batch manifest must be a JSON array of items")
    if not items:
        raise ValueError(f"batch manifest is empty: {path}")
    return items


def duplicate_outputs(runnable) -> list:
    """Absolute paths that more than one item would write, -1/-2 expansions included.

    Checked before billing: sequentially the second write fails only AFTER the first copy
    is paid for, and in parallel both are paid for and then race to the same file.
    """
    seen, dupes = set(), []
    for _, req, *_ in runnable:
        for path in io_utils.output_paths(req.out, req.n):
            key = path.lower()   # Windows paths are case-insensitive
            if key in seen and path not in dupes:
                dupes.append(path)
            seen.add(key)
    return dupes


def _attempt(req, provider, prompt_file, dry_run, log_path) -> dict:
    """run_one, with the expected per-item failures turned into a failed outcome."""
    try:
        return run_one(req, provider, prompt_file, dry_run, log_path)
    except (ProviderError, OSError) as e:
        print(f"    ERROR: {e}\n")
        return {"ok": False, "outputs": [], "cost": None}


def run(items, dry_run=False, max_images=spend.DEFAULT_MAX_IMAGES,
        force=False, log_path=None, parallel=concurrency.DEFAULT_PARALLEL) -> int:
    """Build-and-validate everything first, then generate. -> process exit code."""
    concurrency.configure(parallel)
    runnable, failed = [], set()
    for idx, item in enumerate(items, 1):
        label = idx if len(items) > 1 else None
        try:
            req, provider, prompt_file = build(item)
            est, _ = preview(req, provider, index=label)
        except (ValueError, OSError, ProviderError) as e:
            print(f"[{idx}] SKIP: {e}\n")
            failed.add(idx)
            continue
        runnable.append((idx, req, provider, prompt_file, est))

    if not runnable:
        print("Nothing runnable.")
        return 1

    # Counted from what will ACTUALLY run — a rejected item must not inflate the
    # image count the human is approving.
    total_images = sum(req.n for _, req, _, _, _ in runnable)
    priced = [e for *_, e in runnable if e is not None]
    unpriced = sum(1 for *_, e in runnable if e is None)
    print(f"TOTAL: {len(runnable)} item(s), {total_images} image(s), "
          f"est ~${round(sum(priced), 4)}"
          + (f" plus {unpriced} unpriced item(s)" if unpriced else "")
          + f", parallel={parallel}")

    dupes = duplicate_outputs(runnable)
    if dupes:
        print("\nERROR: more than one item would write the same file — nothing was run:\n  "
              + "\n  ".join(dupes)
              + "\n  Give every item (and every -1/-2 candidate of an n>1 item) its own out.")
        return 2

    if not dry_run:
        try:
            spend.enforce_cap(total_images, max_images, force)
        except spend.SpendCapExceeded as e:
            print(f"\nERROR: {e}")
            return 2
    print()

    def tag(idx):
        return f"[{idx}/{len(items)}]" if len(items) > 1 else None

    def settle(idx, outcome):
        nonlocal spent
        if not outcome["ok"]:
            failed.add(idx)
        elif outcome.get("cost"):
            spent += outcome["cost"]

    spent = 0.0
    if parallel <= 1 or len(runnable) == 1:
        # Strictly sequential, printing live — exactly the pre-concurrency behaviour.
        for idx, req, provider, prompt_file, _ in runnable:
            if tag(idx):
                print(tag(idx))
            settle(idx, _attempt(req, provider, prompt_file, dry_run, log_path))
    else:
        print(f"running {len(runnable)} item(s), up to {parallel} request(s) at a time — "
              "each item prints when it finishes.\n")
        with concurrency.routed_stdout():
            pool = ThreadPoolExecutor(max_workers=parallel)
            futures = {
                pool.submit(concurrency.buffered, _attempt,
                            req, provider, prompt_file, dry_run, log_path): idx
                for idx, req, provider, prompt_file, _ in runnable
            }
            try:
                for fut in as_completed(futures):
                    idx = futures[fut]
                    outcome, text, err = fut.result()
                    print(tag(idx))
                    print(text, end="")
                    if err is not None:
                        print(f"    ERROR (unexpected): {type(err).__name__}: {err}\n")
                        outcome = {"ok": False}
                    settle(idx, outcome)
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                live = sorted(i for f, i in futures.items() if f.running())
                dropped = sorted(i for f, i in futures.items() if f.cancelled())
                print("\ninterrupted.")
                if live:
                    print(f"  still in flight (will finish, be billed and be logged): {live}")
                if dropped:
                    print(f"  never started (not billed): {dropped}")
                raise
            pool.shutdown()

    if len(items) > 1 or failed:
        print(f"Done: {len(items) - len(failed)}/{len(items)} ok."
              + (f"  Spent ~${spent:.4f}." if spent else ""))
    return 1 if failed else 0
