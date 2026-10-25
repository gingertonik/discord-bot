"""
Pokémon cog: catch a random Pokémon every 90 minutes, keep a collection and a Pokédex, and trade with other trainers.

Commands (prefix ! or ;, or the slash version):
    pokemon                              catch a random Pokémon (alias: catch)
    inventory [@user] [name]             the Pokémon someone owns, paged and sortable (aliases: inv, box)
    info <id>                            everything about one Pokémon, by the #ID shown in the inventory
    pokedex [name | number | gen N]      your Pokédex progress, a species entry, or a whole generation (alias: dex)
    trade @user <your ids> <their ids>   propose a swap; IDs are comma-separated, e.g. 12,15

A trainer's Pokémon, Pokédex and 90-minute cooldown follow them across servers. A trade only needs both
trainers to be in the server where it's proposed.

Environment variable:
    POKEMON_DB   optional: where to keep the SQLite file (default: data/pokemon.db next to the bot)
"""

import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from common import UserError, clean, clip
from pokedex import REGIONS, TIER_RANK, Pokedex, Species, normalize
from pokestore import Caught, Store

log = logging.getLogger(__name__)

CATCH_COOLDOWN = 90 * 60  # seconds
PER_PAGE = 10  # Pokémon per inventory page
DEX_PER_PAGE = 30  # species per Pokédex page
TRADE_TIMEOUT = 120  # seconds the other trainer has to answer an offer
MAX_TRADE_SIDE = 6  # Pokémon per side of a trade
DB_PATH = Path(os.environ.get("POKEMON_DB") or Path(__file__).resolve().parent.parent / "data" / "pokemon.db")

SORTS = {
    "newest": "Newest first",
    "oldest": "Oldest first",
    "dex": "Pokédex number",
    "name": "Name (A-Z)",
    "rarity": "Rarest first",
}


def fmt_wait(seconds: int) -> str:
    hours, rest = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = width * done // total if total else 0  # floors, so a full bar always means complete
    return "█" * filled + "░" * (width - filled)


def parse_ids(text: str, what: str, limit: int = MAX_TRADE_SIDE) -> list[int]:
    """'12, #15,16' -> [12, 15, 16]. Raises UserError for anything that isn't a short list of Pokémon IDs."""
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if not parts:
        raise UserError(f"Tell me which Pokémon ID{'s' if limit > 1 else ''} {what}.")
    ids = []
    for part in parts:
        match = re.fullmatch(r"#?([0-9]{1,15})", part)
        if not match:
            raise UserError(f"`{clean(clip(part, 20))}` isn't a Pokémon ID. IDs are the numbers in the inventory, like `12` or `12,15`.")
        ids.append(int(match.group(1)))
    if len(set(ids)) != len(ids):
        raise UserError("You listed the same Pokémon ID twice.")
    if len(ids) > limit:
        raise UserError(f"That's too many: at most {limit} per side.")
    return ids


# ------------------- views -------------------

class OwnedView(discord.ui.View):
    """A view only the person who ran the command can use. Greys itself out when it times out."""

    def __init__(self, owner: discord.abc.User, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.owner = owner
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.id:
            return True
        await interaction.response.send_message("That's not your menu. Run the command yourself to get one.", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Pager(OwnedView):
    """Previous / next buttons over some number of pages. Subclasses say how many pages there are and draw one."""

    def __init__(self, owner: discord.abc.User):
        super().__init__(owner)
        self.page = 0

    def page_count(self) -> int:
        raise NotImplementedError

    def render(self) -> discord.Embed:
        raise NotImplementedError

    def refresh_buttons(self) -> None:
        self.page = max(0, min(self.page, self.page_count() - 1))
        self.first.disabled = self.prev.disabled = self.page == 0
        self.next.disabled = self.last.disabled = self.page >= self.page_count() - 1

    async def show(self, interaction: discord.Interaction) -> None:
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await self.show(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self.show(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.show(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.page_count() - 1
        await self.show(interaction)


class InventoryView(Pager):
    def __init__(self, dex: Pokedex, owner: discord.abc.User, trainer: discord.abc.User, owned: list[Caught], search: str | None):
        super().__init__(owner)
        self.dex = dex
        self.trainer = trainer
        self.search = (search or "").strip()
        self.sort = "newest"
        self.items = self._filter(owned)
        self.shown = self._sorted()
        self.select = discord.ui.Select(
            placeholder="Sort by…",
            options=[discord.SelectOption(label=label, value=key, default=key == self.sort) for key, label in SORTS.items()],
            row=1,
        )
        self.select.callback = self.on_sort
        self.add_item(self.select)
        self.refresh_buttons()

    def _filter(self, owned: list[Caught]) -> list[Caught]:
        if not self.search:
            return owned
        number = re.fullmatch(r"#?([0-9]+)", self.search)
        if number:
            return [c for c in owned if c.species_id == int(number.group(1))]
        key = normalize(self.search)
        if not key:
            return owned
        return [c for c in owned if key in self.dex.search_names[c.species_id]]

    def _sorted(self) -> list[Caught]:
        dex = self.dex
        keys = {
            "newest": lambda c: -c.id,
            "oldest": lambda c: c.id,
            "dex": lambda c: (c.species_id, c.id),
            "name": lambda c: (dex[c.species_id].name.casefold(), c.id),
            "rarity": lambda c: (-TIER_RANK[dex[c.species_id].tier], c.species_id, c.id),
        }
        return sorted(self.items, key=keys[self.sort])

    def page_count(self) -> int:
        return max(1, math.ceil(len(self.items) / PER_PAGE))

    def line(self, caught: Caught) -> str:
        s = self.dex[caught.species_id]
        return f"`#{caught.id}` {s.tier_emoji} **{s.name}** · No. {s.number} · {s.types_text} · Gen {s.gen_roman}"

    def render(self) -> discord.Embed:
        chunk = self.shown[self.page * PER_PAGE : (self.page + 1) * PER_PAGE]
        embed = discord.Embed(
            title=f"{clip(self.trainer.display_name, 80)}'s Pokémon",
            description="\n".join(self.line(c) for c in chunk),
            color=discord.Color.blurple(),
        )
        footer = f"Page {self.page + 1}/{self.page_count()} · {len(self.items)} Pokémon"
        if self.search:
            footer += f" matching “{clip(self.search, 30)}”"
        embed.set_footer(text=f"{footer} · {SORTS[self.sort]}")
        return embed

    async def on_sort(self, interaction: discord.Interaction):
        self.sort = self.select.values[0]
        for option in self.select.options:
            option.default = option.value == self.sort
        self.shown = self._sorted()
        self.page = 0
        await self.show(interaction)


class DexView(Pager):
    """One generation of the Pokédex, with a tick next to the species you've registered."""

    def __init__(self, owner: discord.abc.User, gen: int, species: list[Species], registered: set[int]):
        super().__init__(owner)
        self.gen = gen
        self.species = species
        self.registered = registered
        self.refresh_buttons()

    def page_count(self) -> int:
        return max(1, math.ceil(len(self.species) / DEX_PER_PAGE))

    def render(self) -> discord.Embed:
        chunk = self.species[self.page * DEX_PER_PAGE : (self.page + 1) * DEX_PER_PAGE]
        lines = [f"{'✅' if s.id in self.registered else '⬜'} `{s.number}` {s.name}" for s in chunk]
        have = sum(s.id in self.registered for s in self.species)
        embed = discord.Embed(
            title=f"Generation {self.species[0].gen_roman} · {REGIONS.get(self.gen, '')}".strip(" ·"),
            description="\n".join(lines),
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"{have}/{len(self.species)} registered · Page {self.page + 1}/{self.page_count()}")
        return embed


class TradeView(discord.ui.View):
    """
    A trade offer. Only the other trainer can accept; either trainer can decline or cancel. Nothing moves
    until Accept is pressed, and the swap re-checks who owns what at that moment.
    """

    def __init__(self, cog: "PokemonGame", guild_id: int, proposer: discord.Member, partner: discord.Member, give: list[Caught], get: list[Caught]):
        super().__init__(timeout=TRADE_TIMEOUT)
        self.cog = cog
        self.guild_id = guild_id
        self.proposer = proposer
        self.partner = partner
        self.give = give  # what the proposer hands over
        self.get = get  # what the partner hands over
        self.message: discord.Message | None = None
        self.done = False

    def embed(self, title: str, color: discord.Color, footer: str | None = None) -> discord.Embed:
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name=f"{clip(self.proposer.display_name, 40)} gives", value=self.cog.describe(self.give), inline=False)
        embed.add_field(name=f"{clip(self.partner.display_name, 40)} gives", value=self.cog.describe(self.get), inline=False)
        if footer:
            embed.set_footer(text=footer)
        return embed

    def release(self) -> None:
        self.done = True
        self.cog.trading.discard(self.proposer.id)
        self.cog.trading.discard(self.partner.id)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in (self.proposer.id, self.partner.id):
            return True
        await interaction.response.send_message("This trade is between two other trainers.", ephemeral=True)
        return False

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.partner.id:
            return await interaction.response.send_message(f"Only {clean(self.partner.display_name)} can accept this trade.", ephemeral=True)
        if self.done:
            return await interaction.response.defer()
        self.release()
        await interaction.response.defer()
        try:
            result = await self.cog.store.swap(
                self.guild_id, self.proposer.id, [c.id for c in self.give], self.partner.id, [c.id for c in self.get], now=int(time.time())
            )
        except Exception:
            log.exception("Trade between %s and %s failed", self.proposer.id, self.partner.id)
            embed = self.embed("Trade failed", discord.Color.red(), "Something went wrong, and nothing was traded.")
            return await interaction.edit_original_response(embed=embed, view=None)

        if not result.ok:
            reason = {
                "not_owner": "Somebody no longer owns a Pokémon in this offer (" + ", ".join(f"#{i}" for i in result.bad_ids) + ").",
                "missing": "A Pokémon in this offer no longer exists.",
            }.get(result.error, "This trade isn't valid.")
            embed = self.embed("Trade failed", discord.Color.red(), f"{reason} Nothing was traded.")
            return await interaction.edit_original_response(embed=embed, view=None)

        embed = self.embed("Trade complete 🤝", discord.Color.green())
        for trainer, species_ids in ((self.proposer, result.new_for_a), (self.partner, result.new_for_b)):
            if species_ids:
                names = ", ".join(self.cog.dex[i].name for i in species_ids)
                embed.add_field(name=f"✨ New for {clip(trainer.display_name, 40)}'s Pokédex", value=clip(names, 1000), inline=False)
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="✖️")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.defer()
        self.release()
        verb = "declined" if interaction.user.id == self.partner.id else "cancelled"
        embed = self.embed(f"Trade {verb}", discord.Color.red())
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        if self.done:
            return
        self.release()
        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed("Trade expired", discord.Color.dark_grey(), "Nobody answered in time."), view=None)
            except discord.HTTPException:
                pass


# ------------------- the cog -------------------

class PokemonGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.dex = Pokedex.load()
        self.store = Store(DB_PATH)
        self.trading: set[int] = set()  # trainers with an offer open, so nobody gets buried in them

    async def cog_load(self) -> None:
        await self.store.open()
        print(f"Pokémon: {len(self.dex)} species loaded, collection stored in {self.store.path}")

    async def cog_unload(self) -> None:
        await self.store.close()

    def describe(self, items: list[Caught]) -> str:
        lines = []
        for caught in items:
            s = self.dex[caught.species_id]
            lines.append(f"`#{caught.id}` {s.tier_emoji} **{s.name}** · No. {s.number}")
        return "\n".join(lines)

    # ------------------- catching -------------------

    @commands.hybrid_command(name="pokemon", aliases=["catch"], description="Catch a random Pokémon (once every 90 minutes)")
    async def pokemon(self, ctx: commands.Context):
        """Catch a random Pokémon. You can catch one every 90 minutes."""
        now = int(time.time())
        species = self.dex.roll()
        result = await self.store.catch(ctx.author.id, species.id, now=now, cooldown=CATCH_COOLDOWN, guild_id=ctx.guild.id if ctx.guild else None)
        if result.pokemon is None:
            return await ctx.send(
                f"⏳ Your next catch is ready in **{fmt_wait(result.retry_at - now)}** (<t:{result.retry_at}:t>).", ephemeral=True
            )

        profile = await self.store.profile(ctx.author.id)
        embed = discord.Embed(title=f"{clip(ctx.author.display_name, 60)} caught {species.name}!", description=f"*{species.genus}*", color=species.color)
        embed.set_image(url=species.artwork_url)
        embed.add_field(name="Type", value=species.types_text)
        embed.add_field(name="Rarity", value=species.tier_label)
        embed.add_field(name="Generation", value=f"{species.gen_roman} · {REGIONS.get(species.gen, '')}".strip(" ·"))
        embed.add_field(name="Pokédex", value=f"No. {species.number}")
        embed.add_field(name="ID", value=f"`#{result.pokemon.id}`")
        if result.new_entry:
            embed.add_field(name="✨ New Pokédex entry!", value=f"{species.name} is now registered.", inline=False)
        embed.set_footer(text=f"Pokédex {profile.registered}/{len(self.dex)} · you can catch again in 90 minutes")
        await ctx.send(embed=embed)

    # ------------------- looking at your Pokémon -------------------

    @commands.hybrid_command(name="inventory", aliases=["inv", "box"], description="List the Pokémon you (or someone else) own")
    @app_commands.describe(member="Whose Pokémon to show (default: you)", search="Only show Pokémon whose name contains this, or a Pokédex number")
    async def inventory(self, ctx: commands.Context, member: Optional[discord.Member] = None, *, search: Optional[str] = None):
        """List a trainer's Pokémon, newest first. Add a name to filter, e.g. `inv pika`."""
        trainer = member or ctx.author
        view = InventoryView(self.dex, ctx.author, trainer, await self.store.owned(trainer.id), search)
        who = "You don't" if trainer.id == ctx.author.id else f"{clean(trainer.display_name)} doesn't"
        if not view.items:
            if view.search:
                raise UserError(f"No Pokémon matching “{clean(clip(view.search, 40))}”.")
            raise UserError(f"{who} have any Pokémon yet. Try `{ctx.clean_prefix}pokemon`!")
        if len(view.items) == 1:
            return await ctx.send(embed=view.render())
        view.message = await ctx.send(embed=view.render(), view=view)

    @commands.hybrid_command(name="info", aliases=["pokeinfo"], description="Show everything about one Pokémon, by its ID")
    @app_commands.describe(pokemon_id="The #ID shown in the inventory")
    async def info(self, ctx: commands.Context, pokemon_id: str):
        """Show everything about one Pokémon, by the #ID shown in the inventory."""
        (wanted,) = parse_ids(pokemon_id, "to look up", limit=1)
        caught = await self.store.pokemon(wanted)
        if caught is None:
            raise UserError(f"There's no Pokémon with ID #{wanted}.")
        s = self.dex[caught.species_id]
        embed = discord.Embed(title=f"{s.name} · No. {s.number}", description=f"*{s.genus}*", color=s.color)
        embed.set_image(url=s.artwork_url)
        embed.add_field(name="Type", value=s.types_text)
        embed.add_field(name="Rarity", value=s.tier_label)
        embed.add_field(name="Generation", value=f"{s.gen_roman} · {REGIONS.get(s.gen, '')}".strip(" ·"))
        embed.add_field(name="Height", value=f"{s.height_m:g} m")
        embed.add_field(name="Weight", value=f"{s.weight_kg:g} kg")
        embed.add_field(name="Owner", value=f"<@{caught.owner_id}>")
        embed.add_field(name="Base stats", value=s.stats_text, inline=False)
        origin = f"Caught by <@{caught.caught_by}> on <t:{caught.caught_at}:D>"
        embed.add_field(name="History", value=origin, inline=False)
        embed.set_footer(text=f"ID #{caught.id}")
        await ctx.send(embed=embed)

    # ------------------- the Pokédex -------------------

    @commands.hybrid_command(name="pokedex", aliases=["dex"], description="Your Pokédex progress, or look up any Pokémon")
    @app_commands.describe(query="A name or Pokédex number, or 'gen 3' to browse a generation")
    async def pokedex(self, ctx: commands.Context, *, query: Optional[str] = None):
        """Your Pokédex progress. Add a name or number to look a Pokémon up, or `gen 3` to browse a generation."""
        registered = set(await self.store.registered(ctx.author.id))
        if query is None or not query.strip():
            return await self._dex_progress(ctx, registered)

        gen = re.fullmatch(r"(?:gen(?:eration)?|g)\s*([0-9]{1,2})", query.strip(), re.IGNORECASE)
        if gen:
            number = int(gen.group(1))
            if number not in self.dex.generations:
                raise UserError(f"There's no generation {number}. Try 1 to {self.dex.generations[-1]}.")
            view = DexView(ctx.author, number, self.dex.generation(number), registered)
            view.message = await ctx.send(embed=view.render(), view=view)
            return

        matches = self.dex.find(query)
        if not matches:
            raise UserError(f"No Pokémon matches “{clean(clip(query, 40))}”.")
        if len(matches) > 1:
            shown = "\n".join(f"`{s.number}` {s.name}" for s in matches[:15])
            more = f"\n…and {len(matches) - 15} more" if len(matches) > 15 else ""
            embed = discord.Embed(title=f"Several Pokémon match “{clip(query, 40)}”", description=shown + more, color=discord.Color.red())
            embed.set_footer(text="Use the name or number to pick one.")
            return await ctx.send(embed=embed)
        await self._dex_entry(ctx, matches[0], registered)

    async def _dex_progress(self, ctx: commands.Context, registered: set[int]) -> None:
        profile = await self.store.profile(ctx.author.id)
        total = len(self.dex)
        embed = discord.Embed(
            title=f"{clip(ctx.author.display_name, 80)}'s Pokédex",
            description=(
                f"**{len(registered)}/{total}** registered ({100 * len(registered) / total:.1f}%)\n"
                f"{profile.total_caught} caught in all · {profile.owned} owned right now"
            ),
            color=discord.Color.red(),
        )
        for gen in self.dex.generations:
            species = self.dex.generation(gen)
            have = sum(s.id in registered for s in species)
            roman = species[0].gen_roman
            embed.add_field(name=f"Gen {roman} · {REGIONS.get(gen, '')}".strip(" ·"), value=f"`{progress_bar(have, len(species))}` {have}/{len(species)}")
        embed.set_footer(text=f"{ctx.clean_prefix}pokedex <name or number> looks one up · {ctx.clean_prefix}pokedex gen 1 browses a generation")
        await ctx.send(embed=embed)

    async def _dex_entry(self, ctx: commands.Context, s: Species, registered: set[int]) -> None:
        owned = sum(c.species_id == s.id for c in await self.store.owned(ctx.author.id))
        embed = discord.Embed(title=f"No. {s.number} · {s.name}", description=f"*{s.genus}*", color=s.color)
        embed.set_image(url=s.artwork_url)
        embed.add_field(name="Type", value=s.types_text)
        embed.add_field(name="Rarity", value=s.tier_label)
        embed.add_field(name="Generation", value=f"{s.gen_roman} · {REGIONS.get(s.gen, '')}".strip(" ·"))
        embed.add_field(name="Height", value=f"{s.height_m:g} m")
        embed.add_field(name="Weight", value=f"{s.weight_kg:g} kg")
        embed.add_field(name="Base stats", value=s.stats_text, inline=False)
        if s.id in registered:
            embed.set_footer(text=f"✅ In your Pokédex · you own {owned}")
        else:
            embed.set_footer(text="❌ Not in your Pokédex yet")
        await ctx.send(embed=embed)

    # ------------------- trading -------------------

    @commands.hybrid_command(name="trade", description="Offer to swap Pokémon with another trainer", ignore_extra=False)
    @app_commands.describe(
        member="The trainer to trade with",
        give="IDs of your Pokémon to give, comma-separated (e.g. 12,15)",
        get="IDs of their Pokémon you want, comma-separated",
    )
    @commands.guild_only()
    async def trade(self, ctx: commands.Context, member: discord.Member, give: str, get: str):
        """Offer to swap Pokémon. IDs are the #numbers in the inventory, comma-separated: `trade @user 12,15 40`."""
        if member.bot:
            raise UserError("Bots don't trade Pokémon.")
        if member.id == ctx.author.id:
            raise UserError("You can't trade with yourself.")
        give_ids = parse_ids(give, "you want to give")
        get_ids = parse_ids(get, "you want from them")
        if set(give_ids) & set(get_ids):
            raise UserError("The same Pokémon can't be on both sides of a trade.")
        if ctx.author.id in self.trading:
            raise UserError("You already have a trade open. Finish or decline it first.")
        if member.id in self.trading:
            raise UserError(f"{clean(member.display_name)} already has a trade open. Try again in a moment.")

        found = await self.store.pokemon_many(give_ids + get_ids)
        mine, theirs = [], []
        for ids, owner, bucket, who in ((give_ids, ctx.author, mine, "You don't"), (get_ids, member, theirs, f"{clean(member.display_name)} doesn't")):
            for pokemon_id in ids:
                caught = found.get(pokemon_id)
                if caught is None:
                    raise UserError(f"There's no Pokémon with ID #{pokemon_id}.")
                if caught.owner_id != owner.id:
                    raise UserError(f"{who} own #{pokemon_id}.")
                bucket.append(caught)

        view = TradeView(self, ctx.guild.id, ctx.author, member, mine, theirs)
        embed = view.embed(
            f"{clip(ctx.author.display_name, 40)} wants to trade with {clip(member.display_name, 40)}",
            discord.Color.blurple(),
            f"{clip(member.display_name, 40)}: press Accept to swap, or Decline. This offer expires in {TRADE_TIMEOUT // 60} minutes.",
        )
        self.trading.update((ctx.author.id, member.id))
        try:
            # The one place the bot pings someone on purpose: the person being asked to trade.
            view.message = await ctx.send(content=member.mention, embed=embed, view=view, allowed_mentions=discord.AllowedMentions(users=[member]))
        except BaseException:
            view.release()
            raise


async def setup(bot: commands.Bot):
    await bot.add_cog(PokemonGame(bot))
