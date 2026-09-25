"""How a Company_Region server is laid out, and the steps that make it so.

A server holds site categories and site channels, and nothing else. A
technician lands in their channel and sees only that: @everyone is denied
View Channels server-wide, staff roles see every site channel through the
channel's own overwrites, and a technician gets a per-channel overwrite of
their own (utils/access.py).

Every step here is safe to run again — after a crash, a restart, or on a
server set up by hand earlier (the pilot) — because each one looks before
it acts. Used by the bot (cogs/servers.py); the site brief is also what the
greeting repeats (utils/greeting.py).
"""
import re

import discord

from config import CONFIG

STAFF_ROLE_PERMS = {
    CONFIG.management_role_name: dict(manage_threads=True, mention_everyone=True),
    CONFIG.engineer_role_name: dict(manage_threads=True),
    CONFIG.coordinator_role_name: {},
}
ROLE_NAMES = (*STAFF_ROLE_PERMS, CONFIG.technician_role_name)

# What Discord puts in a server a person creates. Removed only from a server
# we've just been added to, and only by these exact names.
DEFAULT_TEXT, DEFAULT_VOICE = "general", "General"
DEFAULT_CATEGORIES = ("Text Channels", "Voice Channels")

CATEGORY_NAME = re.compile(r"^Sites (\d{3})-(\d{3})$")


class LayoutError(Exception):
    """Something a person has to fix in Discord; the message says what."""


def build_brief_text(site: dict) -> str:
    """The pinned brief. Columns are the site inventory's."""
    g = lambda k: (site.get(k) or "").strip() or "-"
    return (
        f"**{site['site_id']}**, {g('city')} ({g('region')}) | {g('priority')} | {g('rms_category')}\n"
        f"\U0001f4cd {g('maps_url')}\n"
        f"Tenants: {g('tenants')} | SIM: {g('sim1')} / {g('sim2')}\n\n"
        f"**On site now:** {g('existing_equipment')}\n"
        f"**Install (BOQ):** {g('install_boq')}\n"
        f"Tapping: {g('boq_remarks')} | Integrate: {g('smart_integration')}\n\n"
        f"MBU {g('mbu')}: {g('mbu_lead')} {g('mbu_lead_contact')} | Zonal: {g('zonal_manager')} {g('zonal_manager_contact')}"
    )[:1990]


def is_fresh(guild: discord.Guild, known_channel_ids: set) -> bool:
    """Whether this looks like a server someone has just created for us:
    under a week old, nobody in it but its owner and the bot, and none of our
    site channels. Anything else (a personal server picked by mistake, say)
    is set up only after an HQ admin confirms it."""
    age = discord.utils.utcnow() - guild.created_at
    members = guild.member_count or len(guild.members)
    has_ours = any(c.id in known_channel_ids for c in guild.channels)
    return age.days < 7 and members <= 2 and not has_ours


async def strip_defaults(guild: discord.Guild, known_channel_ids: set) -> list[str]:
    """Deletes Discord's stock #general, General voice and their empty
    categories. Nobody but the owner can see them once @everyone is locked,
    so this is tidiness, and it only ever touches those exact names."""
    removed = []
    for channel in list(guild.channels):
        if channel.id in known_channel_ids:
            continue
        kind = getattr(channel, "type", None)
        stock = ((kind == discord.ChannelType.text and channel.name == DEFAULT_TEXT)
                 or (kind == discord.ChannelType.voice and channel.name == DEFAULT_VOICE))
        if stock:
            await channel.delete(reason="Stock channel in a new site server")
            removed.append(channel.name)
    for category in list(guild.categories):
        if category.name in DEFAULT_CATEGORIES and not category.channels:
            await category.delete(reason="Empty stock category in a new site server")
            removed.append(category.name)
    return removed


async def lock_everyone(guild: discord.Guild) -> bool:
    """@everyone may not see channels, join voice, make invites or ping
    everyone, server-wide. Compares what's wanted with what's there, so it
    also tightens a server set up before these rules (the pilot still let
    everyone create invites). True if anything changed."""
    perms = guild.default_role.permissions
    wanted = dict(view_channel=False, connect=False, speak=False, create_instant_invite=False, mention_everyone=False)
    if all(getattr(perms, k) == v for k, v in wanted.items()):
        return False
    perms.update(**wanted)
    await guild.default_role.edit(permissions=perms, reason="Technicians see only their assigned channels")
    return True


async def ensure_roles(guild: discord.Guild) -> dict:
    """{name: role} for the four functional roles, creating any that are missing."""
    roles = {}
    for name in ROLE_NAMES:
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            perms = discord.Permissions(**STAFF_ROLE_PERMS.get(name, {}))
            role = await guild.create_role(name=name, permissions=perms, mentionable=True,
                                           reason="RMS functional role")
        roles[name] = role
    return roles


def check_hierarchy(guild: discord.Guild, roles: dict) -> None:
    """The bot can only give out roles below its own. A server whose owner
    dragged a role above the bot would fail every add; say so plainly."""
    top = guild.me.top_role
    above = [name for name, role in roles.items() if role >= top]
    if above:
        raise LayoutError(f"In Discord, drag the bot's role above {', '.join(above)} "
                          "(Server Settings → Roles) — it can't give those roles out otherwise.")


def site_overwrites(guild: discord.Guild, roles: dict) -> dict:
    """A site channel: hidden from @everyone, open to the staff roles.
    Technicians are added one by one, by their own overwrite."""
    staff = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                        attach_files=True, add_reactions=True)
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for name in STAFF_ROLE_PERMS:
        if name in roles:
            overwrites[roles[name]] = staff
    return overwrites


def site_categories(guild: discord.Guild, known_channel_ids: set) -> list:
    """The categories site channels go in: ours by name ("Sites 001-050"), or
    any that already hold one of our channels (the pilot's own)."""
    out = []
    for category in guild.categories:
        if CATEGORY_NAME.match(category.name) or any(c.id in known_channel_ids for c in category.channels):
            out.append(category)
    return out


async def pick_category(guild: discord.Guild, known_channel_ids: set, overwrites: dict):
    """The first site category with room (Discord allows 50 channels in one),
    or a new one named for the next block of 50. Refuses before the server
    reaches its channel ceiling (500 counts categories too)."""
    size = CONFIG.site_category_size
    for category in site_categories(guild, known_channel_ids):
        if len(category.channels) < size:
            if len(guild.channels) + 1 > CONFIG.channel_count_ceiling:
                raise LayoutError("This server is full. The next sites go into the next server for this region.")
            return category
    if len(guild.channels) + 2 > CONFIG.channel_count_ceiling:
        raise LayoutError("This server is full. The next sites go into the next server for this region.")
    used = {int(m.group(1)) for c in guild.categories if (m := CATEGORY_NAME.match(c.name))}
    start = 1
    while start in used:
        start += size
    name = f"Sites {start:03d}-{start + size - 1:03d}"
    return await guild.create_category(name, overwrites=overwrites, reason="Site category")
