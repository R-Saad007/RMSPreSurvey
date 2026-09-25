"""
Single-file SQLite storage, shared by the bot and the web portal.

v2 scope: the bot writes what it observes (messages, attachments, flagged
blockers); the portal writes what people decide (assignments, progress,
blocker resolution). Deliberately boring — at this scale a database server
is one more thing to operate, and WAL handles the two writers fine.
"""
import secrets
import sqlite3
import time
from contextlib import contextmanager

from config import BLOCKER_KEYWORDS, BLOCKER_NEGATIONS, BLOCKER_PATTERNS, CONFIG
from utils.phone import phone_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    discord_id    INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL,
    registered_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS site_channels (
    site_id       TEXT PRIMARY KEY,
    city          TEXT,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    category_name TEXT,
    thread_id     INTEGER,
    brief         TEXT,
    created_at    REAL NOT NULL
);

-- Who may see a site channel. The row is a mirror of the Discord channel
-- overwrite that actually controls visibility; portal/reconcile.py diffs
-- the two, because nothing else will notice if they drift.
CREATE TABLE IF NOT EXISTS site_techs (
    site_id    TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    added_by   TEXT,
    added_at   REAL NOT NULL,
    removed_at REAL,
    PRIMARY KEY (site_id, user_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    site_id    TEXT,
    author_id  INTEGER,
    filename   TEXT,
    path       TEXT,
    tag        TEXT,
    is_blurry  INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(message_id, filename)
);

-- Every message in a site channel. Feeds blocker detection today and the
-- Hermes RAG corpus later; backfilling this after the fact is impossible
-- once Discord ages the history out of easy reach.
CREATE TABLE IF NOT EXISTS messages (
    message_id  INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    site_id     TEXT,
    author_id   INTEGER,
    author_name TEXT,
    content     TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS blockers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     TEXT NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER UNIQUE,
    author_id   INTEGER,
    author_name TEXT,
    text        TEXT NOT NULL,
    matched_on  TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    flagged_at  REAL NOT NULL,
    resolved_by TEXT,
    resolved_at REAL
);

CREATE TABLE IF NOT EXISTS site_progress (
    site_id    TEXT PRIMARY KEY,
    state      TEXT NOT NULL DEFAULT 'not_started',
    note       TEXT,
    updated_by TEXT,
    updated_at REAL NOT NULL
);

-- must_change_password defaults to 1 so an account created by an admin
-- forces its own password on first sign-in: the admin sets a temporary one,
-- hands it over, and never learns the password that outlives it.
-- role is hq_admin, hq_staff or company_manager. company_id is NULL for the
-- two HQ roles (they see everything) and names the firm for a company
-- manager (who sees only that firm's sites).
CREATE TABLE IF NOT EXISTS portal_users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    email                TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,
    name                 TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'hq_staff',
    company_id           INTEGER,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    created_at           REAL NOT NULL,
    last_login           REAL
);

CREATE TABLE IF NOT EXISTS bot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- HQ -> company -> technician -> site. A company is a subcontractor firm, or
-- "HQ Tech" for technicians HQ employs directly: those are modelled as a
-- company too, so every technician follows the same flow.
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_internal INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    created_at  REAL NOT NULL
);

-- The site inventory, uploaded from the customer's workbook. It exists on its
-- own: a site can sit here, allocated to a company, long before anyone runs
-- provision.py to give it a Discord channel (site_channels is that fact).
-- company_id is NULL until HQ allocates the site.
CREATE TABLE IF NOT EXISTS sites (
    site_id               TEXT PRIMARY KEY,
    region                TEXT,
    city                  TEXT,
    district              TEXT,
    priority              TEXT,
    rms_category          TEXT,
    site_type             TEXT,
    grid                  TEXT,
    power_source          TEXT,
    tenants               TEXT,
    tenancy_summary       TEXT,
    sim1                  TEXT,
    sim2                  TEXT,
    existing_equipment    TEXT,
    install_boq           TEXT,
    boq_remarks           TEXT,
    smart_integration     TEXT,
    maps_url              TEXT,
    mbu                   TEXT,
    mbu_lead              TEXT,
    mbu_lead_contact      TEXT,
    zonal_manager         TEXT,
    zonal_manager_contact TEXT,
    company_id            INTEGER,
    target_install_date   TEXT,
    uploaded_by           TEXT,
    created_at            REAL NOT NULL,
    updated_at            REAL NOT NULL
);

-- A technician as a company defines them — before they have a Discord account.
-- discord_id stays NULL until we learn which account is theirs (they join
-- through an invite, or we recognise the number from an earlier onboarding).
-- phone_key is the canonical form of the number, so the same person typed two
-- ways is recognised as one.
CREATE TABLE IF NOT EXISTS technicians (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL,
    name        TEXT NOT NULL,
    email       TEXT,
    phone       TEXT NOT NULL,
    phone_key   TEXT NOT NULL,
    discord_id  INTEGER,
    created_by  TEXT,
    created_at  REAL NOT NULL,
    removed_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_technicians_phone
    ON technicians(phone_key) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_technicians_discord
    ON technicians(discord_id) WHERE discord_id IS NOT NULL AND removed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_technicians_company ON technicians(company_id);

-- What the bot flags as a blocker. Editable from the portal so the detector
-- can be tuned from real traffic without a deploy. Rules are switched off,
-- never deleted, so a seeded rule can't quietly reappear.
CREATE TABLE IF NOT EXISTS hermes_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at REAL NOT NULL,
    UNIQUE(kind, value)
);

-- The regions a site can be in. Each company gets one Discord server per
-- region it works in, so a typo here would ask HQ for a server nobody needs:
-- uploads are checked against this list. Seeded once; HQ adds to it.
CREATE TABLE IF NOT EXISTS regions (
    name       TEXT PRIMARY KEY COLLATE NOCASE,
    created_by TEXT,
    created_at REAL NOT NULL
);

-- Every site-template upload: who, for which company, and what it did. HQ
-- sees these, which is what makes it safe to let a company take unallocated
-- sites from the inventory by uploading them.
CREATE TABLE IF NOT EXISTS site_imports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER,
    filename   TEXT,
    added      INTEGER NOT NULL,
    updated    INTEGER NOT NULL,
    claimed    INTEGER NOT NULL,
    skipped    INTEGER NOT NULL,
    created_by TEXT,
    created_at REAL NOT NULL
);

-- One Discord server per company and region ("FieldCo_South", then
-- "_2" once it's full). A person creates each server — Discord stopped
-- letting bots do it — and adds the bot through the portal, which is how a
-- row gets claimed: the code exchange proves which server it was. Until
-- then a server the bot finds itself in is 'unrecognised' and left alone.
--   status: unrecognised | claimed | review | confirmed | ready | left
CREATE TABLE IF NOT EXISTS guilds (
    guild_id      INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    company_id    INTEGER,
    region        TEXT COLLATE NOCASE,
    seq           INTEGER,
    status        TEXT NOT NULL,
    owner_id      INTEGER,
    claimed_by    TEXT,
    channel_count INTEGER,
    error         TEXT,
    released_at   REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_guilds_slot ON guilds(company_id, region, seq)
    WHERE status != 'left' AND company_id IS NOT NULL;

-- HQ's own team in Discord: added to every server, once, through the same
-- personal link a technician gets. Not portal logins, and not technicians.
CREATE TABLE IF NOT EXISTS staff (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    email        TEXT,
    phone        TEXT,
    phone_key    TEXT,
    country      TEXT,
    discord_role TEXT NOT NULL,
    discord_id   INTEGER,
    created_by   TEXT,
    created_at   REAL NOT NULL,
    removed_at   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_discord ON staff(discord_id)
    WHERE discord_id IS NOT NULL AND removed_at IS NULL;

-- Which staff the bot has put into which server (written by the bot).
CREATE TABLE IF NOT EXISTS guild_staff (
    guild_id   INTEGER NOT NULL,
    staff_id   INTEGER NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    checked_at REAL NOT NULL,
    PRIMARY KEY (guild_id, staff_id)
);

-- What someone's one-time Discord sign-in left us: enough for the bot to put
-- them into any server it's in, now or later. The refresh token rotates on
-- every use, so only the bot refreshes, and it swaps tokens only if nobody
-- replaced them in the meantime (compare-and-swap on the old one).
CREATE TABLE IF NOT EXISTS discord_tokens (
    discord_id    INTEGER PRIMARY KEY,
    username      TEXT,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    REAL NOT NULL,
    scope         TEXT NOT NULL,
    updated_at    REAL NOT NULL,
    dead_at       REAL
);

-- A person's one link (/j/<token>): for a technician or an HQ staff member.
-- expires_at limits only the first connect; once bound to their Discord
-- account it stays their page of sites, usable only by that account.
CREATE TABLE IF NOT EXISTS join_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL UNIQUE,
    technician_id INTEGER,
    staff_id      INTEGER,
    created_by    TEXT,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    used_at       REAL,
    discord_id    INTEGER,
    revoked_at    REAL,
    CHECK ((technician_id IS NULL) != (staff_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_links_tech ON join_links(technician_id);
CREATE INDEX IF NOT EXISTS idx_links_staff ON join_links(staff_id);

-- A site assigned to a technician that isn't open to them in Discord yet.
-- The portal writes the row (the assignment); the bot opens it — adds them to
-- the server if need be, grants the channel, greets them — once they've
-- connected Discord and the site's channel exists.
CREATE TABLE IF NOT EXISTS site_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    technician_id INTEGER NOT NULL,
    site_id       TEXT NOT NULL,
    created_by    TEXT,
    created_at    REAL NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_try_at   REAL,
    error         TEXT,
    greeted_at    REAL,
    opened_at     REAL,
    cancelled_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_waiting ON site_queue(technician_id, site_id)
    WHERE opened_at IS NULL AND cancelled_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_queue_site ON site_queue(site_id);

-- Emails the portal sent (or tried to). WhatsApp sends can't be seen: that's
-- a tap on the operator's own phone.
CREATE TABLE IF NOT EXISTS emails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    technician_id INTEGER,
    staff_id      INTEGER,
    to_addr       TEXT NOT NULL,
    subject       TEXT NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    created_by    TEXT,
    created_at    REAL NOT NULL,
    sent_at       REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_site ON messages(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_blockers_status ON blockers(status, flagged_at);
CREATE INDEX IF NOT EXISTS idx_attachments_site ON attachments(site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sites_company ON sites(company_id);
"""

# The customer's regions. Anything else already in the inventory is
# added alongside them when the list is first seeded.
DEFAULT_REGIONS = ("North", "South", "Central A", "Central B")

# Tables the v1 slash commands wrote. v2 tracks progress and blockers only,
# so these are dropped rather than left as dead schema. The v1 database is
# backed up alongside the live file before this runs.
LEGACY_TABLES = (
    "punch_items",
    "approvals",
    "readings",
    "alarm_tests",
    "inventory",
    "site_completions",
    "presence_events",
    # Discord-invite onboarding, replaced by one Connect-Discord link per
    # person (join_links + site_queue). Dropped only once nothing waited on it.
    "pending_invites",
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS
# won't add them to a database that already exists, so they're applied
# explicitly — the VM's database is live and shouldn't need recreating.
MIGRATIONS = (
    ("site_channels", "brief", "TEXT"),
    ("site_techs", "removed_at", "REAL"),
    # Backfills to 1 on existing rows, which is deliberate: every password
    # created before this shipped was typed into a plaintext HTTP form and
    # should be treated as compromised.
    ("portal_users", "must_change_password", "INTEGER NOT NULL DEFAULT 1"),
    # NULL means HQ-level: sees every site. A company manager's row names
    # their firm.
    ("portal_users", "company_id", "INTEGER"),
    # Numbers are stored in international form with their country; rows from
    # before that are Pakistani, which is what every one of them was.
    ("technicians", "country", "TEXT NOT NULL DEFAULT 'PK'"),
)

# Values, not columns: the portal roles were renamed when companies arrived.
# Idempotent — once no row matches, these are no-ops — so they simply run on
# every start alongside MIGRATIONS. engineer and coordinator collapse into
# one role because neither could reach anything the other couldn't.
DATA_MIGRATIONS = (
    "UPDATE portal_users SET role = 'hq_admin' WHERE role = 'admin'",
    "UPDATE portal_users SET role = 'hq_staff' WHERE role IN ('engineer', 'coordinator')",
)

# Indexes on a column that MIGRATIONS adds can't live in SCHEMA: on a database
# that already exists, SCHEMA runs before the column does, and the index would
# fail to build. They go here, after the migrations have run.
POST_MIGRATION_SQL = ""


def _seed_hermes_rules(conn) -> None:
    """Loads the detector's built-in lists into the table, exactly once.

    The flag matters: rules are switched off rather than deleted, but if the
    seed ran on every start, anything an admin had removed would come back.
    """
    if conn.execute("SELECT 1 FROM bot_meta WHERE key = 'hermes_rules_seeded'").fetchone():
        return
    now = time.time()
    rows = (
        [("keyword", v) for v in BLOCKER_KEYWORDS]
        + [("negation", v) for v in BLOCKER_NEGATIONS]
        + [("pattern", v) for v in BLOCKER_PATTERNS]
    )
    conn.executemany(
        "INSERT OR IGNORE INTO hermes_rules (kind, value, active, created_by, created_at) "
        "VALUES (?, ?, 1, 'built-in', ?)",
        [(kind, value, now) for kind, value in rows],
    )
    conn.execute("INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('hermes_rules_seeded', '1')")


def _seed_regions(conn) -> None:
    """The region list, once — the same reason as the rules above."""
    if conn.execute("SELECT 1 FROM bot_meta WHERE key = 'regions_seeded'").fetchone():
        return
    now = time.time()
    conn.executemany(
        "INSERT OR IGNORE INTO regions (name, created_by, created_at) VALUES (?, 'built-in', ?)",
        [(name, now) for name in DEFAULT_REGIONS],
    )
    conn.execute(
        "INSERT OR IGNORE INTO regions (name, created_by, created_at) "
        "SELECT DISTINCT trim(region), 'inventory', ? FROM sites WHERE trim(COALESCE(region, '')) != ''",
        (now,),
    )
    conn.execute("INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('regions_seeded', '1')")


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        for table in LEGACY_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table, column, coltype in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if existing and column not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                except sqlite3.OperationalError as exc:
                    # The bot and the portal both run this at start; if they
                    # start together, the other one may have just added it.
                    if "duplicate column name" not in str(exc):
                        raise
        for statement in DATA_MIGRATIONS:
            conn.execute(statement)
        conn.executescript(POST_MIGRATION_SQL)
        _seed_hermes_rules(conn)
        _seed_regions(conn)


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _one(row):
    return dict(row) if row else None


def _many(rows):
    return [dict(r) for r in rows]


# --- users ---------------------------------------------------------------

def upsert_user(discord_id, name, phone):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (discord_id, name, phone, registered_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET name=excluded.name, phone=excluded.phone
            """,
            (discord_id, name, phone, time.time()),
        )


def get_user(discord_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone())


def all_users():
    with get_conn() as conn:
        return _many(conn.execute("SELECT * FROM users ORDER BY name").fetchall())


# --- site_channels -------------------------------------------------------

def upsert_site_channel(site_id, city, guild_id, channel_id, category_name, thread_id=None, brief=None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO site_channels
                (site_id, city, guild_id, channel_id, category_name, thread_id, brief, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site_id) DO UPDATE SET
                city=excluded.city,
                guild_id=excluded.guild_id,
                channel_id=excluded.channel_id,
                category_name=excluded.category_name,
                thread_id=COALESCE(excluded.thread_id, site_channels.thread_id),
                brief=COALESCE(excluded.brief, site_channels.brief)
            """,
            (site_id, city, guild_id, channel_id, category_name, thread_id, brief, time.time()),
        )


def get_site_channel(site_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM site_channels WHERE site_id = ?", (site_id,)).fetchone())


def site_id_for_channel(channel_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT site_id FROM site_channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["site_id"] if row else None


def all_site_channels(guild_id=None):
    with get_conn() as conn:
        if guild_id:
            rows = conn.execute(
                "SELECT * FROM site_channels WHERE guild_id = ? ORDER BY site_id", (guild_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM site_channels ORDER BY site_id").fetchall()
        return _many(rows)


def delete_site_channel(site_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM site_channels WHERE site_id = ?", (site_id,))


# --- site_techs ----------------------------------------------------------

def assign_tech(site_id, user_id, added_by):
    """Records the assignment. The caller is responsible for writing the
    matching Discord channel overwrite — see portal/discord_api.py."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO site_techs (site_id, user_id, added_by, added_at, removed_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(site_id, user_id) DO UPDATE SET
                added_by=excluded.added_by, added_at=excluded.added_at, removed_at=NULL
            """,
            (site_id, user_id, added_by, time.time()),
        )


def revoke_tech(site_id, user_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE site_techs SET removed_at = ? WHERE site_id = ? AND user_id = ?",
            (time.time(), site_id, user_id),
        )


def techs_for_site(site_id, include_removed=False):
    with get_conn() as conn:
        sql = """
            SELECT st.*, u.name, u.phone
            FROM site_techs st LEFT JOIN users u ON u.discord_id = st.user_id
            WHERE st.site_id = ?
        """
        if not include_removed:
            sql += " AND st.removed_at IS NULL"
        return _many(conn.execute(sql + " ORDER BY st.added_at", (site_id,)).fetchall())


def sites_for_tech(user_id):
    with get_conn() as conn:
        return _many(conn.execute(
            "SELECT * FROM site_techs WHERE user_id = ? AND removed_at IS NULL", (user_id,)
        ).fetchall())


def active_assignments():
    with get_conn() as conn:
        return _many(conn.execute(
            "SELECT * FROM site_techs WHERE removed_at IS NULL"
        ).fetchall())


# --- attachments ---------------------------------------------------------

def log_attachment(message_id, channel_id, site_id, author_id, filename, path, tag, is_blurry):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO attachments
                (message_id, channel_id, site_id, author_id, filename, path, tag, is_blurry, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, channel_id, site_id, author_id, filename, path, tag, int(is_blurry), time.time()),
        )


def set_attachment_tag(message_id, tag):
    with get_conn() as conn:
        conn.execute("UPDATE attachments SET tag = ? WHERE message_id = ?", (tag, message_id))


def set_attachment_tag_by_id(attachment_id, tag):
    with get_conn() as conn:
        conn.execute("UPDATE attachments SET tag = ? WHERE id = ?", (tag, attachment_id))


def known_message_ids(channel_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT message_id FROM attachments WHERE channel_id = ?", (channel_id,)
        ).fetchall()
        return {r["message_id"] for r in rows}


def get_attachment(attachment_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone())


def attachments_for_site(site_id, untagged_only=False):
    with get_conn() as conn:
        sql = "SELECT * FROM attachments WHERE site_id = ?"
        if untagged_only:
            sql += " AND (tag IS NULL OR tag = '')"
        return _many(conn.execute(sql + " ORDER BY created_at DESC", (site_id,)).fetchall())


# --- messages ------------------------------------------------------------

def log_message(message_id, channel_id, site_id, author_id, author_name, content, created_at=None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (message_id, channel_id, site_id, author_id, author_name, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, channel_id, site_id, author_id, author_name, content, created_at or time.time()),
        )


def last_activity(site_id):
    """Replaces the old Arrived/Leaving buttons: a tech is 'on site' when
    they're talking, which is more truthful than a button someone forgot."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) ts FROM messages WHERE site_id = ?", (site_id,)
        ).fetchone()
        return row["ts"] if row and row["ts"] else None


def last_activity_all():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT site_id, MAX(created_at) ts, COUNT(*) n FROM messages GROUP BY site_id"
        ).fetchall()
        return {r["site_id"]: {"last_activity": r["ts"], "message_count": r["n"]} for r in rows}


def message_count(site_id):
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) n FROM messages WHERE site_id = ?", (site_id,)).fetchone()["n"]


def messages_for_site(site_id, limit=200):
    with get_conn() as conn:
        return _many(conn.execute(
            "SELECT * FROM messages WHERE site_id = ? ORDER BY created_at DESC LIMIT ?",
            (site_id, limit),
        ).fetchall())


# --- blockers ------------------------------------------------------------

def open_blocker(site_id, channel_id, message_id, author_id, author_name, text, matched_on):
    """Returns the new blocker id, or None if this message already flagged."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO blockers
                (site_id, channel_id, message_id, author_id, author_name, text, matched_on, status, flagged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (site_id, channel_id, message_id, author_id, author_name, text, matched_on, time.time()),
        )
        return cur.lastrowid if cur.rowcount else None


def resolve_blocker(blocker_id, resolved_by, status="resolved"):
    with get_conn() as conn:
        conn.execute(
            "UPDATE blockers SET status=?, resolved_by=?, resolved_at=? WHERE id=?",
            (status, resolved_by, time.time(), blocker_id),
        )


def resolve_blocker_by_message(message_id, resolved_by, status="resolved"):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE blockers SET status=?, resolved_by=?, resolved_at=? WHERE message_id=? AND status='open'",
            (status, resolved_by, time.time(), message_id),
        )
        return cur.rowcount > 0


def get_blocker(blocker_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM blockers WHERE id = ?", (blocker_id,)).fetchone())


def open_blockers(site_id=None):
    with get_conn() as conn:
        if site_id:
            rows = conn.execute(
                "SELECT * FROM blockers WHERE status='open' AND site_id=? ORDER BY flagged_at DESC",
                (site_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM blockers WHERE status='open' ORDER BY flagged_at DESC"
            ).fetchall()
        return _many(rows)


def blockers_for_site(site_id, limit=100):
    with get_conn() as conn:
        return _many(conn.execute(
            "SELECT * FROM blockers WHERE site_id = ? ORDER BY flagged_at DESC LIMIT ?",
            (site_id, limit),
        ).fetchall())


def open_blocker_counts():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT site_id, COUNT(*) c FROM blockers WHERE status='open' GROUP BY site_id"
        ).fetchall()
        return {r["site_id"]: r["c"] for r in rows}


# --- site_progress -------------------------------------------------------

PROGRESS_STATES = ("not_started", "on_site", "blocked", "done")


def set_site_progress(site_id, state, updated_by, note=None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO site_progress (site_id, state, note, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(site_id) DO UPDATE SET
                state=excluded.state, note=excluded.note,
                updated_by=excluded.updated_by, updated_at=excluded.updated_at
            """,
            (site_id, state, note, updated_by, time.time()),
        )


def get_site_progress(site_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM site_progress WHERE site_id = ?", (site_id,)).fetchone())


def all_site_progress():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM site_progress").fetchall()
        return {r["site_id"]: dict(r) for r in rows}


# --- portal_users --------------------------------------------------------

def create_portal_user(email, password_hash, name, role="hq_staff", must_change=True, company_id=None):
    """must_change=False only when the person chose the password themselves,
    which is the case for the command-line tools and nowhere else.

    company_id belongs to company managers and nobody else: the caller is
    responsible for passing None for the HQ roles."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO portal_users
                (email, password_hash, name, role, company_id, must_change_password, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (email.lower().strip(), password_hash, name, role, company_id, int(must_change), time.time()),
        )
        return cur.lastrowid


def get_portal_user_by_email(email):
    with get_conn() as conn:
        return _one(conn.execute(
            "SELECT * FROM portal_users WHERE email = ?", (email.lower().strip(),)
        ).fetchone())


def get_portal_user(user_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM portal_users WHERE id = ?", (user_id,)).fetchone())


def touch_portal_login(user_id):
    with get_conn() as conn:
        conn.execute("UPDATE portal_users SET last_login = ? WHERE id = ?", (time.time(), user_id))


def all_portal_users():
    with get_conn() as conn:
        return _many(conn.execute(
            """
            SELECT u.*, c.name AS company_name
            FROM portal_users u LEFT JOIN companies c ON c.id = u.company_id
            ORDER BY u.name
            """
        ).fetchall())


def set_portal_password(user_id, password_hash, must_change=False):
    """Clears the forced-change flag by default: whoever just set this chose
    it themselves, so there is nothing left to force."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE portal_users SET password_hash = ?, must_change_password = ? WHERE id = ?",
            (password_hash, int(must_change), user_id),
        )


def delete_portal_user(user_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM portal_users WHERE id = ?", (user_id,))


# --- site_queue: assigned, not yet open in Discord --------------------------

def queue_sites(technician_id, site_ids, created_by):
    """Records the assignment of these sites to a technician. Returns
    (queued, already): a site already waiting for them isn't queued twice."""
    now = time.time()
    queued, already = [], []
    with get_conn() as conn:
        for site_id in dict.fromkeys(site_ids):
            try:
                conn.execute(
                    "INSERT INTO site_queue (technician_id, site_id, created_by, created_at) VALUES (?, ?, ?, ?)",
                    (technician_id, site_id, created_by, now),
                )
                queued.append(site_id)
            except sqlite3.IntegrityError:
                already.append(site_id)
    return queued, already


_QUEUE_SELECT = """
    SELECT q.*, t.name AS technician_name, t.phone AS technician_phone, t.country AS technician_country,
           t.discord_id, t.company_id, sc.guild_id, sc.channel_id, sc.brief,
           COALESCE(s.city, sc.city) AS city, g.status AS guild_status, g.name AS guild_name
    FROM site_queue q
    JOIN technicians t ON t.id = q.technician_id
    LEFT JOIN sites s ON s.site_id = q.site_id
    LEFT JOIN site_channels sc ON sc.site_id = q.site_id
    LEFT JOIN guilds g ON g.guild_id = sc.guild_id
"""
_WAITING = "q.opened_at IS NULL AND q.cancelled_at IS NULL"


def get_queue_row(queue_id):
    with get_conn() as conn:
        return _one(conn.execute(_QUEUE_SELECT + " WHERE q.id = ?", (queue_id,)).fetchone())


def queue_for_technician(technician_id):
    with get_conn() as conn:
        return _many(conn.execute(
            _QUEUE_SELECT + f" WHERE q.technician_id = ? AND {_WAITING} ORDER BY q.site_id",
            (technician_id,)).fetchall())


def queue_for_site(site_id):
    with get_conn() as conn:
        return _many(conn.execute(
            _QUEUE_SELECT + f" WHERE q.site_id = ? AND {_WAITING} ORDER BY t.name", (site_id,)).fetchall())


def queued_row(technician_id, site_id):
    with get_conn() as conn:
        return _one(conn.execute(
            _QUEUE_SELECT + f" WHERE q.technician_id = ? AND q.site_id = ? AND {_WAITING}",
            (technician_id, site_id)).fetchone())


def cancel_queue_row(queue_id):
    """True if it was still waiting (and now isn't)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE site_queue SET cancelled_at = ? WHERE id = ? AND opened_at IS NULL AND cancelled_at IS NULL",
            (time.time(), queue_id))
        return cur.rowcount == 1


def cancel_queue_for_technician(technician_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE site_queue SET cancelled_at = ? WHERE technician_id = ? AND opened_at IS NULL AND cancelled_at IS NULL",
            (time.time(), technician_id))


# How long a row waits before the bot tries it again: 5 s, doubling, capped at an hour.
def queue_backoff(attempts):
    return min(5 * 2 ** max(attempts - 1, 0), 3600) if attempts else 0


def openable_queue_rows(limit=25, now=None):
    """Rows the bot can open now: the technician is on a roster and has
    connected Discord, the site's channel is fully set up in a ready server,
    and the row isn't in its retry wait. A row waiting on the person to
    connect again ('needs_connect') sits out until they do."""
    now = now or time.time()
    with get_conn() as conn:
        rows = _many(conn.execute(
            _QUEUE_SELECT + f"""
            WHERE {_WAITING} AND t.removed_at IS NULL AND t.discord_id IS NOT NULL
              AND sc.brief IS NOT NULL AND g.status = 'ready'
              AND COALESCE(q.error, '') != 'needs_connect'
            ORDER BY q.id
            """).fetchall())
    ready = [r for r in rows if not r["last_try_at"] or r["last_try_at"] + queue_backoff(r["attempts"]) <= now]
    return ready[:limit]


def claim_queue_row(queue_id):
    """The bot takes a row to work on. False: it was opened or cancelled meanwhile."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE site_queue SET attempts = attempts + 1, last_try_at = ? "
            "WHERE id = ? AND opened_at IS NULL AND cancelled_at IS NULL",
            (time.time(), queue_id))
        return cur.rowcount == 1


def finish_queue_row(queue_id):
    """False means the portal cancelled it while the bot worked: undo."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE site_queue SET opened_at = ?, error = NULL "
            "WHERE id = ? AND opened_at IS NULL AND cancelled_at IS NULL",
            (time.time(), queue_id))
        return cur.rowcount == 1


def queue_row_error(queue_id, error):
    with get_conn() as conn:
        conn.execute("UPDATE site_queue SET error = ? WHERE id = ?", ((error or "")[:300], queue_id))


def mark_queue_greeted(queue_id):
    with get_conn() as conn:
        conn.execute("UPDATE site_queue SET greeted_at = ? WHERE id = ?", (time.time(), queue_id))


def reset_queue_backoff(technician_id):
    """They've just connected: everything waiting for them is worth trying now."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE site_queue SET attempts = 0, last_try_at = NULL, error = NULL "
            "WHERE technician_id = ? AND opened_at IS NULL AND cancelled_at IS NULL",
            (technician_id,))


# --- personal links ----------------------------------------------------------

def _link_owner(technician_id, staff_id):
    if (technician_id is None) == (staff_id is None):
        raise ValueError("a link belongs to a technician or a staff member")
    return ("technician_id", technician_id) if technician_id is not None else ("staff_id", staff_id)


def current_link(technician_id=None, staff_id=None):
    """The person's newest link that hasn't been withdrawn, or None."""
    column, value = _link_owner(technician_id, staff_id)
    with get_conn() as conn:
        return _one(conn.execute(
            f"SELECT * FROM join_links WHERE {column} = ? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
            (value,)).fetchone())


def ensure_link(technician_id=None, staff_id=None, created_by=None, days=14, fresh=False):
    """The person's link, making one if they have none — or if theirs expired
    before it was ever used (or fresh=True). A link that's been used to
    connect is theirs for good and is never replaced here."""
    column, value = _link_owner(technician_id, staff_id)
    now = time.time()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        link = _one(conn.execute(
            f"SELECT * FROM join_links WHERE {column} = ? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
            (value,)).fetchone())
        if link and (link["used_at"] or (link["expires_at"] > now and not fresh)):
            return link
        if link:
            conn.execute("UPDATE join_links SET revoked_at = ? WHERE id = ?", (now, link["id"]))
        token = secrets.token_urlsafe(24)
        cur = conn.execute(
            f"INSERT INTO join_links (token, {column}, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token, value, created_by, now, now + days * 86400))
        return _one(conn.execute("SELECT * FROM join_links WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_link(token):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM join_links WHERE token = ?", (token,)).fetchone())


def get_link_by_id(link_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM join_links WHERE id = ?", (link_id,)).fetchone())


def revoke_links(technician_id=None, staff_id=None):
    column, value = _link_owner(technician_id, staff_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE join_links SET revoked_at = ? WHERE {column} = ? AND revoked_at IS NULL",
                     (time.time(), value))


def bind_link(link_id, discord_id, username, access_token, refresh_token, expires_in, scope):
    """Connects a person's Discord account through their link, in one
    transaction. Returns None on success, or the reason it was refused:

        'gone'      the link was withdrawn, or the person was removed
        'expired'   never used, and past its first-connect deadline
        'other'     this person is already connected to a different account
        'taken'     this account already belongs to someone else on the same side
    """
    now = time.time()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        link = conn.execute("SELECT * FROM join_links WHERE id = ?", (link_id,)).fetchone()
        if not link or link["revoked_at"]:
            return "gone"
        table = "technicians" if link["technician_id"] is not None else "staff"
        person_id = link["technician_id"] if link["technician_id"] is not None else link["staff_id"]
        person = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (person_id,)).fetchone()
        if not person or person["removed_at"]:
            return "gone"
        if not link["used_at"] and link["expires_at"] <= now:
            return "expired"
        if person["discord_id"] is not None and person["discord_id"] != discord_id:
            return "other"
        clash = conn.execute(
            f"SELECT 1 FROM {table} WHERE discord_id = ? AND id != ? AND removed_at IS NULL",
            (discord_id, person_id)).fetchone()
        if clash:
            return "taken"
        conn.execute(f"UPDATE {table} SET discord_id = ? WHERE id = ?", (discord_id, person_id))
        conn.execute(
            "UPDATE join_links SET used_at = COALESCE(used_at, ?), discord_id = ? WHERE id = ?",
            (now, discord_id, link_id))
        conn.execute(
            """
            INSERT INTO discord_tokens (discord_id, username, access_token, refresh_token, expires_at, scope, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET username = excluded.username,
                access_token = excluded.access_token, refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at, scope = excluded.scope,
                updated_at = excluded.updated_at, dead_at = NULL
            """,
            (discord_id, username, access_token, refresh_token, now + float(expires_in), scope, now))
        if table == "technicians":
            conn.execute(
                "INSERT INTO users (discord_id, name, phone, registered_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET name = excluded.name, phone = excluded.phone",
                (discord_id, person["name"], person["phone"], now))
            conn.execute(
                "UPDATE site_queue SET attempts = 0, last_try_at = NULL, error = NULL "
                "WHERE technician_id = ? AND opened_at IS NULL AND cancelled_at IS NULL",
                (person_id,))
    return None


# --- Discord tokens ------------------------------------------------------------

def get_tokens(discord_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM discord_tokens WHERE discord_id = ?", (discord_id,)).fetchone())


def replace_tokens_if(discord_id, old_refresh_token, access_token, refresh_token, expires_in):
    """Stores refreshed tokens — only if nobody replaced them since we read
    them (a person reconnecting writes new ones). False: keep what's there."""
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE discord_tokens SET access_token = ?, refresh_token = ?, expires_at = ?, updated_at = ?, "
            "dead_at = NULL WHERE discord_id = ? AND refresh_token = ?",
            (access_token, refresh_token, now + float(expires_in), now, discord_id, old_refresh_token))
        return cur.rowcount == 1


def mark_tokens_dead(discord_id, refresh_token=None):
    """Discord said the sign-in is no good any more (they revoked it, or it
    lapsed): the person has to connect again. With refresh_token, only if it's
    still the one that failed — a reconnect in the meantime wins."""
    with get_conn() as conn:
        if refresh_token is None:
            conn.execute("UPDATE discord_tokens SET dead_at = ? WHERE discord_id = ?", (time.time(), discord_id))
        else:
            conn.execute("UPDATE discord_tokens SET dead_at = ? WHERE discord_id = ? AND refresh_token = ?",
                         (time.time(), discord_id, refresh_token))


def delete_tokens(discord_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM discord_tokens WHERE discord_id = ?", (discord_id,))


def discord_account_in_use(discord_id):
    """Whether any active technician or staff member is connected as this account."""
    with get_conn() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM technicians WHERE discord_id = ? AND removed_at IS NULL "
            "UNION SELECT 1 FROM staff WHERE discord_id = ? AND removed_at IS NULL",
            (discord_id, discord_id)).fetchone())


# --- emails ----------------------------------------------------------------------

def log_email(to_addr, subject, created_by, technician_id=None, staff_id=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO emails (technician_id, staff_id, to_addr, subject, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (technician_id, staff_id, to_addr, subject, created_by, time.time()))
        return cur.lastrowid


def set_email_status(email_id, status, error=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE emails SET status = ?, error = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END "
            "WHERE id = ?",
            (status, (error or None) and error[:300], status, time.time(), email_id))


def last_email(technician_id=None, staff_id=None):
    column, value = _link_owner(technician_id, staff_id)
    with get_conn() as conn:
        return _one(conn.execute(
            f"SELECT * FROM emails WHERE {column} = ? ORDER BY id DESC LIMIT 1", (value,)).fetchone())


# --- HQ staff in Discord ----------------------------------------------------------

STAFF_ROLES = ("Management", "Engineer", "Coordinator")


def create_staff(name, discord_role, email=None, phone=None, phone_key=None, country=None, created_by=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO staff (name, email, phone, phone_key, country, discord_role, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), (email or "").strip() or None, phone, phone_key, country, discord_role,
             created_by, time.time()))
        return cur.lastrowid


def get_staff(staff_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone())


def all_staff(include_removed=False):
    sql = "SELECT * FROM staff" + ("" if include_removed else " WHERE removed_at IS NULL")
    with get_conn() as conn:
        return _many(conn.execute(sql + " ORDER BY name").fetchall())


def staff_by_email(email):
    with get_conn() as conn:
        return _one(conn.execute(
            "SELECT * FROM staff WHERE lower(email) = lower(?) AND removed_at IS NULL ORDER BY id LIMIT 1",
            ((email or "").strip(),)).fetchone())


def remove_staff(staff_id):
    with get_conn() as conn:
        conn.execute("UPDATE staff SET removed_at = ? WHERE id = ? AND removed_at IS NULL", (time.time(), staff_id))


def record_guild_staff(guild_id, staff_id, ok, error=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_staff (guild_id, staff_id, ok, error, checked_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, staff_id) DO UPDATE SET ok = excluded.ok, error = excluded.error, "
            "checked_at = excluded.checked_at",
            (guild_id, staff_id, int(bool(ok)), (error or None) and error[:300], time.time()))


def guild_staff_rows():
    with get_conn() as conn:
        return _many(conn.execute("SELECT * FROM guild_staff").fetchall())


# --- Discord servers ---------------------------------------------------------------

def server_name(company_name, region, seq):
    """The name a Company_Region server is created with."""
    return f"{company_name}_{region}" + (f"_{seq}" if seq and seq > 1 else "")


def register_guild_seen(guild_id, name, owner_id):
    """The bot is in this server (it just joined, or it's starting up). A new
    one is 'unrecognised' until the portal claims it; one we know keeps its
    status — except one that had left, which starts over as unrecognised."""
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO guilds (guild_id, name, status, owner_id, created_at, updated_at)
            VALUES (?, ?, 'unrecognised', ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                name = excluded.name, owner_id = excluded.owner_id, updated_at = excluded.updated_at,
                company_id = CASE WHEN guilds.status = 'left' THEN NULL ELSE guilds.company_id END,
                region = CASE WHEN guilds.status = 'left' THEN NULL ELSE guilds.region END,
                seq = CASE WHEN guilds.status = 'left' THEN NULL ELSE guilds.seq END,
                status = CASE WHEN guilds.status = 'left' THEN 'unrecognised' ELSE guilds.status END
            """,
            (guild_id, name, owner_id, now, now))


def claim_guild(guild_id, name, company_id, region, seq, claimed_by, status="claimed"):
    """Registers a server as company+region+seq, if it isn't already somebody's.
    True if it's now claimed."""
    now = time.time()
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO guilds (guild_id, name, company_id, region, seq, status, claimed_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    name = excluded.name, company_id = excluded.company_id, region = excluded.region,
                    seq = excluded.seq, status = excluded.status, claimed_by = excluded.claimed_by,
                    error = NULL, released_at = NULL, updated_at = excluded.updated_at
                WHERE guilds.status IN ('unrecognised', 'left')
                """,
                (guild_id, name, company_id, region, seq, status, claimed_by, now, now))
            return cur.rowcount == 1
    except sqlite3.IntegrityError:
        return False        # that company/region/number is already another server


def get_guild(guild_id):
    with get_conn() as conn:
        return _one(conn.execute(
            "SELECT g.*, c.name AS company_name FROM guilds g LEFT JOIN companies c ON c.id = g.company_id "
            "WHERE g.guild_id = ?", (guild_id,)).fetchone())


def all_guilds(statuses=None):
    sql = ("SELECT g.*, c.name AS company_name, "
           "(SELECT COUNT(*) FROM site_channels sc WHERE sc.guild_id = g.guild_id) AS site_count "
           "FROM guilds g LEFT JOIN companies c ON c.id = g.company_id")
    params = []
    if statuses:
        sql += f" WHERE g.status IN ({','.join('?' for _ in statuses)})"
        params = list(statuses)
    with get_conn() as conn:
        return _many(conn.execute(sql + " ORDER BY c.name, g.region, g.seq, g.name", params).fetchall())


def guild_in_slot(company_id, region, seq):
    with get_conn() as conn:
        return _one(conn.execute(
            "SELECT * FROM guilds WHERE company_id = ? AND region = ? AND seq = ? AND status != 'left'",
            (company_id, region, seq)).fetchone())


def set_guild_status(guild_id, status, error=None):
    with get_conn() as conn:
        conn.execute("UPDATE guilds SET status = ?, error = ?, updated_at = ? WHERE guild_id = ?",
                     (status, error, time.time(), guild_id))


def set_guild_error(guild_id, error):
    with get_conn() as conn:
        conn.execute("UPDATE guilds SET error = ?, updated_at = ? WHERE guild_id = ?",
                     ((error or None) and error[:300], time.time(), guild_id))


def update_guild_facts(guild_id, name=None, owner_id=None, channel_count=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE guilds SET name = COALESCE(?, name), owner_id = COALESCE(?, owner_id), "
            "channel_count = COALESCE(?, channel_count), updated_at = ? WHERE guild_id = ?",
            (name, owner_id, channel_count, time.time(), guild_id))


def mark_guild_left(guild_id):
    with get_conn() as conn:
        conn.execute("UPDATE guilds SET status = 'left', updated_at = ? WHERE guild_id = ?",
                     (time.time(), guild_id))


def release_guild(guild_id, actor):
    """A server that's gone (deleted, or the bot removed): its sites go back
    to waiting for a server. Everyone who held one of them stays assigned —
    their rows go back in the queue and open again in the replacement server.
    Only for a server that has left. Returns how many sites were released."""
    now = time.time()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM guilds WHERE guild_id = ?", (guild_id,)).fetchone()
        if not row or row["status"] != "left":
            return None
        sites = [r["site_id"] for r in conn.execute(
            "SELECT site_id FROM site_channels WHERE guild_id = ?", (guild_id,))]
        for site_id in sites:
            holders = conn.execute(
                "SELECT t.id FROM site_techs st JOIN technicians t ON t.discord_id = st.user_id AND t.removed_at IS NULL "
                "WHERE st.site_id = ? AND st.removed_at IS NULL", (site_id,)).fetchall()
            for holder in holders:
                conn.execute(
                    "INSERT OR IGNORE INTO site_queue (technician_id, site_id, created_by, created_at) VALUES (?, ?, ?, ?)",
                    (holder["id"], site_id, actor, now))
            conn.execute("UPDATE site_techs SET removed_at = ? WHERE site_id = ? AND removed_at IS NULL", (now, site_id))
            conn.execute("DELETE FROM site_channels WHERE site_id = ?", (site_id,))
        conn.execute("UPDATE guilds SET released_at = ?, updated_at = ? WHERE guild_id = ?", (now, now, guild_id))
    return len(sites)


def unprovisioned_by_slot():
    """[(company_id, company_name, region, count)] of allocated sites that
    have no Discord channel yet — what the servers are needed for."""
    with get_conn() as conn:
        return _many(conn.execute(
            """
            SELECT s.company_id, c.name AS company_name, s.region, COUNT(*) AS n
            FROM sites s
            JOIN companies c ON c.id = s.company_id
            LEFT JOIN site_channels sc ON sc.site_id = s.site_id
            WHERE sc.site_id IS NULL
            GROUP BY s.company_id, lower(COALESCE(s.region, ''))
            ORDER BY c.name, s.region
            """).fetchall())


def sites_to_provision(company_id, region, limit):
    """This company's sites in this region that still need a channel, in order."""
    if limit <= 0:
        return []
    with get_conn() as conn:
        return _many(conn.execute(
            """
            SELECT s.* FROM sites s LEFT JOIN site_channels sc ON sc.site_id = s.site_id
            WHERE s.company_id = ? AND lower(COALESCE(s.region, '')) = lower(?) AND sc.site_id IS NULL
            ORDER BY s.site_id LIMIT ?
            """, (company_id, region, limit)).fetchall())


def set_site_brief(site_id, brief):
    with get_conn() as conn:
        conn.execute("UPDATE site_channels SET brief = ? WHERE site_id = ?", (brief, site_id))


def set_site_thread(site_id, thread_id):
    with get_conn() as conn:
        conn.execute("UPDATE site_channels SET thread_id = ? WHERE site_id = ?", (thread_id, site_id))


def site_mode(site_id):
    """'read_only' once the survey is complete, otherwise 'writable'."""
    with get_conn() as conn:
        row = conn.execute("SELECT state FROM site_progress WHERE site_id = ?", (site_id,)).fetchone()
    return "read_only" if row and row["state"] == "done" else "writable"


# --- bot heartbeat -------------------------------------------------------------

def beat():
    set_meta("bot_heartbeat", time.time())


def bot_online(within=60):
    try:
        return time.time() - float(get_meta("bot_heartbeat", 0)) < within
    except (TypeError, ValueError):
        return False


# --- companies -----------------------------------------------------------

def create_company(name, is_internal=False, created_by=None):
    """Raises sqlite3.IntegrityError if the name is taken (case-insensitive)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO companies (name, is_internal, created_by, created_at) VALUES (?, ?, ?, ?)",
            (name.strip(), int(is_internal), created_by, time.time()),
        )
        return cur.lastrowid


def ensure_company(name, is_internal=False, created_by=None):
    """The id of the company with this name, creating it if it isn't there."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM companies WHERE name = ?", (name.strip(),)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO companies (name, is_internal, created_by, created_at) VALUES (?, ?, ?, ?)",
            (name.strip(), int(is_internal), created_by, time.time()),
        )
        return cur.lastrowid


def get_company(company_id):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone())


def get_company_by_name(name):
    with get_conn() as conn:
        return _one(conn.execute("SELECT * FROM companies WHERE name = ?", (name.strip(),)).fetchone())


def all_companies():
    with get_conn() as conn:
        return _many(conn.execute("SELECT * FROM companies ORDER BY name").fetchall())


def remove_company(company_id):
    """Deletes a company nothing depends on any more. Returns {} once it's
    gone, otherwise what still hangs off it. The check and the delete are one
    statement, so a site allocated in the same instant can't slip between.
    A removed technician's history row may keep the old id, and so may a
    server that's gone from Discord; that's fine — company ids are never
    reused (AUTOINCREMENT). A server still in Discord blocks it, even with no
    sites in it: it would be left belonging to nobody."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            DELETE FROM companies WHERE id = ?
              AND NOT EXISTS (SELECT 1 FROM sites WHERE company_id = ?)
              AND NOT EXISTS (SELECT 1 FROM technicians WHERE company_id = ? AND removed_at IS NULL)
              AND NOT EXISTS (SELECT 1 FROM portal_users WHERE company_id = ?)
              AND NOT EXISTS (SELECT 1 FROM guilds WHERE company_id = ? AND status != 'left')
            """,
            (company_id,) * 5,
        )
        if cur.rowcount:
            return {}
        count = lambda sql: conn.execute(sql, (company_id,)).fetchone()[0]
        return {
            "site": count("SELECT COUNT(*) FROM sites WHERE company_id = ?"),
            "technician": count("SELECT COUNT(*) FROM technicians WHERE company_id = ? AND removed_at IS NULL"),
            "login": count("SELECT COUNT(*) FROM portal_users WHERE company_id = ?"),
            "Discord server": count("SELECT COUNT(*) FROM guilds WHERE company_id = ? AND status != 'left'"),
        }


# --- sites (inventory) ---------------------------------------------------

SITE_COLUMNS = (
    "region", "city", "district", "priority", "rms_category", "site_type", "grid",
    "power_source", "tenants", "tenancy_summary", "sim1", "sim2", "existing_equipment",
    "install_boq", "boq_remarks", "smart_integration", "maps_url", "mbu", "mbu_lead",
    "mbu_lead_contact", "zonal_manager", "zonal_manager_contact",
)


def _chunks(items, size=400):
    """SQLite caps the number of ? in one statement; stay well under it."""
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i : i + size]


def upsert_sites(rows, uploaded_by):
    """Inserts new sites and refreshes the inventory columns of known ones.

    company_id and target_install_date are left out of the update on purpose.
    A re-upload of the customer's workbook has no idea who a site was given
    to, and must never wipe an allocation made in the portal.
    """
    now = time.time()
    columns = ", ".join(SITE_COLUMNS)
    marks = ", ".join("?" for _ in SITE_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in SITE_COLUMNS)
    sql = f"""
        INSERT INTO sites (site_id, {columns}, uploaded_by, created_at, updated_at)
        VALUES (?, {marks}, ?, ?, ?)
        ON CONFLICT(site_id) DO UPDATE SET
            {updates}, uploaded_by = excluded.uploaded_by, updated_at = excluded.updated_at
    """
    inserted = updated = 0
    with get_conn() as conn:
        known = {r["site_id"] for r in conn.execute("SELECT site_id FROM sites")}
        for row in rows:
            site_id = row["site_id"]
            values = [(row.get(c) or "") for c in SITE_COLUMNS]
            conn.execute(sql, (site_id, *values, uploaded_by, now, now))
            if site_id in known:
                updated += 1
            else:
                inserted += 1
                known.add(site_id)
    return {"inserted": inserted, "updated": updated}


def get_site(site_id):
    with get_conn() as conn:
        return _one(conn.execute(
            """
            SELECT s.*, c.name AS company_name
            FROM sites s LEFT JOIN companies c ON c.id = s.company_id
            WHERE s.site_id = ?
            """,
            (site_id,),
        ).fetchone())


def all_sites(city=None, company_id=None, unassigned_only=False, unprovisioned_only=False):
    sql = """
        SELECT s.*, c.name AS company_name, (sc.site_id IS NOT NULL) AS provisioned
        FROM sites s
        LEFT JOIN companies c ON c.id = s.company_id
        LEFT JOIN site_channels sc ON sc.site_id = s.site_id
        WHERE 1 = 1
    """
    params = []
    if city:
        sql += " AND lower(s.city) = lower(?)"
        params.append(city)
    if company_id is not None:
        sql += " AND s.company_id = ?"
        params.append(company_id)
    if unassigned_only:
        sql += " AND s.company_id IS NULL"
    if unprovisioned_only:
        sql += " AND sc.site_id IS NULL"
    with get_conn() as conn:
        return _many(conn.execute(sql + " ORDER BY s.city, s.site_id", params).fetchall())


def site_ids_for_company(company_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT site_id FROM sites WHERE company_id = ?", (company_id,)).fetchall()
        return {r["site_id"] for r in rows}


def bulk_assign_company(site_ids, company_id, target_date=None, updated_by=None):
    """Allocates sites to a company (or, with company_id=None, takes them back).

    Returns (assigned, skipped): skipped is [(site_id, reason)]. A site that
    already belongs to a DIFFERENT company and still has technicians waiting
    on it or holding access is left alone. Moving it would leave the old
    company's technicians in a channel that is no longer theirs, with nothing
    on screen to say so — those have to be unassigned first, deliberately.
    """
    now = time.time()
    assigned, skipped = [], []
    with get_conn() as conn:
        for chunk in _chunks(dict.fromkeys(site_ids)):
            marks = ",".join("?" for _ in chunk)
            current = {
                r["site_id"]: r["company_id"]
                for r in conn.execute(f"SELECT site_id, company_id FROM sites WHERE site_id IN ({marks})", chunk)
            }
            busy = {
                r["site_id"] for r in conn.execute(
                    f"SELECT DISTINCT site_id FROM site_techs WHERE removed_at IS NULL AND site_id IN ({marks})", chunk)
            } | {
                r["site_id"] for r in conn.execute(
                    f"SELECT DISTINCT site_id FROM site_queue WHERE opened_at IS NULL AND cancelled_at IS NULL "
                    f"AND site_id IN ({marks})", chunk)
            }
            # A site's channel lives in its company's Discord server, and a
            # channel can't move between servers — so once a site has one, it
            # stays with that company.
            in_discord = {
                r["site_id"] for r in conn.execute(
                    f"SELECT site_id FROM site_channels WHERE site_id IN ({marks})", chunk)
            }
            ok = []
            for site_id in chunk:
                if site_id not in current:
                    skipped.append((site_id, "not in the inventory"))
                elif current[site_id] != company_id and site_id in in_discord:
                    skipped.append((site_id, "it already has a Discord channel in its company's server"))
                elif current[site_id] is not None and current[site_id] != company_id and site_id in busy:
                    skipped.append((site_id, "technicians are still assigned"))
                else:
                    ok.append(site_id)
            if ok:
                ok_marks = ",".join("?" for _ in ok)
                conn.execute(
                    f"UPDATE sites SET company_id = ?, target_install_date = COALESCE(?, target_install_date), "
                    f"updated_at = ? WHERE site_id IN ({ok_marks})",
                    (company_id, target_date, now, *ok),
                )
                assigned.extend(ok)
    return assigned, skipped


def set_target_install_date(site_id, iso_date):
    """iso_date is 'YYYY-MM-DD', or None to clear it."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE sites SET target_install_date = ?, updated_at = ? WHERE site_id = ?",
            (iso_date, time.time(), site_id),
        )


def apply_site_upload(rows, company_id, actor, filename=None):
    """Takes the rows of a filled-in site template into the inventory for one
    company, in a single transaction. Per site:

        not in the inventory       -> added, for this company
        this company's already     -> its details refreshed (blank cells keep what's there)
        in the inventory, nobody's -> this company takes it
        another company's          -> skipped, never touched

    Two more refusals, both because a Discord channel can't move between
    servers: a nobody's site that already has a channel isn't taken, and a
    site with a channel keeps its region. company_id=None is HQ loading
    inventory only: new sites arrive unallocated, and existing sites get their
    details refreshed without any change of owner.

    Returns {"added", "updated", "claimed", "skipped": [(site_id, why)]}.
    """
    now = time.time()
    added = updated = claimed = 0
    skipped = []
    with get_conn() as conn:
        # One writer from the first read to the last write: a site another
        # company takes in the meantime can't be claimed twice.
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            site_id = row["site_id"]
            current = conn.execute(
                "SELECT s.company_id, s.region, sc.site_id IS NOT NULL AS in_discord "
                "FROM sites s LEFT JOIN site_channels sc ON sc.site_id = s.site_id WHERE s.site_id = ?",
                (site_id,),
            ).fetchone()
            planned = row.get("target_install_date") or None

            if current is None:
                columns = ", ".join(SITE_COLUMNS)
                marks = ", ".join("?" for _ in SITE_COLUMNS)
                conn.execute(
                    f"INSERT INTO sites (site_id, {columns}, company_id, target_install_date, "
                    f"uploaded_by, created_at, updated_at) VALUES (?, {marks}, ?, ?, ?, ?, ?)",
                    (site_id, *[(row.get(c) or "") for c in SITE_COLUMNS], company_id, planned, actor, now, now),
                )
                added += 1
                continue

            owner = current["company_id"]
            if company_id is not None and owner is not None and owner != company_id:
                skipped.append((site_id, "belongs to another company"))
                continue
            if company_id is not None and owner is None and current["in_discord"]:
                skipped.append((site_id, "already has a Discord channel — ask HQ"))
                continue
            new_region = (row.get("region") or "").strip()
            if (current["in_discord"] and new_region
                    and new_region.lower() != (current["region"] or "").strip().lower()):
                skipped.append((site_id, f"already has a Discord channel in the {current['region']} server, "
                                         "so its region can't change"))
                continue

            changes = {c: row[c] for c in SITE_COLUMNS if (row.get(c) or "").strip()}
            if planned:
                changes["target_install_date"] = planned
            if company_id is not None and owner is None:
                changes["company_id"] = company_id
                claimed += 1
            else:
                updated += 1
            changes["uploaded_by"] = actor
            changes["updated_at"] = now
            assignments = ", ".join(f"{c} = ?" for c in changes)
            conn.execute(f"UPDATE sites SET {assignments} WHERE site_id = ?", (*changes.values(), site_id))

        conn.execute(
            "INSERT INTO site_imports (company_id, filename, added, updated, claimed, skipped, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, (filename or "")[:200], added, updated, claimed, len(skipped), actor, now),
        )
    return {"added": added, "updated": updated, "claimed": claimed, "skipped": skipped}


def recent_site_imports(company_id=None, limit=8):
    """The latest uploads — every company's for HQ (company_id None), or one
    company's own."""
    sql = ("SELECT i.*, c.name AS company_name FROM site_imports i "
           "LEFT JOIN companies c ON c.id = i.company_id")
    params = []
    if company_id is not None:
        sql += " WHERE i.company_id = ?"
        params.append(company_id)
    with get_conn() as conn:
        return _many(conn.execute(sql + " ORDER BY i.id DESC LIMIT ?", (*params, limit)).fetchall())


# --- regions -------------------------------------------------------------

def all_regions():
    with get_conn() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM regions ORDER BY name").fetchall()]


def add_region(name, created_by=None):
    """True if it was new. Case-insensitive, so 'north' can't sit beside 'North'."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO regions (name, created_by, created_at) VALUES (?, ?, ?)",
            (" ".join(name.split()), created_by, time.time()),
        )
        return cur.rowcount == 1


# --- change fingerprint (live pages) ---------------------------------------

def change_fingerprint(company_id=None):
    """A short string that changes whenever anything a page could show
    changes — for everyone (company_id None, HQ) or for one company's sites
    only. Pages poll it and refresh their live parts when it moves.

    Scoped per company deliberately: another company's activity must not even
    make this company's pages blink, let alone show up on them."""
    import hashlib

    if company_id is None:
        site_scope, params = "", ()
        own = ""
    else:
        site_scope = " WHERE site_id IN (SELECT site_id FROM sites WHERE company_id = ?)"
        params = (company_id,)
        own = " WHERE company_id = ?"
    queries = (
        (f"SELECT COUNT(*), MAX(message_id) FROM messages{site_scope}", params),
        (f"SELECT COUNT(*), MAX(flagged_at), MAX(resolved_at), SUM(status = 'open') FROM blockers{site_scope}", params),
        (f"SELECT COUNT(*), MAX(updated_at) FROM site_progress{site_scope}", params),
        (f"SELECT COUNT(*), MAX(added_at), MAX(removed_at), SUM(removed_at IS NULL) FROM site_techs{site_scope}", params),
        (f"SELECT COUNT(*), MAX(created_at), MAX(opened_at), MAX(cancelled_at), MAX(last_try_at) "
         f"FROM site_queue{site_scope}", params),
        (f"SELECT COUNT(*), MAX(created_at), SUM(brief IS NOT NULL) FROM site_channels{site_scope}", params),
        (f"SELECT COUNT(*), MAX(id), SUM(tag IS NOT NULL) FROM attachments{site_scope}", params),
        (f"SELECT COUNT(*), MAX(updated_at), MAX(target_install_date) FROM sites{own}", params),
        (f"SELECT COUNT(*), MAX(created_at), MAX(removed_at), SUM(discord_id IS NOT NULL) FROM technicians{own}", params),
        (f"SELECT COUNT(*), MAX(id) FROM site_imports{own}", params),
        ("SELECT COUNT(*), MAX(e.id), SUM(e.status = 'sent') FROM emails e JOIN technicians t ON t.id = e.technician_id"
         + ("" if company_id is None else " WHERE t.company_id = ?"), params),
        ("SELECT COUNT(*), MAX(j.id), MAX(j.used_at) FROM join_links j JOIN technicians t ON t.id = j.technician_id"
         + ("" if company_id is None else " WHERE t.company_id = ?"), params),
    )
    if company_id is None:
        queries += (
            ("SELECT COUNT(*), MAX(created_at) FROM companies", ()),
            ("SELECT COUNT(*), MAX(updated_at) FROM guilds", ()),
            ("SELECT COUNT(*), MAX(created_at), MAX(removed_at), SUM(discord_id IS NOT NULL) FROM staff", ()),
            ("SELECT COUNT(*), SUM(ok), MAX(checked_at) FROM guild_staff", ()),
        )
    with get_conn() as conn:
        state = [tuple(conn.execute(sql, args).fetchone()) for sql, args in queries]
    state.append(bot_online())
    return hashlib.sha1(repr(state).encode()).hexdigest()[:16]


# --- technicians (a company's roster) -----------------------------------

def create_technician(company_id, name, phone, email=None, created_by=None, country="PK"):
    """`phone` as typed; it's stored in international form for `country` (a
    number typed with its own +code wins). Raises ValueError with a message
    for a person if it isn't a mobile number, and sqlite3.IntegrityError if
    the number already belongs to an active technician.

    Which Discord account is theirs is never guessed from the number: it's
    whatever account they connect through their own link."""
    from utils.phone import normalize

    e164, key, region = normalize(phone, country)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO technicians
                (company_id, name, email, phone, phone_key, country, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (company_id, name.strip(), (email or "").strip() or None, e164, key, region, created_by, time.time()),
        )
        return cur.lastrowid


_TECH_SELECT = """
    SELECT t.*, c.name AS company_name
    FROM technicians t LEFT JOIN companies c ON c.id = t.company_id
"""


def get_technician(technician_id):
    with get_conn() as conn:
        return _one(conn.execute(_TECH_SELECT + " WHERE t.id = ?", (technician_id,)).fetchone())


def technicians_for_company(company_id, include_removed=False):
    sql = _TECH_SELECT + " WHERE t.company_id = ?"
    if not include_removed:
        sql += " AND t.removed_at IS NULL"
    with get_conn() as conn:
        return _many(conn.execute(sql + " ORDER BY t.name", (company_id,)).fetchall())


def all_technicians(include_removed=False):
    sql = _TECH_SELECT
    if not include_removed:
        sql += " WHERE t.removed_at IS NULL"
    with get_conn() as conn:
        return _many(conn.execute(sql + " ORDER BY c.name, t.name").fetchall())


def find_technician_by_phone_key(key):
    with get_conn() as conn:
        return _one(conn.execute(
            _TECH_SELECT + " WHERE t.phone_key = ? AND t.removed_at IS NULL", (key,)
        ).fetchone())


def technician_by_discord_id(discord_id):
    with get_conn() as conn:
        return _one(conn.execute(
            _TECH_SELECT + " WHERE t.discord_id = ? AND t.removed_at IS NULL", (discord_id,)
        ).fetchone())


def link_technician_discord_id(technician_id, discord_id):
    """Records which Discord account is this technician's. Only ever fills a
    blank: False means they were already linked (a second server, same
    account — fine) or that this account is already someone else's."""
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE technicians SET discord_id = ? WHERE id = ? AND discord_id IS NULL",
                (discord_id, technician_id),
            )
            return cur.rowcount > 0
    except sqlite3.IntegrityError:
        return False


def remove_technician(technician_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE technicians SET removed_at = ? WHERE id = ? AND removed_at IS NULL",
            (time.time(), technician_id),
        )


def assigned_sites_for_technician(discord_id):
    """Sites this account currently holds, with what the roster page needs."""
    with get_conn() as conn:
        return _many(conn.execute(
            """
            SELECT st.site_id, st.added_at, sc.city, sc.guild_id, sc.channel_id,
                   COALESCE(p.state, 'not_started') AS state
            FROM site_techs st
            JOIN site_channels sc ON sc.site_id = st.site_id
            LEFT JOIN site_progress p ON p.site_id = st.site_id
            WHERE st.user_id = ? AND st.removed_at IS NULL
            ORDER BY st.added_at DESC
            """,
            (discord_id,),
        ).fetchall())


def active_site_count_in_guild(user_id, guild_id):
    """How many sites, in this one server, the account still holds. The
    Technician role is per server, so 'no sites left' has to mean here."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) n FROM site_techs st
            JOIN site_channels sc ON sc.site_id = st.site_id
            WHERE st.user_id = ? AND st.removed_at IS NULL AND sc.guild_id = ?
            """,
            (user_id, guild_id),
        ).fetchone()
        return row["n"]


def active_tech_counts():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT site_id, COUNT(*) n FROM site_techs WHERE removed_at IS NULL GROUP BY site_id"
        ).fetchall()
        return {r["site_id"]: r["n"] for r in rows}


def technician_load():
    """(sites held per Discord account, sites still waiting per technician)."""
    with get_conn() as conn:
        held = {
            r["user_id"]: r["n"] for r in conn.execute(
                "SELECT user_id, COUNT(*) n FROM site_techs WHERE removed_at IS NULL GROUP BY user_id")
        }
        waiting = {
            r["technician_id"]: r["n"] for r in conn.execute(
                "SELECT technician_id, COUNT(*) n FROM site_queue "
                "WHERE opened_at IS NULL AND cancelled_at IS NULL GROUP BY technician_id")
        }
    return held, waiting


def site_tech_rows(site_ids):
    """Every assignment ever made on these sites, removed ones included —
    performance counts sites a technician finished, not sites they hold."""
    rows = []
    with get_conn() as conn:
        for chunk in _chunks(site_ids):
            marks = ",".join("?" for _ in chunk)
            rows += _many(conn.execute(
                f"SELECT site_id, user_id, removed_at FROM site_techs WHERE site_id IN ({marks})", chunk
            ).fetchall())
    return rows


# --- hermes rules --------------------------------------------------------

RULE_KINDS = ("keyword", "negation", "pattern")


def list_hermes_rules(active_only=False):
    sql = "SELECT * FROM hermes_rules"
    if active_only:
        sql += " WHERE active = 1"
    with get_conn() as conn:
        return _many(conn.execute(sql + " ORDER BY kind, id").fetchall())


def add_hermes_rule(kind, value, created_by):
    """Adds a rule, or switches an existing one back on. Returns True if
    anything changed."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, active FROM hermes_rules WHERE kind = ? AND value = ?", (kind, value)
        ).fetchone()
        if row:
            if row["active"]:
                return False
            conn.execute("UPDATE hermes_rules SET active = 1 WHERE id = ?", (row["id"],))
            return True
        conn.execute(
            "INSERT INTO hermes_rules (kind, value, active, created_by, created_at) VALUES (?, ?, 1, ?, ?)",
            (kind, value, created_by, time.time()),
        )
        return True


def set_hermes_rule_active(rule_id, active):
    with get_conn() as conn:
        conn.execute("UPDATE hermes_rules SET active = ? WHERE id = ?", (int(active), rule_id))


# --- bot_meta ------------------------------------------------------------

def set_meta(key, value):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bot_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(value)),
        )


def get_meta(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
