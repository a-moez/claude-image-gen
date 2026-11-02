#!/usr/bin/env python3
"""
Generate images from the terminal through OpenAI (GPT Image) or Nano Banana (Gemini),
with one flag set, one spend gate, and one spend log — from any repo on this machine.

    imagegen.py --prompt-file p.txt --ref a.jpg --model flare --aspect 4:5 -o out.png

THE MODEL PICKS THE PROVIDER. `--model flare` routes to OpenAI, `--model pro` to Gemini;
a full model id routes by prefix, so one released tomorrow still works. `--provider` is
only needed to break a tie.

ONE VOCABULARY, TWO ENGINES. `--aspect` and `--res` mean the same thing everywhere:
Gemini takes them natively, and the OpenAI provider solves them into a concrete pixel
size. So a feed locked to 4:5 gets 4:5 from either engine. `--size 1536x864` (OpenAI)
bypasses that. Flags a provider does not understand WARN and continue, so one batch
manifest can mix providers.

MONEY — three layers, do not bypass them:
  1. (soft)   Before any real run, price it with AskUserQuestion and get approval.
              Prefer ONE --batch over many single calls: one confirmation, not six.
  2. (script) A run over --max-images (default 8) is refused without --force.
  3. (harness) A PreToolUse hook forces a confirmation on every real run, in every repo.
  --dry-run prices anything for free and spends nothing.

PARALLEL BY DEFAULT. A batch (and a Gemini -n>1) runs up to --parallel requests at once
(default 4). That changes wall-clock time, never cost. --parallel 1 restores strictly
sequential runs. Only a rate-limit refusal (429) is retried; see core/concurrency.py.

OUTPUT NAMING IS THE CALLER'S JOB. Writes exactly to -o (with -1/-2 suffixes when n>1)
and never overwrites without --overwrite.

    imagegen.py --batch runs.json          # JSON array; keys mirror the flags below
    imagegen.py --usage [--project slug]   # what has been spent, everywhere
    imagegen.py --calibrate                # measured $/image from real API usage
    imagegen.py --check                    # keys + SDKs, without spending anything

KEYS live in ~/.config/claude-imagegen/.env (run setup_key.py once). A project .env and a
shell variable both still win over it. See core/config.py for the full chain.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402

from core import batch, concurrency, io_utils, spend  # noqa: E402
from core import config as cfg  # noqa: E402
from providers.base import ProviderError  # noqa: E402


def _at_least_one(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def build_parser():
    p = argparse.ArgumentParser(
        prog="imagegen",
        description="Generate images via OpenAI GPT Image or Nano Banana (Gemini).",
        epilog="Run --list-models to see the aliases, --check to verify your setup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("what to make")
    g.add_argument("--prompt", help="prompt text (prefer --prompt-file for long/Arabic)")
    g.add_argument("--prompt-file", help="path to a UTF-8 file holding the prompt")
    g.add_argument("--ref", action="append", default=[], metavar="PATH",
                   help="reference image; repeatable. >=1 switches OpenAI to edit mode")
    g.add_argument("-o", "--out", help="output path (required unless --batch/--usage/...)")
    g.add_argument("-n", type=int, default=1, help="number of candidates (default 1)")

    g = p.add_argument_group("engine")
    g.add_argument("--model", help="alias (flare|sunburst|flash|pro|…) or a full model id")
    g.add_argument("--provider", choices=["openai", "gemini"],
                   help="only needed when --model alone cannot decide")

    g = p.add_argument_group("geometry — universal")
    g.add_argument("--aspect", default="4:5", help="aspect ratio (default 4:5)")
    g.add_argument("--res", default="2K", choices=["1K", "2K", "4K"],
                   help="resolution tier (default 2K; 4K for heroes/print)")

    g = p.add_argument_group("OpenAI-specific")
    g.add_argument("--quality", choices=["low", "medium", "high", "xhigh", "max", "auto"],
                   help="default high; xhigh/max are 2.5-only and unpriced pre-call")
    g.add_argument("--size", help="raw WIDTHxHEIGHT, bypassing --aspect/--res")
    g.add_argument("--mask", help="PNG with alpha marking where to edit (needs --ref)")
    g.add_argument("--background", choices=["transparent", "opaque", "auto"])
    g.add_argument("--moderation", choices=["low", "auto"])

    g = p.add_argument_group("Gemini-specific")
    g.add_argument("--thinking", choices=["minimal", "high"],
                   help="reasoning depth for complex composites")

    g = p.add_argument_group("output & money")
    g.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])
    g.add_argument("--out-dir", metavar="DIR",
                   help="base directory a RELATIVE -o resolves against (default: cwd). "
                        "With --batch, the default for every item without its own "
                        "out_dir. Never overrides an absolute -o.")
    g.add_argument("--project", help="slug recorded in the spend log; also filters --usage")
    g.add_argument("--repo", metavar="NAME",
                   help="filter --usage to runs made from this repository")
    g.add_argument("--overwrite", action="store_true", help="allow replacing existing files")
    g.add_argument("--max-images", type=int, default=spend.DEFAULT_MAX_IMAGES,
                   help=f"refuse a real run above this (default {spend.DEFAULT_MAX_IMAGES})")
    g.add_argument("--force", action="store_true", help="override the --max-images cap")
    g.add_argument("--parallel", type=_at_least_one, default=concurrency.DEFAULT_PARALLEL,
                   metavar="N",
                   help=f"requests in flight at once (default {concurrency.DEFAULT_PARALLEL}; "
                        "1 = sequential). Changes speed, never cost")
    g.add_argument("--dry-run", action="store_true",
                   help="print the request and cost estimate, make NO API call")
    g.add_argument("--log", help="use a different spend log file")

    g = p.add_argument_group("modes that never spend")
    g.add_argument("--batch", metavar="MANIFEST.json", help="run a JSON array of items")
    g.add_argument("--usage", action="store_true", help="spend totals across every repo")
    g.add_argument("--calibrate", action="store_true",
                   help="measured $/image from real API usage payloads")
    g.add_argument("--check", action="store_true", help="verify keys and SDKs")
    g.add_argument("--list-models", action="store_true", help="aliases per provider")
    return p


def list_models():
    for provider in cfg.all_providers():
        print(f"{provider.name}  (default: {provider.default_model})")
        width = max(len(a) for a in provider.aliases) if provider.aliases else 8
        for alias, model_id in provider.aliases.items():
            mark = "  ←default" if alias == provider.default_model else ""
            print(f"  {alias:<{width}}  {model_id}{mark}")
        print(f"  any id starting with: {', '.join(provider.id_prefixes)}")
        print()


def check():
    """Diagnostics that never spend. Key VALUES are never printed — only a masked tail."""
    print(f"skill:    {cfg.SKILL_DIR}")
    print(f"user env: {cfg.USER_ENV}" + ("" if cfg.USER_ENV.is_file() else "   (not created yet)"))
    print(f"log:      {cfg.USAGE_LOG}" + ("" if cfg.USAGE_LOG.is_file() else "   (no spend yet)"))
    print(f"python:   {sys.version.split()[0]}  ({sys.executable})")
    print()

    ok = True
    for provider in cfg.all_providers():
        key, source = cfg.resolve_key(provider.env_keys)
        if key:
            print(f"{provider.name:<8} key {cfg.mask(key)}  from {source}")
        else:
            ok = False
            print(f"{provider.name:<8} NO KEY — run: python \"{cfg.SCRIPTS_DIR / 'setup_key.py'}\"")

    print()
    for module, need in (("openai", "2"), ("google.genai", None), ("PIL", None)):
        try:
            mod = __import__(module, fromlist=["__version__"])
            version = getattr(mod, "__version__", "?")
            warn = ""
            if need and str(version).split(".")[0].isdigit() and int(str(version).split(".")[0]) < int(need):
                warn = f"   ⚠️  too old, need >={need}: pip install -U \"openai>={need}\""
                ok = False
            print(f"{module:<14} {version}{warn}")
        except ImportError:
            ok = False
            print(f"{module:<14} NOT INSTALLED — pip install -r \"{cfg.REQUIREMENTS}\"")
    return 0 if ok else 1


def main():
    io_utils.force_utf8_stdout()
    args = build_parser().parse_args()

    if args.list_models:
        list_models()
        return 0
    if args.check:
        return check()
    if args.usage:
        spend.report_usage(project=args.project, repo=args.repo, log_path=args.log)
        return 0
    if args.calibrate:
        spend.report_calibration(log_path=args.log)
        return 0

    try:
        if args.batch:
            items = batch.load_manifest(args.batch)
            # --out-dir is the run-wide base for relative outputs. An item's own "out_dir"
            # wins over it, and an absolute "out" ignores both (resolve_out). Without this,
            # a batch silently wrote into the cwd — usually a repo root — while the docs
            # promised --out-dir applied.
            if args.out_dir:
                for item in items:
                    if isinstance(item, dict) and not item.get("out_dir"):
                        item["out_dir"] = args.out_dir
        else:
            if not args.out:
                print("ERROR: -o/--out is required (unless --batch, --usage, --calibrate, "
                      "--check or --list-models).", file=sys.stderr)
                return 2
            items = [{
                "prompt": args.prompt, "prompt_file": args.prompt_file,
                "refs": args.ref, "out": args.out, "model": args.model,
                "provider": args.provider, "n": args.n, "aspect": args.aspect,
                "res": args.res, "quality": args.quality, "size": args.size,
                "mask": args.mask, "background": args.background,
                "moderation": args.moderation, "thinking": args.thinking,
                "output_format": args.output_format, "out_dir": args.out_dir,
                "project": args.project, "overwrite": args.overwrite,
            }]
        return batch.run(
            items, dry_run=args.dry_run, max_images=args.max_images,
            force=args.force, log_path=args.log, parallel=args.parallel,
        )
    except (ProviderError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
