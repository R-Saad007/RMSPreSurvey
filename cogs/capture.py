"""Everything the bot does inside a site channel.

The technician sees a chat and nothing else — no buttons, no prompts, no
commands. So this cog watches instead of asking: it archives what's said,
flags blockers out of the conversation, and marks a flagged message with a
🚩 so anyone in the channel can see it was picked up.

Resolving is a reaction, not a button: ✅ on the flagged message closes it.
That's the closest Discord gets to how the WhatsApp groups already work.
"""
import discord
from discord.ext import commands

from config import BLOCKER_RESOLVE_EMOJI
from storage import db
from utils.ingest import FLAG_EMOJI, ingest_message


class CaptureCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        site_id = db.site_id_for_channel(message.channel.id)
        if not site_id:
            return

        await ingest_message(message, site_id, live=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """✅ on a flagged message closes the blocker."""
        if payload.member is None or payload.member.bot:
            return
        if str(payload.emoji) not in BLOCKER_RESOLVE_EMOJI:
            return

        if not db.resolve_blocker_by_message(payload.message_id, payload.member.display_name):
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(FLAG_EMOJI, self.bot.user)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(CaptureCog(bot))
