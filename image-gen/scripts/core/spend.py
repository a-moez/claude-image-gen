"""
Money: the image-count cap, the spend log, and the two reports that read it back.

ONE GLOBAL LOG. ~/.config/claude-imagegen/usage.log holds every call from every repo, so
"what have I spent on images" has a single answer. Per-project logs fragment that into a
number nobody can total. `--project <slug>` tags a row; `--usage --project <slug>` filters.

TWO KINDS OF NUMBER, AND THE DIFFERENCE MATTERS. Gemini bills a flat rate per image, so
its cost is known before the call and `estimated` stays true. OpenAI bills tokens, so the
real figure only exists in the response's `usage` payload — those rows land with
`estimated: false` and are authoritative.

That is what makes --calibrate possible: it reports measured $/image from rows the API
itself priced. When a published token anchor is wrong, or a tier has none at all
(xhigh, max), one real run is worth more than any table. Correct
providers/openai_images.py's TOKEN_ANCHORS from this, not from a blog post.

IF YOU CHANGE THIS FILE: _read() must keep tolerating the legacy schema
(`brand`, `est_cost`, no `provider`). Those rows are real spend history; dropping them
silently understates the total.
"""
import json
import os
import threading
from datetime import datetime, timezone

from core.config import USAGE_LOG, find_git_root

DEFAULT_MAX_IMAGES = 8

# Parallel workers finish at the same moment; without this two rows can tear into one
# unparseable line, and _read() would silently drop real spend.
_LOG_LOCK = threading.Lock()


class SpendCapExceeded(Exception):
    pass


def enforce_cap(total_images: int, max_images: int, force: bool):
    """Layer 2 of the spend gate: a runaway batch cannot start by accident."""
    if force or total_images <= max_images:
        return
    raise SpendCapExceeded(
        f"this run would generate {total_images} images, over the --max-images cap of "
        f"{max_images}.\n  Re-run with --force if that is intended, or split it up.\n"
        "  --dry-run prices the whole thing for free."
    )


def log_run(req, results, prompt_file=None, log_path=None):
    """Append one JSON line per run. Called only after real images exist on disk."""
    if not results:
        return
    path = log_path or USAGE_LOG
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    costs = [r.cost for r in results if r.cost is not None]
    root = find_git_root()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": results[0].provider,
        "model": results[0].model_id,
        # WHERE it ran (automatic) vs WHAT it was for (manual). Both, because they diverge:
        # an Acme asset generated from the acme-web repo is repo=acme-web,
        # project=acme, and each answers a question the other cannot.
        "repo": str(root) if root else None,
        "repo_name": root.name if root else None,
        "project": req.project,
        "n": len(results),
        "cost": round(sum(costs), 6) if costs else None,
        "estimated": any(r.estimated for r in results),
        "usage": results[0].usage,
        "aspect": req.aspect,
        "res": req.res,
        "size": (f"{results[0].width}x{results[0].height}"
                 if results[0].width else req.size),
        "quality": req.quality,
        "prompt_file": prompt_file,
        "prompt_head": req.head(),
        "refs": list(req.refs),
        "outputs": [r.path for r in results],
        "cwd": os.getcwd(),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _LOG_LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _read(log_path=None) -> list:
    """Every row, with legacy-schema rows normalised into the current shape."""
    path = log_path or USAGE_LOG
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            # --- legacy schema -----------------------------------------------
            if "project" not in e and "brand" in e:
                e["project"] = e.get("brand")
            if "cost" not in e and "est_cost" in e:
                e["cost"] = e.get("est_cost")
                e["estimated"] = True
            if "provider" not in e:
                model = e.get("model", "")
                e["provider"] = "gemini" if model.startswith("gemini-") else "openai"
            rows.append(e)
    return rows


def _norm_1024(tokens, size):
    """Output tokens rescaled to a 1024x1024 frame, so a measurement at any size can be
    compared straight against a provider's per-megapixel anchor. '?' when size is unknown."""
    try:
        w, h = (int(x) for x in str(size).lower().split("x"))
    except (ValueError, AttributeError):
        return "?"
    if not (w and h):
        return "?"
    return f"{tokens * 1_048_576 / (w * h):.0f}"


def _totals_by(rows, key, fallback):
    """{label: (cost, images)} for one grouping column."""
    out = {}
    for r in rows:
        acc = out.setdefault(r.get(key) or fallback, [0.0, 0])
        acc[0] += r.get("cost") or 0.0
        acc[1] += r.get("n") or 0
    return out


def report_usage(project=None, repo=None, log_path=None):
    rows = _read(log_path)
    if not rows:
        print(f"No spend logged yet ({log_path or USAGE_LOG}).")
        return
    if project:
        rows = [r for r in rows if r.get("project") == project]
        if not rows:
            print(f"No spend logged for project '{project}'.")
            return
    if repo:
        rows = [r for r in rows if r.get("repo_name") == repo]
        if not rows:
            print(f"No spend logged for repo '{repo}'. "
                  "(Rows written before 2026-09-16 carry no repo.)")
            return

    total = sum(r.get("cost") or 0.0 for r in rows)
    images = sum(r.get("n") or 0 for r in rows)
    measured = sum(r.get("cost") or 0.0 for r in rows if r.get("estimated") is False)
    unknown = sum(1 for r in rows if r.get("cost") is None)

    scope = f" for project '{project}'" if project else ""
    print(f"Image spend{scope}: {len(rows)} calls, {images} images, ~${total:.3f}")
    print(f"  ${measured:.3f} of that is API-measured; the rest is estimated from "
          f"published rates.")
    if unknown:
        print(f"  ⚠️  {unknown} call(s) carry no cost at all (unpriced quality tier).")

    by_model = {}
    for r in rows:
        key = (r.get("provider", "?"), r.get("model", "?"))
        acc = by_model.setdefault(key, [0.0, 0])
        acc[0] += r.get("cost") or 0.0
        acc[1] += r.get("n") or 0
    print()
    for (provider, model), (cost, n) in sorted(by_model.items(), key=lambda kv: -kv[1][0]):
        print(f"  {provider:<7} {model:<26} {n:>3} images  ~${cost:.3f}")

    if not project:
        print("\n  by project — what the spend was FOR")
        for name, (cost, n) in sorted(_totals_by(rows, "project", "(untagged)").items(),
                                      key=lambda kv: -kv[1][0]):
            print(f"    {name:<26} {n:>3} images  ~${cost:.3f}")

    if not repo:
        print("\n  by repo — WHERE the command ran")
        for name, (cost, n) in sorted(_totals_by(rows, "repo_name", "(no repo)").items(),
                                      key=lambda kv: -kv[1][0]):
            print(f"    {name:<26} {n:>3} images  ~${cost:.3f}")


def report_calibration(log_path=None):
    """Measured $/image per (model, quality, size), from rows the API itself priced.

    This is the antidote to a stale cost table. A tier with no published token count
    (xhigh, max) has no estimate at all until it has been run once — after that, the
    number here is real.
    """
    rows = [r for r in _read(log_path)
            if r.get("estimated") is False and r.get("cost") is not None and r.get("n")]
    if not rows:
        print("Nothing to calibrate from yet.")
        print("  Only API-measured rows count, and so far every logged call was priced "
              "from a published rate rather than a usage payload.")
        print("  Run one OpenAI generation and this will have something to say.")
        return

    # Bucket by reference count too. Without that, a no-ref generate and a 2-ref edit
    # average into one "$/image" that is not comparable to anything: the estimate
    # predicts OUTPUT tokens, while references are an INPUT cost that can dominate.
    buckets = {}
    for r in rows:
        refs = len(r.get("refs") or [])
        key = (r.get("model", "?"), r.get("quality") or "(default)",
               r.get("size") or "?", refs)
        acc = buckets.setdefault(key, {"cost": 0.0, "n": 0, "calls": 0,
                                       "out": 0, "in": 0})
        acc["cost"] += r["cost"]
        acc["n"] += r["n"]
        acc["calls"] += 1
        u = r.get("usage") or {}
        acc["out"] += u.get("output_tokens") or 0
        acc["in"] += u.get("input_tokens") or 0

    print("Measured from API usage payloads — not from a published table.\n")
    print(f"  {'model':<24} {'quality':<9} {'size':<11} {'refs':>4} "
          f"{'out tok':>8} {'@1024²':>7} {'$/image':>9}  calls")
    for (model, quality, size, refs), a in sorted(buckets.items()):
        per_out = a["out"] / a["n"] if a["n"] else 0
        print(f"  {model:<24} {quality:<9} {size:<11} {refs:>4} "
              f"{per_out:>8.0f} {_norm_1024(per_out, size):>7} "
              f"{a['cost'] / a['n']:>9.4f}  {a['calls']}")

    print("\n  'out tok' is what TOKEN_ANCHORS predicts; '@1024²' normalises it by pixel\n"
          "  count so it compares directly against the anchor values in\n"
          "  providers/openai_images.py. Correct the anchors from THIS, never from a\n"
          "  published table — and leave a tier you have not measured alone.")
    if any(refs for *_, refs in buckets):
        print("\n  ⚠️  Rows with refs>0 carry input tokens the pre-call estimate cannot\n"
              "      see. Compare $/image only within the same refs count.")
