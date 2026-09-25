"""Which Discord servers exist, which are needed, and what state they're in.

A company gets one server per region it works in — "FieldCo_South" — and a
second ("_2") once the first holds SITES_PER_SERVER sites. Discord no longer
lets a bot create servers, so an HQ admin creates each one and adds the bot to
it from the Discord page; everything after that is automatic. This module
works out what's needed and describes what's there. Nothing here talks to
Discord.
"""
from config import CONFIG
from storage import db

ACTIVE = ("claimed", "review", "confirmed", "ready")


def needed_servers() -> list[dict]:
    """[{company_id, company_name, region, seq, name, sites}] — one entry per
    server that has to be created before these sites can have channels."""
    servers = [g for g in db.all_guilds(ACTIVE) if g["company_id"] is not None]
    needed = []
    for slot in db.unprovisioned_by_slot():
        region = (slot["region"] or "").strip()
        if not region:
            continue                                   # can't place it anywhere; reported separately
        mine = [g for g in servers
                if g["company_id"] == slot["company_id"] and (g["region"] or "").lower() == region.lower()]
        room = sum(max(CONFIG.sites_per_server - g["site_count"], 0) for g in mine)
        waiting = slot["n"] - room
        seq = max((g["seq"] or 0 for g in mine), default=0)
        while waiting > 0:
            seq += 1
            count = min(waiting, CONFIG.sites_per_server)
            needed.append({
                "company_id": slot["company_id"], "company_name": slot["company_name"], "region": region,
                "seq": seq, "name": db.server_name(slot["company_name"], region, seq), "sites": count,
            })
            waiting -= count
    return needed


def sites_without_region() -> int:
    return sum(s["n"] for s in db.unprovisioned_by_slot() if not (s["region"] or "").strip())


def overview() -> dict:
    """Everything the Discord page shows, grouped."""
    guilds = db.all_guilds()
    staff = db.all_staff()
    placed = {}
    for row in db.guild_staff_rows():
        placed.setdefault(row["guild_id"], {})[row["staff_id"]] = row
    linked = [s for s in staff if s["discord_id"]]
    for g in guilds:
        rows = placed.get(g["guild_id"], {})
        g["staff_in"] = sum(1 for s in linked if rows.get(s["id"], {}).get("ok"))
        g["staff_problems"] = [f"{s['name']}: {rows[s['id']]['error']}" for s in linked
                               if s["id"] in rows and not rows[s["id"]]["ok"] and rows[s["id"]]["error"]]
    return {
        "needed": needed_servers(),
        "no_region": sites_without_region(),
        "connected": [g for g in guilds if g["status"] in ("claimed", "confirmed", "ready")],
        "review": [g for g in guilds if g["status"] == "review"],
        "unrecognised": [g for g in guilds if g["status"] == "unrecognised"],
        "gone": [g for g in guilds if g["status"] == "left" and not g["released_at"] and g["site_count"]],
        "staff": staff,
        "staff_linked": len(linked),
    }
