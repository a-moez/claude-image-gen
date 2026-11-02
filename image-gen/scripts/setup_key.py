#!/usr/bin/env python3
"""
Store API keys once, for every repo on this machine.

    python setup_key.py            # prompt for whichever keys are missing
    python setup_key.py --force    # prompt for all of them, replacing what is there
    python setup_key.py --show     # where each key currently resolves from (masked)

Writes ~/.config/claude-imagegen/.env. Keys are read with getpass, so they are never
echoed to the terminal and never land in shell history — which is exactly why this is a
prompt rather than a `--openai-key sk-...` flag.

This file is the LAST link in the resolution chain. A shell variable, or a .env inside the
project you are standing in, still wins — see core/config.py.

IF YOU CHANGE THIS FILE: it must never print a key. Everything that surfaces a value goes
through config.mask().
"""
import argparse
import os
import stat
import sys
from getpass import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import config as cfg  # noqa: E402

PROVIDERS = [
    ("OPENAI_API_KEY", "OpenAI (GPT Image)", "https://platform.openai.com/api-keys"),
    ("GEMINI_API_KEY", "Nano Banana (Gemini)", "https://aistudio.google.com/apikey"),
]


def show():
    print(f"user key file: {cfg.USER_ENV}"
          + ("" if cfg.USER_ENV.is_file() else "   (not created yet)"))
    print()
    for provider in cfg.all_providers():
        key, source = cfg.resolve_key(provider.env_keys)
        if key:
            print(f"  {provider.name:<8} {cfg.mask(key):<22} from {source}")
        else:
            print(f"  {provider.name:<8} not set")
    print("\nA shell variable or a project .env overrides the user key file.")


def write(values: dict):
    cfg.USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = {}
    if cfg.USER_ENV.is_file():
        existing = cfg._parse_env_file(cfg.USER_ENV)
    existing.update({k: v for k, v in values.items() if v})

    lines = [
        "# API keys for the image-gen skill. Used from every repo on this machine.",
        "# Written by setup_key.py — a shell variable or a project .env still wins.",
        "",
    ]
    lines += [f"{k}={v}" for k, v in sorted(existing.items())]
    cfg.USER_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        os.chmod(cfg.USER_ENV, stat.S_IRUSR | stat.S_IWUSR)   # owner read/write only
    except OSError:
        pass

    print(f"\nWrote {cfg.USER_ENV}")
    for k, v in sorted(existing.items()):
        print(f"  {k} = {cfg.mask(v)}")
    print("\nVerify with:  python imagegen.py --check")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--show", action="store_true", help="report current keys, masked")
    ap.add_argument("--force", action="store_true", help="re-prompt even if a key is set")
    args = ap.parse_args()

    if args.show:
        show()
        return 0

    if not sys.stdin.isatty():
        print("ERROR: this needs an interactive terminal — it reads keys with getpass so "
              "they never reach your shell history.\n"
              f"  Run it yourself:  python \"{__file__}\"\n"
              f"  Or write {cfg.USER_ENV} by hand with KEY=value lines.", file=sys.stderr)
        return 2

    print("Paste a key to store it, or press Enter to skip.\n")
    collected = {}
    for var, label, url in PROVIDERS:
        current, source = cfg.resolve_key((var,))
        if current and not args.force:
            print(f"{label}: already set ({cfg.mask(current)} from {source}) — skipping.")
            continue
        print(f"{label}  —  get one at {url}")
        value = getpass(f"  {var}: ").strip()
        if value:
            collected[var] = value
        print()

    if not collected:
        print("Nothing to write.")
        return 0
    write(collected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
