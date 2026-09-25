"""One message, handled once — archive it, log it, flag it if it reads like
a blocker.

Shared by the live listener and the startup backfill so a message the bot
missed while it was down is treated exactly like one it saw, which is the
whole point of the backfill.
"""
import discord

from storage import db
from utils import blockers
from utils.attach import BOTH_LANGUAGES_BLURRY, archive_message_attachments

FLAG_EMOJI = "\U0001f6a9"


async def ingest_message(message: discord.Message, site_id: str, live: bool = True) -> None:
    db.log_message(
        message.id,
        message.channel.id,
        site_id,
        message.author.id,
        message.author.display_name,
        message.content,
        message.created_at.timestamp(),
    )

    matched = blockers.detect(message.content)
    if matched:
        blocker_id = db.open_blocker(
            site_id,
            message.channel.id,
            message.id,
            message.author.id,
            message.author.display_name,
            message.content,
            matched,
        )
        if blocker_id:
            try:
                await message.add_reaction(FLAG_EMOJI)
            except discord.HTTPException:
                pass  # the flag is a nicety; the dashboard row is what matters

    blurry = await archive_message_attachments(message, site_id)

    # Only nag about a blurry photo as it happens. Telling someone to retake
    # a picture from two days ago is noise.
    if blurry and live:
        await message.reply(BOTH_LANGUAGES_BLURRY)
