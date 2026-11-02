"""
Filesystem plumbing shared by every provider: reading prompts, validating references,
naming outputs, and writing image bytes to disk truthfully.

The one non-obvious piece is write_image(): a provider hands back raw bytes whose real
container may not match the extension you asked for. Gemini in particular returns JPEG
bytes that, written straight to `foo.png`, produce a file that is a JPEG wearing a .png
name — which then breaks every downstream tool that trusts the extension. So we sniff
the bytes and re-encode when they disagree with the requested format.

IF YOU CHANGE THIS FILE: write_image() is the only place allowed to touch output bytes.
If you add a format, add it to _PIL_FORMAT too or the re-encode branch will silently
fall back to a straight byte dump.
"""
import os
import sys

_PIL_FORMAT = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "webp": "WEBP"}


def force_utf8_stdout():
    """Windows consoles default to cp1252, which cannot print Arabic prompt heads or the
    ✓ / → in the summary. Without this the tool crashes on its own output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_prompt(prompt: str = None, prompt_file: str = None) -> tuple:
    """-> (text, source_path_or_None).

    Prefer prompt_file everywhere: real prompts are long, multiline, and often contain
    Arabic and quotes, all of which are painful as a shell argument (especially in
    PowerShell)."""
    if prompt_file:
        if not os.path.isfile(prompt_file):
            raise FileNotFoundError(f"prompt file not found: {prompt_file}")
        with open(prompt_file, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if not text:
            raise ValueError(f"prompt file is empty: {prompt_file}")
        return text, prompt_file
    if prompt:
        return prompt.strip(), None
    raise ValueError("provide --prompt or --prompt-file")


def check_refs(paths, max_refs: int) -> list:
    """Validate reference paths up front, before any money is spent. Returns warnings."""
    warnings = []
    for p in paths:
        if not os.path.isfile(p):
            raise FileNotFoundError(f"reference image not found: {p}")
    if len(paths) > max_refs:
        warnings.append(
            f"{len(paths)} references, but this model accepts at most {max_refs} — "
            "extras may be ignored or degrade the result."
        )
    return warnings


def resolve_out(out: str, out_dir: str = None) -> str:
    """-> the ABSOLUTE output path.

    A relative -o resolves against --out-dir when one is given, otherwise against the
    current directory. Resolving once, here, is what makes a log row still meaningful
    months later: the tool is app-wide, so "restyled/x.png" on its own does not say which
    repo it landed in. --out-dir never overrides an absolute -o.
    """
    if not os.path.isabs(out):
        base = os.path.expanduser(out_dir) if out_dir else os.getcwd()
        out = os.path.join(base, out)
    return os.path.abspath(out)


def output_paths(out: str, n: int) -> list:
    """One path for n=1; -1/-2/-3 suffixes beyond that. Naming is the caller's job —
    this only handles the multi-candidate case. Always absolute (see resolve_out)."""
    out = os.path.abspath(out)
    if n <= 1:
        return [out]
    stem, ext = os.path.splitext(out)
    return [f"{stem}-{i}{ext}" for i in range(1, n + 1)]


def guard_overwrite(paths, overwrite: bool):
    if overwrite:
        return
    clashes = [p for p in paths if os.path.exists(p)]
    if clashes:
        raise FileExistsError(
            "output already exists (pass --overwrite to replace): " + ", ".join(clashes)
        )


def _sniff(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "unknown"


def write_image(data: bytes, dest: str, want: str = "png") -> tuple:
    """Write image bytes to `dest` in the `want` container. -> (width, height).

    Re-encodes when the bytes are not already in the requested format, so a .png on disk
    is genuinely a PNG.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    want = (want or "png").lower()
    actual = _sniff(data)

    if actual == want or (want == "jpg" and actual == "jpeg"):
        with open(dest, "wb") as f:
            f.write(data)
        return _dimensions(dest)

    try:
        import io
        from PIL import Image
    except ImportError:
        # Better a correctly-named-but-wrong-container file than no file at all after a
        # paid call — but say so loudly.
        with open(dest, "wb") as f:
            f.write(data)
        print(f"  ⚠️  wrote {actual} bytes to a .{want} name — Pillow not installed, "
              f"could not re-encode. pip install pillow")
        return (None, None)

    im = Image.open(io.BytesIO(data))
    fmt = _PIL_FORMAT.get(want, "PNG")
    if fmt == "JPEG" and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    elif im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGB")
    im.save(dest, format=fmt)
    return im.size


def _dimensions(path: str) -> tuple:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (None, None)
