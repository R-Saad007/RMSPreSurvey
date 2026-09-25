"""What the dashboards show: sites with their status, the forecast, how each
company and technician is doing, and the technical view of a single site.

These are plain functions over the database, kept out of the routes so they
can be tested against a real (temporary) database without a web server.
Nothing here decides who may see what — callers pass the scope in, and the
scoping tests are what prove it holds.
"""
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import PurePath

from config import CONFIG, INVENTORY_TAG_HINTS, TAG_LABELS
from storage import db

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
MEDIA_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".amr",
                    ".mp4", ".mov", ".mkv", ".webm", ".3gp"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".txt",
                       ".ppt", ".pptx", ".rtf", ".zip", ".rar", ".json", ".log", ".cfg", ".conf"}


def portfolio(scope=None, city=None, company_id=None) -> list[dict]:
    """Every site the system knows about, with what the dashboards need.

    A site can be known from the inventory (uploaded, maybe not yet in
    Discord), from Discord (provisioned before the inventory existed), or
    both — so the universe is the union. `scope` is the set of site ids the
    caller may see (None = all); it is applied here, once, so no dashboard
    has to remember to.
    """
    inventory = {s["site_id"]: s for s in db.all_sites()}
    channels = {c["site_id"]: c for c in db.all_site_channels()}
    progress = db.all_site_progress()
    blockers = db.open_blocker_counts()
    activity = db.last_activity_all()
    tech_counts = db.active_tech_counts()
    company_names = {c["id"]: c["name"] for c in db.all_companies()}

    rows = []
    for site_id in sorted(set(inventory) | set(channels)):
        if scope is not None and site_id not in scope:
            continue
        inv = inventory.get(site_id) or {}
        channel = channels.get(site_id)
        site_city = inv.get("city") or (channel or {}).get("city")
        owner = inv.get("company_id")
        if company_id is not None and owner != company_id:
            continue
        if city and (site_city or "").lower() != city.lower():
            continue
        act = activity.get(site_id, {})
        rows.append({
            "site_id": site_id,
            "city": site_city,
            "company_id": owner,
            "company_name": company_names.get(owner),
            "provisioned": channel is not None,
            "category_name": (channel or {}).get("category_name"),
            "state": (progress.get(site_id) or {}).get("state", "not_started"),
            "open_blockers": blockers.get(site_id, 0),
            "last_activity": act.get("last_activity"),
            "message_count": act.get("message_count", 0),
            "tech_count": tech_counts.get(site_id, 0),
            "target_install_date": inv.get("target_install_date"),
        })
    return rows


def state_counts(rows: list[dict]) -> dict:
    counts = Counter(r["state"] for r in rows)
    return {state: counts.get(state, 0) for state in ("not_started", "on_site", "blocked", "done")}


def forecast(rows: list[dict], today: date) -> dict:
    """Sites not yet done, bucketed by target install date.

    The windows can overlap (a week that straddles a month end sits inside
    both), because each answers its own question — "how many are planned next
    week?", "how many next month?" — so each carries its date range for the
    page to print underneath.
    """
    this_sunday = today + timedelta(days=6 - today.weekday())
    next_monday = this_sunday + timedelta(days=1)
    next_sunday = next_monday + timedelta(days=6)
    first_next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    last_next_month = (first_next_month + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    windows = {
        "overdue": (None, today - timedelta(days=1)),
        "this_week": (today, this_sunday),
        "next_week": (next_monday, next_sunday),
        "next_month": (first_next_month, last_next_month),
    }
    result = {key: {"count": 0, "start": lo, "end": hi} for key, (lo, hi) in windows.items()}
    result["unscheduled"] = {"count": 0}

    for row in rows:
        if row["state"] == "done":
            continue
        try:
            planned = date.fromisoformat(row["target_install_date"]) if row["target_install_date"] else None
        except ValueError:
            planned = None
        if planned is None:
            # Only a site someone owns can be scheduled, so only those are
            # worth counting as "no date set".
            if row["company_id"] is not None:
                result["unscheduled"]["count"] += 1
            continue
        for key, (lo, hi) in windows.items():
            if (lo is None or planned >= lo) and planned <= hi:
                result[key]["count"] += 1
    return result


def company_summaries(rows: list[dict]) -> tuple[list[dict], dict]:
    """One line per company, and the sites nobody has been given yet."""
    technicians = db.all_technicians()
    by_company = defaultdict(list)
    for row in rows:
        by_company[row["company_id"]].append(row)

    summaries = []
    for company in db.all_companies():
        mine = by_company.get(company["id"], [])
        counts = state_counts(mine)
        summaries.append({
            **company,
            "sites": len(mine),
            "done": counts["done"],
            "active": len(mine) - counts["done"],
            "open_blockers": sum(r["open_blockers"] for r in mine),
            "technicians": sum(1 for t in technicians if t["company_id"] == company["id"]),
        })

    unassigned = by_company.get(None, [])
    return summaries, {"sites": len(unassigned), "provisioned": sum(1 for r in unassigned if r["provisioned"])}


def technician_ranking(company_id: int, rows: list[dict]) -> tuple[list[dict], dict | None]:
    """Ranks a company's technicians and names the best performer.

    A deliberately plain definition, built from what's already recorded so
    nothing new has to be tracked: most sites finished, then fewest open
    blockers on the sites they're still working. It's a first cut — the
    people who'll be judged by it should see it before it's trusted.

    `rows` is that company's portfolio. Sites a technician *finished* count
    even if they've since been unassigned from them.
    """
    state = {r["site_id"]: r["state"] for r in rows}
    blockers = {r["site_id"]: r["open_blockers"] for r in rows}

    held_by = defaultdict(list)
    for assignment in db.site_tech_rows(list(state)):
        held_by[assignment["user_id"]].append(assignment)

    ranking = []
    for tech in db.technicians_for_company(company_id):
        mine = held_by.get(tech["discord_id"], []) if tech["discord_id"] else []
        finished = {a["site_id"] for a in mine if state.get(a["site_id"]) == "done"}
        active = {a["site_id"] for a in mine
                  if a["removed_at"] is None and state.get(a["site_id"]) != "done"}
        ranking.append({
            "technician": tech,
            "completed": len(finished),
            "active": len(active),
            "open_blockers": sum(blockers.get(site_id, 0) for site_id in active),
        })

    ranking.sort(key=lambda r: (-r["completed"], r["open_blockers"], r["technician"]["name"].lower()))
    for position, entry in enumerate(ranking, start=1):
        entry["rank"] = position
    best = ranking[0] if ranking and ranking[0]["completed"] > 0 else None
    return ranking, best


# --- the technical view of one site --------------------------------------

def classify_file(filename: str) -> str:
    suffix = PurePath(filename or "").suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "photo"
    if suffix in MEDIA_EXTENSIONS:
        return "media"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    return "other"


def _hint_tags(label: str):
    lowered = label.lower()
    for pattern, tags in INVENTORY_TAG_HINTS:
        if re.search(pattern, lowered):
            return tags
    return None


def reconcile_items(section: str, text: str, tag_counts: Counter) -> list[dict]:
    """Checks each inventory line against the photos actually tagged.

    Only lines with a defensible tag get a verdict; the rest say so plainly
    instead of being marked missing on the strength of a guess. This is a
    heuristic and the page labels it as one.
    """
    items = []
    for label in (part.strip() for part in (text or "").split(";")):
        if not label:
            continue
        tags = _hint_tags(label)
        if tags is None:
            items.append({"section": section, "label": label, "status": "unchecked", "evidence": None, "tags": ()})
            continue
        evidence = sum(tag_counts.get(tag, 0) for tag in tags)
        items.append({
            "section": section, "label": label, "tags": tags,
            "evidence": evidence, "status": "documented" if evidence else "missing",
        })
    return items


def technical_view(site_id: str) -> dict:
    attachments = db.attachments_for_site(site_id)
    kinds = Counter(classify_file(a["filename"]) for a in attachments)
    tag_counts = Counter(a["tag"] for a in attachments if a["tag"])
    inventory = db.get_site(site_id) or {}

    reconciliation = (
        reconcile_items("On site now", inventory.get("existing_equipment"), tag_counts)
        + reconcile_items("To install", inventory.get("install_boq"), tag_counts)
    )
    checked = [r for r in reconciliation if r["status"] != "unchecked"]

    return {
        "inventory": inventory,
        "total": len(attachments),
        "photos": kinds["photo"],
        "documents": kinds["document"],
        "media": kinds["media"],
        "other": kinds["other"],
        "blurry": sum(1 for a in attachments if a["is_blurry"]),
        "untagged": sum(1 for a in attachments if not a["tag"]),
        # Distinct kinds of equipment photographed — a stand-in for "how much
        # has been configured", since that isn't recorded anywhere directly.
        "configurations": len({tag for tag in tag_counts if tag != "other"}),
        "tag_counts": [(TAG_LABELS.get(tag, tag), n) for tag, n in tag_counts.most_common()],
        "reconciliation": reconciliation,
        "missing": sum(1 for r in checked if r["status"] == "missing"),
        "checked": len(checked),
        "messages": db.message_count(site_id),
    }
