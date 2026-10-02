"""Discord OAuth2: connecting a person's Discord account once, and adding the
bot to a new server with proof of which server it was.

A person (technician or HQ staff) authorises "identify guilds.join". That
gives us who they are, and a token the bot can use to put them into any
server it's in — so one link, tapped once, covers every server their work
will ever be in. Access tokens last a week; the refresh token renews them.
Refresh tokens rotate (each refresh returns a new one and retires the old),
so only one process ever refreshes: the bot. See cogs/servers.py.

Adding the bot to a server goes through the same authorisation endpoint with
the "bot" scope and a code: with "Requires OAuth2 Code Grant" switched on in
the Developer Portal, the bot joins only when we exchange that code, and the
exchange tells us — authoritatively — which server it joined.

Every call returns Discord's answer or raises OAuthError: a dropped
connection, a timeout and an answer that isn't JSON (Discord's edge sometimes
sends an HTML error page) included, as in portal/discord_api.py. Only asking
who signed in is tried twice; a code or refresh token works once, so a token
request is never repeated from here.
"""
import asyncio
import json
from urllib.parse import urlencode

import aiohttp

from config import CONFIG

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
API = "https://discord.com/api/v10"
REDIRECT_PATH = "/oauth/discord/callback"
USER_SCOPES = ("identify", "guilds.join")
BOT_PERMISSIONS = "8"     # Administrator: it creates channels, roles and overwrites in the server
EDGE_FAILURES = (502, 503, 504)     # Discord's edge couldn't reach the API; usually gone a moment later
RETRY_PAUSE = 0.5


class OAuthError(Exception):
    """Discord refused or couldn't be reached. invalid_grant means the code or
    refresh token is no good any more (as opposed to a temporary failure)."""

    def __init__(self, message: str, invalid_grant: bool = False, status: int | None = None):
        super().__init__(message)
        self.invalid_grant = invalid_grant
        self.status = status


def redirect_uri() -> str:
    return CONFIG.portal_base_url.rstrip("/") + REDIRECT_PATH


def user_authorize_url(state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": CONFIG.discord_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "scope": " ".join(USER_SCOPES),
        "state": state,
    })


def bot_authorize_url(state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": CONFIG.discord_client_id,
        "scope": "bot",
        "permissions": BOT_PERMISSIONS,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "integration_type": "0",
        "state": state,
    })


async def _call(send) -> tuple[int, object]:
    """One round trip: the status, and the body as JSON — None if it isn't
    JSON. Nothing back is an OAuthError with no status, named by the failure's
    type only and not chained to it: an aiohttp error can carry the request,
    with the client secret or a person's token in its headers."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with send(session) as resp:
                status, raw = resp.status, await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise OAuthError(f"Couldn't reach Discord ({type(exc).__name__}).") from None
    try:
        return status, json.loads(raw)
    except ValueError:              # an HTML error page, or an empty or cut-off body
        return status, None


async def _token_request(form: dict) -> dict:
    auth = aiohttp.BasicAuth(CONFIG.discord_client_id, CONFIG.discord_client_secret)
    status, body = await _call(lambda session: session.post(TOKEN_URL, data=form, auth=auth))
    if status == 200 and isinstance(body, dict) and body.get("access_token"):
        return body
    if body is None:
        raise OAuthError(f"Discord's answer to the sign-in wasn't readable ({status}).", status=status)
    error = body.get("error") if isinstance(body, dict) else None
    detail = f"{status}, {error}" if error else f"{status}"
    raise OAuthError(f"Discord refused the sign-in ({detail}).", invalid_grant=error == "invalid_grant", status=status)


async def exchange_code(code: str) -> dict:
    """The token response; for a bot authorisation it also carries `guild`."""
    return await _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()})


async def refresh(refresh_token: str) -> dict:
    return await _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


async def get_me(access_token: str) -> dict:
    """{'id', 'username', 'global_name', ...} for the account that authorised.
    Asked twice if Discord's edge fails or nothing answers: it changes nothing."""
    headers = {"Authorization": f"Bearer {access_token}"}
    for last in (False, True):
        try:
            status, body = await _call(lambda session: session.get(f"{API}/users/@me", headers=headers))
        except OAuthError:
            if last:
                raise
            await asyncio.sleep(RETRY_PAUSE)
            continue
        if status in EDGE_FAILURES and not last:
            await asyncio.sleep(RETRY_PAUSE)
            continue
        if status == 200 and isinstance(body, dict) and body.get("id"):
            return body
        raise OAuthError(f"Discord wouldn't say who signed in ({status}).", status=status)
