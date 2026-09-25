"""Startup backfill: re-reads recent history in each known site channel and
handles anything that happened while the bot was down — archiving files,
logging messages, and flagging blockers that were raised in its absence.

Uses the same ingest path as the live listener, so a message caught here is
treated identically to one seen in real time. A per-channel high-water mark
in bot_meta means a restart only re-scans what's new.
"""
import discord

from storage import db
from utils.ingest import ingest_message

FIRST_RUN_LIMIT = 200


async def backfill_channel(channel: discord.TextChannel, site_id: str) -> int:
    key = f"backfill:{channel.id}"
    last_id = db.get_meta(key)

    kwargs = {"oldest_first": True}
    if last_id:
        kwargs["after"] = discord.Object(id=int(last_id))
    else:
        kwargs["limit"] = FIRST_RUN_LIMIT

    highest_seen = int(last_id) if last_id else 0
    processed = 0
    async for message in channel.history(**kwargs):
        if not message.author.bot:
            await ingest_message(message, site_id, live=False)
            processed += 1
        highest_seen = max(highest_seen, message.id)

    if highest_seen:
        db.set_meta(key, highest_seen)
    return processed


async def backfill_guild(bot: discord.Client, guild_id: int) -> int:
    total = 0
    for row in db.all_site_channels(guild_id):
        channel = bot.get_channel(row["channel_id"])
        if channel is None:
            continue
        total += await backfill_channel(channel, row["site_id"])
    return total
