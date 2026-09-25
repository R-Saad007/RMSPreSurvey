"""Throttles failed sign-ins.

The portal is reachable from anywhere by design — SubCons work from the
field on mobile networks, so an IP allowlist isn't an option — which means
the login form is the only thing between the internet and an account that
can grant Discord access. Without a limit it accepts unlimited guesses.

Two counters, because they stop different attacks:
  - per IP: one host spraying many accounts.
  - per email: many hosts grinding at one account.

Kept in memory deliberately. The portal runs as a single uvicorn worker
(no --workers in the unit file), so there is exactly one process and one
dict; a shared store would be real infrastructure to operate for no gain
at this scale. The cost is that a restart forgives everyone, which is an
acceptable trade for a service that restarts on deploys only.
"""
import time

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60

# key -> list of failure timestamps
_failures: dict[str, list[float]] = {}


def _recent(key: str, now: float) -> list[float]:
    return [t for t in _failures.get(key, []) if now - t < WINDOW_SECONDS]


def _prune(now: float) -> None:
    """Drops keys whose failures have all aged out, so a long-running
    process doesn't accumulate an entry per IP that ever typo'd."""
    for key in [k for k, times in _failures.items() if not any(now - t < WINDOW_SECONDS for t in times)]:
        del _failures[key]


def retry_after(*keys: str) -> int:
    """Seconds until the caller may try again, or 0 if they may try now."""
    now = time.time()
    worst = 0
    for key in keys:
        times = _recent(key, now)
        if len(times) >= MAX_ATTEMPTS:
            worst = max(worst, int(WINDOW_SECONDS - (now - min(times))) + 1)
    return worst


def record_failure(*keys: str) -> None:
    now = time.time()
    _prune(now)
    for key in keys:
        _failures[key] = _recent(key, now) + [now]


def clear(*keys: str) -> None:
    """A correct password forgives the earlier fumbling."""
    for key in keys:
        _failures.pop(key, None)
