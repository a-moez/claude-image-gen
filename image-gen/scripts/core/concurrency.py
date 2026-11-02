"""
Concurrency: how many requests may be in flight, whose output goes where, and the one
error that is safe to retry.

PARALLELISM CHANGES WALL-CLOCK TIME, NEVER COST. A batch of six bills six images whether
they run one after another or four at a time. What it does change is that several calls
now run at once, and four things they share have to be made safe:

  1. the request budget — ONE process-wide semaphore. A batch worker and a Gemini `n>1`
     fan-out both draw from it, so `--parallel 4` means at most 4 requests in flight in
     total, never 4 items × 4 candidates.
  2. stdout — each worker's prints are buffered per thread and emitted as one block when
     the item finishes, so two items' "✓ wrote" lines never interleave. Providers keep
     calling plain print(); they do not know this exists.
  3. the spend log — locked in core/spend.py.
  4. output paths — two items writing the same file is refused before billing, in
     core/batch.py.

ONLY A RATE LIMIT IS RETRIED. A 429 means the request was refused before any work was
done, so it is not billed and retrying it cannot double-spend. A timeout or a 5xx is NOT
retried: the provider may have finished (and billed) a call whose response we never saw.

IF YOU CHANGE THIS FILE: a request must hold a slot only while it is actually on the
wire. Sleeping through a backoff inside in_flight() would starve the other workers, and
holding a slot while waiting on child threads that also need one deadlocks at
`--parallel 1`.
"""
import contextlib
import os
import random
import sys
import threading
import time
from datetime import datetime

from providers.base import RateLimitedError

TRACE = bool(os.environ.get("IMAGEGEN_TRACE"))


def trace(event: str):
    """IMAGEGEN_TRACE=1 prints a wall-clock stamp per HTTP event. Free; for diagnosing where
    time goes (before the send = our side, between send and receive = the provider)."""
    if TRACE:
        print(f"    trace {datetime.now().strftime('%H:%M:%S.%f')[:-3]} {event}")

DEFAULT_PARALLEL = 4
RETRY_DELAYS = (2, 4, 8)          # seconds before retry 1, 2, 3 — plus up to 50% jitter

_slots = threading.BoundedSemaphore(DEFAULT_PARALLEL)
_local = threading.local()
_write_lock = threading.Lock()


def configure(parallel: int):
    """Set the process-wide limit on requests in flight. Call before any work starts."""
    global _slots
    if parallel < 1:
        raise ValueError(f"--parallel must be at least 1, got {parallel}")
    _slots = threading.BoundedSemaphore(parallel)


@contextlib.contextmanager
def in_flight():
    """Hold one request slot for the duration of a single API call."""
    slots = _slots
    with slots:
        yield


def call_with_retry(fn):
    """Run one API call inside a slot, retrying ONLY RateLimitedError with backoff.

    The slot is released during the backoff, so a throttled call never blocks the rest.
    """
    for attempt, delay in enumerate((*RETRY_DELAYS, None), 1):
        try:
            with in_flight():
                return fn()
        except RateLimitedError:
            if delay is None:
                raise
            wait = delay + random.uniform(0, delay / 2)
            print(f"    rate-limited — retry {attempt}/{len(RETRY_DELAYS)} in {wait:.0f}s")
            time.sleep(wait)


# --- per-thread stdout --------------------------------------------------------
class _Router:
    """sys.stdout stand-in: a thread with an active buffer writes into it, every other
    thread writes straight through (under a lock, so lines never tear)."""

    def __init__(self, real):
        self._real = real

    def write(self, s):
        buf = getattr(_local, "buffer", None)
        if buf is None:
            with _write_lock:
                return self._real.write(s)
        buf.append(s)
        return len(s)

    def flush(self):
        if getattr(_local, "buffer", None) is None:
            self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def routed_stdout():
    """Install the router for the duration of a parallel run."""
    real = sys.stdout
    sys.stdout = _Router(real)
    try:
        yield
    finally:
        sys.stdout = real


def buffered(fn, *args, **kwargs) -> tuple:
    """Run fn with this thread's prints captured. -> (result, text, unexpected_error).

    An exception is returned rather than raised so its item's output is not lost with it.
    """
    _local.buffer = []
    try:
        return fn(*args, **kwargs), "".join(_local.buffer), None
    except Exception as e:
        return None, "".join(_local.buffer), e
    finally:
        _local.buffer = None


def inherit(fn):
    """Wrap fn so a child thread prints into its PARENT's buffer (e.g. a Gemini n>1
    fan-out inside a batch worker). Outside a parallel run this is a no-op."""
    buf = getattr(_local, "buffer", None)

    def run(*args, **kwargs):
        _local.buffer = buf
        try:
            return fn(*args, **kwargs)
        finally:
            _local.buffer = None
    return run
