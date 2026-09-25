"""Registers a server the bot is already in as one company's server for one
region, ready to use. For servers set up before the portal could claim them
— the Islamabad pilot. New servers are claimed from the portal's Discord page.

    .venv/bin/python -m tools.register_guild --guild 123456789012345678 --company "HQ Tech" --region North
        [--seq 1] [--rename "HQ Tech_North"] [--rename-category "ISB Sites 001-020=Sites 001-050"]

Safe to repeat. Without the rename options nothing in Discord changes here;
once the server is 'ready' the bot keeps it locked down and its roles in place
on its own. --rename-category gives the pilot's category the name the bot
fills in blocks of 50, so new sites for the same company and region join it.
"""
import argparse
import asyncio
import sys

from portal import discord_api
from storage import db


async def _discord(guild_id: int, rename: str | None, rename_category: str | None) -> str:
    guild = await discord_api._request("GET", f"/guilds/{guild_id}")
    name = guild["name"]
    if rename and rename != name:
        await discord_api._request("PATCH", f"/guilds/{guild_id}", json={"name": rename}, reason="Site server name")
        print(f"[register] renamed the server: {name} -> {rename}")
        name = rename
    if rename_category:
        old, _, new = rename_category.partition("=")
        channels = await discord_api._request("GET", f"/guilds/{guild_id}/channels")
        category = next((c for c in channels if c["type"] == 4 and c["name"] == old.strip()), None)
        if category:
            await discord_api._request("PATCH", f"/channels/{category['id']}", json={"name": new.strip()},
                                       reason="Site category name")
            with db.get_conn() as conn:
                conn.execute("UPDATE site_channels SET category_name = ? WHERE guild_id = ? AND category_name = ?",
                             (new.strip(), guild_id, old.strip()))
            print(f"[register] renamed the category: {old.strip()} -> {new.strip()}")
        elif any(c["type"] == 4 and c["name"] == new.strip() for c in channels):
            print(f"[register] the category is already called {new.strip()}")
        else:
            sys.exit(f"No category called {old.strip()!r} in that server.")
    return name


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--guild", type=int, required=True)
    p.add_argument("--company", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--seq", type=int, default=1)
    p.add_argument("--rename")
    p.add_argument("--rename-category", help='"old name=new name"')
    args = p.parse_args()

    db.init_db()
    company = db.get_company_by_name(args.company)
    if not company:
        sys.exit(f"No company called {args.company!r}.")
    region = {r.lower(): r for r in db.all_regions()}.get(args.region.strip().lower())
    if not region:
        sys.exit(f"{args.region!r} isn't a region. Known: {', '.join(db.all_regions())}")

    existing = db.get_guild(args.guild)
    if existing and existing["status"] == "ready" and (existing["company_id"], existing["region"], existing["seq"]) \
            == (company["id"], region, args.seq):
        print(f"[register] already registered: {existing['name']} = {company['name']} / {region} / {args.seq}")
    elif existing and existing["status"] not in ("unrecognised", "left"):
        sys.exit(f"That server is already registered as {existing['company_name']} / {existing['region']} "
                 f"({existing['status']}). Nothing changed.")
    else:
        taken = db.guild_in_slot(company["id"], region, args.seq)
        if taken:
            sys.exit(f"{company['name']} / {region} / {args.seq} is already server {taken['name']}. Nothing changed.")

    name = asyncio.run(_discord(args.guild, args.rename, args.rename_category))
    if not (existing and existing["status"] == "ready"):
        if not db.claim_guild(args.guild, name, company["id"], region, args.seq, "register_guild", status="ready"):
            sys.exit("Couldn't register it (it was claimed a moment ago?). Nothing changed.")
        print(f"[register] {name} is {company['name']}'s server for {region} (#{args.seq}), ready")
    else:
        db.update_guild_facts(args.guild, name=name)


if __name__ == "__main__":
    main()
