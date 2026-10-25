"""
Discord bot with three features:
    !rules               reposts the server's rules message as an embed
    music                plays songs from a Navidrome library in voice (!play, !search, !skip, ...)
    Pokémon game         ;pokemon catches one every 90 minutes; inventory, Pokédex and trading

Every command works with the ! prefix, the ; prefix, and as a slash command.

Setup:
    uv venv && uv pip install -r requirements.txt
        (or: python -m venv .venv && .venv/bin/pip install -r requirements.txt)
    Install ffmpeg and make sure it's on your PATH (music only).
    In the developer portal (Bot tab) enable "Message Content Intent".
    Invite the bot with the "bot" and "applications.commands" scopes and the permissions View Channels,
    Send Messages, Embed Links, Read Message History, Connect and Speak.

Run:
    .venv/bin/python bot.py

Settings are environment variables, or lines in a .env file next to this one (see .env.example):
    DISCORD_TOKEN      bot token from the Discord developer portal
    GUILD_ID           optional: your server's ID, makes slash commands appear instantly
    NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASS       optional: turn on music (cogs/music.py)
    RULES_CHANNEL_ID, RULES_MESSAGE_ID                  optional: which message !rules reposts (cogs/rules.py)
    POKEMON_DB         optional: where the Pokémon game keeps its data (cogs/pokemon.py)
"""

import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from common import UserError
from config import env_int, load_env_file

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
load_env_file(HERE / ".env")

COGS = ["cogs.rules", "cogs.music", "cogs.pokemon"]


def root_cause(error: Exception) -> Exception:
    """Peel off the wrappers discord.py puts around whatever a command actually raised."""
    while isinstance(error, (commands.CommandInvokeError, commands.HybridCommandError, app_commands.CommandInvokeError)):
        error = error.original
    return error


class Bot(commands.Bot):
    async def setup_hook(self) -> None:
        for name in COGS:
            try:
                await self.load_extension(name)
            except commands.ExtensionFailed as e:
                sys.exit(f"Couldn't load {name}: {e.original}")
        await self.sync_slash_commands()

    async def sync_slash_commands(self) -> None:
        guild_id = env_int("GUILD_ID")
        try:
            if guild_id:
                # Guild commands show up instantly; global ones can take up to an hour.
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
        except discord.HTTPException as e:
            print(f"Couldn't register slash commands ({e}). The ! and ; commands still work.")

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user}")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        error = root_cause(error)
        if isinstance(error, commands.CommandNotFound):
            return  # someone typed ";)" or "!!!"
        usage = f"`{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`" if ctx.command else ""
        if isinstance(error, UserError):
            message = f"⚠️ {error}"
        elif isinstance(error, commands.NoPrivateMessage):
            message = "That command only works in a server."
        elif isinstance(error, commands.MissingRequiredArgument):
            message = f"You're missing `{error.param.name}`. Usage: {usage}"
        elif isinstance(error, commands.TooManyArguments):
            message = f"That's too many arguments. Usage: {usage}"
        elif isinstance(error, commands.UserInputError):
            message = f"{error} Usage: {usage}"
        elif isinstance(error, commands.CheckFailure):
            message = "You can't use that command here."
        elif isinstance(error, discord.Forbidden):
            message = "I don't have permission to do that here."
        else:
            log.error("Error in command %s", ctx.command, exc_info=error)
            message = "Something went wrong running that command."
        try:
            await ctx.send(message, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN isn't set. Put it in a .env file next to bot.py (see .env.example) or export it.")
    intents = discord.Intents.default()
    intents.message_content = True  # needed for ! and ; commands; also switch it on in the developer portal
    bot = Bot(
        command_prefix=commands.when_mentioned_or("!", ";"),
        intents=intents,
        case_insensitive=True,
        allowed_mentions=discord.AllowedMentions.none(),  # nothing pings unless a command asks to
    )
    bot.run(token)


if __name__ == "__main__":
    main()
