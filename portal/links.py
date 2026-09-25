"""Each person's one link, and how it reaches them.

Every technician (and every HQ staff member) gets a personal link to the
portal: /j/<token>. It shows who it's for and which sites are theirs, and has
one button — Connect Discord. Connecting once is all it ever takes: from then
on the bot puts them into every server their sites are in, and after they've
connected the same link is their page of sites, a button per channel.

The link goes out by email on its own when we have an address, and by
WhatsApp with one tap: the portal can't send WhatsApp messages itself, so
the button opens WhatsApp on the operator's phone or computer with the number
and the message already filled in.
"""
import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

from config import CONFIG
from storage import db
from utils.phone import wa_digits

_ONE_LINE = re.compile(r"[\r\n]+")


def link_url(token: str) -> str:
    return f"{CONFIG.portal_base_url.rstrip('/')}/j/{token}"


def first_name(name: str) -> str:
    return (name or "").split()[0] if (name or "").split() else ""


def _site_list(site_ids, most=12) -> str:
    ids = list(site_ids)
    return ", ".join(ids[:most]) + (f" and {len(ids) - most} more" if len(ids) > most else "")


def whatsapp_text(tech: dict, company: str, site_ids, token: str, connected: bool) -> str:
    """The message the operator sends. Roman Urdu first — it's how the crews
    write — then the same in English."""
    who, sites, url = first_name(tech["name"]), _site_list(site_ids), link_url(token)
    if not site_ids:
        return (f"Assalam o Alaikum {who}, {company} ke RMS sites ke liye yeh link kholein aur "
                f"'Connect Discord' dabayein (sirf ek dafa): {url}\n"
                f"For your RMS sites with {company}, open this link and tap Connect Discord — once: {url}")
    if connected:
        return (f"Assalam o Alaikum {who}, {company} ne aap ko nayi sites di hain: {sites}. "
                f"Discord mein khulne ke liye: {url}\n"
                f"New sites from {company}: {sites}. Open them here: {url}")
    return (f"Assalam o Alaikum {who}, {company} ne aap ko Discord par sites di hain: {sites}. "
            f"Yeh link kholein aur 'Connect Discord' dabayein (sirf ek dafa): {url}\n"
            f"{company} has given you sites on Discord: {sites}. Open this link and tap Connect Discord — once: {url}")


def whatsapp_url(phone_e164: str, text: str) -> str:
    """wa.me opens a chat with this number, message filled in. Built here, never
    in a template: a name typed by a company manager must not reach JavaScript."""
    return f"https://wa.me/{wa_digits(phone_e164)}?text={quote(text, safe='')}"


def email_for(person: dict, company: str, site_ids, token: str, connected: bool) -> tuple[str, str]:
    """(subject, body)."""
    sites, url, who = _site_list(site_ids, most=40), link_url(token), first_name(person["name"])
    if not site_ids:
        subject = f"Connect Discord for your RMS sites ({company})"
        body = (f"Hi {who},\n\n{company} will give you RMS sites on Discord. Open this link and tap "
                f"Connect Discord — once. Every site you're given then opens by itself:\n{url}\n\n"
                f"Assalam o Alaikum {who}, yeh link kholein aur 'Connect Discord' dabayein. Sirf ek dafa.\n")
        return _ONE_LINE.sub(" ", subject)[:200], body
    if connected:
        subject = f"New RMS sites from {company}"
        body = (f"Hi {who},\n\n{company} has given you these sites: {sites}.\n\n"
                f"They open in Discord on their own; you'll get a message in each one. "
                f"Your page, with a button for each site:\n{url}\n\n"
                f"Assalam o Alaikum {who}, {company} ne aap ko nayi sites di hain. Upar wala link kholein.\n")
    else:
        subject = f"Connect Discord for your RMS sites ({company})"
        body = (f"Hi {who},\n\n{company} has given you sites on Discord: {sites}.\n\n"
                f"Open this link and tap Connect Discord. You only do this once — every site "
                f"you're given later opens by itself:\n{url}\n\n"
                f"If you don't have Discord yet, install it first (it's free) and make an account.\n\n"
                f"Assalam o Alaikum {who}, yeh link kholein aur 'Connect Discord' dabayein. Sirf ek dafa.\n")
    return _ONE_LINE.sub(" ", subject)[:200], body


def staff_email(person: dict, token: str) -> tuple[str, str]:
    who, url = first_name(person["name"]), link_url(token)
    subject = "Connect your Discord to the RMS site servers"
    body = (f"Hi {who},\n\nYou've been added to HQ's team on the RMS site servers "
            f"({person['discord_role']}). Open this link and tap Connect Discord — once. You'll be put "
            f"into every site server, now and as new ones are made:\n{url}\n")
    return subject, body


def _send_now(to_addr: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = CONFIG.smtp_from
    message["To"] = to_addr
    message["Subject"] = _ONE_LINE.sub(" ", subject)
    message.set_content(body)
    context = ssl.create_default_context()
    if CONFIG.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(CONFIG.smtp_host, CONFIG.smtp_port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(CONFIG.smtp_host, CONFIG.smtp_port, timeout=20)
        server.starttls(context=context)
    with server:
        server.login(CONFIG.smtp_user, CONFIG.smtp_password)
        server.send_message(message)


async def deliver(email_id: int, to_addr: str, subject: str, body: str) -> None:
    """Sends an email logged with db.log_email and records how it went. Runs
    after the page has answered (FastAPI BackgroundTasks), in a worker thread:
    a slow mail server never holds up the person who pressed Assign."""
    try:
        await asyncio.to_thread(_send_now, to_addr, subject, body)
    except Exception as exc:                            # noqa: BLE001 — any failure is shown, not raised
        db.set_email_status(email_id, "failed", f"{type(exc).__name__}: {exc}")
        return
    db.set_email_status(email_id, "sent")


def queue_email(background, to_addr: str, subject: str, body: str, actor: str,
                technician_id=None, staff_id=None) -> int | None:
    """Logs the email and hands it to the background. None if email isn't set
    up on this server (the WhatsApp button still works)."""
    if not CONFIG.email_ready() or not to_addr:
        return None
    email_id = db.log_email(to_addr, subject, actor, technician_id=technician_id, staff_id=staff_id)
    background.add_task(deliver, email_id, to_addr, subject, body)
    return email_id
