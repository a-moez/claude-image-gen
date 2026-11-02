"""
The shared vocabulary every provider speaks.

A GenRequest is provider-agnostic on purpose: `--aspect 4:5 --res 2K` means the same
thing whether it lands on Gemini (which takes aspect + size tiers natively) or OpenAI
(where the provider solves them into a `WIDTHxHEIGHT` string). Provider-specific fields
are present but optional, and a provider that doesn't understand one WARNS rather than
erroring — a batch manifest shared across providers must not fail on an ignorable field.

A GenResult always carries the output path and the model that made it. `cost` is the
ACTUAL cost when the provider reports token usage (OpenAI), and the estimate otherwise
(Gemini, which bills a flat per-image rate); `estimated` says which.

IF YOU CHANGE THIS FILE: both providers and core/batch.py construct GenRequest by
keyword. Adding a field is safe (give it a default); renaming one is not — grep
`providers/` and `core/batch.py` first.
"""
from dataclasses import dataclass, field


@dataclass
class GenRequest:
    """One image-generation job. `out` is the caller's chosen path; naming is never
    inferred (see the naming rules in SKILL.md)."""

    prompt: str
    out: str
    refs: list = field(default_factory=list)   # reference image paths, in order
    model: str = ""                            # alias ("flare", "pro") or full model id
    n: int = 1

    # --- universal: every provider must honour these -------------------------
    aspect: str = "4:5"
    res: str = "2K"                            # 1K | 2K | 4K

    # --- OpenAI-native (ignored, with a warning, by other providers) ---------
    quality: str = None                        # low|medium|high|xhigh|max|auto
    size: str = None                           # raw "WxH"; bypasses the aspect/res solver
    mask: str = None                           # PNG with alpha; applies to refs[0]
    background: str = None                     # transparent|opaque|auto
    moderation: str = None                     # low|auto

    # --- Gemini-native (ignored, with a warning, by other providers) ---------
    thinking: str = None                       # minimal|high

    # --- shared plumbing ------------------------------------------------------
    output_format: str = "png"                 # png|jpeg|webp
    out_dir: str = None                        # base for a relative `out`; never overrides an absolute one
    project: str = None                        # tag recorded in the spend log
    overwrite: bool = False
    extra: dict = field(default_factory=dict)  # passthrough to the provider's SDK call

    def head(self, chars: int = 80) -> str:
        """First line of the prompt, for logs and the dry-run summary."""
        flat = " ".join(self.prompt.split())
        return flat[:chars] + ("…" if len(flat) > chars else "")


@dataclass
class GenResult:
    """One produced image."""

    path: str
    model_id: str
    provider: str
    cost: float = None          # None when genuinely unknown (unpriced quality tier)
    estimated: bool = True      # False once a real `usage` payload backed the number
    usage: dict = None          # provider's raw token accounting, when it reports one
    width: int = None
    height: int = None
