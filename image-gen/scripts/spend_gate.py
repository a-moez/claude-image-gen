#!/usr/bin/env python3
"""
PreToolUse spend gate — layer 3 of the money guardrail.

Installed in ~/.claude/settings.json (NOT a project's settings), so it fires in EVERY
repo. It reads the pending tool call from stdin and, when the command is a real paid image
generation, returns permissionDecision "ask" so the harness forces a human confirmation
before the command runs — even if Bash is otherwise allow-listed, and even if the model
skipped the soft layer-1 rule.

This is what makes the gate enforced by the harness rather than by model goodwill.

Everything that cannot spend passes through silently: --dry-run, --usage, --calibrate,
--check, --list-models, --help, and every non-image command.

IF YOU CHANGE THIS FILE: the failure modes are not symmetric. Gating something harmless
costs one keystroke; missing a real call spends money silently. When in doubt, gate.
Verify with:
    echo '{"tool_input":{"command":"python imagegen.py -o a.png"}}' | python spend_gate.py
    echo '{"tool_input":{"command":"python imagegen.py -o a.png --dry-run"}}' | python spend_gate.py
"""
import json
import re
import sys

PAID_SCRIPTS = ("imagegen.py",)
FREE_FLAGS = ("--dry-run", "--usage", "--calibrate", "--check", "--list-models")


def spends_money(command: str) -> bool:
    if not any(name in command for name in PAID_SCRIPTS):
        return False
    if any(flag in command for flag in FREE_FLAGS):
        return False
    if re.search(r"(?:^|\s)(?:--help|-h)(?:\s|$)", command):
        return False
    return True


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        # Unparseable event — stay out of the way rather than block every command.
        return 0

    command = (data.get("tool_input") or {}).get("command", "") or ""
    if spends_money(command):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    "This generates images through a paid API and SPENDS money. "
                    "Confirm the model, size/quality and image count before proceeding. "
                    "Add --dry-run to price it for free instead."
                ),
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
