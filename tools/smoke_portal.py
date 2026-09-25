"""Renders every GET page in the portal, as each kind of user, and reports any
that fail — against whatever database it's pointed at, real data included.

Written after a dashboard shipped broken: the deploy check only confirmed
that protected routes redirect to /login, which they did, so a template bug
one line into the authenticated page went unnoticed until someone signed in.
Checking status codes on logged-out routes proves almost nothing.

It now runs the whole sweep three times — as an HQ admin, HQ staff, and a
company manager — because a scoping bug doesn't crash a page, it renders a
perfectly healthy one with someone else's data on it. A "does it throw" check
cannot see that, so each company identity is also asked for a site that
belongs to a different company and must get a 404.

It signs in by overriding the auth dependencies rather than by posting a
password, so it needs no credentials and can run on the VM after a deploy.
(tools/test_portal.py is the deeper suite, on a throwaway database.)

    .venv/bin/python -m tools.smoke_portal
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from portal import auth, main
from storage import db

STYLE_CSS = Path(main.BASE_DIR) / "static" / "style.css"

# --- alignment ---------------------------------------------------------------
#
# A number under a heading has to line up with it. The page that prompted this
# had every count right-aligned under a left-aligned heading, because the
# stylesheet right-aligned td.num and nothing else. Two checks catch that
# class of fault: every table's headers and cells agree column by column, and
# the stylesheet really right-aligns a numeric header.

ALIGN_CLASSES = {"num"}


class _Tables(HTMLParser):
    """Collects each table's rows as lists of (tag, classes, colspan)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._open, self._row = [], [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            table = []
            self.tables.append(table)
            self._open.append(table)
        elif tag == "tr" and self._open:
            self._row = []
            self._open[-1].append(self._row)
        elif tag in ("th", "td") and self._row is not None:
            span = a.get("colspan") or "1"
            self._row.append((tag, set((a.get("class") or "").split()), int(span) if span.isdigit() else 1))

    def handle_endtag(self, tag):
        if tag == "tr":
            self._row = None
        elif tag == "table" and self._open:
            self._open.pop()


def misaligned_columns(html: str) -> list[str]:
    """One line per table column whose cells aren't aligned like its header."""
    parser = _Tables()
    parser.feed(html)
    problems = set()
    for number, rows in enumerate(parser.tables, start=1):
        header = next((r for r in rows if r and all(tag == "th" for tag, _, _ in r)), None)
        if header is None:
            continue

        def columns(row):
            out = []
            for _tag, classes, span in row:
                out.extend([frozenset(classes & ALIGN_CLASSES)] * span)
            return out

        want = columns(header)
        for row in rows:
            if row is header or not row or any(tag == "th" for tag, _, _ in row):
                continue
            if len(row) == 1 and row[0][2] > 1:     # a full-width message row
                continue
            for column, (head, cell) in enumerate(zip(want, columns(row)), start=1):
                if head != cell:
                    problems.add(f"table {number}, column {column}: heading "
                                 f"{'/'.join(sorted(head)) or 'left'}, cells {'/'.join(sorted(cell)) or 'left'}")
    return sorted(problems)


def css_right_aligns_numeric_headers(css: str) -> bool:
    """Whether some rule covering `th.num` sets text-align: right."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        names = {s.strip() for s in selectors.split(",")}
        if "th.num" in names and re.search(r"text-align\s*:\s*right", body):
            return True
    return False

# Pages only an admin, or only HQ, may open. A manager must be refused (403),
# never served — and never a 404 either, which would mean the page is missing.
ADMIN_ONLY = ("/inventory", "/companies", "/discord", "/hermes/rules", "/users", "/reconcile",
              "/sites/template.xlsx")


def identity(role: str, company_id=None) -> dict:
    return {
        "id": 0, "email": f"{role}@smoke", "name": f"Smoke {role}", "role": role,
        "company_id": company_id,
        # Templates and the forced-change gate read these off the user.
        "must_change_password": 0, "password_hash": "",
    }


def install(user: dict) -> None:
    """Makes every request act as `user`, with the real role rules: the admin
    and HQ dependencies still refuse anyone who isn't one."""
    def as_user():
        return user

    def as_admin():
        if user["role"] != "hq_admin":
            raise HTTPException(status_code=403, detail="Admins only")
        return user

    def as_hq():
        if user["role"] not in auth.HQ_ROLES:
            raise HTTPException(status_code=403, detail="HQ staff only")
        return user

    main.current_user = lambda request: user
    main.app.dependency_overrides[auth.require_user] = as_user
    main.app.dependency_overrides[auth.require_admin] = as_admin
    main.app.dependency_overrides[auth.require_hq] = as_hq


# Not pages to sweep: a person's link (/j/<token>) and Discord's sign-in
# answer are opened from outside with a key the sweep doesn't have, and
# add-bot sends the browser off to Discord.
NOT_PAGES = ("/j/", "/oauth/", "/discord/add-bot")


def fill(path: str, scope, company_id) -> str | None:
    """Puts real ids into a parameterised path, from what this user may see.
    None means there's nothing to point it at (skip, and say so)."""
    if path.startswith(NOT_PAGES):
        return None
    if "{site_id}" in path:
        mine = sorted(scope) if scope is not None else [s["site_id"] for s in db.all_site_channels()]
        provisioned = {s["site_id"] for s in db.all_site_channels()}
        mine = [s for s in mine if s in provisioned]
        return path.replace("{site_id}", mine[0]) if mine else None
    if "{technician_id}" in path:
        techs = db.technicians_for_company(company_id) if company_id else db.all_technicians()
        return path.replace("{technician_id}", str(techs[0]["id"])) if techs else None
    if any(p in path for p in ("{blocker_id}", "{attachment_id}", "{user_id}", "{rule_id}", "{action}")):
        return None     # these are POST-only targets or need a specific row
    return path


def main_():
    companies = db.all_companies()
    with_sites = [c for c in companies if db.site_ids_for_company(c["id"])]

    identities = [("HQ admin", identity("hq_admin")), ("HQ staff", identity("hq_staff"))]
    if with_sites:
        identities.append((f"company manager ({with_sites[0]['name']})",
                           identity("company_manager", with_sites[0]["id"])))
    else:
        print("(no company owns any site yet, so there's no company-manager pass)")

    failures, total = [], 0
    for label, user in identities:
        install(user)
        scope = auth.visible_site_ids(user)
        print(f"\nas {label}")

        targets = []
        for route in main.app.routes:
            if "GET" not in getattr(route, "methods", set()) or route.path.startswith("/static"):
                continue
            filled = fill(route.path, scope, user.get("company_id"))
            if filled is None:
                print(f"  skip  {route.path}   (nothing to point it at)")
            else:
                targets.append(filled)

        with TestClient(main.app, raise_server_exceptions=False) as client:
            for path in sorted(set(targets)):
                r = client.get(path, follow_redirects=False)
                refused = r.status_code == 403
                # 403 is the right answer for a page this role may not open;
                # anything else >= 400 is a page that's broken.
                ok = r.status_code < 400 or refused
                total += 1
                print(f"  {r.status_code}  {path}" + ("" if ok else "   <-- FAILED"))
                if not ok:
                    failures.append((label, path, r.status_code))
                if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                    for problem in misaligned_columns(r.text):
                        print(f"        ^ misaligned: {problem}")
                        failures.append((label, f"{path} misaligned: {problem}", r.status_code))
                if user["role"] == "company_manager":
                    must_refuse = path in ADMIN_ONLY or path.endswith("/technical")
                    if must_refuse and not refused:
                        failures.append((label, f"{path} served to a company manager", r.status_code))
                        print(f"        ^ a company manager must be refused this, got {r.status_code}")

            if user["role"] == "company_manager":
                # The check a crash-detector can't do: someone else's site must not exist for them.
                # "Theirs" comes straight from the database, NOT from the scoping function
                # under test — otherwise a broken function would quietly hide its own leak.
                theirs = db.site_ids_for_company(user["company_id"])
                others = [s["site_id"] for s in db.all_site_channels() if s["site_id"] not in theirs]
                if others:
                    total += 1
                    r = client.get(f"/sites/{others[0]}", follow_redirects=False)
                    ok = r.status_code == 404
                    print(f"  {r.status_code}  /sites/{others[0]}   (another company's site: must be 404)"
                          + ("" if ok else "   <-- LEAK"))
                    if not ok:
                        failures.append((label, f"/sites/{others[0]} reachable across companies", r.status_code))
                else:
                    print("  skip  cross-company check   (every provisioned site belongs to this company, "
                          "so there's nothing of anyone else's to ask for)")

    print()
    total += 1
    if css_right_aligns_numeric_headers(STYLE_CSS.read_text(encoding="utf-8")):
        print("stylesheet: numeric headings are right-aligned, like their numbers")
    else:
        failures.append(("stylesheet", "th.num is not right-aligned in style.css", 0))

    main.app.dependency_overrides.clear()
    if failures:
        print(f"{len(failures)} problem(s):")
        for who, path, code in failures:
            print(f"  {code}  {path}   [{who}]")
        sys.exit(1)
    print(f"All {total} page checks passed.")


if __name__ == "__main__":
    main_()
