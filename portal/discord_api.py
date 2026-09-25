"""What the portal asks Discord to do directly, over REST with the bot's token.

Only the things that have to happen while someone waits: taking a site back
(access has to be gone when the page says so), switching a site's technicians
between writable and read-only when its survey is completed or reopened, and
the reconcile page's comparison. Everything slower — putting people into
servers, creating channels — is the bot's job (cogs/servers.py); the portal
records the intent and the bot carries it out.
"""
import asyncio

import aiohttp

from config import CONFIG
from utils import access

API = "https://discord.com/api/v10"
OVERWRITE_TYPE_MEMBER = 1
RETRIES_ON_RATE_LIMIT = 3


def _headers(reason: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bot {CONFIG.discord_token}",
        "Content-Type": "application/json",
    }
    if reason:
        headers["X-Audit-Log-Reason"] = reason
    return headers


class DiscordError(RuntimeError):
    """status is the HTTP status; code is Discord's own error code. Both are
    here so a caller can tell an expected 'no' from a real fault without
    parsing the message."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


async def _request(method: str, path: str, *, json=None, reason=None):
    """One call, waiting out Discord's rate limit (it says how long) a few
    times before giving up — the reconcile page makes one call per channel."""
    for attempt in range(RETRIES_ON_RATE_LIMIT + 1):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.request(method, f"{API}{path}", headers=_headers(reason), json=json) as resp:
                if resp.status == 204:
                    return None
                body = await resp.json(content_type=None)
                if resp.status == 429 and attempt < RETRIES_ON_RATE_LIMIT:
                    wait = float(body.get("retry_after", 1)) if isinstance(body, dict) else 1.0
                    await asyncio.sleep(min(max(wait, 0.1), 30))
                    continue
                if resp.status >= 400:
                    code = body.get("code") if isinstance(body, dict) else None
                    raise DiscordError(f"{method} {path} -> {resp.status}: {body}", status=resp.status, code=code)
                return body
    raise DiscordError(f"{method} {path} -> still rate limited", status=429)


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
