"""
Rules cog: !rules reposts the server's existing rules message as an embed.

Environment variables:
    RULES_CHANNEL_ID   optional: channel holding the rules (default: a channel named "rules")
    RULES_MESSAGE_ID   optional: the specific rules message (default: oldest message in that channel)
"""

import discord
from discord.ext import commands

from config import env_int

RULES_CHANNEL_ID = env_int("RULES_CHANNEL_ID")
RULES_MESSAGE_ID = env_int("RULES_MESSAGE_ID")


def find_rules_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if RULES_CHANNEL_ID:
        channel = guild.get_channel(RULES_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            return channel
    return discord.utils.find(lambda c: "rules" in c.name.lower(), guild.text_channels)


async def fetch_rules_message(channel: discord.TextChannel) -> discord.Message | None:
    if RULES_MESSAGE_ID:
        return await channel.fetch_message(RULES_MESSAGE_ID)
    async for msg in channel.history(limit=1, oldest_first=True):
        return msg


def rules_embed(source: discord.Message, channel: discord.TextChannel) -> discord.Embed:
    # If the rules were already posted as an embed, reuse it as-is.
    if source.embeds and not source.content:
        return source.embeds[0]
    embed = discord.Embed(
        title="Server Rules",
        description=source.content[:4096],
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"From #{channel.name}")
    if source.attachments and (source.attachments[0].content_type or "").startswith("image/"):
        embed.set_image(url=source.attachments[0].url)
    return embed


class Rules(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="rules", description="Repost this server's rules as an embed")
    @commands.guild_only()
    async def rules(self, ctx: commands.Context):
        """Repost this server's rules as an embed."""
        channel = find_rules_channel(ctx.guild)
        if channel is None:
            return await ctx.send("I couldn't find a rules channel. Set RULES_CHANNEL_ID.")

        try:
            source = await fetch_rules_message(channel)
        except discord.NotFound:
            return await ctx.send("I couldn't find that rules message.")
        except discord.Forbidden:
            return await ctx.send(f"I can't read {channel.mention}.")
        if source is None:
            return await ctx.send(f"{channel.mention} has no messages yet.")

        await ctx.send(embed=rules_embed(source, channel))


async def setup(bot: commands.Bot):
    await bot.add_cog(Rules(bot))
