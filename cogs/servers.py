"""The bot's Discord work, carried out in the background.

The portal records what people decide — a server claimed for a company and
region, sites assigned to a technician, HQ staff who belong in every server —
and this carries it out, every few seconds:

  1. open_queue     give technicians the sites assigned to them: put them in the
                    server through the Discord account they connected once, open
                    the channel (read-only if the survey is complete), greet them
  2. setup_claimed  get a newly claimed server ready: lock @everyone, create the
                    roles, remove Discord's stock channels, give it its name
  3. provision      create a channel for each site of that company and region
  4. sync_staff     put HQ's staff in every server, with their role
  5. shape_ready    now and then, re-check every server is still set up right
  6. heartbeat      so the portal can say when the bot is down

Each runs on its own: one that fails is logged and the rest still run — an
exception escaping a discord.py task loop would stop the loop for good.

Tokens are refreshed only here, one account at a time: Discord hands out a new
refresh token each time and retires the old one, so two processes refreshing
would lock a person out. See storage/db.replace_tokens_if.
"""
import asyncio
import time
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from discord.http import Route

from config import CONFIG
from storage import db
from utils import access, oauth
from utils.discord_layout import (
    ROLE_NAMES, STAFF_ROLE_PERMS, LayoutError, build_brief_text, check_hierarchy, ensure_roles, is_fresh,
    lock_everyone, pick_category, site_overwrites, strip_defaults,
)
from utils.greeting import build_greeting

TICK_SECONDS = 3
QUEUE_PER_TICK = 25
SITES_PER_TICK = 10
STAFF_EVERY = 30
SHAPE_EVERY = 600
BEAT_EVERY = 15
PAUSE_BETWEEN_SITES = 0.5

# Discord error codes worth saying in words, on the portal's screens.
_EXPLAINED = {
    30001: "they're already in 100 Discord servers (Discord's limit) — they need to leave one",
    40007: "they're banned from this server",
    10003: "the site's channel no longer exists",
    10004: "the bot is no longer in this server",
    50013: "the bot is missing a permission it needs in this server",
    50025: "their Discord sign-in is no longer valid",
}
INVALID_OAUTH_TOKEN = 50025


class NeedsConnect(Exception):
    """They have to connect Discord again through their link."""


class Worker:
    """What outlives a single tick: one lock per Discord account (refreshing
    a token twice at once would lose it), and when each periodic job last ran."""

    def __init__(self, bot):
        self.bot = bot
        self.locks = defaultdict(asyncio.Lock)
        self.last = defaultdict(float)


def describe(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code in _EXPLAINED:
        return _EXPLAINED[code]
    status = getattr(exc, "status", None)
    text = getattr(exc, "text", None) or str(exc)
    return (f"Discord said {status}: {text}" if status else f"{type(exc).__name__}: {text}")[:200]


# --- people into servers ------------------------------------------------------

async def usable_token(worker: Worker, discord_id: int) -> str | None:
    """A current access token for this account, refreshed if it's within an
    hour of lapsing. None: they have to connect again."""
    async with worker.locks[discord_id]:
        tokens = db.get_tokens(discord_id)
        if not tokens or tokens["dead_at"]:
            return None
        if tokens["expires_at"] - time.time() > 3600:
            return tokens["access_token"]
        try:
            fresh = await oauth.refresh(tokens["refresh_token"])
        except oauth.OAuthError as exc:
            if exc.invalid_grant:
                db.mark_tokens_dead(discord_id, tokens["refresh_token"])
                return None
            raise           # Discord unreachable: a temporary failure, tried again later
        if db.replace_tokens_if(discord_id, tokens["refresh_token"], fresh["access_token"],
                                fresh.get("refresh_token") or tokens["refresh_token"],
                                fresh.get("expires_in", 604800)):
            return fresh["access_token"]
        current = db.get_tokens(discord_id)       # they reconnected meanwhile; theirs is newer
        return current["access_token"] if current and not current["dead_at"] else None


async def add_member(worker: Worker, guild, discord_id: int):
    """The member, putting them into the server first if they aren't in it.
    Deliberately without roles: roles in the same call make Discord refuse the
    whole add if any role sits above the bot's. Roles are added separately."""
    member = guild.get_member(discord_id)
    if member is not None:
        return member
    token = await usable_token(worker, discord_id)
    if token is None:
        raise NeedsConnect()
    try:
        await worker.bot.http.request(
            Route("PUT", "/guilds/{guild_id}/members/{user_id}", guild_id=guild.id, user_id=discord_id),
            json={"access_token": token},
        )
    except discord.HTTPException as exc:
        if exc.code == INVALID_OAUTH_TOKEN:
            db.mark_tokens_dead(discord_id)
            raise NeedsConnect() from exc
        raise
    return guild.get_member(discord_id) or await guild.fetch_member(discord_id)


async def set_mode(bot, channel_id: int, discord_id: int, mode: str, site_id: str) -> None:
    allow, deny = access.overwrite_for(mode)
    await bot.http.edit_channel_permissions(channel_id, discord_id, str(allow), str(deny), 1,
                                            reason=f"{site_id}: {mode.replace('_', '-')}")


async def open_row(worker: Worker, row: dict) -> None:
    bot = worker.bot
    site_id, uid = row["site_id"], row["discord_id"]
    guild = bot.get_guild(row["guild_id"])
    if guild is None:
        raise LayoutError("the bot can't see this site's server")
    channel = guild.get_channel(row["channel_id"])
    if channel is None:
        raise LayoutError("the site's channel no longer exists")

    member = await add_member(worker, guild, uid)
    mode = db.site_mode(site_id)
    await set_mode(bot, channel.id, uid, mode, site_id)
    db.assign_tech(site_id, uid, row["created_by"] or "portal")
    # The survey may have been completed or reopened while we worked; the
    # portal's own update only reaches holders it can see, so check again.
    latest = db.site_mode(site_id)
    if latest != mode:
        await set_mode(bot, channel.id, uid, latest, site_id)
        mode = latest

    role = discord.utils.get(guild.roles, name=CONFIG.technician_role_name)
    if role is not None and role not in member.roles:
        try:
            await member.add_roles(role, reason=f"Assigned {site_id}")
        except discord.HTTPException:
            pass                    # a label only; access is the channel overwrite

    if mode == access.WRITABLE and not row["greeted_at"]:
        # A message at the bottom, not a pin: they've just arrived and will
        # never scroll up looking for a card.
        await channel.send(build_greeting(f"<@{uid}>", site_id, row["brief"]))
        db.mark_queue_greeted(row["id"])

    if not db.finish_queue_row(row["id"]):
        # Withdrawn in the portal while we were at it: take it back again.
        try:
            await bot.http.delete_channel_permissions(channel.id, uid, reason=f"{site_id}: assignment withdrawn")
        except discord.HTTPException:
            pass
        db.revoke_tech(site_id, uid)


async def open_queue(worker: Worker) -> int:
    opened = 0
    for row in db.openable_queue_rows(QUEUE_PER_TICK):
        if not db.claim_queue_row(row["id"]):
            continue
        try:
            await open_row(worker, row)
            opened += 1
        except NeedsConnect:
            db.queue_row_error(row["id"], "needs_connect")
        except (discord.HTTPException, LayoutError, oauth.OAuthError) as exc:
            db.queue_row_error(row["id"], describe(exc))
        except Exception as exc:                        # noqa: BLE001 — one bad row mustn't stop the rest
            db.queue_row_error(row["id"], describe(exc))
    return opened


# --- servers ----------------------------------------------------------------------

def _known_channels(guild_id: int) -> set:
    return {r["channel_id"] for r in db.all_site_channels(guild_id)}


async def setup_claimed(worker: Worker) -> None:
    """Every newly claimed (or admin-confirmed) server — rare, and a few calls
    each. All of them, so one stuck on something a person must fix never
    holds up the others."""
    for row in db.all_guilds(("claimed", "confirmed")):
        try:
            await _setup_one(worker, row)
        except Exception as exc:                        # noqa: BLE001 — recorded, and the next one still runs
            db.set_guild_error(row["guild_id"], describe(exc))


async def _setup_one(worker: Worker, row: dict) -> None:
    guild = worker.bot.get_guild(row["guild_id"])
    if guild is None:
        if time.time() - row["updated_at"] > 600 and not row["error"]:
            db.set_guild_error(row["guild_id"], "The bot isn't in this server. Add it again from the Discord page.")
        return
    known = _known_channels(guild.id)
    if row["status"] == "claimed" and not is_fresh(guild, known):
        db.set_guild_status(row["guild_id"], "review",
                            "This server isn't brand new (older than a week, other members, or site channels "
                            "already in it). Check it's the right one, then set it up.")
        return
    try:
        if row["status"] == "claimed":
            await strip_defaults(guild, known)
        await lock_everyone(guild)
        roles = await ensure_roles(guild)
        check_hierarchy(guild, roles)
        name = db.server_name(row["company_name"], row["region"], row["seq"])
        changes = {}
        if guild.name != name:
            changes["name"] = name
        if guild.verification_level != discord.VerificationLevel.none:
            changes["verification_level"] = discord.VerificationLevel.none
        if guild.default_notifications != discord.NotificationLevel.all_messages:
            changes["default_notifications"] = discord.NotificationLevel.all_messages
        if guild.system_channel is not None:
            changes["system_channel"] = None
        if changes:
            await guild.edit(reason="Site server set-up", **changes)
    except LayoutError as exc:
        db.set_guild_error(row["guild_id"], str(exc))
        return
    except discord.HTTPException as exc:
        db.set_guild_error(row["guild_id"], describe(exc))
        return
    db.set_guild_status(row["guild_id"], "ready")
    db.update_guild_facts(row["guild_id"], name=name, channel_count=len(guild.channels))


async def ensure_site_channel(guild, site: dict, known: set, overwrites: dict) -> None:
    """One site's channel, brief and access thread. The row is registered as
    soon as the channel exists, and each later step checks before it acts, so
    a crash half-way never leaves a second channel, a second pinned brief or a
    second thread behind."""
    site_id = site["site_id"]
    row = db.get_site_channel(site_id)
    channel = guild.get_channel(row["channel_id"]) if row and row["guild_id"] == guild.id else None
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name=site_id.lower())
    if channel is None:
        category = await pick_category(guild, known, overwrites)
        channel = await guild.create_text_channel(
            site_id.lower(), category=category, overwrites=overwrites,
            topic=f"Site {site_id} — {site.get('city') or ''}", reason="Site channel")
    known.add(channel.id)
    if row is None or row["channel_id"] != channel.id:
        db.upsert_site_channel(site_id, site.get("city"), guild.id, channel.id,
                               channel.category.name if channel.category else None)
        row = db.get_site_channel(site_id)

    if not row["brief"]:
        brief = build_brief_text(site)
        pinned = [m async for m in channel.pins(limit=None) if m.author.id == guild.me.id]
        if not pinned:
            message = await channel.send(brief)
            await message.pin(reason="Site brief")
        db.set_site_brief(site_id, brief)

    if not row["thread_id"]:
        thread = next((t for t in channel.threads if t.name == CONFIG.access_thread_name), None)
        if thread is None:
            thread = await channel.create_thread(name=CONFIG.access_thread_name,
                                                 type=discord.ChannelType.private_thread, invitable=False)
            await thread.send("WG IP, AnyDesk IDs, gwID and passwords go here. "
                              "Engineers and Management can see this thread; technicians cannot.")
        db.set_site_thread(site_id, thread.id)


async def provision(worker: Worker) -> int:
    """Up to SITES_PER_TICK channels this tick, server by server: each one's
    unfinished channels first, then new sites of its company and region. A
    server that can't take more (full, or refusing) is noted and skipped, so it
    never holds up the others."""
    budget = SITES_PER_TICK
    done = 0
    for row in db.all_guilds(("ready",)):
        if budget <= 0:
            break
        guild = worker.bot.get_guild(row["guild_id"])
        if guild is None or row["company_id"] is None:
            continue
        unfinished = [r["site_id"] for r in db.all_site_channels(guild.id) if not r["brief"]]
        room = CONFIG.sites_per_server - row["site_count"]
        todo = [db.get_site(s) or {"site_id": s} for s in unfinished[:budget]]
        todo += db.sites_to_provision(row["company_id"], row["region"], min(budget - len(todo), room))
        if not todo:
            continue
        known = _known_channels(guild.id)
        roles = {name: discord.utils.get(guild.roles, name=name) for name in ROLE_NAMES}
        overwrites = site_overwrites(guild, {k: v for k, v in roles.items() if v is not None})
        for site in todo:
            try:
                await ensure_site_channel(guild, site, known, overwrites)
            except LayoutError as exc:
                db.set_guild_error(row["guild_id"], str(exc))
                break
            done += 1
            budget -= 1
            await asyncio.sleep(PAUSE_BETWEEN_SITES)
        db.update_guild_facts(row["guild_id"], channel_count=len(guild.channels))
    return done


async def sync_staff(worker: Worker) -> None:
    """Every linked HQ staff member in every ready server, with their role
    (and only that one of the staff roles); removed staff lose theirs."""
    staff = [s for s in db.all_staff(include_removed=True) if s["discord_id"]]
    if not staff:
        return
    recorded = {(r["guild_id"], r["staff_id"]): (bool(r["ok"]), r["error"]) for r in db.guild_staff_rows()}
    for row in db.all_guilds(("ready",)):
        guild = worker.bot.get_guild(row["guild_id"])
        if guild is None:
            continue
        roles = {name: discord.utils.get(guild.roles, name=name) for name in STAFF_ROLE_PERMS}
        for person in staff:
            member = guild.get_member(person["discord_id"])
            try:
                if person["removed_at"]:
                    theirs = [r for r in roles.values() if r is not None and member is not None and r in member.roles]
                    if theirs:
                        await member.remove_roles(*theirs, reason="No longer HQ staff")
                    continue
                if member is None:
                    member = await add_member(worker, guild, person["discord_id"])
                wanted = roles.get(person["discord_role"])
                stale = [r for name, r in roles.items() if r is not None and name != person["discord_role"]
                         and r in member.roles]
                if stale:
                    await member.remove_roles(*stale, reason="Staff role changed")
                if wanted is not None and wanted not in member.roles:
                    await member.add_roles(wanted, reason="HQ staff")
                result = (True, None)
            except NeedsConnect:
                result = (False, "needs_connect")
            except (discord.HTTPException, oauth.OAuthError) as exc:
                result = (False, describe(exc))
            if recorded.get((guild.id, person["id"])) != result:
                db.record_guild_staff(guild.id, person["id"], *result)


async def shape_ready(worker: Worker) -> None:
    """Every so often: every ready server still locked, with its roles, and
    the bot still above them."""
    for row in db.all_guilds(("ready",)):
        guild = worker.bot.get_guild(row["guild_id"])
        if guild is None:
            continue
        try:
            await lock_everyone(guild)
            roles = await ensure_roles(guild)
            check_hierarchy(guild, roles)
            if row["error"]:
                db.set_guild_error(row["guild_id"], None)
        except LayoutError as exc:
            db.set_guild_error(row["guild_id"], str(exc))
        except discord.HTTPException as exc:
            db.set_guild_error(row["guild_id"], describe(exc))
        db.update_guild_facts(row["guild_id"], channel_count=len(guild.channels))


async def _safely(name: str, work) -> None:
    try:
        await work
    except Exception as exc:                            # noqa: BLE001 — see the module docstring
        print(f"[servers] {name} failed: {describe(exc)}")


async def tick(worker: Worker) -> None:
    now = time.time()
    if now - worker.last["beat"] >= BEAT_EVERY:
        worker.last["beat"] = now
        await _safely("heartbeat", asyncio.to_thread(db.beat))
    await _safely("open_queue", open_queue(worker))
    await _safely("setup_claimed", setup_claimed(worker))
    await _safely("provision", provision(worker))
    if now - worker.last["staff"] >= STAFF_EVERY:
        worker.last["staff"] = now
        await _safely("sync_staff", sync_staff(worker))
    if now - worker.last["shape"] >= SHAPE_EVERY:
        worker.last["shape"] = now
        await _safely("shape_ready", shape_ready(worker))


class ServersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.worker = Worker(bot)
        self.work.start()

    def cog_unload(self):
        self.work.cancel()

    @tasks.loop(seconds=TICK_SECONDS)
    async def work(self):
        await tick(self.worker)

    @work.before_loop
    async def before_work(self):
        await self.bot.wait_until_ready()
        # Servers the bot is already in are known before the first tick —
        # never marked as left just because they weren't listed.
        for guild in self.bot.guilds:
            db.register_guild_seen(guild.id, guild.name, guild.owner_id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        db.register_guild_seen(guild.id, guild.name, guild.owner_id)
        print(f"[servers] added to {guild.name} ({guild.id})")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        db.mark_guild_left(guild.id)
        print(f"[servers] removed from {guild.name} ({guild.id})")

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if before.name != after.name or before.owner_id != after.owner_id:
            db.update_guild_facts(after.id, name=after.name, owner_id=after.owner_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServersCog(bot))
