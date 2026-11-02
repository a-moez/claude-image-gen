"""
OpenAI GPT Image (the Images API: images.generate / images.edit).

Verified against developers.openai.com on 2026-09-16. This area moves fast — GPT Image 2.5
shipped on 2026-09-08, eight days before this file was written — so RE-CHECK the model
table and the quality tiers against live docs rather than trusting this comment.

TWO THINGS THAT MAKE THIS DIFFERENT FROM THE GEMINI PROVIDER
------------------------------------------------------------
1. SIZE. OpenAI has no aspect-ratio parameter; it takes an explicit "WIDTHxHEIGHT". So the
   universal `--aspect 4:5 --res 2K` is SOLVED here into a concrete pixel size (multiples
   of 16, inside the documented envelope). That is what lets one flag set drive both
   engines — a studio whose feed is locked to 4:5 gets 4:5 from either. `--size 1536x864`
   bypasses the solver entirely.

2. COST IS NOT KNOWABLE BEFORE THE CALL. OpenAI bills tokens ($30/1M output), not a flat
   per-image rate. So we estimate from published token anchors beforehand, and then read
   the response's `usage` payload to record what it ACTUALLY cost. The logged number is
   the real one. Where a tier has no published token count (xhigh, max) we return None and
   print "?" — inventing a number there would produce a figure nobody ever re-checks.

REFERENCES: >=1 ref switches generate() -> edit(). edit() takes up to 16 images
(png/webp/jpg, <50MB each) plus an optional mask, which applies to the FIRST image only.

`input_fidelity` exists on edit() for gpt-image-1 and gpt-image-1.5. It is deliberately
NOT sent for gpt-image-2 and later, which already process every input at high fidelity —
sending it is an error there, not a no-op.

IF YOU CHANGE THIS FILE:
  - _solve_size() is arithmetic with published constraints. If you change the pixel
    budgets, re-run the size assertions in reference/architecture.md ("The free regression suite").
  - TOKEN_ANCHORS are measurements, not preferences. `imagegen.py --calibrate` prints the
    real $/image from the spend log; correct the anchors from THAT, not from a blog post.
"""
import math
import os

from providers.base import (
    ImageProvider, ProviderError, AuthError, BlockedError, RateLimitedError,
)

MAX_REFS = 16
MAX_EDGE = 3840
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
EXPERIMENTAL_ABOVE = 2560 * 1440      # docs: "above 2560x1440 is experimental"
SIZE_MULTIPLE = 16
MIN_RATIO, MAX_RATIO = 1 / 3, 3.0     # documented aspect envelope

PIXEL_BUDGET = {"1K": 1_048_576, "2K": 2_097_152, "4K": 4_194_304}

VALID_QUALITY = {"low", "medium", "high", "xhigh", "max", "auto"}
# xhigh/max were introduced with the 2.5 family; older models reject them.
PREMIUM_QUALITY = {"xhigh", "max"}

# Output tokens for a 1024x1024 image, per quality tier. Scales linearly with pixel count
# (the docs' "portrait/landscape ~1.5x" matches 1024x1536 being 1.5x the pixels).
# xhigh and max are ABSENT ON PURPOSE — no published figure exists yet.
#
# These are the published gpt-image-1.x figures. The 2.5 family uses the measured figures
# below instead, wherever they exist.
TOKEN_ANCHORS = {"low": 272, "medium": 1056, "high": 4160}

# ⭐ MEASURED 2026-11-02 from 322 logged gpt-image-2.5 calls (`--calibrate` + the log's usage
# payloads). The published anchors are wrong for 2.5 in three ways, so _estimate_25() replaces
# them for this family at the measured tiers:
#   1. Output tokens are near-FLAT with size, not linear: `low` 157–211 and `medium` 366–462
#      per image across 0.9–2.4 MP. Measured only up to MEASURED_MAX_PIXELS; above it, the
#      linear published anchor is used (the safe direction).
#   2. Text input is ~0.24 tokens per prompt character (0.235–0.243 across 8 prompt files).
#   3. Each reference adds image-input tokens by its size, and they add up: 768 at ~0.5 MP,
#      1,032 at ~1 MP, 1,452–1,518 from ~2 MP up (3 refs = 1452+1452+768 = 3,672, exactly).
# Back-test on those calls: the old estimate was within ±15% on 5% of them (often 4x too low,
# since it ignored references); this one is within ±15% on 81%, and at worst 15% low.
# `high` is unmeasured on 2.5 and keeps the published anchor. Re-check with --calibrate.
MEASURED_OUT_25 = {"low": 211, "medium": 462}
MEASURED_MAX_PIXELS = 2048 * 1152
TEXT_TOKENS_PER_CHAR = 0.25
REF_TOKEN_STEPS = ((500_000, 768), (1_100_000, 1032))   # (max pixels, tokens); larger → REF_TOKENS_LARGE
REF_TOKENS_LARGE = 1520

# $ per 1M tokens: (text_in, image_in, output)
RATES = {
    "gpt-image-2.5-flare":    (5.00, 8.00, 30.00),
    "gpt-image-2.5-sunburst": (5.00, 8.00, 30.00),
    "gpt-image-2":            (5.00, 8.00, 30.00),
    "gpt-image-2-2026-04-21": (5.00, 8.00, 30.00),
    "chatgpt-image-latest":   (5.00, 8.00, 30.00),
    "gpt-image-1.5":          (5.00, 8.00, 32.00),
    "gpt-image-1":            (5.00, 10.00, 40.00),
    "gpt-image-1-mini":       (2.00, 2.50, 8.00),
}
DEFAULT_RATE = (5.00, 8.00, 30.00)

# Models that accept input_fidelity on edit(). Later models are always high-fidelity.
ACCEPTS_INPUT_FIDELITY = {"gpt-image-1", "gpt-image-1.5"}


def _parse_ratio(aspect: str) -> float:
    try:
        a, _, b = aspect.partition(":")
        ratio = float(a) / float(b)
    except (ValueError, ZeroDivisionError):
        raise ProviderError(f"could not read aspect '{aspect}' — expected W:H, e.g. 4:5")
    if not ratio:
        raise ProviderError(f"aspect '{aspect}' has zero width or height")
    return ratio


def _ref_tokens(path: str) -> int:
    """Measured image-input tokens for one reference, by its pixel count (see REF_TOKEN_STEPS)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            pixels = im.width * im.height
    except Exception:
        return REF_TOKENS_LARGE     # unreadable here: assume the largest measured step
    for max_pixels, tokens in REF_TOKEN_STEPS:
        if pixels <= max_pixels:
            return tokens
    return REF_TOKENS_LARGE


def _round16(x: float) -> int:
    return max(SIZE_MULTIPLE, int(round(x / SIZE_MULTIPLE)) * SIZE_MULTIPLE)


def _exact_pair(aspect: str, budget: int):
    """The multiple-of-16 pair that hits the ratio EXACTLY, nearest the pixel budget.

    Rounding width and height independently drifts the aspect: 4:5 at 1K lands on
    912x1152, which is 0.792:1, not 0.8:1. For a feed whose whole point is a locked 4:5,
    an exact ratio is worth more than hitting the pixel budget precisely — so scale the
    reduced ratio (4:5 -> a 64x80 tile) by whichever integer lands closest to budget.

    Returns None for a ratio whose reduced tile already exceeds the budget (e.g. 100:99),
    which falls back to independent rounding.
    """
    try:
        a_s, _, b_s = aspect.partition(":")
        a, b = int(a_s), int(b_s)
    except ValueError:
        return None                       # non-integer ratio like "1.5:1"
    if a <= 0 or b <= 0:
        return None
    g = math.gcd(a, b)
    base_w, base_h = SIZE_MULTIPLE * (a // g), SIZE_MULTIPLE * (b // g)
    tile = base_w * base_h
    if tile > budget:
        return None
    k = math.sqrt(budget / tile)
    candidates = [max(1, int(math.floor(k))), max(1, int(math.ceil(k)))]
    best = min(candidates, key=lambda c: abs(base_w * c * base_h * c - budget))
    return base_w * best, base_h * best


def solve_size(aspect: str, res: str) -> tuple:
    """(aspect, res) -> (width, height, warnings). The universal vocabulary, made concrete.

    Fills the resolution tier's pixel budget at the requested ratio, preferring a size
    that reproduces the ratio exactly (see _exact_pair) and falling back to independent
    rounding. Both edges are multiples of 16; the result is checked against the documented
    envelope.
    """
    ratio = _parse_ratio(aspect)
    if not (MIN_RATIO <= ratio <= MAX_RATIO):
        raise ProviderError(
            f"aspect '{aspect}' is outside OpenAI's supported range — the ratio must sit "
            f"between 1:3 and 3:1 (this one is {ratio:.2f}:1)."
        )
    if res not in PIXEL_BUDGET:
        raise ProviderError(f"res '{res}' is not supported. Use 1K, 2K or 4K.")

    budget = PIXEL_BUDGET[res]
    pair = _exact_pair(aspect, budget)
    if pair is None:
        pair = (_round16(math.sqrt(budget * ratio)), _round16(math.sqrt(budget / ratio)))
    w, h = pair
    return (w, h, _check_size(w, h, f"{aspect} at {res}"))


def _check_size(w: int, h: int, label: str) -> list:
    """Validate against the documented envelope. Errors name the constraint that failed."""
    warnings = []
    if w % SIZE_MULTIPLE or h % SIZE_MULTIPLE:
        raise ProviderError(
            f"{label} -> {w}x{h}: both edges must be multiples of {SIZE_MULTIPLE}."
        )
    if max(w, h) > MAX_EDGE:
        raise ProviderError(
            f"{label} -> {w}x{h}: the longest edge may not exceed {MAX_EDGE}px."
        )
    total = w * h
    if total < MIN_PIXELS:
        raise ProviderError(
            f"{label} -> {w}x{h} is {total:,}px, below the {MIN_PIXELS:,}px minimum. "
            "Use a higher --res."
        )
    if total > MAX_PIXELS:
        raise ProviderError(
            f"{label} -> {w}x{h} is {total:,}px, above the {MAX_PIXELS:,}px maximum. "
            "Use a lower --res."
        )
    if total > EXPERIMENTAL_ABOVE:
        warnings.append(
            f"{w}x{h} ({total:,}px) is above 2560x1440, which OpenAI documents as "
            "experimental — expect slower calls and occasional refusals."
        )
    return warnings


def parse_size(size: str) -> tuple:
    """Raw '--size WxH' passthrough, validated against the same envelope."""
    try:
        w_s, _, h_s = size.lower().partition("x")
        w, h = int(w_s), int(h_s)
    except ValueError:
        raise ProviderError(f"could not read --size '{size}' — expected WIDTHxHEIGHT, "
                            "e.g. 1536x864")
    return (w, h, _check_size(w, h, f"--size {size}"))


class OpenAIImages(ImageProvider):
    name = "openai"
    default_model = "flare"
    key_url = "https://platform.openai.com/api-keys"
    env_keys = ("OPENAI_API_KEY",)
    id_prefixes = ("gpt-image-", "dall-e-", "chatgpt-image-")
    max_refs = MAX_REFS
    aliases = {
        "flare":    "gpt-image-2.5-flare",     # fast default, high quality (2026-09-08)
        "sunburst": "gpt-image-2.5-sunburst",  # editing precision, slower
        "gpt2":     "gpt-image-2",
        "gpt15":    "gpt-image-1.5",
        "mini":     "gpt-image-1-mini",        # cheapest
    }

    # --- offline -------------------------------------------------------------
    def _geometry(self, req) -> tuple:
        if req.size:
            return parse_size(req.size)
        return solve_size(req.aspect, req.res)

    def _quality(self, req) -> str:
        # `high` is the default because this tool exists for product photography, where
        # the detail that proves a product is real is the whole point. It is also the
        # expensive tier, which is why the dry-run prints it loudly.
        return req.quality or "high"

    def validate(self, req) -> list:
        warnings = []
        _, _, size_warnings = self._geometry(req)
        warnings.extend(size_warnings)

        quality = self._quality(req)
        if quality not in VALID_QUALITY:
            raise ProviderError(
                f"quality '{quality}' is not valid. Use one of: "
                + ", ".join(sorted(VALID_QUALITY))
            )
        model_id = self.resolve_model_id(req.model)
        if quality in PREMIUM_QUALITY and not model_id.startswith("gpt-image-2.5"):
            warnings.append(
                f"quality '{quality}' was introduced with the 2.5 family; "
                f"{model_id} may reject it."
            )
        if quality in PREMIUM_QUALITY:
            warnings.append(
                f"quality '{quality}' has no published token count — the cost estimate "
                "will read '?'. The ACTUAL cost is recorded after the call."
            )

        # (the reference-count check lives in core/io_utils.check_refs, driven by max_refs)
        if req.mask and not req.refs:
            raise ProviderError(
                "--mask needs at least one --ref: a mask marks where to edit an existing "
                "image, so there must be an image to edit."
            )
        if not (1 <= req.n <= 10):
            raise ProviderError("n must be between 1 and 10 for OpenAI image models.")
        if req.background == "transparent" and req.output_format not in ("png", "webp"):
            raise ProviderError(
                "--background transparent requires --output-format png or webp "
                f"(got {req.output_format})."
            )
        if req.thinking:
            warnings.append("--thinking is a Gemini option; OpenAI ignores it.")
        return warnings

    def describe(self, req) -> dict:
        w, h, _ = self._geometry(req)
        source = "--size" if req.size else f"{req.aspect} @ {req.res}"
        return {
            "model": self.resolve_model_id(req.model),
            "size": f"{w}x{h}  (from {source})",
            "quality": self._quality(req),
            "mode": f"edit with {len(req.refs)} ref(s)" if req.refs else "generate",
        }

    def estimate_cost(self, req) -> tuple:
        """Pre-call estimate: measured rates on 2.5 where they exist, else published anchors."""
        quality = self._quality(req)
        if quality not in TOKEN_ANCHORS:
            return None, (f"quality '{quality}' has no published token count; "
                          "the real cost is recorded after the call")
        w, h, _ = self._geometry(req)
        model_id = self.resolve_model_id(req.model)
        if (model_id.startswith("gpt-image-2.5") and quality in MEASURED_OUT_25
                and w * h <= MEASURED_MAX_PIXELS):
            return self._estimate_25(req, quality, model_id)
        _, _, out_rate = RATES.get(model_id, DEFAULT_RATE)

        tokens = TOKEN_ANCHORS[quality] * (w * h) / 1_048_576
        cost = tokens * req.n * out_rate / 1_000_000

        note = None
        if req.refs:
            note = (f"output tokens only — {len(req.refs)} reference image(s) add input "
                    "tokens that OpenAI does not publish a rate card for; the logged "
                    "figure after the call includes them")
        return round(cost, 4), note

    def _estimate_25(self, req, quality, model_id) -> tuple:
        """Measured 2.5 estimate: output + prompt text + every reference (see MEASURED_OUT_25)."""
        text_rate, image_rate, out_rate = RATES.get(model_id, DEFAULT_RATE)
        text_tokens = len(req.prompt or "") * TEXT_TOKENS_PER_CHAR
        ref_tokens = sum(_ref_tokens(p) for p in req.refs)
        # n is one call, but the logged n=2 calls were billed the inputs n times too
        # (text 650 = 2 x 325), so everything scales with n.
        cost = (MEASURED_OUT_25[quality] * out_rate
                + text_tokens * text_rate + ref_tokens * image_rate) * req.n / 1_000_000
        note = f"measured rates, incl. {len(req.refs)} reference(s)" if req.refs else None
        return round(cost, 4), note

    # --- the paid call --------------------------------------------------------
    def generate(self, req) -> list:
        from core.request import GenResult
        from core import concurrency, io_utils

        client = self._client()
        model_id = self.resolve_model_id(req.model)
        w, h, _ = self._geometry(req)
        quality = self._quality(req)

        params = {
            "model": model_id,
            "prompt": req.prompt,
            "size": f"{w}x{h}",
            "quality": quality,
            "n": req.n,
            "output_format": req.output_format,
        }
        if req.background:
            params["background"] = req.background
        if req.moderation:
            params["moderation"] = req.moderation
        params.update(req.extra)

        handles = []
        try:
            if req.refs:
                handles = [open(p, "rb") for p in req.refs]
                params["image"] = handles
                if req.mask:
                    handles.append(open(req.mask, "rb"))
                    params["mask"] = handles[-1]
                # Only pre-2 models take this; later ones are always high-fidelity and
                # reject the parameter outright.
                if model_id in ACCEPTS_INPUT_FIDELITY:
                    params["input_fidelity"] = "high"
                method = client.images.edit
            else:
                method = client.images.generate

            call_kwargs, extra_body = self._split_params(method, params)
            if extra_body:
                call_kwargs["extra_body"] = extra_body

            def call():
                # A retry after a 429 must re-send the files from the start: the failed
                # attempt has already read every handle to EOF.
                for fh in handles:
                    fh.seek(0)
                concurrency.trace(f"sent      {os.path.basename(req.out)}")
                try:
                    response = method(**call_kwargs)
                except Exception as e:
                    concurrency.trace(f"failed    {os.path.basename(req.out)}")
                    raise self._classify(e)
                concurrency.trace(f"received  {os.path.basename(req.out)}")
                return response

            response = concurrency.call_with_retry(call)
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass

        return self._collect(req, response, model_id, io_utils, GenResult)

    def _collect(self, req, response, model_id, io_utils, GenResult) -> list:
        import base64

        data = getattr(response, "data", None) or []
        if not data:
            raise ProviderError("the API returned no image data.")

        actual_total = self._actual_cost(response, model_id)
        per_image = (actual_total / len(data)) if actual_total is not None else None
        if per_image is None:
            est, _ = self.estimate_cost(req)
            per_image = (est / req.n) if est is not None else None

        paths = io_utils.output_paths(req.out, len(data))
        results = []
        for i, item in enumerate(data):
            b64 = getattr(item, "b64_json", None)
            if not b64:
                print(f"  NO IMAGE ({i + 1}/{len(data)}): the response carried no bytes.")
                continue
            w, h = io_utils.write_image(base64.b64decode(b64), paths[i], req.output_format)
            print(f"  ✓ wrote {paths[i]}" + (f"  ({w}×{h})" if w else ""))
            results.append(GenResult(
                path=paths[i], model_id=model_id, provider=self.name,
                cost=per_image, estimated=actual_total is None,
                usage=self._usage_dict(response), width=w, height=h,
            ))
        return results

    # --- cost from real usage -------------------------------------------------
    @staticmethod
    def _usage_dict(response) -> dict:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            return usage.model_dump()
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }

    @classmethod
    def _actual_cost(cls, response, model_id: str):
        """The authoritative number: what the call really cost, from its own usage payload.
        Returns None when the response carried no usage (then the estimate stands in)."""
        usage = cls._usage_dict(response)
        if not usage:
            return None
        text_rate, image_rate, out_rate = RATES.get(model_id, DEFAULT_RATE)

        details = usage.get("input_tokens_details") or {}
        text_in = details.get("text_tokens")
        image_in = details.get("image_tokens")
        if text_in is None and image_in is None:
            # No breakdown — bill the whole input at the (higher) image rate rather than
            # flattering the number.
            text_in, image_in = 0, usage.get("input_tokens") or 0
        out = usage.get("output_tokens") or 0

        cost = ((text_in or 0) * text_rate
                + (image_in or 0) * image_rate
                + out * out_rate) / 1_000_000
        return round(cost, 6)

    # --- internals ------------------------------------------------------------
    def _client(self):
        from core import config as cfg
        key, _ = cfg.resolve_key(self.env_keys)
        if not key:
            raise AuthError(cfg.missing_key_message(self))
        try:
            import openai
        except ImportError:
            raise ProviderError(
                "the openai package is not installed for this interpreter.\n"
                f"  Fix: pip install -r \"{cfg.REQUIREMENTS}\""
            )
        major = int((getattr(openai, "__version__", "0") or "0").split(".")[0] or 0)
        if major < 2:
            raise ProviderError(
                f"openai {openai.__version__} is too old for the GPT Image models "
                "(gpt-image-2.5-* and input_fidelity need v2+).\n"
                f"  Fix: pip install -U \"openai>=2\""
            )
        # max_retries=0: the SDK's own retry also re-sends after timeouts and 5xx, where
        # the call may already have been billed. core/concurrency.py retries 429s only.
        return openai.OpenAI(api_key=key, max_retries=0)

    @staticmethod
    def _split_params(method, params: dict) -> tuple:
        """Route parameters the installed SDK does not know about into extra_body.

        The SDK's typed signature lags the API by weeks — GPT Image 2.5 landed eight days
        before this was written. Rather than pinning ourselves to whatever the local
        version happens to accept, send known params normally and pass the rest through
        extra_body, which the SDK forwards verbatim.
        """
        import inspect
        try:
            sig = inspect.signature(method)
        except (TypeError, ValueError):
            return dict(params), {}
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return dict(params), {}

        known = set(sig.parameters)
        supported, extra = {}, {}
        for k, v in params.items():
            (supported if k in known else extra)[k] = v
        return supported, extra

    @staticmethod
    def _classify(exc) -> Exception:
        """Distinguish 'a proxy blocked us' from 'the key is bad'. These look alike (both
        surface as 403) and conflating them sends people to regenerate a working key."""
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()

        if "proxy" in low or "tunnel" in low or "econnrefused" in low:
            return BlockedError(
                "the request never reached OpenAI — a network proxy refused it.\n"
                "  This is NOT a bad API key. Sandboxed environments (e.g. Claude Cowork) "
                "route egress through an allowlist that excludes api.openai.com.\n"
                "  Run the same command in your own terminal, outside the sandbox.\n"
                f"  Underlying error: {text}"
            )
        if "authenticationerror" in low or "invalid_api_key" in low or "incorrect api key" in low:
            return AuthError(
                f"OpenAI rejected the credential: {text}\n"
                "  Check the key at https://platform.openai.com/api-keys"
            )
        if "permissiondenied" in low or "insufficient_quota" in low or "billing" in low:
            return AuthError(
                f"OpenAI accepted the key but refused the request: {text}\n"
                "  This is usually billing or model access, not the key itself — check "
                "https://platform.openai.com/settings/organization/billing"
            )
        # After the quota branch on purpose: `insufficient_quota` also arrives as a
        # RateLimitError, and retrying an empty balance is pointless.
        if "ratelimiterror" in low or "rate_limit_exceeded" in low or "rate limit" in low:
            return RateLimitedError(
                f"OpenAI throttled the request (rate limit): {text}\n"
                "  Lower --parallel if this keeps happening."
            )
        if "moderation" in low or "safety" in low or "content_policy" in low:
            return ProviderError(
                f"the prompt was refused by content moderation: {text}\n"
                "  --moderation low relaxes the threshold where your account permits it."
            )
        return ProviderError(text)
