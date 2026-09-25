"""Portal accounts.

Email and password rather than Discord login: SubCon coordinators are
external and may not hold a Discord account, and requiring one just to
manage access would add friction for no gain.
"""
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import HTTPException, Request, status

from storage import db

_hasher = PasswordHasher()

# HQ sees everything. A company manager sees only the sites HQ allocated to
# their company. (These are portal logins — unrelated to the Discord guild
# roles Management/Engineer/Coordinator/Technician that provision.py creates.)
ROLES = ("hq_admin", "hq_staff", "company_manager")
HQ_ROLES = ("hq_admin", "hq_staff")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def authenticate(email: str, password: str):
    user = db.get_portal_user_by_email(email)
    if not user or not verify_password(user["password_hash"], password):
        return None
    db.touch_portal_login(user["id"])
    return user


def current_user(request: Request):
    """The signed-in user, or None. Templates use this to decide what to show."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get_portal_user(user_id)


PASSWORD_CHANGE_PATH = "/account/password"

# Reachable while a password change is outstanding: the change page itself,
# or the door out. Anything else would be a redirect loop.
_EXEMPT_WHILE_FORCED = (PASSWORD_CHANGE_PATH, "/logout")


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    # Every route depends on this one, directly or through require_admin, so
    # the forced change is enforced here rather than repeated per handler.
    if user["must_change_password"] and request.url.path not in _EXEMPT_WHILE_FORCED:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER,
                            headers={"Location": PASSWORD_CHANGE_PATH})
    return user


def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "hq_admin":
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def require_hq(request: Request):
    """Any HQ login, admin or staff — never a company manager."""
    user = require_user(request)
    if user["role"] not in HQ_ROLES:
        raise HTTPException(status_code=403, detail="HQ staff only")
    return user


# --- who may see what ------------------------------------------------------
#
# Plain functions rather than dependencies, called explicitly in each route:
# the routes stay readable, and a test can hand them any user dict without
# rewiring the dependency graph.

def visible_site_ids(user: dict):
    """None means unrestricted (HQ). Otherwise, the set of site ids the user
    may see. Fails closed: a company account with no company, or a role this
    code doesn't recognise, sees nothing rather than everything."""
    if user["role"] in HQ_ROLES:
        return None
    company_id = user.get("company_id")
    if company_id is None:
        return set()
    return db.site_ids_for_company(company_id)


def assert_site_visible(user: dict, site_id: str) -> None:
    """404, deliberately not 403: a company manager must not be able to tell
    'exists but isn't yours' from 'doesn't exist'."""
    scope = visible_site_ids(user)
    if scope is not None and site_id not in scope:
        raise HTTPException(status_code=404, detail="Unknown site")


def assert_technician_visible(user: dict, technician: dict | None) -> dict:
    """Returns the technician, or raises 404 if there isn't one this user is
    allowed to see. A company sees only its own roster."""
    if technician is None or technician.get("removed_at"):
        raise HTTPException(status_code=404, detail="Unknown technician")
    if user["role"] not in HQ_ROLES and technician["company_id"] != user.get("company_id"):
        raise HTTPException(status_code=404, detail="Unknown technician")
    return technician
