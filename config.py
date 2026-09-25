"""
Central config loader for both the bot and the portal. Everything reads
from environment variables (via .env in development, real env vars on the
VM). Nothing else in this codebase should call os.environ directly.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


@dataclass(frozen=True)
class Config:
    discord_token: str = os.getenv("DISCORD_BOT_TOKEN", "")

    # --- Discord sign-in (OAuth) -------------------------------------------
    # The bot's own application is also what people connect their Discord
    # account through: once connected, the bot can add them to every server
    # their work is in, and an HQ admin adds the bot to a new server through
    # it too. The client secret is set on the VM with tools/set_secret.py.
    discord_client_id: str = os.getenv("DISCORD_CLIENT_ID", "")
    discord_client_secret: str = os.getenv("DISCORD_CLIENT_SECRET", "")

    # --- Servers ---------------------------------------------------------
    # One Discord server per company and region ("FieldCo_South"). Discord
    # caps a server at 500 channels, categories included, and 50 channels per
    # category; 450 sites in 9 categories stays well clear of both.
    sites_per_server: int = _int("SITES_PER_SERVER", 450)

    # --- Roles (functional only; access to a site is a per-channel
    # overwrite, never a per-site role — Discord caps a guild at 250) ---
    management_role_name: str = os.getenv("MANAGEMENT_ROLE_NAME", "Management")
    engineer_role_name: str = os.getenv("ENGINEER_ROLE_NAME", "Engineer")
    coordinator_role_name: str = os.getenv("COORDINATOR_ROLE_NAME", "Coordinator")
    technician_role_name: str = os.getenv("TECHNICIAN_ROLE_NAME", "Technician")

    # --- Server layout ---------------------------------------------------
    # A server holds site categories and site channels. Nothing else: a
    # technician lands in their channel and sees only that.
    access_thread_name: str = os.getenv("ACCESS_THREAD_NAME", "\U0001f512 access & config")
    site_category_size: int = _int("SITE_CATEGORY_SIZE", 50)
    channel_count_warn_at: int = _int("CHANNEL_COUNT_WARN_AT", 400)
    channel_count_ceiling: int = _int("CHANNEL_COUNT_CEILING", 480)

    # --- Blur check (Laplacian variance; lower = blurrier) ---------------
    blur_variance_threshold: float = _float("BLUR_VARIANCE_THRESHOLD", 100.0)

    # --- Personal links ----------------------------------------------------
    # How long a person's link can be used to connect Discord for the first
    # time. Once they've connected it stays their page of sites for good,
    # usable only by that same Discord account.
    join_link_days: int = _int("JOIN_LINK_DAYS", 14)

    # --- Email (the link goes out by email when we have an address) ------
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_security: str = os.getenv("SMTP_SECURITY", "starttls")    # starttls (587) or ssl (465)

    # --- Portal ----------------------------------------------------------
    portal_secret_key: str = os.getenv("PORTAL_SECRET_KEY", "")
    # Every link that leaves the portal (emails, WhatsApp, Discord's sign-in
    # redirect) is built from this, never from the request's Host header.
    portal_base_url: str = os.getenv("PORTAL_BASE_URL", "http://localhost:8000")
    portal_host: str = os.getenv("PORTAL_HOST", "127.0.0.1")
    portal_port: int = _int("PORTAL_PORT", 8000)

    # --- Storage ---------------------------------------------------------
    timezone_name: str = os.getenv("TIMEZONE_NAME", "Asia/Karachi")
    storage_root: str = os.getenv("STORAGE_ROOT", "storage/sites")
    db_path: str = os.getenv("DB_PATH", "rms_bot.sqlite3")

    def email_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.smtp_user and self.smtp_password)

    def discord_login_ready(self) -> bool:
        return bool(self.discord_client_id and self.discord_client_secret)


CONFIG = Config()

TAG_LABELS = {
    "grid_meter": "Grid Meter",
    "gen_meter": "Gen Meter",
    "rectifier": "Rectifier",
    "idmu": "iDMU",
    "dc_breaker": "DC Breaker",
    "gen_control": "Gen Control",
    "alarms": "Alarms",
    "sim": "SIM",
    "other": "Other",
}

# caption keyword (lowercased) -> tag key, checked as a substring match.
# Anything this can't classify is tagged by staff in the portal — the
# technician is never asked.
CAPTION_KEYWORDS = {
    "grid meter": "grid_meter",
    "gen meter": "gen_meter",
    "generator meter": "gen_meter",
    "rectifier": "rectifier",
    "idmu": "idmu",
    "dc breaker": "dc_breaker",
    "gen control": "gen_control",
    "generator control": "gen_control",
    "alarm": "alarms",
    "sim": "sim",
}

# Best-effort bridge between the inventory columns (what the customer says
# is on site, and what's to be installed) and the tags a technician's photos
# end up with. Only items with a defensible match are listed; anything else
# is shown as "no tag to check" rather than guessed at — the tag vocabulary
# was built for photographing existing equipment, so most of the BOQ has no
# tag at all. (regex on the lowercased item label, tags that count as proof)
INVENTORY_TAG_HINTS = (
    (r"rectifier", ("rectifier",)),
    (r"\bidmu\b", ("idmu",)),
    (r"\bac (?:energy )?meter", ("grid_meter", "gen_meter")),
    (r"^dg\d", ("gen_control", "gen_meter")),
)

# --- Blocker detection ---------------------------------------------------
# Nobody files a blocker; the bot reads the conversation and flags it. The
# field crew writes English and Roman Urdu interchangeably, so both are
# matched. Tuned against a real site's WhatsApp thread — extend it from
# live traffic rather than from imagination.

BLOCKER_KEYWORDS = (
    # English — explicit trouble words
    "no power", "no supply", "no access", "no signal", "no network",
    "issue", "problem", "blocked", "blocker", "stuck", "fault", "faulty",
    "error", "fail", "damaged", "broken", "missing", "dead", "offline",
    "tripped", "leakage", "short circuit", "urgent", "help needed",
    "still waiting", "not the same", "mismatch", "wrong reading",
    # Roman Urdu
    "masla", "kharab", "nahi ho raha", "nahi horaha", "nahi chal raha",
    "nahi chalra", "nahi mil raha", "nahi milra", "nahi aa raha",
    "nahi aara", "band hai", "band hy", "bijli nahi", "light nahi",
    "madad", "taala", "lock hai", "kaam nahi", "chal nahi raha",
    "nahi ho rha", "khatam", "blur",
)

# The real ISB0001 thread phrases problems as negated verbs far more often
# than as trouble words — "still huawei is not readable in putty", "data is
# not walking in mib", "is it not accessible?". A keyword list alone caught
# 1 message in 167; these patterns are what actually match how the crew
# writes. Verbs are restricted to the ones that appear in practice, because
# a bare "not \w+" flags half the conversation.
BLOCKER_PATTERNS = (
    r"\b(?:not|no|isn'?t|aren'?t|does\s?n'?t|do\s?n'?t|did\s?n'?t|"
    r"wo\s?n'?t|ca\s?n'?t|cannot|unable\s+to|failed\s+to)\s+\w*"
    r"(?:work|read|access|reach|ping|respond|detect|show|walk|connect|"
    r"start|open|avail|boot|sync|updat|load|regist|communicat)",
)

# Checked first: if any of these appear, the message is NOT a blocker even
# if a keyword above also matches ("koi masla nahi" contains "masla").
BLOCKER_NEGATIONS = (
    "no problem", "no issue", "not a problem", "not an issue",
    "masla nahi", "koi masla nahi", "issue nahi", "problem nahi",
    "theek hai", "theek hy", "thik hai", "sab theek", "sab thik",
    "ho gaya", "hogaya", "ho gya", "done", "resolved", "fixed", "sorted",
    "working fine", "all good", "ok hai", "okay hai",
)

# Reacting with any of these on a flagged message closes the blocker,
# which costs a tap and needs no button in the channel.
BLOCKER_RESOLVE_EMOJI = ("✅", "✔️", "✔", "\U0001f44d")

PROGRESS_LABELS = {
    "not_started": "Not started",
    "on_site": "On site",
    "blocked": "Blocked",
    "done": "Done",
}
