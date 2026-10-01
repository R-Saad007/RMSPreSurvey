"""The control tower.

Assignment, progress and blocker visibility for people working remotely.
It never becomes the place a problem gets solved — that happens in the
site channel, in conversation, the way the WhatsApp groups already work.
This just makes sure nothing gets lost and the right people can see it.

Who sees what: HQ logins (admin, staff) see every site. A company manager
sees only the sites HQ allocated to their company, and only that company's
technicians. Every route that touches a site says so explicitly — see
portal/auth.py — and tools/test_portal.py is what proves it holds.

    uvicorn portal.main:app --host 0.0.0.0 --port 8000
"""
import hashlib
import re
import secrets
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from config import CONFIG, PROGRESS_LABELS, TAG_LABELS
from portal import assign, discord_api, insights, inventory, links, ratelimit, servers
from portal.auth import (
    authenticate, assert_site_visible, assert_technician_visible, current_user, hash_password,
    require_admin, require_manager, require_hq, require_user, verify_password, visible_site_ids,
    PASSWORD_CHANGE_PATH, HQ_ROLES, ROLES,
)
from storage import db
from utils import access, oauth
from utils import blockers as blocker_rules
from utils.attach import rename_tagged_files
from utils.phone import COUNTRIES, COUNTRY_NAMES, display as phone_display, normalize

BASE_DIR = Path(__file__).parent
MIN_PASSWORD_LENGTH = 10
_SECRET = CONFIG.portal_secret_key or secrets.token_hex(32)

# openapi_url is disabled alongside the docs pages: it needs no auth, so it
# would otherwise publish every route and parameter to anyone who asks.
app = FastAPI(title="RMS Control Tower", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SECRET,
    # The portal is served over HTTPS by Caddy, so the session cookie is
    # marked secure and never travels in the clear. If this is ever run on
    # plain HTTP again the browser will silently drop the cookie and nobody
    # will be able to stay signed in — that is the intended failure.
    https_only=True,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# The Discord sign-in carries a signed note of what it's for (whose link, or
# which server slot), so the answer that comes back can't be pointed at
# something else. Signed, not encrypted: a link's token is never put in it.
_signer = URLSafeTimedSerializer(_SECRET, salt="discord-oauth")
STATE_MAX_AGE = 1800


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    path = request.url.path
    if path.startswith("/j/") or path.startswith("/oauth/"):
        # A personal link is a key. Never cached, and never sent on to
        # another site in a Referer when they tap through to Discord.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
    return response

templates = Jinja2Templates(directory=BASE_DIR / "templates")
_TZ = ZoneInfo(CONFIG.timezone_name)


def _static_version() -> str:
    """A fingerprint of the static files, appended to their URLs so a browser
    fetches the new stylesheet after a deploy instead of reusing its cached
    copy (a fixed layout that nobody sees until they clear their cache)."""
    digest = hashlib.sha1()
    for path in sorted((BASE_DIR / "static").rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _fmt_time(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts), _TZ).strftime("%d %b, %H:%M")


def _fmt_day(iso):
    if not iso:
        return "—"
    try:
        return date.fromisoformat(iso).strftime("%d %b %Y")
    except ValueError:
        return iso


def _ago(ts):
    if not ts:
        return "never"
    seconds = time.time() - float(ts)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


templates.env.filters["fmt_time"] = _fmt_time
templates.env.filters["fmt_day"] = _fmt_day
templates.env.filters["ago"] = _ago
templates.env.filters["phone"] = lambda e164, country=None: phone_display(e164, country) if e164 else "—"
templates.env.globals["STATIC_VERSION"] = _static_version()
templates.env.globals["COUNTRIES"] = COUNTRIES
templates.env.globals["STAFF_ROLES"] = db.STAFF_ROLES
templates.env.globals["READ_ONLY"] = access.READ_ONLY
templates.env.globals["PROGRESS_LABELS"] = PROGRESS_LABELS
templates.env.globals["TAG_LABELS"] = TAG_LABELS
templates.env.globals["HQ_ROLES"] = HQ_ROLES
templates.env.globals["ROLE_LABELS"] = {
    "hq_admin": "HQ admin",
    "hq_staff": "HQ staff",
    "company_manager": "Company manager",
}
# The Sites page's "Planned" filter, and what each person on a site is waiting for.
PLAN_FILTERS = {"overdue": "Overdue", "this_week": "This week", "next_week": "Next week",
                "next_month": "Next month", "none": "No date set"}
PERSON_STATES = {"in": "", "link": "not connected to Discord yet", "reconnect": "needs to open their link again",
                 "waiting_server": "opens once the site has a channel", "opening": "opening in Discord…"}
templates.env.globals["PLAN_FILTERS"] = PLAN_FILTERS
templates.env.globals["PERSON_STATES"] = PERSON_STATES


def _fingerprint_for(user) -> str:
    """What the live pages poll: every company's changes for HQ, only its own
    for a company. A company login with no company sees nothing, so nothing
    it could see ever changes."""
    if user["role"] in HQ_ROLES:
        return db.change_fingerprint(None)
    if user.get("company_id") is None:
        return "none"
    return db.change_fingerprint(user["company_id"])


def render(request: Request, template: str, **context):
    context.setdefault("user", current_user(request))
    context.setdefault("flash", request.query_params.get("msg"))
    context.setdefault("error", request.query_params.get("err"))
    if context["user"] and not context["user"]["must_change_password"]:
        context.setdefault("LIVE_VERSION", _fingerprint_for(context["user"]))
        if context["user"]["role"] in HQ_ROLES:
            context.setdefault("BOT_OFFLINE", not db.bot_online())
    return templates.TemplateResponse(request, template, context)


def render_public(request: Request, template: str, status_code: int = 200, **context):
    """The pages a technician sees from their link: no sign-in, no navigation."""
    context.setdefault("user", None)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _go(path: str, msg: str | None = None, err: str | None = None):
    """Redirect back with a message. Encoded properly: Discord's error text
    can hold '&' and '#', which would silently cut a hand-built query string."""
    params = {}
    if msg:
        params["msg"] = msg[:400]
    if err:
        params["err"] = err[:400]
    if params:
        path += ("&" if "?" in path else "?") + urlencode(params)
    return RedirectResponse(path, status_code=303)


def _int_or_none(value) -> int | None:
    """Filter dropdowns submit an empty string for 'Any'; that means no
    filter, not an error."""
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _today() -> date:
    return datetime.now(_TZ).date()


def _parse_day(text: str) -> str | None:
    """'' clears a date; anything else must be a real YYYY-MM-DD."""
    text = (text or "").strip()
    if not text:
        return None
    return date.fromisoformat(text).isoformat()     # ValueError if it isn't one


def _referer_path(request: Request, fallback: str) -> str:
    """Where the person came from. Only the path is ever used — never the
    host from the Referer header, which would turn a resolve click into an
    open redirect — and a path starting '//' is refused too, since a browser
    reads that as another site."""
    ref = request.headers.get("referer")
    if ref:
        parts = urlparse(ref)
        if parts.path.startswith("/") and not parts.path.startswith("//"):
            return parts.path + (f"?{parts.query}" if parts.query else "")
    return fallback


# --- auth ----------------------------------------------------------------

@app.get("/login")
async def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    # Throttled per IP and per email: see portal/ratelimit.py for why both.
    # request.client.host is the browser's address rather than Caddy's only
    # because the service runs with --proxy-headers.
    ip = request.client.host if request.client else "unknown"
    keys = (f"ip:{ip}", f"email:{email.lower().strip()}")

    wait = ratelimit.retry_after(*keys)
    if wait:
        minutes = max(1, round(wait / 60))
        return RedirectResponse(
            f"/login?err=Too+many+failed+attempts.+Try+again+in+{minutes}+minutes.",
            status_code=303,
        )

    user = authenticate(email, password)
    if not user:
        ratelimit.record_failure(*keys)
        return RedirectResponse("/login?err=Wrong+email+or+password", status_code=303)

    ratelimit.clear(*keys)
    request.session["user_id"] = user["id"]
    if user["must_change_password"]:
        return RedirectResponse(PASSWORD_CHANGE_PATH, status_code=303)
    return RedirectResponse("/", status_code=303)


# --- own password --------------------------------------------------------

@app.get(PASSWORD_CHANGE_PATH)
async def password_form(request: Request, user=Depends(require_user)):
    return render(request, "password.html",
                  forced=bool(user["must_change_password"]),
                  min_length=MIN_PASSWORD_LENGTH)


@app.post(PASSWORD_CHANGE_PATH)
async def change_password(request: Request, current: str = Form(...),
                          new: str = Form(...), confirm: str = Form(...),
                          user=Depends(require_user)):
    def back(err):
        return RedirectResponse(f"{PASSWORD_CHANGE_PATH}?err={err}", status_code=303)

    if not verify_password(user["password_hash"], current):
        return back("Current+password+is+wrong")
    if new != confirm:
        return back("The+new+passwords+do+not+match")
    if len(new) < MIN_PASSWORD_LENGTH:
        return back(f"Use+at+least+{MIN_PASSWORD_LENGTH}+characters")
    if new == current:
        return back("That+is+the+password+you+already+have")

    db.set_portal_password(user["id"], hash_password(new))
    return RedirectResponse("/?msg=Password+changed", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- dashboard -----------------------------------------------------------

@app.get("/")
async def dashboard(request: Request, user=Depends(require_user)):
    scope = visible_site_ids(user)
    rows = insights.portfolio(scope=scope)
    open_blockers = [b for b in db.open_blockers() if scope is None or b["site_id"] in scope]
    # Sorted here rather than in the template: last_activity is None for a
    # site nobody has messaged yet, and Jinja's sort filter compares those
    # against real timestamps and dies. A site with no messages isn't
    # "recently active" anyway, so it's left out entirely.
    recent = sorted((r for r in rows if r["last_activity"]),
                    key=lambda r: r["last_activity"], reverse=True)[:10]
    shared = dict(
        sites=rows,
        recent=recent,
        blockers=open_blockers[:15],
        blocker_total=len(open_blockers),
        counts=insights.state_counts(rows),
        forecast=insights.forecast(rows, _today()),
    )

    if scope is None:
        companies, unassigned = insights.company_summaries(rows)
        return render(
            request, "dashboard.html", **shared,
            companies=companies,
            unassigned=unassigned,
            technician_total=len(db.all_technicians()),
            cities=sorted({r["city"] for r in rows if r["city"]}),
            servers_needed=servers.needed_servers() if user["role"] == "hq_admin" else [],
        )

    company = db.get_company(user["company_id"]) if user.get("company_id") is not None else None
    ranking, best = ([], None)
    if company:
        ranking, best = insights.technician_ranking(company["id"], rows)
    return render(
        request, "company_dashboard.html", **shared,
        company=company,
        active=[r for r in rows if r["state"] != "done"][:12],
        ranking=ranking,
        best=best,
    )


# --- sites ---------------------------------------------------------------

@app.get("/sites")
async def sites_list(request: Request, q: str = "", city: str = "", status: str = "", planned: str = "",
                     tech: str = "", region: str = "", company_id: str = "", discord: str = "",
                     user=Depends(require_user)):
    """Where sites are worked, for everyone: one filter bar, one list, and a bar
    that acts on the ticked sites. What that bar offers depends on who's
    looking — a company manager gives sites to technicians; HQ adds sites and
    allocates them to companies."""
    scope = visible_site_ids(user)
    is_hq = scope is None
    everything = insights.portfolio(scope=scope)
    people = db.site_people()
    for row in everything:
        row["people"] = people.get(row["site_id"], [])

    filters = {
        "q": q.strip(),
        "city": city.strip(),
        "status": status if status in PROGRESS_LABELS else "",
        "planned": planned if planned in PLAN_FILTERS else "",
        "tech": tech.strip(),
        # A company manager has one company; these only mean something to HQ.
        "region": region.strip() if is_hq else "",
        "company_id": company_id.strip() if is_hq else "",
        "discord": discord if is_hq and discord in ("yes", "no") else "",
    }
    today = _today()
    rows = [r for r in everything if _site_matches(r, filters, today)]

    roster, whatsapp = [], {}
    if user["role"] == "company_manager" and user.get("company_id") is not None:
        roster = db.technicians_for_company(user["company_id"])
        # Whoever hasn't connected Discord yet (or has to again) gets their
        # link sent from right here: one tap, no trip to their page.
        for tech in roster:
            standing = _discord_status(tech)
            if standing["state"] != "connected":
                waiting = [r["site_id"] for r in db.queue_for_technician(tech["id"])]
                url = _whatsapp_for(tech, waiting, standing["link"], False)
                if url:
                    whatsapp[tech["id"]] = url
    context = dict(
        sites=rows,
        filters=filters,
        filtered=any(filters.values()),
        total=len(everything),
        is_hq=is_hq,
        roster=roster,
        whatsapp=whatsapp,
        cities=sorted({r["city"] for r in everything if r["city"]}),
    )
    if is_hq:
        context.update(
            regions=db.all_regions(),
            companies=db.all_companies(),
            tiles={"unallocated": sum(1 for r in everything if r["company_id"] is None),
                   "not_in_discord": sum(1 for r in everything if not r["provisioned"])},
        )
        if user["role"] == "hq_admin":
            context["imports"] = db.recent_site_imports()
    return render(request, "sites.html", **context)


def _site_matches(row: dict, f: dict, today: date) -> bool:
    if f["q"] and f["q"].lower() not in row["site_id"].lower():
        return False
    if f["city"] and (row["city"] or "").lower() != f["city"].lower():
        return False
    if f["status"] and row["state"] != f["status"]:
        return False
    if f["planned"] and not insights.planned_in(row, f["planned"], today):
        return False
    if f["tech"] == "none" and row["people"]:
        return False
    if f["tech"] not in ("", "none") and not any(str(p["technician_id"]) == f["tech"] for p in row["people"]):
        return False
    if f["region"] and (row["region"] or "").lower() != f["region"].lower():
        return False
    if f["company_id"] == "none" and row["company_id"] is not None:
        return False
    if f["company_id"] not in ("", "none") and str(row["company_id"]) != f["company_id"]:
        return False
    if f["discord"] == "yes" and not row["provisioned"]:
        return False
    if f["discord"] == "no" and row["provisioned"]:
        return False
    return True


def _back_to_sites(request: Request) -> str:
    """Back to the Sites page the way the person had it — their filters, not
    the previous message."""
    back = urlparse(_referer_path(request, "/sites"))
    if back.path != "/sites":
        return "/sites"
    kept = [(k, v) for k, v in parse_qsl(back.query) if k not in ("msg", "err")]
    return "/sites" + (f"?{urlencode(kept)}" if kept else "")


def _sites_text(site_ids) -> str:
    return site_ids[0] if len(site_ids) == 1 else f"{len(site_ids)} sites"


BULK_ACTIONS = ("assign", "assign_new", "unassign", "date", "complete", "reopen", "allocate")


@app.post("/sites/bulk")
async def sites_bulk(request: Request, background: BackgroundTasks, action: str = Form(""),
                     site_ids: list[str] = Form(default=[]), technician_id: str = Form(""),
                     target_install_date: str = Form(""), company_id: str = Form(""),
                     name: str = Form(""), phone: str = Form(""), country: str = Form("PK"),
                     email: str = Form(""), user=Depends(require_user)):
    """Everything the Sites page does to sites: the bar acts on the ticked
    ones, a row's own controls on that one site. A company manager assigns and
    takes back; an HQ admin allocates sites to companies; anyone who can see a
    site sets its planned date and completes or reopens it."""
    if action not in BULK_ACTIONS:
        raise HTTPException(400, "Unknown action")
    if action in ("assign", "assign_new", "unassign") and user["role"] != "company_manager":
        raise HTTPException(403, "Company managers only")
    if action == "allocate" and user["role"] != "hq_admin":
        raise HTTPException(403, "Admins only")
    back = _back_to_sites(request)
    site_ids = list(dict.fromkeys(s.strip() for s in site_ids if s.strip()))
    if not site_ids:
        return _go(back, err="Tick at least one site first.")
    # Every site, before any is touched: one that isn't theirs refuses the lot.
    for site_id in site_ids:
        assert_site_visible(user, site_id)
    if action != "allocate":
        for site_id in site_ids:
            if not (db.get_site(site_id) or db.get_site_channel(site_id)):
                raise HTTPException(404, "Unknown site")

    if action == "assign":
        return await _bulk_assign(background, user, site_ids, technician_id, "", "", country, "", back)
    if action == "assign_new":
        if not name.strip() or not phone.strip():
            return _go(back, err="For someone new, give their name and WhatsApp number.")
        return await _bulk_assign(background, user, site_ids, "", name, phone, country, email, back)
    if action == "unassign":
        return await _bulk_unassign(user, site_ids, technician_id, back)
    if action == "date":
        try:
            planned = _parse_day(target_install_date)
        except ValueError:
            return _go(back, err="That isn't a valid date.")
        for site_id in site_ids:
            db.set_target_install_date(site_id, planned)
        return _go(back, msg=f"Planned date {'set' if planned else 'cleared'} for {_sites_text(site_ids)}.")
    if action == "complete":
        problems = []
        for site_id in site_ids:
            db.set_site_progress(site_id, "done", user["name"], None)
            problems += [f"{site_id}: {p}" for p in await assign.set_site_mode(site_id, access.READ_ONLY)]
        if problems:
            return _go(back, err="Marked complete, but Discord didn't take it for everyone "
                                 f"({'; '.join(problems[:3])}). Mark them complete again to retry.")
        return _go(back, msg=f"{_sites_text(site_ids)} marked complete. "
                             "Technicians can still read the channel, but not post.")
    if action == "reopen":
        done = [s for s in site_ids if db.site_mode(s) == access.READ_ONLY]
        if not done:
            return _go(back, err="None of those sites is complete, so there's nothing to reopen.")
        problems = []
        for site_id in done:
            db.set_site_progress(site_id, "on_site", user["name"], "reopened")
            problems += [f"{site_id}: {p}" for p in await assign.set_site_mode(site_id, access.WRITABLE)]
        if problems:
            return _go(back, err="Reopened, but Discord didn't take it for everyone "
                                 f"({'; '.join(problems[:3])}). Reopen them again to retry.")
        return _go(back, msg=f"{_sites_text(done)} reopened. Technicians can post in the channel again.")
    return _bulk_allocate(site_ids, company_id, target_install_date, user, back)


async def _bulk_assign(background, user, site_ids, technician_id, name, phone, country, email, back):
    """Gives the sites to one of the company's technicians, or to someone new
    typed into the bar (added to the roster in the same step)."""
    company_id = user.get("company_id")
    if company_id is None:
        return _go(back, err="Your account isn't linked to a company yet.")
    if technician_id.strip():
        chosen = _int_or_none(technician_id)
        tech = assert_technician_visible(user, db.get_technician(chosen) if chosen else None)
    elif name.strip() and phone.strip():
        tech, problem = _new_technician(company_id, name, phone, country, email, user["name"])
        if problem:
            return _go(back, err=problem)
    else:
        return _go(back, err="Pick a technician, or add someone new.")
    try:
        outcome = assign.assign_sites(tech, site_ids, user["name"])
    except assign.AssignError as exc:
        return _go(back, err=str(exc))
    note = ""
    if outcome.queued and (tech.get("email") or not outcome.connected):
        note = _send_link(background, tech, outcome.queued, user["name"], outcome.connected, outcome.link)
    return _go(back, msg=_assign_message(tech, outcome, note))


async def _bulk_unassign(user, site_ids, technician_id, back):
    """Takes the sites back from one technician: waiting or open, they're gone."""
    chosen = _int_or_none(technician_id)
    tech = assert_technician_visible(user, db.get_technician(chosen) if chosen else None)
    held = {s["site_id"] for s in db.assigned_sites_for_technician(tech["discord_id"])} if tech["discord_id"] else set()
    waiting = {r["site_id"] for r in db.queue_for_technician(tech["id"])}
    theirs = [s for s in site_ids if s in held or s in waiting]
    if not theirs:
        return _go(back, err=f"{tech['name']} doesn't have {'that site' if len(site_ids) == 1 else 'any of those sites'}.")
    try:
        for site_id in theirs:
            await assign.unassign(tech, site_id)
    except assign.AssignError as exc:
        return _go(back, err=str(exc))
    return _go(back, msg=f"{_sites_text(theirs)} taken back from {tech['name']}.")


def _bulk_allocate(site_ids, company_id, target_install_date, user, back):
    if company_id == "none":
        chosen = None
    else:
        chosen = _int_or_none(company_id)
        if chosen is None or not db.get_company(chosen):
            return _go(back, err="Choose a company to give them to.")
    try:
        planned = _parse_day(target_install_date)
    except ValueError:
        return _go(back, err="That isn't a valid date.")
    assigned, skipped = db.bulk_assign_company(site_ids, chosen, planned, user["name"])
    target = db.get_company(chosen)["name"] if chosen else "no company"
    message = f"{len(assigned)} site(s) now allocated to {target}."
    for why in dict.fromkeys(w for _, w in skipped):
        these = [s for s, w in skipped if w == why]
        message += f" {len(these)} left alone ({why}): {', '.join(these[:6])}{'…' if len(these) > 6 else ''}."
    return _go(back, msg=message)


# Only an HQ admin adds sites. A site's company and region decide which
# Discord server gets built for it, so ingestion stays with HQ; a company
# manager works the sites they're given.

@app.get("/sites/template.xlsx")
async def site_template(request: Request, user=Depends(require_admin)):
    """The one layout every site upload uses."""
    data = inventory.build_template(db.all_regions())
    return Response(data, media_type=inventory.XLSX_MEDIA_TYPE,
                    headers={"Content-Disposition": 'attachment; filename="RMS site template.xlsx"'})


@app.post("/sites/upload")
async def upload_sites(request: Request, file: UploadFile = File(...), company_id: str = Form(""),
                       user=Depends(require_admin)):
    """A filled-in template, for the company the admin picks (or none, to load
    sites unallocated). Nothing is imported yet: the admin first sees what it
    would do — which sites are new, which already exist — and confirms."""
    back = "/sites"
    if company_id == "none":
        chosen = None
    else:
        chosen = _int_or_none(company_id)
        if chosen is None or db.get_company(chosen) is None:
            return _go(back, err="Choose which company these sites are for.")

    data = await file.read(inventory.MAX_UPLOAD_BYTES + 1)
    try:
        rows = inventory.parse_sites_file(file.filename or "", data, db.all_regions())
    except inventory.UploadError as exc:
        return _go(back, err=str(exc))
    upload_id = db.create_pending_upload(user["id"], chosen, file.filename, rows)
    return RedirectResponse(f"/sites/upload/{upload_id}", status_code=303)


FIELD_HEADINGS = {field: heading for heading, field, _ in inventory.TEMPLATE_COLUMNS}
EXPIRED_UPLOAD = "That check has expired or was already imported. Upload the file again."


def _pending_upload(user: dict, upload_id: int):
    """The checked upload, for the admin who uploaded it and nobody else.
    (None: expired, or already imported.)"""
    pending = db.get_pending_upload(upload_id)
    if pending and pending["user_id"] != user["id"]:
        raise HTTPException(404, "No upload waiting here")
    return pending


@app.get("/sites/upload/{upload_id}")
async def upload_check(request: Request, upload_id: int, user=Depends(require_admin)):
    pending = _pending_upload(user, upload_id)
    if pending is None:
        return _go("/sites", err=EXPIRED_UPLOAD)
    company = db.get_company(pending["company_id"]) if pending["company_id"] is not None else None
    if pending["company_id"] is not None and company is None:
        return _go("/sites", err="That company has been removed since the upload. Nothing was imported.")
    return render(request, "upload_check.html", upload=pending, company=company, total=len(pending["rows"]),
                  check=db.check_site_upload(pending["rows"], pending["company_id"]), headings=FIELD_HEADINGS)


@app.post("/sites/upload/{upload_id}/confirm")
async def upload_confirm(request: Request, upload_id: int, update_existing: str = Form(""),
                         user=Depends(require_admin)):
    """Imports a checked upload. The rules are re-applied to the sites as they
    are now (db.apply_site_upload), so anything that changed since the check
    was shown is still caught."""
    pending = _pending_upload(user, upload_id)
    if pending is None:
        return _go("/sites", err=EXPIRED_UPLOAD)
    company = None
    if pending["company_id"] is not None:
        company = db.get_company(pending["company_id"])
        if company is None:
            return _go("/sites", err="That company has been removed since the check. Nothing was imported.")
    if not db.claim_pending_upload(upload_id, user["id"]):
        return _go("/sites", err=EXPIRED_UPLOAD)

    result = db.apply_site_upload(pending["rows"], pending["company_id"], user["name"], pending["filename"],
                                  update_existing=bool(update_existing))
    parts = [f"{result['added']} new"]
    if result["claimed"]:
        parts.append(f"{result['claimed']} taken from the master list")
    if result["updated"]:
        parts.append(f"{result['updated']} updated")
    if result["kept"]:
        parts.append(f"{len(result['kept'])} already there, left as they were")
    target = company["name"] if company else "the master list (not allocated)"
    message = f"Imported into {target}: " + ", ".join(parts) + "."
    if result["skipped"]:
        shown = "; ".join(f"{site_id} ({why})" for site_id, why in result["skipped"][:3])
        more = f" and {len(result['skipped']) - 3} more" if len(result["skipped"]) > 3 else ""
        message += f" Not imported, {len(result['skipped'])}: {shown}{more}."
    return _go("/sites", msg=message)


@app.post("/sites/upload/{upload_id}/cancel")
async def upload_cancel(request: Request, upload_id: int, user=Depends(require_admin)):
    if _pending_upload(user, upload_id) is not None:
        db.discard_pending_upload(upload_id, user["id"])
    return _go("/sites", msg="Upload cancelled. Nothing was imported.")


@app.get("/healthz")
async def healthz():
    """For the container's health check (and, later, a load balancer): the
    process answers and the database opens. Says nothing about the data."""
    with db.get_conn() as conn:
        conn.execute("SELECT 1").fetchone()
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/live/version")
async def live_version(request: Request, user=Depends(require_user)):
    return JSONResponse({"v": _fingerprint_for(user)}, headers={"Cache-Control": "no-store"})


STATUS_LABELS = {
    "connected": "Connected",
    "reconnect": "Needs to open their link again",
    "expired": "Link expired",
    "link_sent": "Link sent",
    "not_sent": "Not sent yet",
}
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS


def _discord_status(tech: dict) -> dict:
    """Where someone stands with Discord, for the pages: connected (we know
    their account, and nothing waits on them), or at which step of getting
    their link. 'reconnect': a site waits in a server they have to join
    themselves — their link page has the invite."""
    link = db.current_link(technician_id=tech["id"])
    tokens = db.get_tokens(tech["discord_id"]) if tech.get("discord_id") else None
    if tech.get("discord_id"):
        stuck = any(r["error"] == "needs_connect" for r in db.queue_for_technician(tech["id"]))
        state = "reconnect" if stuck else "connected"
    elif link and link["expires_at"] <= time.time():
        state = "expired"
    elif link:
        state = "link_sent"
    else:
        state = "not_sent"
    username = tokens["username"] if tokens else None
    if username is None and tech.get("discord_id"):
        joined = [i for i in db.invites_for_technician(tech["id"]) if i["outcome"] == "joined"]
        username = joined[0]["joined_name"] if joined else None
    return {"state": state, "link": link, "username": username}


def _whatsapp_for(tech: dict, site_ids, link: dict | None, connected: bool) -> str | None:
    if not link or not tech.get("phone"):
        return None
    company = tech.get("company_name") or ""
    return links.whatsapp_url(tech["phone"], links.whatsapp_text(tech, company, site_ids, link["token"], connected))


def _queue_state(row: dict, connected: bool) -> str:
    if row["error"] == "needs_connect":
        return "waiting for them to open their link and tap Open Discord"
    if not row["channel_id"]:
        return "waiting for its Discord server"
    if not connected and not row["discord_id"]:
        return "waiting for them to open their link and join in Discord"
    if row["error"]:
        return f"not opened yet: {row['error']}"
    return "opening in Discord…"


def _send_link(background: BackgroundTasks, tech: dict, site_ids, actor: str, connected: bool, link: dict) -> str:
    """Emails the link when there's an address (and email is set up); returns
    what to tell the operator about it."""
    if not tech.get("email"):
        return "Send it with the WhatsApp button by their name."
    subject, body = links.email_for(tech, tech.get("company_name") or "", site_ids, link["token"], connected)
    if links.queue_email(background, tech["email"], subject, body, actor, technician_id=tech["id"]):
        return f"It's on its way to {tech['email']}."
    return "Email isn't set up on the server yet — send it with the WhatsApp button by their name."


def _assign_message(tech: dict, outcome, email_note: str) -> str:
    parts = []
    if outcome.queued:
        n = len(outcome.queued)
        parts.append(f"{n} site{'' if n == 1 else 's'} assigned to {tech['name']}")
    if outcome.already:
        parts.append(f"{', '.join(outcome.already[:5])} already theirs")
    text = "; ".join(parts) + "."
    if outcome.connected:
        return text + " They're connected to Discord, so the sites open for them by themselves."
    return text + " They open once they tap Open Discord on their link. " + email_note


@app.get("/sites/{site_id}")
async def site_detail(request: Request, site_id: str, user=Depends(require_user)):
    assert_site_visible(user, site_id)
    site = db.get_site_channel(site_id)
    inventory_row = db.get_site(site_id)
    if not site and not inventory_row:
        raise HTTPException(404, "Unknown site")
    company_id = inventory_row["company_id"] if inventory_row else None
    techs = db.techs_for_site(site_id)
    waiting = db.queue_for_site(site_id)
    progress = db.get_site_progress(site_id)
    completed = bool(progress and progress["state"] == "done")
    open_blockers = db.open_blockers(site_id)
    state = insights.site_state(progress, len(open_blockers), bool(db.sites_with_technician_messages(site_id)))

    for row in waiting:
        tech = db.get_technician(row["technician_id"])
        status = _discord_status(tech)
        row["state"] = _queue_state(row, status["state"] == "connected")
        row["whatsapp"] = None if status["state"] == "connected" else _whatsapp_for(tech, [site_id], status["link"], False)
        row["link_url"] = links.link_url(status["link"]["token"]) if status["link"] else None

    # Who could be assigned here: this company's technicians who don't
    # already hold the site or have it waiting for them. Only the company's
    # manager assigns, so only they get the list.
    roster = []
    if company_id is not None and user["role"] == "company_manager":
        held = {t["user_id"] for t in techs}
        queued = {r["technician_id"] for r in waiting}
        roster = [t for t in db.technicians_for_company(company_id)
                  if (t["discord_id"] is None or t["discord_id"] not in held) and t["id"] not in queued]

    return render(
        request,
        "site_detail.html",
        site_id=site_id,
        site=site,
        city=(site or {}).get("city") or (inventory_row or {}).get("city"),
        inventory=inventory_row,
        company_name=inventory_row["company_name"] if inventory_row else None,
        company_id=company_id,
        roster=roster,
        progress=progress,
        completed=completed,
        state=state,
        techs=techs,
        waiting=waiting,
        blockers=db.blockers_for_site(site_id),
        attachments=db.attachments_for_site(site_id)[:24],
        messages=db.messages_for_site(site_id, limit=30),
        last_activity=db.last_activity(site_id),
    )


@app.post("/sites/{site_id}/complete")
async def complete_site(request: Request, site_id: str, user=Depends(require_user)):
    """The survey is done: technicians keep the channel but can only read it."""
    assert_site_visible(user, site_id)
    if not (db.get_site_channel(site_id) or db.get_site(site_id)):
        raise HTTPException(404, "Unknown site")
    db.set_site_progress(site_id, "done", user["name"], None)
    problems = await assign.set_site_mode(site_id, access.READ_ONLY)
    if problems:
        return _go(f"/sites/{site_id}", err="Marked complete, but Discord didn't take it for everyone "
                                            f"({'; '.join(problems[:3])}). Press 'Mark survey complete' again to retry.")
    return _go(f"/sites/{site_id}", msg="Survey complete. Technicians can still read the channel, but not post.")


@app.post("/sites/{site_id}/reopen")
async def reopen_site(request: Request, site_id: str, user=Depends(require_user)):
    assert_site_visible(user, site_id)
    if not (db.get_site_channel(site_id) or db.get_site(site_id)):
        raise HTTPException(404, "Unknown site")
    db.set_site_progress(site_id, "on_site", user["name"], "reopened")
    problems = await assign.set_site_mode(site_id, access.WRITABLE)
    if problems:
        return _go(f"/sites/{site_id}", err="Reopened, but Discord didn't take it for everyone "
                                            f"({'; '.join(problems[:3])}). Press Reopen again to retry.")
    return _go(f"/sites/{site_id}", msg="Reopened. Technicians can post in the channel again.")


@app.post("/sites/{site_id}/target-date")
async def set_target_date(request: Request, site_id: str, target_install_date: str = Form(""),
                          user=Depends(require_user)):
    assert_site_visible(user, site_id)
    if not db.get_site(site_id):
        raise HTTPException(404, "Unknown site")
    try:
        planned = _parse_day(target_install_date)
    except ValueError:
        return _go(f"/sites/{site_id}", err="That isn't a valid date.")
    db.set_target_install_date(site_id, planned)
    return _go(f"/sites/{site_id}", msg="Target date saved" if planned else "Target date cleared")


def _new_technician(company_id: int, name: str, phone: str, country: str, email: str, actor: str):
    """(technician, error). The same number, any spelling, is the same
    person: an existing technician of this company is reused, one of another
    company is refused."""
    if not name.strip():
        return None, "Give the technician a name."
    try:
        _e164, key, _region = normalize(phone, country)
    except ValueError as exc:
        return None, str(exc)
    if email.strip() and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        return None, "That email address doesn't look right."
    existing = db.find_technician_by_phone_key(key)
    if existing:
        if existing["company_id"] == company_id:
            return existing, None
        return None, "That number is already registered under another company. Ask HQ to sort it out."
    try:
        return db.get_technician(db.create_technician(company_id, name, phone, email, actor, country)), None
    except sqlite3.IntegrityError:          # someone added the same number a moment ago
        return db.find_technician_by_phone_key(key), None
    except ValueError as exc:
        return None, str(exc)


@app.post("/sites/{site_id}/assign")
async def assign_to_site(request: Request, background: BackgroundTasks, site_id: str,
                         technician_id: str = Form(""), name: str = Form(""), phone: str = Form(""),
                         country: str = Form("PK"), email: str = Form(""), user=Depends(require_manager)):
    """Pick someone from the roster, or type a new person in, and assign them.
    (The Sites page does the same for many sites at once.)"""
    assert_site_visible(user, site_id)
    inventory_row = db.get_site(site_id)
    if not inventory_row and not db.get_site_channel(site_id):
        raise HTTPException(404, "Unknown site")
    back = f"/sites/{site_id}"
    company_id = inventory_row["company_id"] if inventory_row else None
    if company_id is None:
        return _go(back, err="This site hasn't been allocated to a company yet — HQ does that on the Sites page.")

    if technician_id.strip():
        chosen = _int_or_none(technician_id)
        tech = assert_technician_visible(user, db.get_technician(chosen) if chosen else None)
        if tech["company_id"] != company_id:
            return _go(back, err="That technician belongs to a different company than this site.")
    else:
        if not name.strip() or not phone.strip():
            return _go(back, err="Pick a technician, or enter a name and WhatsApp number.")
        tech, problem = _new_technician(company_id, name, phone, country, email, user["name"])
        if problem:
            return _go(back, err=problem)

    try:
        outcome = assign.assign_sites(tech, [site_id], user["name"])
    except assign.AssignError as exc:
        return _go(back, err=str(exc))
    note = "" if outcome.connected or not outcome.queued else _send_link(
        background, tech, outcome.queued, user["name"], False, outcome.link)
    if outcome.connected and outcome.queued and tech.get("email"):
        _send_link(background, tech, outcome.queued, user["name"], True, outcome.link)
    return _go(back, msg=_assign_message(tech, outcome, note))


@app.post("/sites/{site_id}/revoke")
async def revoke_tech(request: Request, site_id: str, user_id: int = Form(...),
                      user=Depends(require_user)):
    assert_site_visible(user, site_id)
    site = db.get_site_channel(site_id)
    if not site:
        raise HTTPException(404, "Unknown site")
    try:
        await assign.revoke_site_access(site, user_id)
    except assign.AssignError as exc:
        return _go(f"/sites/{site_id}", err=str(exc))
    return _go(f"/sites/{site_id}", msg="Access revoked")


@app.post("/sites/{site_id}/cancel-queued")
async def cancel_queued(request: Request, site_id: str, queue_id: int = Form(...),
                        user=Depends(require_user)):
    assert_site_visible(user, site_id)
    row = db.get_queue_row(queue_id)
    # The row must belong to THIS site: it's the only thing tying it to
    # something the visibility check above actually covered.
    if not row or row["site_id"] != site_id:
        raise HTTPException(404, "Unknown assignment")
    db.cancel_queue_row(queue_id)
    return _go(f"/sites/{site_id}", msg=f"{site_id} is no longer assigned to {row['technician_name']}")


@app.get("/sites/{site_id}/technical")
async def site_technical(request: Request, site_id: str, user=Depends(require_hq)):
    site = db.get_site_channel(site_id)
    if not site and not db.get_site(site_id):
        raise HTTPException(404, "Unknown site")
    view = insights.technical_view(site_id)
    return render(request, "site_technical.html", site_id=site_id, site=site, view=view)


# --- technicians (a company's own roster) ------------------------------------
#
# Company managers only. HQ allocates sites to companies; each company adds
# its own technicians and gives them its sites. (HQ's in-house technicians
# are a company too, "HQ Tech", with its own manager login.)

@app.get("/technicians")
async def technicians_view(request: Request, user=Depends(require_manager)):
    techs = db.technicians_for_company(user["company_id"]) if user.get("company_id") is not None else []
    held, waiting = db.technician_load()
    for tech in techs:
        tech["sites_held"] = held.get(tech["discord_id"], 0) if tech["discord_id"] else 0
        tech["sites_waiting"] = waiting.get(tech["id"], 0)
        tech["discord"] = _discord_status(tech)
    return render(request, "technicians.html", technicians=techs)


@app.post("/technicians")
async def create_technician(request: Request, name: str = Form(...), phone: str = Form(...),
                            country: str = Form("PK"), email: str = Form(""), user=Depends(require_manager)):
    # Always the manager's own company, whatever the form says.
    chosen = user.get("company_id")
    if chosen is None:
        return _go("/technicians", err="Your account isn't linked to a company yet.")

    before = {t["id"] for t in db.technicians_for_company(chosen)}
    tech, problem = _new_technician(chosen, name, phone, country, email, user["name"])
    if problem:
        return _go("/technicians", err=problem)
    if tech["id"] in before:
        return _go(f"/technicians/{tech['id']}", msg=f"{tech['name']} is already on the roster.")
    return _go(f"/technicians/{tech['id']}", msg=f"Added {tech['name']}. Now give them their sites.")


@app.get("/technicians/{technician_id}")
async def technician_detail(request: Request, technician_id: int, user=Depends(require_manager)):
    tech = assert_technician_visible(user, db.get_technician(technician_id))
    status = _discord_status(tech)
    connected = status["state"] == "connected"
    held = db.assigned_sites_for_technician(tech["discord_id"]) if tech["discord_id"] else []
    for site in held:
        site["mode"] = db.site_mode(site["site_id"])
        site["open_url"] = f"https://discord.com/channels/{site['guild_id']}/{site['channel_id']}"
    queued = db.queue_for_technician(tech["id"])
    for row in queued:
        row["state"] = _queue_state(row, connected)

    taken = {s["site_id"] for s in held} | {r["site_id"] for r in queued}
    available = [r for r in insights.portfolio(company_id=tech["company_id"])
                 if r["state"] != "done" and r["site_id"] not in taken]
    by_city = {}
    for row in available:
        by_city.setdefault(row["city"] or "No city", []).append(row)

    link = status["link"]
    wanted = [r["site_id"] for r in queued] or [s["site_id"] for s in held]
    return render(
        request, "technician_detail.html",
        tech=tech,
        status=status,
        link_url=links.link_url(link["token"]) if link else None,
        whatsapp=_whatsapp_for(tech, wanted, link, connected),
        last_email=db.last_email(technician_id=tech["id"]),
        email_ready=CONFIG.email_ready(),
        held=held,
        queued=queued,
        available_by_city=sorted(by_city.items()),
        available_count=len(available),
    )


@app.post("/technicians/{technician_id}/assign-sites")
async def assign_sites(request: Request, background: BackgroundTasks, technician_id: int,
                       site_ids: list[str] = Form(default=[]), user=Depends(require_manager)):
    tech = assert_technician_visible(user, db.get_technician(technician_id))
    back = f"/technicians/{technician_id}"
    if not site_ids:
        return _go(back, err="Tick at least one site.")
    for site_id in site_ids:          # all of them, before any is touched: never a partial success
        assert_site_visible(user, site_id)
    try:
        outcome = assign.assign_sites(tech, site_ids, user["name"])
    except assign.AssignError as exc:
        return _go(back, err=str(exc))
    note = ""
    if outcome.queued:
        note = _send_link(background, tech, outcome.queued, user["name"], outcome.connected, outcome.link) \
            if (tech.get("email") or not outcome.connected) else ""
    return _go(back, msg=_assign_message(tech, outcome, note))


@app.post("/technicians/{technician_id}/send-link")
async def send_link(request: Request, background: BackgroundTasks, technician_id: int,
                    user=Depends(require_manager)):
    """Their link again — a fresh one if the old one expired before it was used."""
    tech = assert_technician_visible(user, db.get_technician(technician_id))
    back = f"/technicians/{technician_id}"
    link = db.ensure_link(technician_id=tech["id"], created_by=user["name"], days=CONFIG.join_link_days)
    status = _discord_status(tech)
    sites = [r["site_id"] for r in db.queue_for_technician(tech["id"])]
    if tech["discord_id"]:
        sites = sites or [s["site_id"] for s in db.assigned_sites_for_technician(tech["discord_id"])]
    if not tech.get("email"):
        return _go(back, msg="Their link is ready — send it with the WhatsApp button.")
    note = _send_link(background, tech, sites, user["name"], status["state"] == "connected", link)
    return _go(back, msg=note)


@app.post("/technicians/{technician_id}/unassign-site")
async def unassign_site(request: Request, technician_id: int, site_id: str = Form(...),
                        user=Depends(require_manager)):
    tech = assert_technician_visible(user, db.get_technician(technician_id))
    assert_site_visible(user, site_id)
    back = f"/technicians/{technician_id}"
    try:
        await assign.unassign(tech, site_id)
    except assign.AssignError as exc:
        return _go(back, err=str(exc))
    return _go(back, msg=f"{site_id} taken back from {tech['name']}")


@app.post("/technicians/{technician_id}/remove")
async def remove_technician(request: Request, technician_id: int, user=Depends(require_manager)):
    tech = assert_technician_visible(user, db.get_technician(technician_id))
    try:
        await assign.remove_technician(tech)
    except assign.AssignError as exc:
        return _go(f"/technicians/{technician_id}", err=str(exc))
    return _go("/technicians", msg=f"{tech['name']} removed from the roster")


# --- companies (HQ) -----------------------------------------------------

@app.get("/companies")
async def companies_view(request: Request, user=Depends(require_admin)):
    summaries, unassigned = insights.company_summaries(insights.portfolio())
    return render(request, "companies.html", companies=summaries, unassigned=unassigned,
                  regions=db.all_regions())


@app.post("/companies/regions")
async def add_region(request: Request, name: str = Form(...), user=Depends(require_admin)):
    name = " ".join(name.split())
    if not name or len(name) > 40:
        return _go("/companies", err="Give the region a name (up to 40 characters).")
    if not db.add_region(name, user["name"]):
        return _go("/companies", msg=f"{name} is already a region.")
    return _go("/companies", msg=f"Added region {name}. It's in the site template's dropdown from now on.")


@app.post("/companies")
async def create_company(request: Request, name: str = Form(...), is_internal: str = Form(""),
                         user=Depends(require_admin)):
    name = name.strip()
    if not name:
        return _go("/companies", err="Give the company a name.")
    try:
        db.create_company(name, is_internal=bool(is_internal), created_by=user["name"])
    except sqlite3.IntegrityError:
        return _go("/companies", err=f"There's already a company called {name}.")
    return _go("/companies", msg=f"Added {name}")


@app.post("/companies/{company_id}/remove")
async def remove_company(request: Request, company_id: int, user=Depends(require_admin)):
    """Only an empty company can go: its sites, technicians and logins have to
    be taken back first, so nothing is left pointing at a firm that's gone."""
    company = db.get_company(company_id)
    if not company:
        raise HTTPException(404, "Unknown company")
    still = db.remove_company(company_id)
    if still:
        held = ", ".join(f"{n} {word}{'' if n == 1 else 's'}" for word, n in still.items() if n)
        hint = " (a server: delete it in Discord, then Release it on the Discord page)" if still.get("Discord server") else ""
        return _go("/companies", err=f"{company['name']} still has {held} — take those back first{hint}.")
    return _go("/companies", msg=f"Removed {company['name']}")


# --- inventory (HQ) -----------------------------------------------------

@app.get("/inventory")
async def inventory_view(request: Request, q: str = "", city: str = "", region: str = "",
                         company: str = "", discord: str = "", user=Depends(require_hq)):
    """Each site's equipment: how many items its inventory lists, and how much
    of that has been photographed. A row opens the site's technical view.
    (Adding and allocating sites happens on the Sites page.)"""
    everything = db.all_sites()
    filters = {"q": q.strip(), "city": city.strip(), "region": region.strip(), "company": company.strip(),
               "discord": discord if discord in ("yes", "no") else ""}

    def wanted(s):
        if filters["q"] and filters["q"].lower() not in s["site_id"].lower():
            return False
        if filters["city"] and (s["city"] or "").lower() != filters["city"].lower():
            return False
        if filters["region"] and (s["region"] or "").lower() != filters["region"].lower():
            return False
        if filters["company"] == "none" and s["company_id"] is not None:
            return False
        if filters["company"] not in ("", "none") and str(s["company_id"]) != filters["company"]:
            return False
        if filters["discord"] and bool(s["provisioned"]) != (filters["discord"] == "yes"):
            return False
        return True

    return render(
        request, "inventory.html",
        sites=insights.inventory_overview([s for s in everything if wanted(s)]),
        total=len(everything),
        filters=filters,
        filtered=any(filters.values()),
        cities=sorted({s["city"] for s in everything if s["city"]}),
        regions=db.all_regions(),
        companies=db.all_companies(),
    )


# --- blockers ------------------------------------------------------------

@app.get("/blockers")
async def blockers_view(request: Request, site_id: str = None, city: str = None,
                        tech: str = None, hours: str = None, user=Depends(require_user)):
    scope = visible_site_ids(user)
    site_id = (site_id or "").strip().upper() or None
    city = (city or "").strip() or None
    tech = (tech or "").strip() or None
    hours = _int_or_none(hours)

    rows = db.open_blockers(site_id)
    if scope is not None:
        rows = [r for r in rows if r["site_id"] in scope]

    site_city = {s["site_id"]: s.get("city") for s in db.all_site_channels()}
    if city:
        rows = [r for r in rows if site_city.get(r["site_id"]) == city]
    if tech:
        rows = [r for r in rows if tech.lower() in (r["author_name"] or "").lower()]
    if hours:
        cutoff = time.time() - hours * 3600
        rows = [r for r in rows if r["flagged_at"] >= cutoff]

    for r in rows:
        r["city"] = site_city.get(r["site_id"])

    visible_cities = {c for sid, c in site_city.items() if c and (scope is None or sid in scope)}
    return render(
        request,
        "blockers.html",
        blockers=rows,
        filters={"site_id": site_id, "city": city, "tech": tech, "hours": hours},
        cities=sorted(visible_cities),
    )


@app.post("/blockers/{blocker_id}/{action}")
async def blocker_action(request: Request, blocker_id: int, action: str,
                         user=Depends(require_user)):
    if action not in ("resolve", "dismiss"):
        raise HTTPException(400, "Unknown action")
    blocker = db.get_blocker(blocker_id)
    if not blocker:
        raise HTTPException(404, "Unknown blocker")
    assert_site_visible(user, blocker["site_id"])
    status = "resolved" if action == "resolve" else "dismissed"
    db.resolve_blocker(blocker_id, user["name"], status)
    return RedirectResponse(_referer_path(request, "/blockers"), status_code=303)


# --- media ---------------------------------------------------------------

@app.post("/media/{attachment_id}/tag")
async def tag_attachment(request: Request, attachment_id: int, tag: str = Form(...),
                         user=Depends(require_user)):
    # Everything about the file comes from the database row, never from the
    # form: taking message_id or site_id from the client would let someone
    # who can see their own attachment re-tag any other company's.
    row = db.get_attachment(attachment_id)
    if not row:
        raise HTTPException(404, "Unknown attachment")
    assert_site_visible(user, row["site_id"])
    if tag not in TAG_LABELS:
        raise HTTPException(400, "Unknown tag")
    rename_tagged_files(row["message_id"], tag)
    return _go(f"/sites/{row['site_id']}", msg=f"Tagged as {TAG_LABELS[tag]}")


@app.get("/media/file/{attachment_id}")
async def media_file(request: Request, attachment_id: int, user=Depends(require_user)):
    row = db.get_attachment(attachment_id)
    if not row:
        raise HTTPException(404, "Unknown attachment")
    assert_site_visible(user, row["site_id"])
    path = Path(row["path"])
    # Only ever serve what the archive itself recorded, resolved against the
    # storage root, so a doctored DB row can't walk out of the directory.
    root = Path(CONFIG.storage_root).resolve()
    resolved = path.resolve()
    if root not in resolved.parents or not resolved.exists():
        raise HTTPException(404, "File is no longer on disk")
    return FileResponse(resolved, filename=row["filename"])


# --- Discord servers and HQ staff (HQ admin) ----------------------------

def _bot_add_link(slot: dict) -> str:
    return f"/discord/add-bot?{urlencode({'company_id': slot['company_id'], 'region': slot['region'], 'seq': slot['seq']})}"


@app.get("/discord")
async def discord_view(request: Request, user=Depends(require_admin)):
    view = servers.overview()
    for slot in view["needed"]:
        slot["add_bot"] = _bot_add_link(slot)
    for person in view["staff"]:
        link = db.current_link(staff_id=person["id"])
        tokens = db.get_tokens(person["discord_id"]) if person["discord_id"] else None
        person["connected"] = bool(tokens and not tokens["dead_at"])
        person["username"] = tokens["username"] if tokens else None
        person["link_url"] = links.link_url(link["token"]) if link else None
        person["link_expired"] = bool(link and not link["used_at"] and link["expires_at"] <= time.time())
        person["phone_text"] = phone_display(person["phone"], person["country"]) if person["phone"] else None
        person["whatsapp"] = (links.whatsapp_url(person["phone"], links.whatsapp_text(
            person, "HQ", [], link["token"], False)) if link and person["phone"] and not person["connected"] else None)
    return render(request, "discord.html", **view,
                  login_ready=CONFIG.discord_login_ready(), email_ready=CONFIG.email_ready(),
                  sites_per_server=CONFIG.sites_per_server)


@app.get("/discord/add-bot")
async def add_bot(request: Request, company_id: int, region: str, seq: int, user=Depends(require_admin)):
    """Sends the admin to Discord to add the bot to the server they've just
    created for this company and region. What comes back (the callback) says
    which server it was — that, not the server's name, is what claims it."""
    if not CONFIG.discord_login_ready():
        return _go("/discord", err="Discord sign-in isn't set up on the server yet (the app's client id and secret).")
    company = db.get_company(company_id)
    canonical = {r.lower(): r for r in db.all_regions()}.get(region.strip().lower())
    if not company or not canonical or not 1 <= seq <= 50:
        raise HTTPException(404, "Unknown server slot")
    state = _signer.dumps({"kind": "bot", "company_id": company_id, "region": canonical, "seq": seq,
                           "uid": user["id"], "nonce": secrets.token_hex(8)})
    return RedirectResponse(oauth.bot_authorize_url(state), status_code=302)


@app.post("/discord/guilds/{guild_id}/confirm")
async def confirm_guild(request: Request, guild_id: int, user=Depends(require_admin)):
    row = db.get_guild(guild_id)
    if not row or row["status"] != "review":
        raise HTTPException(404, "No server waiting for a check")
    db.set_guild_status(guild_id, "confirmed")
    return _go("/discord", msg=f"Setting up {row['name']} now.")


@app.post("/discord/guilds/{guild_id}/leave")
async def leave_guild(request: Request, guild_id: int, user=Depends(require_admin)):
    """The bot leaves a server it was added to by mistake. Never a server
    that's in use: that's the Release step, after it's gone."""
    row = db.get_guild(guild_id)
    if not row or row["status"] not in ("unrecognised", "review"):
        raise HTTPException(404, "Not a server the bot can leave from here")
    try:
        await discord_api.leave_guild(guild_id)
    except discord_api.DiscordError as exc:
        if exc.status is None:
            return _go("/discord", err="Discord didn't answer. Try again in a minute.")
        if exc.status != 404:
            return _go("/discord", err=f"Discord refused: {exc.status}.")
    db.mark_guild_left(guild_id)
    return _go("/discord", msg=f"The bot has left {row['name']}.")


@app.post("/discord/guilds/{guild_id}/release")
async def release_guild(request: Request, guild_id: int, user=Depends(require_admin)):
    released = db.release_guild(guild_id, user["name"])
    if released is None:
        raise HTTPException(404, "Only a server the bot has left can be released")
    return _go("/discord", msg=f"{released} site(s) are waiting for a new server; their technicians stay assigned.")


@app.post("/discord/staff")
async def add_staff(request: Request, background: BackgroundTasks, name: str = Form(...),
                    discord_role: str = Form(...), email: str = Form(""), phone: str = Form(""),
                    country: str = Form("PK"), user=Depends(require_admin)):
    if discord_role not in db.STAFF_ROLES:
        raise HTTPException(400, "Unknown role")
    if not name.strip():
        return _go("/discord", err="Give them a name.")
    if email.strip() and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        return _go("/discord", err="That email address doesn't look right.")
    e164 = key = region = None
    if phone.strip():
        try:
            e164, key, region = normalize(phone, country)
        except ValueError as exc:
            return _go("/discord", err=str(exc))
    staff_id = db.create_staff(name, discord_role, email=email, phone=e164, phone_key=key, country=region,
                               created_by=user["name"])
    return _go("/discord", msg=_send_staff_link(background, db.get_staff(staff_id), user["name"]))


def _send_staff_link(background: BackgroundTasks, person: dict, actor: str, fresh: bool = False) -> str:
    link = db.ensure_link(staff_id=person["id"], created_by=actor, days=CONFIG.join_link_days, fresh=fresh)
    if person.get("email"):
        subject, body = links.staff_email(person, link["token"])
        if links.queue_email(background, person["email"], subject, body, actor, staff_id=person["id"]):
            return f"{person['name']}'s link is on its way to {person['email']}."
    return f"{person['name']}'s link is ready: copy it or send it on WhatsApp from the list."


@app.post("/discord/staff/{staff_id}/send-link")
async def resend_staff_link(request: Request, background: BackgroundTasks, staff_id: int,
                            user=Depends(require_admin)):
    person = db.get_staff(staff_id)
    if not person or person["removed_at"]:
        raise HTTPException(404, "Unknown staff member")
    return _go("/discord", msg=_send_staff_link(background, person, user["name"]))


@app.post("/discord/staff/{staff_id}/remove")
async def remove_staff(request: Request, staff_id: int, user=Depends(require_admin)):
    person = db.get_staff(staff_id)
    if not person or person["removed_at"]:
        raise HTTPException(404, "Unknown staff member")
    db.remove_staff(staff_id)
    db.revoke_links(staff_id=staff_id)
    return _go("/discord", msg=f"{person['name']} removed. The bot takes their role away in every server.")


@app.post("/discord/staff/me")
async def connect_my_discord(request: Request, user=Depends(require_admin)):
    """The admin connects their own Discord (as Management) in one go, and comes
    back to this page afterwards — not to the public link page technicians see."""
    if not CONFIG.discord_login_ready():
        return _go("/discord", err="Discord sign-in isn't set up on the server yet.")
    person = db.staff_by_email(user["email"])
    if person is None:
        person = db.get_staff(db.create_staff(user["name"], CONFIG.management_role_name, email=user["email"],
                                              created_by=user["name"]))
    link = db.ensure_link(staff_id=person["id"], created_by=user["name"], days=CONFIG.join_link_days)
    state = _signer.dumps({"kind": "join", "link_id": link["id"], "nonce": secrets.token_hex(8),
                           "back": "discord", "uid": user["id"]})
    return RedirectResponse(oauth.user_authorize_url(state), status_code=303)


# --- a person's link (public: the token is the key) ------------------------

def _link_holder(token: str):
    """(link, person, kind) for a link that still works, else (None, None, None)."""
    link = db.get_link(token)
    if not link or link["revoked_at"]:
        return None, None, None
    if link["technician_id"] is not None:
        person, kind = db.get_technician(link["technician_id"]), "technician"
    else:
        person, kind = db.get_staff(link["staff_id"]), "staff"
    if not person or person["removed_at"]:
        return None, None, None
    return link, person, kind


def _my_sites(person: dict, join_guilds=frozenset()) -> list[dict]:
    """A technician's sites as their page shows them."""
    out = []
    if person["discord_id"]:
        for held in db.assigned_sites_for_technician(person["discord_id"]):
            out.append({"site_id": held["site_id"], "city": held["city"],
                        "state": "read_only" if db.site_mode(held["site_id"]) == access.READ_ONLY else "open",
                        "url": f"https://discord.com/channels/{held['guild_id']}/{held['channel_id']}"})
    for row in db.queue_for_technician(person["id"]):
        if not row["channel_id"]:
            state = "waiting_server"
        elif row["guild_id"] in join_guilds:
            state = "join"
        elif row["error"] == "needs_connect":
            state = "reconnect"
        else:
            state = "opening"
        out.append({"site_id": row["site_id"], "city": row["city"], "state": state, "url": None})
    return out


INVITE_NOTES = {
    "ambiguous": "Two people joined at the same moment, and Discord can't tell us which one was you. "
                 "Finish in the browser instead — once.",
    "vanished": "Discord didn't tell us which account used your invite. Finish in the browser instead — once.",
    "other": "Someone joined with a different Discord account from the one you used before. Open Discord "
             "signed in as your own account, then tap Open Discord again.",
    "taken": "The Discord account that joined is already connected for someone else. Open Discord signed in "
             "as your own account, then tap Open Discord again.",
}


def _my_invites(person: dict) -> tuple[list[dict], str | None, str | None]:
    """A technician's Open Discord buttons (one per server they have to join
    themselves), why an earlier try didn't work, and the account they joined
    with (None if they never joined by invite)."""
    now = time.time()
    waiting = {r["guild_id"] for r in db.queue_for_technician(person["id"]) if r["channel_id"]}
    joins, note, joined_as, shown = [], None, None, set()
    for invite in db.invites_for_technician(person["id"]):         # newest first
        if invite["outcome"] == "joined":
            if joined_as is None:
                joined_as = invite["joined_name"] or ""
            continue
        if invite["guild_id"] not in waiting:
            continue
        if invite["closed_at"] is None:
            if invite["expires_at"] > now and not invite["missing_at"] and invite["guild_id"] not in shown:
                shown.add(invite["guild_id"])
                joins.append({"guild_id": invite["guild_id"], "name": invite["guild_name"],
                              "url": f"https://discord.gg/{invite['code']}"})
        elif note is None:
            reason = (invite["outcome"] or "").split(":")[-1]
            if reason in ("other", "taken") or (reason in BROWSER_NOTES and not person["discord_id"]):
                note = reason
    return joins, note, joined_as


# After these only Connect in the browser can say who they are (they're in the
# server already, so a new invite would do nothing).
BROWSER_NOTES = ("ambiguous", "vanished")


def _my_servers(person: dict) -> list[dict]:
    placed = {r["guild_id"] for r in db.guild_staff_rows() if r["staff_id"] == person["id"] and r["ok"]}
    return [{"name": g["name"], "url": f"https://discord.com/channels/{g['guild_id']}"}
            for g in db.all_guilds(("ready",)) if g["guild_id"] in placed]


def _gone(request: Request):
    return render_public(request, "join.html", status_code=404, gone=True)


@app.get("/j/{token}")
async def my_link(request: Request, token: str):
    link, person, kind = _link_holder(token)
    if not link:
        return _gone(request)
    tokens = db.get_tokens(person["discord_id"]) if person["discord_id"] else None
    # Signed in: the bot can put them into any server itself. A technician
    # we know the account of is connected either way: a server they aren't in
    # yet gets them an invite (their Open Discord button).
    signed_in = bool(link["used_at"] and tokens and not tokens["dead_at"])
    joins, note, joined_as = _my_invites(person) if kind == "technician" else ([], None, None)
    connected = signed_in or (kind == "technician" and bool(link["used_at"] and person["discord_id"]))
    expired = not link["used_at"] and link["expires_at"] <= time.time()
    company = person.get("company_name") if kind == "technician" else "HQ"
    sites = _my_sites(person, {j["guild_id"] for j in joins}) if kind == "technician" else []
    states = {s["state"] for s in sites}
    preparing = not joins and not note and bool(states & {"opening", "reconnect"})
    refresh = None              # the page checks back by itself while something is on its way
    if kind == "technician":
        if preparing:
            refresh = 5
        elif joins:
            refresh = 15
        elif "waiting_server" in states:
            refresh = 60
    return render_public(
        request, "join.html",
        token=token, kind=kind, first_name=links.first_name(person["name"]), company=company,
        connected=connected, signed_in=signed_in, bound=bool(link["used_at"]), expired=expired,
        username=tokens["username"] if tokens else joined_as,
        sites=sites, joins=joins, note=INVITE_NOTES.get(note), ambiguous=note in BROWSER_NOTES,
        preparing=preparing, refresh=refresh,
        servers=_my_servers(person) if kind == "staff" and connected else [],
        role=person.get("discord_role"),
    )


@app.get("/j/{token}/go")
async def my_link_go(request: Request, token: str):
    """Off to Discord to connect. The note that comes back names the link by
    its number only; the token itself never leaves this site."""
    link, person, _kind = _link_holder(token)
    if not link:
        return _gone(request)
    if not link["used_at"] and link["expires_at"] <= time.time():
        return RedirectResponse(f"/j/{token}", status_code=303)
    if not CONFIG.discord_login_ready():
        return render_public(request, "join.html", status_code=503, token=token, problem="not_ready")
    state = _signer.dumps({"kind": "join", "link_id": link["id"], "nonce": secrets.token_hex(8)})
    return RedirectResponse(oauth.user_authorize_url(state), status_code=302)


REFUSALS = {
    "gone": "This link isn't valid any more. Ask for a new one.",
    "expired": "This link has expired. Ask for a new one.",
    "other": "This link has already been used with a different Discord account. Sign in to Discord as that "
             "account, or ask for help.",
    "taken": "That Discord account is already connected for someone else. Use your own account.",
}


@app.get("/oauth/discord/callback")
async def oauth_callback(request: Request, code: str = None, state: str = None, error: str = None,
                         guild_id: str = None):
    try:
        note = _signer.loads(state or "", max_age=STATE_MAX_AGE)
    except BadData:
        # A forged, garbled or expired note. Nothing is asked of Discord for
        # an answer we didn't start.
        return render_public(request, "join.html", status_code=400, problem="stale")
    if note.get("kind") == "bot":
        return await _bot_added(request, note, code, error, guild_id)
    if note.get("kind") != "join":
        return render_public(request, "join.html", status_code=400, problem="stale")

    link = db.get_link_by_id(note.get("link_id"))
    if not link or link["revoked_at"]:
        return _gone(request)
    home = _back_to_discord_tab(request, note)
    again = " Press 'Connect my own Discord' to try again."
    if error or not code:
        if home:
            return _go("/discord", err="Discord wasn't connected (cancelled in Discord). Nothing changed.")
        return render_public(request, "join.html", token=link["token"], problem="cancelled")
    try:
        tokens = await oauth.exchange_code(code)
        if not set(oauth.USER_SCOPES) <= set((tokens.get("scope") or "").split()):
            if home:
                return _go("/discord", err="Discord didn't grant everything needed." + again)
            return render_public(request, "join.html", token=link["token"], problem="scopes")
        me = await oauth.get_me(tokens["access_token"])
    except oauth.OAuthError:
        if home:
            return _go("/discord", err="Discord didn't finish connecting." + again)
        return render_public(request, "join.html", token=link["token"], problem="discord")
    refusal = db.bind_link(link["id"], int(me["id"]), me.get("username"), tokens["access_token"],
                           tokens["refresh_token"], tokens.get("expires_in", 604800), tokens.get("scope") or "")
    if refusal:
        if home:
            return _go("/discord", err=REFUSALS[refusal])
        return render_public(request, "join.html", status_code=409, token=link["token"],
                             problem="refused", refusal=REFUSALS[refusal])
    if home:
        role = (db.get_staff(link["staff_id"]) or {}).get("discord_role") or CONFIG.management_role_name
        return _go("/discord", msg=f"Your Discord (@{me.get('username')}) is connected as {role}. "
                                   "The bot puts you in every site server within a minute.")
    return RedirectResponse(f"/j/{link['token']}", status_code=303)


def _back_to_discord_tab(request: Request, note: dict) -> bool:
    """An admin who connected their own Discord from the Discord tab goes back
    there. Only the admin who started it, still signed in: the destination is
    fixed and comes from the signed note, never from the URL."""
    if note.get("back") != "discord":
        return False
    user = current_user(request)
    return bool(user and user["role"] == "hq_admin" and user["id"] == note.get("uid"))


async def _bot_added(request: Request, note: dict, code, error, guild_id):
    """Discord's answer after an HQ admin added the bot to a new server."""
    user = current_user(request)
    if not user or user["role"] != "hq_admin" or user["id"] != note.get("uid"):
        return RedirectResponse("/login", status_code=303)
    back = "/discord"
    if error or not code:
        return _go(back, err="The bot wasn't added (cancelled in Discord). Nothing changed.")
    company = db.get_company(note.get("company_id"))
    target = _int_or_none(guild_id)
    if not company or target is None:
        return _go(back, err="Discord didn't say which server it was. Add the bot again.")
    name = db.server_name(company["name"], note["region"], note["seq"])
    existing = db.get_guild(target)
    if existing and existing["status"] not in ("unrecognised", "left"):
        return _go(back, err=f"That server is already {existing['name']}. Create a new server for {name} and "
                             "pick that one when you add the bot.")
    if db.guild_in_slot(company["id"], note["region"], note["seq"]):
        return _go(back, err=f"{name} already has a server.")
    try:
        answer = await oauth.exchange_code(code)
    except oauth.OAuthError as exc:
        return _go(back, err=f"Discord didn't finish adding the bot ({exc}). Try again.")
    joined = answer.get("guild") or {}
    if str(joined.get("id")) != str(target):
        return _go(back, err="Discord added the bot to a different server than it said. Nothing was set up — "
                             "check the server list below.")
    if not db.claim_guild(target, joined.get("name") or name, company["id"], note["region"], note["seq"], user["name"]):
        return _go(back, err="That server (or that slot) was claimed a moment ago. Refresh and check.")
    return _go(back, msg=f"The bot is in. {name} is being set up — its channels appear within a few minutes.")


# --- reconcile -----------------------------------------------------------

async def _drift(sites):
    """Where Discord and the portal disagree, per site: someone recorded but
    without access, access nobody recorded, or someone whose access is in the
    wrong mode (posting on a completed site, or read-only on an open one).
    Returns (drift, unchecked). A channel Discord didn't answer for can't be
    compared, so it goes in `unchecked` ("ISB1: no answer"), never in "matches"."""
    out, unchecked = [], []
    for site in sites:
        try:
            live = await discord_api.channel_member_overwrites(site["channel_id"])
        except discord_api.DiscordError as exc:
            unchecked.append(f"{site['site_id']}: {exc.status or 'no answer'}")
            continue
        seeing = {uid: access.mode_of(*bits) for uid, bits in live.items() if access.mode_of(*bits)}
        recorded = {t["user_id"] for t in db.techs_for_site(site["site_id"])}
        wanted = db.site_mode(site["site_id"])
        missing = recorded - set(seeing)          # we think they have access, Discord disagrees
        unexpected = set(seeing) - recorded       # access in Discord we have no record of
        wrong_mode = {uid for uid in recorded & set(seeing) if seeing[uid] != wanted}
        if missing or unexpected or wrong_mode:
            out.append({"site_id": site["site_id"], "channel_id": site["channel_id"], "mode": wanted,
                        "missing": sorted(missing), "unexpected": sorted(unexpected),
                        "wrong_mode": sorted(wrong_mode)})
    return out, unchecked


@app.get("/reconcile")
async def reconcile_view(request: Request, user=Depends(require_admin)):
    """site_techs is a mirror of the Discord channel overwrites that
    actually control visibility. Nothing keeps them in step by itself, so
    this shows where they've drifted."""
    sites = db.all_site_channels()
    drift, unchecked = await _drift(sites)
    return render(request, "reconcile.html", drift=drift, unchecked=unchecked, checked=len(sites) - len(unchecked))


@app.post("/reconcile/apply")
async def reconcile_apply(request: Request, user=Depends(require_admin)):
    """Makes Discord match the portal: grant what we recorded (in the site's
    mode), remove what we didn't. What Discord refuses or doesn't answer is
    reported; pressing again retries."""
    drift, unchecked = await _drift(db.all_site_channels())
    fixed, refused = 0, []
    for d in drift:
        for user_id in d["missing"] + d["wrong_mode"]:
            try:
                await discord_api.set_overwrite(d["channel_id"], user_id, d["mode"], "Reconcile: matching the portal")
                fixed += 1
            except discord_api.DiscordError as exc:
                refused.append(f"{d['site_id']} {user_id}: {exc.status or 'no answer'}")
        for user_id in d["unexpected"]:
            try:
                await discord_api.revoke_channel_access(d["channel_id"], user_id, "Reconcile: no portal record")
                fixed += 1
            except discord_api.DiscordError as exc:
                refused.append(f"{d['site_id']} {user_id}: {exc.status or 'no answer'}")
    done = f"{fixed} overwrite(s) corrected"
    if refused:
        done += f"; Discord didn't take {len(refused)} ({'; '.join(refused[:3])})"
    if unchecked:
        done += f"; {len(unchecked)} channel(s) couldn't be checked"
    if refused or unchecked:
        return _go("/reconcile", err=f"{done}. Press it again to retry.")
    return _go("/reconcile", msg=done)


# --- hermes rules (HQ admin) --------------------------------------------

@app.get("/hermes/rules")
async def hermes_rules(request: Request, user=Depends(require_admin)):
    rules = db.list_hermes_rules()
    grouped = {kind: [r for r in rules if r["kind"] == kind] for kind in db.RULE_KINDS}
    return render(request, "hermes_rules.html", grouped=grouped, kinds=db.RULE_KINDS)


@app.post("/hermes/rules")
async def add_hermes_rule(request: Request, kind: str = Form(...), value: str = Form(...),
                          user=Depends(require_admin)):
    if kind not in db.RULE_KINDS:
        raise HTTPException(400, "Unknown rule type")
    if kind == "pattern":
        value = value.strip()
        try:
            re.compile(value)
        except re.error as exc:
            return _go("/hermes/rules", err=f"That isn't a valid pattern: {exc}")
    else:
        value = " ".join(value.lower().split())
    if not value:
        return _go("/hermes/rules", err="Type the phrase to add.")

    changed = db.add_hermes_rule(kind, value, user["name"])
    blocker_rules.reset_cache()
    if not changed:
        return _go("/hermes/rules", msg="That rule is already on.")
    return _go("/hermes/rules", msg="Rule added — the bot picks it up within a minute.")


@app.post("/hermes/rules/{rule_id}/toggle")
async def toggle_hermes_rule(request: Request, rule_id: int, user=Depends(require_admin)):
    rule = next((r for r in db.list_hermes_rules() if r["id"] == rule_id), None)
    if not rule:
        raise HTTPException(404, "Unknown rule")
    db.set_hermes_rule_active(rule_id, not rule["active"])
    blocker_rules.reset_cache()
    return _go("/hermes/rules", msg="Rule switched off." if rule["active"] else "Rule switched on.")


# --- portal users --------------------------------------------------------

@app.get("/users")
async def users_view(request: Request, user=Depends(require_admin)):
    return render(request, "users.html", users=db.all_portal_users(), roles=ROLES,
                  companies=db.all_companies())


@app.post("/users")
async def create_user(request: Request, email: str = Form(...), name: str = Form(...),
                      password: str = Form(...), role: str = Form("hq_staff"),
                      company_id: str = Form(""), user=Depends(require_admin)):
    if role not in ROLES:
        raise HTTPException(400, "Unknown role")
    if len(password) < 8:
        return RedirectResponse("/users?err=Use+at+least+8+characters+for+the+temporary+password", status_code=303)

    # A company manager has to belong to a company; the HQ roles must not,
    # whatever the form says — company_id is what scopes what a login can see.
    if role == "company_manager":
        chosen = _int_or_none(company_id)
        if chosen is None or not db.get_company(chosen):
            return _go("/users", err="A company manager needs a company.")
    else:
        chosen = None

    if db.get_portal_user_by_email(email):
        return RedirectResponse("/users?err=That+email+already+exists", status_code=303)
    db.create_portal_user(email, hash_password(password), name, role, company_id=chosen)
    return _go("/users", msg=f"Created {name}")


@app.post("/users/{user_id}/delete")
async def remove_user(request: Request, user_id: int, user=Depends(require_admin)):
    if user_id == user["id"]:
        return RedirectResponse("/users?err=You+can%27t+delete+your+own+account", status_code=303)
    db.delete_portal_user(user_id)
    return RedirectResponse("/users?msg=Account+removed", status_code=303)


@app.on_event("startup")
async def _startup():
    db.init_db()
    if not CONFIG.portal_secret_key:
        print("[portal] warning: PORTAL_SECRET_KEY is unset — sessions reset on restart")
