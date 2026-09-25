"""The message a technician sees when a site channel opens up for them.

One builder, two callers: the bot posts it when someone joins through an
invite (cogs/onboarding.py), and the portal posts it when an existing
member is handed another site (portal/assign.py). Either way it lands at
the bottom of the channel, where they're looking — not in a pin they'd have
to scroll up and hunt for.
"""

DISCORD_MESSAGE_LIMIT = 2000


def build_greeting(mention: str, site_id: str, brief: str | None) -> str:
    greeting = (
        f"\U0001f44b {mention} — **{site_id}**\n"
        "Post photos, videos and voice notes here as you work. "
        "Kaam ke dauran yahan photo, video aur voice note bhejein."
    )
    text = f"{greeting}\n\n{brief}" if brief else greeting
    # A long brief must never make Discord reject the whole welcome.
    return text[: DISCORD_MESSAGE_LIMIT - 10]
