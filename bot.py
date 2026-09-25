"""Entrypoint.

The bot has no commands and no buttons. It sits in the site servers (one
per company and region), watches the site channels and writes what it sees
to the database the portal reads; in the background it carries out what the
portal decides — see cogs/servers.py. Everything a person decides happens
either in the channel conversation or in the portal.

    python bot.py
"""
import logging
import sys

# Windows consoles default to a codepage that can't encode the emoji in
# log lines; force UTF-8 so this behaves the same as on the Linux VM. Line
# buffering keeps output flowing when stdout is redirected to a file or
# captured by systemd.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import discord
from discord.ext import commands

from backfill import backfill_guild
from config import CONFIG
from storage import db

EXTENSIONS = (
    "cogs.capture",
    "cogs.servers",
)


class RMSBot(commands.Bot):
    async def setup_hook(self):
        db.init_db()

        for ext in EXTENSIONS:
            await self.load_extension(ext)

        # v1 registered seven slash commands per guild. The tree is empty
        # now, but Discord keeps what was last synced — so without this the
        # old commands stay in every client's picker forever.
        for row in db.all_guilds():
            if row["status"] != "left":
                try:
                    await self.tree.sync(guild=discord.Object(id=row["guild_id"]))
                except discord.HTTPException as exc:
                    print(f"[bot] couldn't clear commands in {row['name']}: {exc}")

    async def on_ready(self):
        print(f"[bot] logged in as {self.user} ({self.user.id})")
        known = {row["guild_id"]: row for row in db.all_guilds()}
        for guild in self.guilds:
            row = known.get(guild.id)
            label = f"{row['company_name']} / {row['region']}" if row and row["company_id"] else "not claimed"
            count = await backfill_guild(self, guild.id)
            print(f"[bot] {guild.name} ({label}) — backfill handled {count} missed message(s)")


def main():
    if not CONFIG.discord_token:
        sys.exit("DISCORD_BOT_TOKEN is not set — copy .env.example to .env and fill it in.")

    discord.utils.setup_logging(level=logging.INFO)

    intents = discord.Intents.default()
    intents.members = True          # who's in each server, and adding people to it
    intents.message_content = True  # reading the conversation

    bot = RMSBot(command_prefix="!", intents=intents)

    try:
        bot.run(CONFIG.discord_token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        sys.exit(
            "Discord rejected the connection: privileged intents are off.\n"
            "Turn on 'Server Members Intent' and 'Message Content Intent' at "
            "discord.com/developers -> RMS Bot -> Bot, then run this again."
        )


if __name__ == "__main__":
    main()
