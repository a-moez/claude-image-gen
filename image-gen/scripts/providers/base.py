"""
The provider contract.

Adding a third engine means ONE new file here plus one line in core/config.py's registry.
Nothing in imagegen.py, core/batch.py or core/spend.py should ever name a provider.

Four things a provider must supply:
  - class attributes that let the router find it (aliases, id_prefixes, env_keys)
  - validate()      cheap, offline, raises ProviderError on a bad combination
  - estimate_cost() best pre-call guess, or None when genuinely unknown
  - generate()      the paid call

IF YOU CHANGE THIS FILE: it is the seam the whole tool is built on. Changing a method
signature means editing every provider in providers/ and the call sites in
imagegen.py + core/batch.py.
"""
from abc import ABC, abstractmethod


class ProviderError(Exception):
    """A request the provider cannot serve. Message is shown to the user verbatim, so
    it must name the constraint that failed, not just say 'invalid'."""


class AuthError(ProviderError):
    """A genuine credential problem — the provider itself rejected the key."""


class BlockedError(ProviderError):
    """The request never reached the provider: a sandbox egress proxy refused it.

    This is NOT a bad key, and must never be reported as one. Claude Cowork routes all
    egress through an allowlisting proxy that excludes the image endpoints, so the call
    dies at the proxy with a 403 that has nothing to do with the credential. Misreading
    this once already sent a user chasing a key regeneration that could not have helped.
    """


class RateLimitedError(ProviderError):
    """The provider throttled the request (HTTP 429 / RESOURCE_EXHAUSTED).

    The ONLY error core/concurrency.py retries: a throttled request was refused before
    any work was done, so it is not billed and a retry cannot double-spend. A quota or
    billing refusal is NOT this — it is an AuthError, and retrying it is pointless.
    """


class ImageProvider(ABC):
    # --- routing metadata (class attributes) ---------------------------------
    name: str = ""                  # "openai" | "gemini"
    aliases: dict = {}              # "flare" -> "gpt-image-2.5-flare"
    id_prefixes: tuple = ()         # ("gpt-image-", "dall-e-") — routes a full model id
    env_keys: tuple = ()            # ("OPENAI_API_KEY",) — first hit wins
    default_model: str = ""         # alias used when --model is omitted
    key_url: str = ""               # where a human gets a key, printed on failure
    max_refs: int = 8               # reference images this engine actually accepts

    # --- the three verbs ------------------------------------------------------
    @abstractmethod
    def validate(self, req) -> list:
        """Check the request offline. Raise ProviderError on anything unserveable.
        Return a list of human-readable warning strings (e.g. an ignored foreign flag)."""

    @abstractmethod
    def describe(self, req) -> dict:
        """What this request will actually ask for, in the provider's own terms, for the
        dry-run summary. Free, offline. e.g. {"size": "1296x1616", "quality": "high"}."""

    @abstractmethod
    def estimate_cost(self, req) -> tuple:
        """-> (dollars | None, note | None). None means genuinely unknown — print '?'
        rather than inventing a number that nobody will re-check."""

    @abstractmethod
    def generate(self, req) -> list:
        """Make the paid call. Returns a list of GenResult. Raises AuthError /
        BlockedError / ProviderError with a message the user can act on."""

    # --- shared helpers -------------------------------------------------------
    def resolve_model_id(self, model: str) -> str:
        """Alias -> full id. An unrecognised name passes through untouched so a model
        released after this file was written still works."""
        return self.aliases.get(model or self.default_model, model or self.default_model)
