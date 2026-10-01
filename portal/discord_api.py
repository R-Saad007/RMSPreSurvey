"""What the portal asks Discord to do directly, over REST with the bot's token.

Only the things that have to happen while someone waits: taking a site back
(access has to be gone when the page says so), switching a site's technicians
between writable and read-only when its survey is completed or reopened, and
the reconcile page's comparison. Everything slower — putting people into
servers, creating channels — is the bot's job (cogs/servers.py); the portal
records the intent and the bot carries it out.

Every call returns Discord's JSON or raises DiscordError: a dropped
connection, a timeout and an answer that isn't JSON included. A caller that
catches DiscordError has caught everything Discord can do to it.
"""
import asyncio
import json as jsonlib

import aiohttp

from config import CONFIG
from utils import access

API = "https://discord.com/api/v10"
OVERWRITE_TYPE_MEMBER = 1
RETRIES_ON_RATE_LIMIT = 3
EDGE_FAILURES = (502, 503, 504)     # Discord's edge couldn't reach the API; usually gone a moment later
RETRY_PAUSE = 0.5                   # seconds before a GET's second try


def _headers(reason: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bot {CONFIG.discord_token}",
        "Content-Type": "application/json",
    }
    if reason:
        headers["X-Audit-Log-Reason"] = reason
    return headers


class DiscordError(RuntimeError):
    """status is the HTTP status, or None when nothing came back (no
    connection, a timeout, a body cut short); code is Discord's own error
    code. Both are here so a caller can tell an expected 'no' from a real
    fault without parsing the message."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


async def _exchange(method: str, path: str, payload, reason) -> tuple[int, bytes]:
    """One round trip: the status and the raw body. Nothing back is a
    DiscordError with no status. Its message names the failure's type only,
    because an aiohttp error can carry the request it was making, the
    Authorization header included."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.request(method, f"{API}{path}", headers=_headers(reason), json=payload) as resp:
                return resp.status, await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise DiscordError(f"{method} {path} -> no answer ({type(exc).__name__})") from None


def _retry_after(body) -> float:
    """How long a rate limit asks us to wait, kept within reason."""
    try:
        return min(max(float(body["retry_after"]), 0.1), 30.0)
    except (KeyError, TypeError, ValueError):
        return 1.0


async def _request(method: str, path: str, *, json=None, reason=None):
    """One call: Discord's JSON (None for a 204), or a DiscordError.

    A rate limit is waited out (Discord says how long) a few times before
    giving up, because the reconcile page makes one call per channel. A GET
    gets one more try when nothing answered or Discord's edge failed
    (502/503/504); a change gets none from here. (aiohttp itself resends a
    GET, PUT or DELETE once if the connection drops before any answer, as HTTP
    allows: ours set an end state, so a repeat is harmless.) A body that isn't
    JSON is a DiscordError carrying the status, whatever the status."""
    second_try = method == "GET"
    waits = 0
    while True:
        try:
            status, raw = await _exchange(method, path, json, reason)
        except DiscordError as exc:
            print(f"[discord] {exc}")
            if not second_try:
                raise
            second_try = False
            await asyncio.sleep(RETRY_PAUSE)
            continue
        if status in EDGE_FAILURES and second_try:
            print(f"[discord] {method} {path} -> {status}; trying once more")
            second_try = False
            await asyncio.sleep(RETRY_PAUSE)
            continue
        if status == 204:
            return None
        try:
            body = jsonlib.loads(raw)
        except ValueError:          # an HTML error page, or an empty or cut-off body
            what = f"an answer that isn't JSON ({len(raw)} bytes)" if raw.strip() else "an empty answer"
            print(f"[discord] {method} {path} -> {status}: {what}")
            raise DiscordError(f"{method} {path} -> {status}: {what}", status=status) from None
        if status == 429 and waits < RETRIES_ON_RATE_LIMIT:
            waits += 1
            await asyncio.sleep(_retry_after(body))
            continue
        if status >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            raise DiscordError(f"{method} {path} -> {status}: {body}", status=status, code=code)
        return body


async def set_overwrite(channel_id: int, user_id: int, mode: str, reason: str | None = None):
    """A technician's overwrite on a site channel, in one of utils/access.py's modes."""
    allow, deny = access.overwrite_for(mode)
    await _request(
        "PUT",
        f"/channels/{channel_id}/permissions/{user_id}",
        json={"allow": str(allow), "deny": str(deny), "type": OVERWRITE_TYPE_MEMBER},
        reason=reason or f"Site access: {mode.replace('_', '-')}",
    )


async def revoke_channel_access(channel_id: int, user_id: int, reason="Revoked via portal"):
    await _request("DELETE", f"/channels/{channel_id}/permissions/{user_id}", reason=reason)


async def channel_member_overwrites(channel_id: int) -> dict:
    """{user_id: (allow, deny)} for every member overwrite on this channel —
    the ground truth that site_techs is only a mirror of."""
    channel = await _request("GET", f"/channels/{channel_id}")
    return {
        int(o["id"]): (int(o.get("allow") or 0), int(o.get("deny") or 0))
        for o in channel.get("permission_overwrites", [])
        if o.get("type") == OVERWRITE_TYPE_MEMBER
    }


async def guild_roles(guild_id: int) -> dict:
    roles = await _request("GET", f"/guilds/{guild_id}/roles")
    return {r["name"]: int(r["id"]) for r in roles}


async def remove_member_role(guild_id: int, user_id: int, role_id: int, reason="No remaining site assignments"):
    await _request("DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}", reason=reason)


async def leave_guild(guild_id: int):
    """The bot leaves a server it was added to by mistake."""
    await _request("DELETE", f"/users/@me/guilds/{guild_id}")
