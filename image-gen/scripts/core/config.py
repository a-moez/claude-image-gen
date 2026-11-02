"""
Paths, the API-key resolution chain, and the provider registry / router.

KEY RESOLUTION — first hit wins, evaluated per provider:
  1. the process environment            (an explicit export always wins)
  2. the nearest .env, walking up from cwd to the git root inclusive
     (this is what keeps a project that already has its own .env working unchanged)
  3. ~/.config/claude-imagegen/.env     (the portable, app-wide store — `setup_key.py`)

A key value is NEVER printed. Every diagnostic goes through mask() first.

ROUTING — a model name picks its provider, so `--provider` is only ever needed to break
a genuine tie:
  1. an explicit --provider
  2. an alias owned by exactly one provider  ("flare" -> openai, "pro" -> gemini)
  3. a full model id matching a provider's id_prefixes  ("gpt-image-2.5-flare" -> openai)

IF YOU CHANGE THIS FILE: providers are imported LAZILY on purpose — `--model pro` must
not require the `openai` package to be installed, and vice versa. Keep the imports inside
load_provider().
"""
import os
from pathlib import Path

# --- paths -------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = SCRIPTS_DIR.parent
USER_CONFIG_DIR = Path.home() / ".config" / "claude-imagegen"
USER_ENV = USER_CONFIG_DIR / ".env"
USAGE_LOG = USER_CONFIG_DIR / "usage.log"
REQUIREMENTS = SCRIPTS_DIR / "requirements.txt"

# --- registry: provider name -> (module path, class name) --------------------
# Lazy so that using one provider never requires the other's SDK.
_REGISTRY = {
    "openai": ("providers.openai_images", "OpenAIImages"),
    "gemini": ("providers.gemini", "GeminiImages"),
}


def mask(value: str) -> str:
    """A key, rendered safe to print. Never returns enough to reconstruct anything."""
    if not value:
        return "(unset)"
    return f"…{value[-4:]} ({len(value)} chars)"


# --- key resolution ----------------------------------------------------------
def _parse_env_file(path: Path) -> dict:
    """Minimal KEY=VALUE reader. Deliberately not python-dotenv: this must work when
    only one of the two SDKs is installed, and the format we write is trivial."""
    out = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _is_repo_root(d: Path) -> bool:
    # .exists() not .is_dir(): a worktree or submodule has .git as a FILE.
    return (d / ".git").exists()


def find_git_root(start: Path = None):
    """The enclosing repository root, or None outside one.

    This is what tags a spend-log row with WHERE a run happened. The log is app-wide, so
    without it "what did this repo cost me" has no answer — cwd alone is a leaf, not a key,
    and it changes with every subdirectory you happen to stand in.
    """
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        if _is_repo_root(d):
            return d
    return None


def _project_env_files(start: Path = None) -> list:
    """Every .env from cwd up to and including the git root (nearest first).

    Stops at the git root rather than walking to the filesystem root, so a stray .env in
    C:\\Users\\<you> can never silently supply a key to an unrelated project.
    """
    here = (start or Path.cwd()).resolve()
    found = []
    for d in [here, *here.parents]:
        candidate = d / ".env"
        if candidate.is_file():
            found.append(candidate)
        if _is_repo_root(d):
            break
    return found


def resolve_key(env_keys, start: Path = None) -> tuple:
    """-> (value, source_description) or (None, None).

    `env_keys` is the provider's tuple of acceptable variable names, in priority order
    (e.g. GEMINI_API_KEY then GOOGLE_API_KEY)."""
    for name in env_keys:
        if os.environ.get(name):
            return os.environ[name], f"environment variable {name}"

    for envfile in _project_env_files(start):
        values = _parse_env_file(envfile)
        for name in env_keys:
            if values.get(name):
                return values[name], f"{envfile}"

    if USER_ENV.is_file():
        values = _parse_env_file(USER_ENV)
        for name in env_keys:
            if values.get(name):
                return values[name], f"{USER_ENV}"

    return None, None


def missing_key_message(provider) -> str:
    """The error a user can actually act on — which file, which variable, where to get
    the key. Never a bare stack trace."""
    primary = provider.env_keys[0]
    return (
        f"No {provider.name} API key found.\n"
        f"  Looked in: the environment ({', '.join(provider.env_keys)}), "
        f"any .env from here up to the git root, and {USER_ENV}\n"
        f"  Fix it once for every repo:\n"
        f"      python \"{SCRIPTS_DIR / 'setup_key.py'}\"\n"
        f"  Or write it yourself: put {primary}=... in {USER_ENV}\n"
        f"  Get a key at {provider.key_url}"
    )


# --- routing -----------------------------------------------------------------
def load_provider(name: str):
    """Instantiate a provider by registry name. Imports its SDK-touching module lazily."""
    import importlib
    if name not in _REGISTRY:
        raise KeyError(f"unknown provider '{name}'; known: {', '.join(sorted(_REGISTRY))}")
    module_path, class_name = _REGISTRY[name]
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


def all_providers() -> list:
    return [load_provider(n) for n in sorted(_REGISTRY)]


def route(model: str = None, provider: str = None):
    """Pick the provider for this request. Returns an ImageProvider instance."""
    if provider:
        return load_provider(provider)

    if not model:
        raise ValueError(
            "No --model given and no --provider to fall back on. "
            "Pass a model alias (see --list-models) or --provider openai|gemini."
        )

    candidates = [p for p in all_providers() if model in p.aliases]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise ValueError(
            f"Alias '{model}' is claimed by more than one provider ({names}). "
            f"Disambiguate with --provider."
        )

    for p in all_providers():
        if any(model.startswith(prefix) for prefix in p.id_prefixes):
            return p

    known = []
    for p in all_providers():
        known.append(f"  {p.name}: {', '.join(sorted(p.aliases))}")
    raise ValueError(
        f"Don't know which provider serves model '{model}'.\n"
        "Known aliases:\n" + "\n".join(known) + "\n"
        "  A brand-new model id also routes automatically if it starts with a known "
        "prefix (gpt-image-, dall-e-, gemini-); otherwise pass --provider."
    )
