"""
Tests for cogs/music.py, driven entirely by fakes: no Discord connection, no network and no ffmpeg.

What is faked, and how closely:
    FakeVoiceClient   mirrors discord.VoiceClient as read in discord/voice_client.py and discord/player.py:
                        - stop() makes is_playing()/is_paused() False at once, and the `after` callback arrives later,
                          from a different thread (the real AudioPlayer thread), followed by source.cleanup();
                        - a source running out flips the player to "not playing" before `after` is called;
                        - play() raises ClientException "Not connected to voice." / "Already playing audio.";
                        - disconnect() stops, marks the client disconnected, can wait on a gate (the real one waits for
                          Discord to confirm) and only then removes itself from guild.voice_client;
                        - VoiceChannel.connect() registers guild.voice_client BEFORE its handshake await.
    FakeFFmpeg        replaces discord.FFmpegPCMAudio inside cogs.music; never starts a process.
    time              cogs.music's `time` is replaced by a FakeClock (the asyncio loop keeps the real clock).

A failing test here means cogs/music.py is wrong, not the test: each of those asserts an invariant (for example "a queued track
eventually plays"), not a particular fix. They are plain failures on purpose.
"""

import asyncio
import inspect
import logging
import os
import re
import threading
import types
import unittest
from unittest import mock

import discord
from discord.ext import commands, tasks

from cogs import music
from common import UserError
from navidrome import Navidrome, NavidromeError, Song

BOT_ID = 900
GUILD_ID = 1
ENV = {"NAVIDROME_URL": "http://navidrome.test", "NAVIDROME_USER": "bot", "NAVIDROME_PASS": "pw"}


# ------------------- fakes -------------------

class FakeClock:
    """Stands in for the `time` module inside cogs.music."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSource:
    def __init__(self, url: str, kwargs: dict):
        self.url = url
        self.kwargs = kwargs
        self.cleanups = 0

    def cleanup(self) -> None:
        self.cleanups += 1


class FakeFFmpeg:
    """Replacement for discord.FFmpegPCMAudio: records the sources the cog asks for."""

    def __init__(self):
        self.created: list[FakeSource] = []
        self.fail_with: Exception | None = None

    def __call__(self, url, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        source = FakeSource(url, kwargs)
        self.created.append(source)
        return source


class FakeAudioPlayer(threading.Thread):
    """
    Stand-in for discord.player.AudioPlayer. It lives on its own thread; once told to end (stop(), or the source running out) it
    calls `after` from that thread and only then cleans the source up, like AudioPlayer.run's try/finally.
    """

    def __init__(self, source, after, *, hold: bool):
        super().__init__(daemon=True, name="fake-audio-player")
        self.source = source
        self.after = after
        self.error: Exception | None = None
        self.after_error: BaseException | None = None
        self.after_thread_id: int | None = None
        self._end = threading.Event()
        self._resumed = threading.Event()
        self._resumed.set()
        self._may_call_after = threading.Event()
        if not hold:
            self._may_call_after.set()
        self.after_returned = threading.Event()

    def run(self) -> None:
        self._end.wait()
        self._may_call_after.wait()
        try:
            if self.after is not None:
                self.after_thread_id = threading.get_ident()
                try:
                    self.after(self.error)
                except Exception as exc:  # the real thread logs this and carries on
                    self.after_error = exc
        finally:
            self.source.cleanup()
            self.after_returned.set()

    def is_playing(self) -> bool:
        return self._resumed.is_set() and not self._end.is_set()

    def is_paused(self) -> bool:
        return not self._end.is_set() and not self._resumed.is_set()

    def stop(self) -> None:
        self._end.set()
        self._resumed.set()

    def pause(self) -> None:
        self._resumed.clear()

    def resume(self) -> None:
        self._resumed.set()

    def let_after_run(self) -> None:
        self._may_call_after.set()


class FakeVoiceClient:
    def __init__(self, channel, *, connected: bool = True, hold_afters: bool = False):
        self.channel = channel
        self.guild = channel.guild
        self.connected = connected
        self.hold_afters = hold_afters  # park every `after` until release_afters(): models ffmpeg winding down slowly
        self._player: FakeAudioPlayer | None = None
        self.players: list[FakeAudioPlayer] = []  # every player ever started, oldest first
        self.play_calls = 0
        self.stop_calls = 0
        self.disconnect_calls = 0
        self.move_calls: list = []
        self.moves_succeed = True  # False: move_to() returns quietly without moving, as the library does when its move times out
        self.disconnect_gate: asyncio.Event | None = None
        self.fail_next_play: BaseException | None = None

    # ---- the part of discord.VoiceClient the cog uses
    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self._player is not None and self._player.is_playing()

    def is_paused(self) -> bool:
        return self._player is not None and self._player.is_paused()

    def play(self, source, *, after=None) -> None:
        if self.fail_next_play is not None:
            error, self.fail_next_play = self.fail_next_play, None
            raise error
        if not self.is_connected():
            raise discord.ClientException("Not connected to voice.")
        if self.is_playing():
            raise discord.ClientException("Already playing audio.")
        self.play_calls += 1
        player = FakeAudioPlayer(source, after, hold=self.hold_afters)
        self._player = player
        self.players.append(player)
        player.start()

    def stop(self) -> None:
        self.stop_calls += 1
        if self._player is not None:
            self._player.stop()
            self._player = None

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()

    def resume(self) -> None:
        if self._player is not None:
            self._player.resume()

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnect_calls += 1
        self.stop()
        self.connected = False
        if self.disconnect_gate is not None:
            await self.disconnect_gate.wait()
        if self.guild.voice_client is self:
            self.guild.voice_client = None

    async def move_to(self, channel, *, timeout: float = 30.0) -> None:
        self.move_calls.append(channel)
        if self.moves_succeed:
            self.channel = channel

    # ---- helpers for the tests
    def finish_track(self, error: Exception | None = None) -> None:
        """The source ran out (or ffmpeg died). Like the real player thread, this does NOT go through vc.stop()."""
        player = self._player
        assert player is not None and not player._end.is_set(), "finish_track(): nothing is playing"
        player.error = error
        player.stop()  # VoiceClient._player stays set, exactly as with the real AudioPlayer

    def release_afters(self) -> None:
        for player in self.players:
            player.let_after_run()

    def wait_for_afters(self) -> None:
        """Block until every player that has been told to end (and may call `after`) has done so."""
        for player in self.players:
            if player._end.is_set() and player._may_call_after.is_set():
                assert player.after_returned.wait(5), "a fake audio player never called its after callback"

    def close(self) -> None:
        for player in self.players:
            player.after = None
            player.stop()
            player.let_after_run()
        for player in self.players:
            player.join(5)


class FakeGuild:
    def __init__(self, guild_id: int = GUILD_ID):
        self.id = guild_id
        self.me = mock.MagicMock(spec=discord.Member)
        self.voice_client: FakeVoiceClient | None = None


class FakeMember:
    def __init__(self, member_id: int, name: str, channel=None, *, bot: bool = False, guild=None):
        self.id = member_id
        self.display_name = name
        self.bot = bot
        self.guild = guild
        self.voice = types.SimpleNamespace(channel=channel) if channel is not None else None

    def __repr__(self) -> str:
        return f"<FakeMember {self.display_name}>"


class FakeNavidrome:
    def __init__(self):
        self.closed = False
        self.ping_error: Exception | None = None
        self.search_error: Exception | None = None
        self.results: list[Song] = []
        self.searches: list[tuple] = []

    def stream_url(self, song_id: str) -> str:
        return f"http://navidrome.test/rest/stream?id={song_id}&t=SECRET"

    async def ping(self) -> None:
        if self.ping_error is not None:
            raise self.ping_error

    async def search_songs(self, query: str, count: int = 10) -> list[Song]:
        self.searches.append((query, count))
        if self.search_error is not None:
            raise self.search_error
        return self.results[:count]

    async def close(self) -> None:
        self.closed = True


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCtx:
    def __init__(self, guild, author, channel):
        self.guild = guild
        self.author = author
        self.channel = channel
        self.send = mock.AsyncMock()

    def typing(self) -> FakeTyping:
        return FakeTyping()


class Gate:
    """Parks calls of `channel.send` whose text starts with `prefix` until release() is called (network latency, made deterministic)."""

    def __init__(self, channel, prefix: str):
        self.reached = asyncio.Event()
        self._released = asyncio.Event()

        async def send(content=None, **kwargs):
            if content and content.startswith(prefix):
                self.reached.set()
                await self._released.wait()

        channel.send.side_effect = send

    def release(self) -> None:
        self._released.set()


def song(n: int = 1, **fields) -> Song:
    values = dict(id=f"s{n}", title=f"Title {n}", artist=f"Artist {n}", album=f"Album {n}", duration=200)
    values.update(fields)
    return Song(**values)


def make_text_channel() -> mock.MagicMock:
    channel = mock.MagicMock()
    channel.send = mock.AsyncMock()
    channel.mention = "<#555>"
    return channel


def texts(channel) -> list[str]:
    """Everything posted as plain text (the "⚠️ ..." and "➕ ..." style messages)."""
    return [c.args[0] for c in channel.send.await_args_list if c.args and c.args[0]]


def embeds(channel) -> list[discord.Embed]:
    return [c.kwargs["embed"] for c in channel.send.await_args_list if c.kwargs.get("embed") is not None]


def http_error(cls=discord.HTTPException, status: int = 500, text: str = "boom"):
    return cls(mock.Mock(status=status, reason="reason"), text)


# ------------------- shared fixture -------------------

class MusicTestCase(unittest.IsolatedAsyncioTestCase):
    """A Music cog wired to fake everything: one guild, two voice channels, two members, one text channel."""

    hold_afters = False

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    async def asyncSetUp(self):
        self.clock = FakeClock()
        self.ffmpeg = FakeFFmpeg()
        self.log = mock.Mock(spec=logging.Logger)
        self.vcs: list[FakeVoiceClient] = []
        self.patch(mock.patch.dict(os.environ, ENV))
        self.patch(mock.patch.object(music, "time", self.clock))
        self.patch(mock.patch.object(music, "log", self.log))
        self.patch(mock.patch.object(music.discord, "FFmpegPCMAudio", self.ffmpeg))

        self.bot = mock.MagicMock()
        self.bot.loop = asyncio.get_running_loop()
        self.bot.user = types.SimpleNamespace(id=BOT_ID)
        self.cog = music.Music(self.bot)
        self.navidrome = FakeNavidrome()
        self.cog.navidrome = self.navidrome

        self.guild = FakeGuild()
        self.voice_a = self.make_channel(10)
        self.voice_b = self.make_channel(20)
        self.alice = FakeMember(1, "Alice", self.voice_a, guild=self.guild)
        self.bob = FakeMember(2, "Bob", self.voice_a, guild=self.guild)
        self.text = make_text_channel()

    async def asyncTearDown(self):
        for vc in self.vcs:
            vc.close()
        await self.settle()

    # ---- building blocks
    def make_channel(self, channel_id: int, *, connect: bool = True, speak: bool = True, guild=None, members=None):
        guild = guild or self.guild
        channel = mock.MagicMock(spec=discord.VoiceChannel)
        channel.id = channel_id
        channel.name = f"voice-{channel_id}"
        channel.mention = f"<#{channel_id}>"
        channel.guild = guild
        channel.members = list(members or [])
        channel.permissions_for = mock.Mock(return_value=discord.Permissions(connect=connect, speak=speak))

        async def fake_connect(**kwargs):
            vc = FakeVoiceClient(channel, connected=False, hold_afters=self.hold_afters)
            self.vcs.append(vc)
            guild.voice_client = vc  # discord.abc.Connectable.connect registers the client before the handshake
            await asyncio.sleep(0)  # the handshake: other tasks get to run
            vc.connected = True
            return vc

        channel.connect = mock.AsyncMock(side_effect=fake_connect)
        return channel

    def attach_vc(self, channel=None, **kwargs) -> FakeVoiceClient:
        """Put an already-connected voice client on the guild without going through Music.enqueue."""
        kwargs.setdefault("hold_afters", self.hold_afters)
        vc = FakeVoiceClient(channel or self.voice_a, **kwargs)
        self.vcs.append(vc)
        self.guild.voice_client = vc
        return vc

    def new_player(self) -> music.GuildPlayer:
        player = music.GuildPlayer(self.cog, self.guild, self.text)
        self.cog.players[self.guild.id] = player
        return player

    async def enqueue(self, a_song=None, member=None, text=None):
        return await self.cog.enqueue(self.guild, member or self.alice, text or self.text, a_song or song())

    async def play_tracks(self, count: int):
        """Enqueue `count` songs from Alice (the first one starts at once). Returns (player, [tracks in request order])."""
        tracks = []
        player = None
        for n in range(1, count + 1):
            player, track, _ = await self.enqueue(song(n))
            tracks.append(track)
        return player, tracks

    @property
    def vc(self) -> FakeVoiceClient:
        return self.guild.voice_client

    async def settle(self) -> None:
        """Let everything the player threads scheduled onto the loop (the after callbacks' _advance) run to completion."""
        for vc in self.vcs:
            vc.wait_for_afters()
        for _ in range(300):
            await asyncio.sleep(0)
            if not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]:
                return

    async def spin_until(self, predicate, what: str = "the condition") -> None:
        for _ in range(3000):
            if predicate():
                return
            await asyncio.sleep(0.001)
        self.fail(f"timed out waiting for {what}")


# ------------------- pure helpers -------------------

class FmtDuration(unittest.TestCase):
    def test_edge_values(self):
        cases = {
            0: "?:??",
            -1: "?:??",
            -3600: "?:??",
            1: "0:01",
            59: "0:59",
            60: "1:00",
            61: "1:01",
            599: "9:59",
            3599: "59:59",
            3600: "1:00:00",
            3661: "1:01:01",
            86399: "23:59:59",
            86400: "24:00:00",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(music.fmt_duration(seconds), expected)


class TrackLine(unittest.TestCase):
    def line(self, **fields) -> str:
        return music.Track(song(**fields), "Alice").line

    def test_plain_title_artist_and_length(self):
        self.assertEqual(self.line(title="Hello", artist="World", duration=65), "**Hello** by World (1:05)")

    def test_unknown_length(self):
        self.assertEqual(self.line(title="Hello", artist="World", duration=0), "**Hello** by World (?:??)")

    def test_markdown_in_the_title_and_artist_is_escaped(self):
        line = self.line(title="*bold* _it_ ~~gone~~ ||spoiler|| `code`", artist="__Under__ & *Star*")
        for raw in ("\\*bold\\*", "\\_it\\_", "\\~\\~gone\\~\\~", "\\|\\|spoiler\\|\\|", "\\`code\\`", "\\_\\_Under\\_\\_", "\\*Star\\*"):
            self.assertIn(raw, line)

    def test_a_title_cannot_break_out_of_the_bold_run(self):
        for title in ("x** @everyone **y", "abc\\", "**", "a\\**b"):
            with self.subTest(title=title):
                line = self.line(title=title)
                # Drop every backslash pair (an escaped character); what is left must be exactly the cog's own two ** pairs.
                unescaped = re.sub(r"\\.", "", line)
                self.assertEqual(unescaped.count("*"), 4, line)

    def test_mentions_in_titles_and_artists_cannot_ping(self):
        line = self.line(title="@everyone and @here", artist="<@123456789012345678> <@&123456789012345678>")
        self.assertNotIn("@everyone", line)
        self.assertNotIn("@here", line)
        self.assertNotIn("<@123456789012345678>", line)
        self.assertNotIn("<@&123456789012345678>", line)
        self.assertIn("@​everyone", line)
        self.assertIn("@​here", line)

    def test_tracks_are_compared_by_identity(self):
        a, b = music.Track(song(1), "Alice"), music.Track(song(1), "Alice")
        self.assertIsNot(a, b)
        self.assertNotEqual(a, b)  # eq=False: two requests for the same song are different tracks
        self.assertEqual(len({a, b}), 2)
