"""The containers' health checks (compose.yaml; later, Kubernetes probes).

    python -m tools.healthcheck portal   # the portal answers and its database opens
    python -m tools.healthcheck bot      # the bot's worker wrote a heartbeat in the last 90 s

Exit code 0 means healthy. Plain Python, because the slim image has no curl.
"""
import sys
import urllib.request

PORTAL_URL = "http://127.0.0.1:8000/healthz"


def main(which: str) -> int:
    if which == "portal":
        with urllib.request.urlopen(PORTAL_URL, timeout=4) as answer:
            return 0 if answer.status == 200 else 1
    if which == "bot":
        from storage import db
        return 0 if db.bot_online(within=90) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
    except Exception as exc:        # unreachable, refused, timed out: unhealthy, and say why
        print(f"unhealthy: {exc}")
        sys.exit(1)
