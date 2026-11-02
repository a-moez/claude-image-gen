"""
Nano Banana (Google Gemini image generation).

This is an earlier standalone script's proven logic, moved behind the ImageProvider interface. Behaviour
is deliberately unchanged — it is the regression baseline for the port. Two things carried
over verbatim because they were learned the hard way:

  - `n` is N SEPARATE CALLS, not a request for N candidates. The API does not honour a
    requested image count, so asking for 4 in one call returns 1. The N calls run
    concurrently (bounded by --parallel, see core/concurrency.py), and each is billed.
  - the returned bytes are often JPEG even when you asked for PNG (handled centrally now,
    in core/io_utils.write_image).

STATELESS: every call is (prompt + 0..N reference images) -> image. An "edit" or a "swap"
is just a generate with the previous version fed back as the first --ref. There are no
chat sessions to maintain.

PRICING is a flat per-image rate by (model, resolution) — genuinely knowable before the
call, unlike OpenAI's token billing. So the estimate here is the final number and
`estimated` stays True only in the sense that no usage payload confirms it.

Every API image carries an invisible SynthID watermark (metadata, not visible). The
visible corner "sparkle" is a Gemini WEB-APP artifact and is NOT added by the API — output
from this path is clean and never needs desparkling.

IF YOU CHANGE THIS FILE: the COST table is the money number shown to the user before they
approve a run. Re-check it against ai.google.dev/pricing rather than adjusting it to match
an invoice you half-remember.
"""
import os

from providers.base import (
    ImageProvider, ProviderError, AuthError, BlockedError, RateLimitedError,
)

MAX_REFS = 14
VALID_ASPECTS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
VALID_RES = {"1K", "2K", "4K"}
VALID_THINKING = {"minimal", "high"}

# approx $/image by model id + resolution (standard tier; the batch tier halves it)
COST = {
    "gemini-3.1-flash-image":      {"1K": 0.067, "2K": 0.101, "4K": 0.151},
    "gemini-3-pro-image":          {"1K": 0.134, "2K": 0.134, "4K": 0.24},
    "gemini-3.1-flash-lite-image": {"1K": 0.0336, "2K": 0.0336, "4K": 0.0336},
    "gemini-2.5-flash-image":      {"1K": 0.039, "2K": 0.078, "4K": 0.156},
}


class GeminiImages(ImageProvider):
    name = "gemini"
    default_model = "flash"
    key_url = "https://aistudio.google.com/apikey"
    env_keys = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    id_prefixes = ("gemini-",)
    max_refs = MAX_REFS
    aliases = {
        "flash":  "gemini-3.1-flash-image",          # default workhorse
        "pro":    "gemini-3-pro-image",              # unforgiving jobs
        "lite":   "gemini-3.1-flash-lite-image",     # cheapest, 1K only
        "legacy": "gemini-2.5-flash-image",          # RETIRES 2026-10-02
    }

    # --- offline -------------------------------------------------------------
    def validate(self, req) -> list:
        warnings = []
        if req.aspect not in VALID_ASPECTS:
            raise ProviderError(
                f"aspect '{req.aspect}' is not supported by Gemini. Use one of: "
                + ", ".join(sorted(VALID_ASPECTS))
            )
        if req.res not in VALID_RES:
            raise ProviderError(f"res '{req.res}' is not supported. Use 1K, 2K or 4K.")
        if req.thinking and req.thinking not in VALID_THINKING:
            raise ProviderError("--thinking must be 'minimal' or 'high'.")
        # (the reference-count check lives in core/io_utils.check_refs, driven by max_refs)
        # Foreign flags warn and continue, so a shared batch manifest still runs.
        for flag, value in (("--quality", req.quality), ("--size", req.size),
                            ("--mask", req.mask), ("--background", req.background),
                            ("--moderation", req.moderation)):
            if value:
                warnings.append(f"{flag} is an OpenAI option; Gemini ignores it.")
        if req.n > 1:
            warnings.append(
                f"n={req.n} runs {req.n} SEPARATE calls — Gemini does not honour a "
                "requested image count, and each call is billed."
            )
        return warnings

    def describe(self, req) -> dict:
        return {
            "model": self.resolve_model_id(req.model),
            "aspect": req.aspect,
            "res": req.res,
            "thinking": req.thinking or "(model default)",
            "mode": "generate" + (f" with {len(req.refs)} ref(s)" if req.refs else ""),
        }

    def estimate_cost(self, req) -> tuple:
        model_id = self.resolve_model_id(req.model)
        table = COST.get(model_id)
        if not table:
            return None, f"no published price for '{model_id}' — cost unknown"
        per = table.get(req.res) or table.get("1K")
        return round(per * req.n, 4), None

    # --- the paid call --------------------------------------------------------
    def generate(self, req) -> list:
        from concurrent.futures import ThreadPoolExecutor
        from core.request import GenResult
        from core import concurrency, io_utils

        client = self._client()
        config = self._config(req)
        model_id = self.resolve_model_id(req.model)
        refs = self._load_refs(req.refs)
        per_image, _ = self.estimate_cost(req)
        per_image = (per_image / req.n) if per_image is not None else None
        paths = io_utils.output_paths(req.out, req.n)

        def one(i):
            # Each call gets its own copies: the SDK serialises a PIL image on send, and
            # concurrent saves of ONE shared image object are not guaranteed safe.
            contents = [req.prompt] + [im.copy() for im in refs]   # prompt, then image 1, 2, ...

            def call():
                concurrency.trace(f"sent      {os.path.basename(paths[i])}")
                try:
                    response = client.models.generate_content(
                        model=model_id, contents=contents, config=config,
                    )
                except Exception as e:
                    concurrency.trace(f"failed    {os.path.basename(paths[i])}")
                    raise self._classify(e)
                concurrency.trace(f"received  {os.path.basename(paths[i])}")
                return response

            response = concurrency.call_with_retry(call)
            data, text = self._extract(response)
            if data is None:
                print(f"  NO IMAGE ({i + 1}/{req.n}): "
                      f"{text or 'no image and no text returned (possibly blocked).'}")
                return None

            w, h = io_utils.write_image(data, paths[i], req.output_format)
            print(f"  ✓ wrote {paths[i]}" + (f"  ({w}×{h})" if w else ""))
            return GenResult(
                path=paths[i], model_id=model_id, provider=self.name,
                cost=per_image, estimated=True, usage=None, width=w, height=h,
            )

        with ThreadPoolExecutor(max_workers=req.n) as pool:
            futures = [pool.submit(concurrency.inherit(one), i) for i in range(req.n)]

        # Images that DID land are returned (and so logged) even when a sibling call
        # failed — they were paid for. Only when nothing landed does the error propagate.
        results, errors = [], []
        for i, fut in enumerate(futures):
            try:
                result = fut.result()
            except ProviderError as e:
                errors.append(e)
                print(f"  FAILED ({i + 1}/{req.n}): {e}")
                continue
            if result is not None:
                results.append(result)
        if errors and not results:
            raise errors[0]
        return results

    # --- internals ------------------------------------------------------------
    def _client(self):
        from core import config as cfg
        key, source = cfg.resolve_key(self.env_keys)
        if not key:
            raise AuthError(cfg.missing_key_message(self))
        try:
            from google import genai
        except ImportError:
            from core import config as c
            raise ProviderError(
                "the google-genai package is not installed for this interpreter.\n"
                f"  Fix: pip install -r \"{c.REQUIREMENTS}\""
            )
        return genai.Client(api_key=key)

    def _config(self, req):
        from google.genai import types
        kwargs = dict(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=req.aspect, image_size=req.res),
        )
        if req.thinking:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=req.thinking)
        kwargs.update(req.extra)
        return types.GenerateContentConfig(**kwargs)

    def _load_refs(self, paths):
        from PIL import Image
        imgs = []
        for p in paths:
            im = Image.open(p)
            im.load()
            imgs.append(im)
        return imgs

    @staticmethod
    def _extract(response) -> tuple:
        """-> (image_bytes | None, joined_text)."""
        def parts():
            direct = getattr(response, "parts", None)
            if direct:
                yield from direct
                return
            for cand in (getattr(response, "candidates", None) or []):
                content = getattr(cand, "content", None)
                yield from (getattr(content, "parts", None) or [])

        data, texts = None, []
        for part in parts():
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None) and data is None:
                data = inline.data
            elif getattr(part, "text", None):
                texts.append(part.text)
        return data, " ".join(t.strip() for t in texts if t).strip()

    @staticmethod
    def _classify(exc) -> Exception:
        """Turn an SDK exception into something the user can act on.

        The distinction that matters: a sandbox egress proxy refusing the request looks
        like a 403, but is NOT an auth problem. Reporting it as one sends people to
        regenerate a perfectly good key.
        """
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        if "proxy" in low or "tunnel" in low or "econnrefused" in low:
            return BlockedError(
                "the request never reached Google — a network proxy refused it.\n"
                "  This is NOT a bad API key. Sandboxed environments (e.g. Claude Cowork) "
                "route egress through an allowlist that excludes "
                "generativelanguage.googleapis.com.\n"
                "  Run the same command in your own terminal, outside the sandbox.\n"
                f"  Underlying error: {text}"
            )
        if "api key" in low or "unauthenticated" in low or "permission_denied" in low:
            return AuthError(
                f"Google rejected the credential: {text}\n"
                "  Check the key at https://aistudio.google.com/apikey. "
                "(An 'AQ.' prefix IS a valid current key format — that is not the problem.)"
            )
        if "resource_exhausted" in low or "429" in low.split() or "rate limit" in low:
            return RateLimitedError(
                f"Google throttled the request (rate limit): {text}\n"
                "  Lower --parallel if this keeps happening."
            )
        return ProviderError(text)
