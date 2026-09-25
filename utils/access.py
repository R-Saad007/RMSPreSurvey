"""What a technician may do in their site channel, as Discord permission bits.

Two modes, one for each state of the site survey:

    writable    while the survey is open: see, read the history, post, attach, react
    read-only   once it's complete: see and read the history, nothing else

Both are explicit allow AND deny bits. A bit that's merely left out of
"allow" is inherited, and @everyone's server-wide permissions include posting
— so read-only has to deny posting outright, not just not allow it.

Technicians can't start threads in either mode: a message in a thread isn't in
the site channel, so the archive and blocker detection would never see it.
"""
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
SEND_TTS_MESSAGES = 1 << 12
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_MESSAGE_HISTORY = 1 << 16
MENTION_EVERYONE = 1 << 17
ADD_REACTIONS = 1 << 6
USE_APPLICATION_COMMANDS = 1 << 31
CREATE_PUBLIC_THREADS = 1 << 35
CREATE_PRIVATE_THREADS = 1 << 36
SEND_MESSAGES_IN_THREADS = 1 << 38
SEND_VOICE_MESSAGES = 1 << 46
SEND_POLLS = 1 << 49
USE_EXTERNAL_APPS = 1 << 50

WRITABLE = "writable"
READ_ONLY = "read_only"

_THREADS = CREATE_PUBLIC_THREADS | CREATE_PRIVATE_THREADS

MODES = {
    WRITABLE: (VIEW_CHANNEL | SEND_MESSAGES | ADD_REACTIONS | ATTACH_FILES | READ_MESSAGE_HISTORY, _THREADS),
    READ_ONLY: (
        VIEW_CHANNEL | READ_MESSAGE_HISTORY,
        SEND_MESSAGES | SEND_TTS_MESSAGES | EMBED_LINKS | ATTACH_FILES | ADD_REACTIONS | MENTION_EVERYONE
        | USE_APPLICATION_COMMANDS | _THREADS | SEND_MESSAGES_IN_THREADS | SEND_VOICE_MESSAGES | SEND_POLLS
        | USE_EXTERNAL_APPS,
    ),
}


def overwrite_for(mode: str) -> tuple[int, int]:
    """(allow, deny) for a technician's overwrite in this mode."""
    return MODES[mode]


def mode_of(allow: int, deny: int) -> str | None:
    """Which mode an existing overwrite amounts to — judged by meaning, not
    exact bits, so an older overwrite (the pilot's, without the thread deny)
    still reads as writable. None: it doesn't let them see the channel."""
    if deny & SEND_MESSAGES and allow & VIEW_CHANNEL:
        return READ_ONLY
    if allow & VIEW_CHANNEL:
        return WRITABLE
    return None
