"""Blocker detection.

Nobody files a blocker. A field tech joins a channel and starts working
from the bottom, like a WhatsApp group — they will never scroll up to find
a button, so asking them to press one means blockers never get reported.
The bot reads the conversation instead.

Two passes, because a real site's thread needs both: explicit trouble
words ("masla", "error", "kharab"), and negated verbs, which is how the
crew actually writes most problems — "not readable in putty", "data is not
walking in mib", "is it not accessible?".

The rules live in the database (hermes_rules), so HQ can tune the detector
from real traffic without a deploy. config.py holds the same lists as the
built-in defaults: they seed the table on first run, and they're the fallback
if the table can't be read — a database hiccup must never stop messages
being ingested.

This is deliberately not clever. It sits behind one function so Hermes can
replace the body later with real classification, which is what this
particular problem actually wants. Over-firing is the safe direction: a
false flag costs one dismissal click, a missed one strands a technician.
"""
import re
import time

from config import BLOCKER_KEYWORDS, BLOCKER_NEGATIONS, BLOCKER_PATTERNS
from storage import db

# Rules are re-read this often, so an edit in the portal takes effect within
# a minute without restarting the bot, while a message costs no query.
CACHE_SECONDS = 60

_cache: dict = {"at": 0.0, "rules": None}


def _compile(patterns) -> tuple:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            # The portal validates on the way in; this is the last line of
            # defence against one bad row disabling detection for everyone.
            print(f"[blockers] skipping a pattern that won't compile: {pattern!r}")
    return tuple(compiled)


def _builtin_rules() -> dict:
    return {
        "negation": tuple(BLOCKER_NEGATIONS),
        "keyword": tuple(BLOCKER_KEYWORDS),
        "pattern": _compile(BLOCKER_PATTERNS),
    }


def _load_rules() -> dict:
    now = time.time()
    if _cache["rules"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["rules"]

    try:
        rows = db.list_hermes_rules(active_only=True)
    except Exception as exc:  # the table missing, the file locked — anything
        print(f"[blockers] couldn't read rules from the database ({exc}); using built-in defaults")
        # Cached like a success, so an outage backs off for a minute instead
        # of failing (and logging) on every single message.
        fallback = _cache["rules"] or _builtin_rules()
        _cache["rules"], _cache["at"] = fallback, now
        return fallback

    by_kind = {"negation": [], "keyword": [], "pattern": []}
    for row in rows:
        if row["kind"] in by_kind:
            by_kind[row["kind"]].append(row["value"])

    rules = {
        "negation": tuple(by_kind["negation"]),
        "keyword": tuple(by_kind["keyword"]),
        "pattern": _compile(by_kind["pattern"]),
    }
    _cache["rules"], _cache["at"] = rules, now
    return rules


def reset_cache() -> None:
    """For tests, and for anything that has just changed the rules."""
    _cache["rules"], _cache["at"] = None, 0.0


def detect(text: str) -> str | None:
    """Returns what matched, or None if this isn't a blocker.

    The match is stored on the blocker row so the lists can be tuned
    against what real traffic trips rather than what we imagined it would.
    """
    if not text:
        return None

    rules = _load_rules()
    lowered = " ".join(text.lower().split())

    # Negations win outright — "koi masla nahi" contains "masla", and
    # "working fine" contains neither but would otherwise slip past.
    for phrase in rules["negation"]:
        if phrase in lowered:
            return None

    for keyword in rules["keyword"]:
        if keyword in lowered:
            return keyword

    for pattern in rules["pattern"]:
        match = pattern.search(lowered)
        if match:
            return match.group(0)

    return None
