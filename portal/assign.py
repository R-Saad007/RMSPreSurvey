"""Assigning technicians to sites — the one thing an operator does.

The operator ticks sites and presses Assign. That records the assignment (a
queue row per site) and makes sure the technician has their personal link;
the bot does the Discord part a few seconds later (cogs/servers.py). There is
nothing else for anyone to do:

    never connected Discord    -> they're sent their link; the moment they
                                  connect, every site opens, in whichever
                                  servers they're in
    connected already          -> the sites simply open; the bot puts them
                                  into any new server itself (no new link)
    site not in Discord yet    -> it opens as soon as its channel exists

One link, used once, ever. The old way — a Discord invite per server, and a
bot guessing who'd used which — could leave someone "waiting to join" for ever;
nothing here depends on guessing.

Taking a site back, and switching technicians between writable and read-only
when a survey is completed or reopened, happen at once over REST: when the
page says access is gone, it's gone.
"""
from dataclasses import dataclass, field

from config import CONFIG
from portal import discord_api
from storage import db
from utils import access


class AssignError(Exception):
    """Something the operator should be told, in plain words."""


@dataclass
class Outcome:
    queued: list
    already: list
    connected: bool                 # has connected Discord with a sign-in that still works
    link: dict                      # their personal link
    notes: list = field(default_factory=list)


def is_connected(tech: dict) -> bool:
    """They've connected Discord and the sign-in still works — so the bot can
    put them into any server without them doing anything."""
    if not tech.get("discord_id"):
        return False
    tokens = db.get_tokens(tech["discord_id"])
    return bool(tokens and not tokens["dead_at"])


def assign_sites(tech: dict, site_ids, actor: str) -> Outcome:
    """Every site must already have been checked as visible to whoever's
    asking; this checks they're the technician's company's, and open."""
    if tech.get("removed_at"):
        raise AssignError(f"{tech['name']} has been removed from the roster.")
    site_ids = list(dict.fromkeys(site_ids))
    if not site_ids:
        raise AssignError("Tick at least one site.")
    for site_id in site_ids:
        site = db.get_site(site_id)
        if not site or site["company_id"] is None:
            raise AssignError(f"{site_id} hasn't been allocated to a company yet.")
        if site["company_id"] != tech["company_id"]:
            raise AssignError(f"{site_id} belongs to a different company than {tech['name']}.")
        if db.site_mode(site_id) == access.READ_ONLY:
            raise AssignError(f"{site_id}'s survey is complete. Reopen it first to assign someone new.")

    held = set()
    if tech.get("discord_id"):
        held = {s["site_id"] for s in db.assigned_sites_for_technician(tech["discord_id"])}
    queued, already = db.queue_sites(tech["id"], [s for s in site_ids if s not in held], actor)
    already += [s for s in site_ids if s in held]

    # Everyone has a link: before they connect it's how they connect; after,
    # it's their page of sites (what the "new sites" email points to).
    link = db.ensure_link(technician_id=tech["id"], created_by=actor, days=CONFIG.join_link_days)
    return Outcome(queued=queued, already=already, connected=is_connected(tech), link=link)


async def revoke_site_access(site: dict, discord_id: int) -> None:
    """Takes an open site back. A 404 from Discord means the overwrite was
    already gone, which is the outcome we wanted — not an error."""
    try:
        await discord_api.revoke_channel_access(site["channel_id"], discord_id)
    except discord_api.DiscordError as exc:
        if exc.status != 404:
            raise AssignError(f"Discord refused to remove access to {site['site_id']}: {exc}") from exc
    db.revoke_tech(site["site_id"], discord_id)
    await _drop_technician_role_if_unused(site["guild_id"], discord_id)


async def _drop_technician_role_if_unused(guild_id: int, discord_id: int) -> None:
    if db.active_site_count_in_guild(discord_id, guild_id) > 0:
        return
    try:
        role_id = (await discord_api.guild_roles(guild_id)).get(CONFIG.technician_role_name)
        if role_id:
            await discord_api.remove_member_role(guild_id, discord_id, role_id)
    except discord_api.DiscordError:
        pass        # a label only; access is the channel overwrite


async def unassign(tech: dict, site_id: str) -> None:
    """Withdraws a site from a technician: waiting or open, it's gone."""
    row = db.queued_row(tech["id"], site_id)
    if row:
        db.cancel_queue_row(row["id"])
    if tech.get("discord_id"):
        site = db.get_site_channel(site_id)
        held = any(s["site_id"] == site_id for s in db.assigned_sites_for_technician(tech["discord_id"]))
        if site and held:
            await revoke_site_access(site, tech["discord_id"])


async def remove_technician(tech: dict) -> None:
    """Takes back everything, then removes them from the roster. If Discord
    refuses any of it the technician is left in place: removing the row while
    they still hold a channel would leave access nothing on screen accounts for."""
    problems = []
    if tech.get("discord_id"):
        for held in db.assigned_sites_for_technician(tech["discord_id"]):
            site = db.get_site_channel(held["site_id"])
            if not site:
                continue
            try:
                await revoke_site_access(site, tech["discord_id"])
            except AssignError as exc:
                problems.append(str(exc))
    db.cancel_queue_for_technician(tech["id"])
    if problems:
        raise AssignError(f"{tech['name']} was not removed. " + " ".join(problems))
    db.revoke_links(technician_id=tech["id"])
    db.remove_technician(tech["id"])
    if tech.get("discord_id") and not db.discord_account_in_use(tech["discord_id"]):
        db.delete_tokens(tech["discord_id"])


async def set_site_mode(site_id: str, mode: str) -> list[str]:
    """Writable or read-only for everyone holding this site. Returns what
    Discord refused (one line per technician); pressing again retries."""
    site = db.get_site_channel(site_id)
    if not site:
        return []
    problems = []
    for holder in db.techs_for_site(site_id):
        try:
            await discord_api.set_overwrite(site["channel_id"], holder["user_id"], mode,
                                            reason=f"{site_id}: survey {'complete' if mode == access.READ_ONLY else 'reopened'}")
        except discord_api.DiscordError as exc:
            problems.append(f"{holder.get('name') or holder['user_id']}: {exc.status or 'no answer'}")
    return problems
