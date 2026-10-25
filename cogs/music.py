"""
Music cog: search a Navidrome library and play songs in a voice channel.

Needs ffmpeg on your PATH, the voice extras (pip install "discord.py[voice]"), and:
    NAVIDROME_URL    e.g. http://192.168.1.50:4533
    NAVIDROME_USER   a Navidrome user (a dedicated one is a good idea)
    NAVIDROME_PASS   that user's password
Without those the cog isn't loaded and the rest of the bot works as usual.
"""

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands, tasks

from common import UserError, clean, clip
from navidrome import Navidrome, NavidromeError, Song

log = logging.getLogger(__name__)

# Reconnect, so a hiccup on the network doesn't end the song.
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
IDLE_TIMEOUT = 300  # seconds of silence, or of an empty channel, before the bot leaves
MIN_PLAY_SECONDS = 2  # a track that "ends" sooner than this never really played
MAX_QUEUE = 100
SEARCH_RESULTS = 10


def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "?:??"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


@dataclass(frozen=True, slots=True, eq=False)  # eq=False: two requests for the same song are still different tracks
class Track:
    song: Song
    requester: str

    @property
    def line(self) -> str:
        return f"**{clean(self.song.title)}** by {clean(self.song.artist)} ({fmt_duration(self.song.duration)})"


# ------------------- per-server player -------------------

class GuildPlayer:
    """The queue and playback state for one server."""

    def __init__(self, cog: "Music", guild: discord.Guild, text_channel: discord.abc.Messageable):
        self.cog = cog
        self.guild = guild
        self.text_channel = text_channel  # where "now playing" announcements go
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.started_at = 0.0
        self.skipping = False
        self.idle_since: float | None = time.monotonic()  # None while something is playing
        self.alone_since: float | None = None
        # Bumped whenever playback starts or is torn down, so an `after` callback that fires late
        # (ffmpeg takes a moment to wind down) can tell it belongs to a track that's already over.
        self._gen = 0

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def active(self) -> bool:
        vc = self.vc
        return vc is not None and (vc.is_playing() or vc.is_paused())

    def reset(self) -> None:
        self._gen += 1
        self.queue.clear()
        self.current = None
        self.idle_since = time.monotonic()

    async def say(self, content: str | None = None, *, embed: discord.Embed | None = None) -> None:
        try:
            await self.text_channel.send(content, embed=embed)
        except discord.HTTPException as e:
            log.warning("Couldn't post in guild %s: %s", self.guild.id, e)

    async def play_next(self, *, announce: bool = True) -> Track | None:
        """Start the next queued track. Returns it, or None if there was nothing to play."""
        vc = self.vc
        if vc is None or not vc.is_connected():
            self.reset()
            return None
        if not self.queue:
            self.current = None
            self.idle_since = time.monotonic()
            return None

        track = self.queue.popleft()
        try:
            source = discord.FFmpegPCMAudio(
                self.cog.navidrome.stream_url(track.song.id), before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS
            )
        except discord.ClientException:
            self.reset()
            raise UserError("I can't find ffmpeg. Install it and make sure it's on your PATH.") from None

        self._gen += 1
        gen = self._gen
        self.current = track
        self.started_at = time.monotonic()
        self.idle_since = None
        try:
            vc.play(source, after=lambda error: self._after(error, gen))
        except discord.ClientException:
            source.cleanup()
            raise
        if announce:
            await self.say(embed=self.cog.now_playing_embed(track))
        return track

    def _after(self, error: Exception | None, gen: int) -> None:
        # Runs on discord.py's player thread, so hop back onto the event loop.
        if error:
            log.warning("Playback error in guild %s: %s", self.guild.id, error)
        asyncio.run_coroutine_threadsafe(self._advance(gen), self.cog.bot.loop)

    async def _advance(self, gen: int) -> None:
        if gen != self._gen:
            return  # stopped, or already moved on
        finished, self.current = self.current, None
        skipped, self.skipping = self.skipping, False
        try:
            if finished and not skipped and time.monotonic() - self.started_at < MIN_PLAY_SECONDS < finished.song.duration:
                await self.say(f"⚠️ Couldn't play {finished.line}. The stream stopped right away, so check the Navidrome login and that the song still exists.")
            await self.play_next()
        except UserError as e:
            await self.say(f"⚠️ {e}")
        except Exception:
            log.exception("Couldn't start the next track in guild %s", self.guild.id)

    def skip(self) -> Track | None:
        vc = self.vc
        if vc is None or not self.active:
            return None
        track = self.current
        self.skipping = True
        vc.stop()  # fires `after`, which starts the next track
        return track

    async def shutdown(self) -> None:
        """Stop, forget the queue and leave the voice channel."""
        self.reset()
        vc = self.vc
        if vc is not None:
            vc.stop()
            await vc.disconnect()
        self.cog.players.pop(self.guild.id, None)

    def leave_reason(self, now: float) -> str | None:
        """Why the bot should leave now, if it should: nobody's listening, or nothing's been playing for a while."""
        vc = self.vc
        if vc is None:
            return None
        if any(not m.bot for m in vc.channel.members):
            self.alone_since = None
        elif self.alone_since is None:
            self.alone_since = now
        if self.alone_since is not None and now - self.alone_since >= IDLE_TIMEOUT:
            return "👋 Leaving, since nobody's listening."
        if self.idle_since is not None and now - self.idle_since >= IDLE_TIMEOUT:
            return "👋 Leaving, since nothing's been playing for a while."
        return None


# ------------------- search picker -------------------

class SearchView(discord.ui.View):
    """A dropdown of search results; picking one queues it. Only the person who searched can use it."""

    def __init__(self, cog: "Music", requester: discord.abc.User, songs: list[Song]):
        super().__init__(timeout=90)
        self.cog = cog
        self.requester = requester
        self.songs = songs
        self.message: discord.Message | None = None
        self.select = discord.ui.Select(
            placeholder="Pick a song to queue",
            options=[
                discord.SelectOption(
                    label=clip(f"{s.title} — {s.artist}", 100),
                    description=clip(f"{s.album} · {fmt_duration(s.duration)}" if s.album else fmt_duration(s.duration), 100),
                    value=str(i),
                )
                for i, s in enumerate(songs)
            ],
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    def embed(self) -> discord.Embed:
        lines = [
            f"`{i}.` **{clean(s.title)}** — {clean(s.artist)}" + (f" · {clean(s.album)}" if s.album else "") + f" ({fmt_duration(s.duration)})"
            for i, s in enumerate(self.songs, 1)
        ]
        return discord.Embed(title="Search results", description="\n".join(lines), color=discord.Color.blurple())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester.id:
            return True
        await interaction.response.send_message("Only the person who searched can pick.", ephemeral=True)
        return False

    async def on_select(self, interaction: discord.Interaction):
        song = self.songs[int(self.select.values[0])]
        await interaction.response.defer()
        try:
            player, track, started = await self.cog.enqueue(interaction.guild, interaction.user, interaction.channel, song)
        except UserError as e:
            return await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
        await interaction.followup.send(**self.cog.enqueue_reply(player, track, started))

    async def on_timeout(self) -> None:
        self.select.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ------------------- the cog -------------------

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.navidrome = Navidrome(os.environ["NAVIDROME_URL"], os.environ["NAVIDROME_USER"], os.environ["NAVIDROME_PASS"])
        self.players: dict[int, GuildPlayer] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        try:
            await self.navidrome.ping()
            print("Navidrome connection OK")
        except NavidromeError as e:
            # Keep going: the server may just not be up yet, and every command reports errors itself.
            print(f"Warning: can't use Navidrome right now. {e}")
        self.reaper.start()

    async def cog_unload(self) -> None:
        self.reaper.cancel()
        for player in list(self.players.values()):
            await player.shutdown()
        await self.navidrome.close()

    # ------------------- housekeeping -------------------

    @tasks.loop(seconds=30)
    async def reaper(self) -> None:
        """Leave voice channels that have gone quiet or empty."""
        now = time.monotonic()
        for guild_id, player in list(self.players.items()):
            try:
                vc = player.vc
                if vc is None or not vc.is_connected():
                    player.reset()
                    self.players.pop(guild_id, None)
                    continue
                reason = player.leave_reason(now)
                if reason:
                    await player.say(reason)
                    await player.shutdown()
            except Exception:
                log.exception("Housekeeping failed for guild %s", guild_id)

    @reaper.before_loop
    async def _before_reaper(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        # Somebody disconnected the bot from the channel by hand.
        if self.bot.user and member.id == self.bot.user.id and after.channel is None:
            player = self.players.pop(member.guild.id, None)
            if player is not None:
                player.reset()

    # ------------------- shared helpers -------------------

    @staticmethod
    def now_playing_embed(track: Track) -> discord.Embed:
        song = track.song
        embed = discord.Embed(
            title="Now playing",
            description=f"**{clean(song.title)}**\n{clean(song.artist)}" + (f" · {clean(song.album)}" if song.album else ""),
            color=discord.Color.green(),
        )
        embed.add_field(name="Length", value=fmt_duration(song.duration))
        embed.set_footer(text=f"Requested by {track.requester}")
        return embed

    def enqueue_reply(self, player: GuildPlayer, track: Track, started: bool) -> dict:
        """Keyword arguments for ctx.send / followup.send confirming that a track was queued or started."""
        if started:
            return {"embed": self.now_playing_embed(track)}
        position = next((i for i, t in enumerate(player.queue, 1) if t is track), 0)
        suffix = f" (#{position} in the queue)" if position else ""
        return {"content": f"➕ Queued {track.line}{suffix}"}

    async def _get_player(self, guild: discord.Guild, member: discord.Member, text_channel: discord.abc.Messageable) -> GuildPlayer:
        """The server's player, joining the member's voice channel first if the bot isn't in one."""
        voice = getattr(member, "voice", None)
        channel = voice.channel if voice else None
        if channel is None:
            raise UserError("Join a voice channel first.")
        if not isinstance(channel, discord.VoiceChannel):
            raise UserError("I can only play in regular voice channels, not stages.")

        vc = guild.voice_client
        if vc is None:
            perms = channel.permissions_for(guild.me)
            if not (perms.connect and perms.speak):
                raise UserError(f"I need the Connect and Speak permissions in {channel.mention}.")
            try:
                await channel.connect(self_deaf=True)
            except asyncio.TimeoutError:
                raise UserError("Timed out joining the voice channel. Try again.") from None
            except (discord.ClientException, discord.HTTPException) as e:
                raise UserError(f"Couldn't join the voice channel: {e}") from None
        elif vc.channel != channel:
            existing = self.players.get(guild.id)
            if existing is not None and (existing.active or existing.queue):
                raise UserError(f"I'm busy playing in {vc.channel.mention}.")
            await vc.move_to(channel)

        player = self.players.get(guild.id)
        if player is None:
            player = self.players[guild.id] = GuildPlayer(self, guild, text_channel)
        player.text_channel = text_channel
        return player

    async def enqueue(
        self, guild: discord.Guild, member: discord.Member, text_channel: discord.abc.Messageable, song: Song
    ) -> tuple[GuildPlayer, Track, bool]:
        """Queue a song for the member, joining their voice channel if needed. Returns (player, track, started_now)."""
        async with self._locks.setdefault(guild.id, asyncio.Lock()):
            player = await self._get_player(guild, member, text_channel)
            if len(player.queue) >= MAX_QUEUE:
                raise UserError(f"The queue is full ({MAX_QUEUE} songs).")
            track = Track(song, member.display_name)
            player.queue.append(track)
            started = False
            if not player.active:
                try:
                    begun = await player.play_next(announce=False)
                except discord.ClientException as e:
                    raise UserError(f"Couldn't start playback: {e}") from None
                started = begun is track
                if begun is not None and not started:
                    # Something older was still waiting in the queue and went first.
                    await player.say(embed=self.now_playing_embed(begun))
            return player, track, started

    def _control_player(self, ctx: commands.Context) -> GuildPlayer:
        """The player for a control command (skip, pause...). Only people in the bot's channel may use it."""
        player = self.players.get(ctx.guild.id)
        vc = ctx.guild.voice_client
        if player is None or vc is None:
            raise UserError("I'm not playing anything.")
        voice = getattr(ctx.author, "voice", None)
        if voice is None or voice.channel != vc.channel:
            raise UserError(f"Join {vc.channel.mention} to control playback.")
        return player

    # ------------------- commands -------------------

    @commands.hybrid_command(name="play", description="Search the library and play the best match")
    @app_commands.describe(query="Song title, artist or album words to search for")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str):
        """Search the Navidrome library and play (or queue) the best match."""
        async with ctx.typing():
            try:
                songs = await self.navidrome.search_songs(query, 1)
            except NavidromeError as e:
                raise UserError(str(e)) from None
            if not songs:
                raise UserError(f"No songs found for **{clean(query)}**.")
            player, track, started = await self.enqueue(ctx.guild, ctx.author, ctx.channel, songs[0])
        await ctx.send(**self.enqueue_reply(player, track, started))

    @commands.hybrid_command(name="search", description="Search the library and pick which song to queue")
    @app_commands.describe(query="Song title, artist or album words to search for")
    @commands.guild_only()
    async def search(self, ctx: commands.Context, *, query: str):
        """Search the Navidrome library and choose from the top results."""
        async with ctx.typing():
            try:
                songs = await self.navidrome.search_songs(query, SEARCH_RESULTS)
            except NavidromeError as e:
                raise UserError(str(e)) from None
        if not songs:
            raise UserError(f"No songs found for **{clean(query)}**.")
        # A select menu holds at most 10 options; Discord rejects the message outright beyond that.
        view = SearchView(self, ctx.author, songs[:SEARCH_RESULTS])
        view.message = await ctx.send(embed=view.embed(), view=view)

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show what's playing and what's up next")
    @commands.guild_only()
    async def queue(self, ctx: commands.Context):
        """Show what's playing and what's up next."""
        player = self.players.get(ctx.guild.id)
        if player is None or (player.current is None and not player.queue):
            return await ctx.send("The queue is empty.")
        lines = []
        if player.current:
            lines.append(f"▶️ {player.current.line}")
        for i, track in enumerate(list(player.queue)[:10], 1):
            lines.append(f"`{i}.` {track.line}")
        embed = discord.Embed(title="Queue", description="\n".join(lines), color=discord.Color.blurple())
        if len(player.queue) > 10:
            embed.set_footer(text=f"…and {len(player.queue) - 10} more")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nowplaying", aliases=["np"], description="Show the song that's playing")
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context):
        """Show the song that's playing."""
        player = self.players.get(ctx.guild.id)
        if player is None or player.current is None:
            return await ctx.send("Nothing is playing.")
        await ctx.send(embed=self.now_playing_embed(player.current))

    @commands.hybrid_command(name="skip", description="Skip the current song")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        """Skip the current song."""
        player = self._control_player(ctx)
        track = player.skip()
        if track is None:
            raise UserError("Nothing is playing.")
        await ctx.send(f"⏭️ Skipped {track.line}")

    @commands.hybrid_command(name="pause", description="Pause playback")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        """Pause playback."""
        player = self._control_player(ctx)
        if player.vc is None or not player.vc.is_playing():
            raise UserError("Nothing is playing.")
        player.vc.pause()
        await ctx.send("⏸️ Paused.")

    @commands.hybrid_command(name="resume", description="Resume playback")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        """Resume playback."""
        player = self._control_player(ctx)
        if player.vc is None or not player.vc.is_paused():
            raise UserError("Playback isn't paused.")
        player.vc.resume()
        await ctx.send("▶️ Resumed.")

    @commands.hybrid_command(name="stop", aliases=["leave", "disconnect"], description="Stop playing, clear the queue and leave")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        """Stop playing, clear the queue and leave the voice channel."""
        player = self._control_player(ctx)
        await player.shutdown()
        await ctx.send("⏹️ Stopped and left the voice channel.")


async def setup(bot: commands.Bot):
    if not all(os.environ.get(k) for k in ("NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASS")):
        print("Music is off: set NAVIDROME_URL, NAVIDROME_USER and NAVIDROME_PASS to turn it on.")
        return
    await bot.add_cog(Music(bot))
