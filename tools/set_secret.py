"""Puts a secret into .env without it ever being shown, logged, or typed on a
command line (where it would land in shell history).

    .venv/bin/python -m tools.set_secret DISCORD_BOT_TOKEN        # Bot page -> Reset Token
    .venv/bin/python -m tools.set_secret DISCORD_CLIENT_SECRET    # OAuth2 page -> Client Secret -> Reset Secret
    .venv/bin/python -m tools.set_secret SMTP_PASSWORD

Asks twice. Discord values are checked with Discord before anything is
saved: a bot token must sign in as this application's bot, and a client
secret must be accepted by Discord's sign-in (the two are easy to mix up, and
a bot token is never a client secret). A Gmail app password is shown by
Google in four groups with spaces; those spaces are dropped. Anything with a
quote or a line break is refused (it would corrupt the file). .env is
rewritten in one step and kept readable by its owner only. Restart both
services afterwards.
"""
import base64
import getpass
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED = ("DISCORD_BOT_TOKEN", "DISCORD_CLIENT_SECRET", "SMTP_PASSWORD")
ENV = Path(__file__).resolve().parent.parent / ".env"
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/Rapptz/discord.py, 2) rms-set-secret"


def clean(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[a-z]{4}( [a-z]{4}){3}", value):       # Gmail app password as displayed
        value = value.replace(" ", "")
    return value


def looks_like_bot_token(value: str) -> bool:
    """Three dot-separated parts, the first being the bot's id in base64."""
    parts = value.split(".")
    if len(parts) != 3 or not all(parts):
        return False
    try:
        return base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)).decode().isdigit()
    except ValueError:
        return False


def _discord(method: str, path: str, headers: dict, data: dict | None = None) -> tuple[int, dict]:
    """(HTTP status, JSON body). Replaced in the tests: they never reach Discord."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(API + path, data=body, method=method,
                                     headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=15) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def check_with_discord(key: str, value: str, client_id: str | None) -> str | None:
    """None if Discord accepts the value; otherwise, in words, why not.
    Raises OSError if Discord can't be reached."""
    if key == "DISCORD_BOT_TOKEN":
        if not looks_like_bot_token(value):
            return "That isn't a bot token. It's on the Developer Portal's Bot page: Reset Token."
        status, me = _discord("GET", "/users/@me", {"Authorization": f"Bot {value}"})
        if status != 200:
            return f"Discord refused that bot token ({status}). Reset it on the Bot page and try again."
        if client_id and str(me.get("id")) != str(client_id):
            return "That token belongs to a different application's bot."
        return None
    if key == "DISCORD_CLIENT_SECRET":
        if looks_like_bot_token(value):
            return ("That's a bot token (from the Bot page), not the client secret. "
                    "The client secret is on the OAuth2 page: Client Secret, Reset Secret.")
        if not client_id:
            return "DISCORD_CLIENT_ID isn't in .env yet, so the secret can't be checked."
        basic = base64.b64encode(f"{client_id}:{value}".encode()).decode()
        status, answer = _discord("POST", "/oauth2/token", {"Authorization": f"Basic {basic}"},
                                  {"grant_type": "client_credentials", "scope": "identify"})
        if status != 200:
            return f"Discord refused that client secret ({answer.get('error') or status}). Reset it and try again."
        return None
    return None


def env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*(#.*)?$", line)
        if m:
            return m.group(1).strip("'\"") or None
    return None


def set_key(path: Path, key: str, value: str) -> None:
    if not value or any(ch in value for ch in "'\"\r\n"):
        raise ValueError("That can't be stored: it's empty, or has a quote or a line break in it.")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    line = f"{key}='{value}'"
    replaced = False
    for i, existing in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}\s*=", existing):
            lines[i], replaced = line, True
    if not replaced:
        lines.append(line)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ALLOWED:
        sys.exit(f"Usage: python -m tools.set_secret {{{'|'.join(ALLOWED)}}}")
    key = sys.argv[1]
    first = clean(getpass.getpass(f"{key}: "))
    second = clean(getpass.getpass(f"{key} again: "))
    if first != second:
        sys.exit("They don't match. Nothing was changed.")
    try:
        problem = check_with_discord(key, first, env_value(ENV, "DISCORD_CLIENT_ID"))
    except OSError as exc:
        sys.exit(f"Couldn't reach Discord to check it ({exc}). Nothing was changed; try again.")
    if problem:
        sys.exit(f"{problem} Nothing was changed.")
    try:
        set_key(ENV, key, first)
    except ValueError as exc:
        sys.exit(f"{exc} Nothing was changed.")
    checked = " Discord accepted it." if key.startswith("DISCORD_") else ""
    print(f"{key} saved in {ENV} (not shown).{checked} Now: systemctl restart rms-portal rms-discord-bot")


if __name__ == "__main__":
    main()
