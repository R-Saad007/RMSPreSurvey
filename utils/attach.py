"""Attachment capture: download every file the moment it's posted, because
Discord's CDN links expire after about a day.

Photos, videos, documents and voice notes all land here — a voice note is
an ordinary audio attachment, and the blur check skips anything that isn't
an image. The technician is never asked anything: captions are matched to
a tag automatically, and whatever can't be classified gets tagged by staff
in the portal.

Shared by the live listener (cogs/capture.py) and the startup backfill so
a message caught either way is handled identically.
"""
import re
from datetime import timezone
from pathlib import Path

import discord

from config import CAPTION_KEYWORDS, CONFIG
from storage import db
from utils.blur import is_blurry

BOTH_LANGUAGES_BLURRY = "This looks blurry — please retake. / Yeh dhundli lag rahi hai, dobara lein."


def guess_tag(caption: str) -> str | None:
    text = (caption or "").lower()
    for keyword, tag in CAPTION_KEYWORDS.items():
        if keyword in text:
            return tag
    return None


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def already_archived(channel_id: int, message_id: int) -> bool:
    return message_id in db.known_message_ids(channel_id)


async def archive_attachment(message: discord.Message, attachment: discord.Attachment, site_id: str, tag: str | None) -> tuple[str, bool]:
    date_str = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    tag_part = tag or "untagged"
    filename = safe_filename(attachment.filename)
    directory = Path(CONFIG.storage_root) / site_id / date_str
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tag_part}_{message.id}_{filename}"

    data = await attachment.read()
    path.write_bytes(data)

    blurry = False
    if (attachment.content_type or "").startswith("image/"):
        blurry, _variance = is_blurry(data)

    db.log_attachment(message.id, message.channel.id, site_id, message.author.id, filename, str(path), tag, blurry)
    return str(path), blurry


def rename_tagged_files(message_id: int, new_tag: str) -> None:
    """Called when staff tag a photo in the portal — renames the untagged_
    files on disk and updates the DB rows to match."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, path FROM attachments WHERE message_id = ?", (message_id,)
        ).fetchall()
        for row in rows:
            old = Path(row["path"])
            if old.exists() and old.name.startswith("untagged_"):
                new_path = old.with_name(old.name.replace("untagged_", f"{new_tag}_", 1))
                old.rename(new_path)
                conn.execute("UPDATE attachments SET path = ? WHERE id = ?", (str(new_path), row["id"]))
    db.set_attachment_tag(message_id, new_tag)


async def archive_message_attachments(message: discord.Message, site_id: str) -> bool:
    """Archives every attachment on `message`. Returns True if any image
    came out blurry. Safe to call twice (live listener + backfill)."""
    if not message.attachments:
        return False
    if already_archived(message.channel.id, message.id):
        return False

    tag = guess_tag(message.content)
    any_blurry = False
    for attachment in message.attachments:
        _path, blurry = await archive_attachment(message, attachment, site_id, tag)
        any_blurry = any_blurry or blurry
    return any_blurry
