"""Small things shared by the bot and its cogs."""

import discord
from discord.ext import commands


class UserError(commands.CommandError):
    """A problem the user can fix. The bot shows the message to them as-is."""


def clean(text: str) -> str:
    """Make user-supplied text (names, song titles) safe to drop into a message: no formatting, no pings."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
