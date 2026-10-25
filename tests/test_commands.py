"""
Drives the bot's real discord.py command machinery without a gateway connection.

What is real here: bot.Bot (built by bot.main(), so the same prefix, intents, case_insensitive and
allowed_mentions as production), the three cogs (loaded with bot.load_extension, against a temporary
POKEMON_DB), discord.py's ConnectionState, Guild, TextChannel, Member, User, Message and Interaction
objects (all built from API-shaped payload dicts), its argument parsing and converters, the command
tree, views and components, and Bot.on_command_error.

What is faked is only the transport. FakeDiscord answers the REST calls that bot.http would make
(send message, history, fetch message, typing) and the webhook calls behind interaction responses, and
records the payload discord.py put on the wire (content, embeds, components, allowed_mentions, flags).
A FakeGateway answers the member-chunk requests that discord.py's MemberConverter makes. Anything the
fake does not know about fails the test instead of reaching the network.

Not covered: voice playback (needs a voice gateway and ffmpeg), real Discord's server-side validation.
"""

import asyncio
import contextlib
import copy
import dataclasses
import datetime
import io
import itertools
import logging
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord
from discord.ext import commands
from discord.webhook.async_ import AsyncWebhookAdapter

import config
import navidrome
from cogs import music as musicmod
from cogs import pokemon as pkmmod
from cogs import rules as rulesmod

# bot.py reads a .env file when it is imported; a test run must never pick up the real one.
with mock.patch.object(config, "load_env_file", lambda path: None):
    import bot as botmod

COOLDOWN = 90 * 60
EPHEMERAL = 64
DISCORD_EPOCH = 1_420_070_400_000
APP_ID = 900_000_000_000_000_001
GUILD_ID = 800_000_000_000_000_001
OTHER_GUILD_ID = 800_000_000_000_000_002
DM_CHANNEL_ID = 810_000_000_000_000_001
GUILD_PERMISSIONS = 3230720  # what README's invite link asks for
WATCHED_TASKS = ("discord.py:", "CommandTree-invoker", "discord-ui-view-dispatch")

_sequence = itertools.count(1)


def snowflake() -> int:
    """A fresh increasing ID stamped with the current time (an interaction older than 15 minutes counts as expired)."""
    return ((int(time.time() * 1000) - DISCORD_EPOCH) << 22) | (next(_sequence) & 0x3FFFFF)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclasses.dataclass(frozen=True)
class Person:
    id: int
    username: str
    global_name: str | None = None
    nick: str | None = None
    bot: bool = False

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    @property
    def display_name(self) -> str:
        return self.nick or self.global_name or self.username

    def user(self) -> dict:
        data = {"id": str(self.id), "username": self.username, "discriminator": "0", "global_name": self.global_name, "avatar": None}
        if self.bot:
            data["bot"] = True
        return data

    def member(self, *, with_user: bool = False) -> dict:
        data = {"roles": [], "joined_at": "2024-01-01T00:00:00+00:00", "nick": self.nick, "flags": 0, "deaf": False, "mute": False}
        if with_user:
            data["user"] = self.user()
        return data


BOT_USER = Person(700_000_000_000_000_001, "testbot", "TestBot", bot=True)
ALICE = Person(100_000_000_000_000_001, "alice", "Alice")
BOB = Person(100_000_000_000_000_002, "bob", "Bob")
CARA = Person(100_000_000_000_000_003, "cara", "Cara Q", nick="Caz")
DAN = Person(100_000_000_000_000_004, "dan", "Dan")
ROBOT = Person(100_000_000_000_000_005, "robot", "Robot", bot=True)
OUTSIDER = Person(100_000_000_000_000_099, "outsider", "Outsider")  # exists on Discord, is not in the test server
EVERYONE = [BOT_USER, ALICE, BOB, CARA, DAN, ROBOT]


class FakeResponse:
    """Just enough of aiohttp.ClientResponse for discord.HTTPException."""

    def __init__(self, status: int, reason: str):
        self.status = status
        self.reason = reason


def http_error(status: int, code: int, text: str) -> discord.HTTPException:
    cls = {403: discord.Forbidden, 404: discord.NotFound}.get(status, discord.HTTPException)
    return cls(FakeResponse(status, text), {"code": code, "message": text})


@dataclasses.dataclass
class Sent:
    """One message the bot put on the wire: what discord.py serialised, and the message our fake Discord made of it."""

    kind: str  # "message", "response", "followup" or "edit"
    channel_id: int
    payload: dict
    message: dict

    @property
    def content(self) -> str:
        return self.payload.get("content") or ""

    @property
    def embeds(self) -> list[dict]:
        return self.payload.get("embeds") or []

    @property
    def embed(self) -> dict:
        assert len(self.embeds) == 1, f"expected exactly one embed, got {self.embeds!r}"
        return self.embeds[0]

    @property
    def components(self) -> list[dict]:
        return self.payload.get("components") or []

    @property
    def allowed_mentions(self) -> dict | None:
        return self.payload.get("allowed_mentions")

    @property
    def ephemeral(self) -> bool:
        return bool((self.payload.get("flags") or 0) & EPHEMERAL)

    @property
    def message_id(self) -> int:
        return int(self.message["id"])

    @property
    def items(self) -> list[dict]:
        """Every button and select in the message, row by row."""
        return [item for row in self.components for item in row.get("components", [])]

    @property
    def buttons(self) -> list[dict]:
        """The buttons in the message, left to right."""
        return [item for item in self.items if item["type"] == 2]

    def button(self, label_or_emoji: str) -> dict:
        for item in self.items:
            if item.get("type") == 2 and label_or_emoji in (item.get("label"), (item.get("emoji") or {}).get("name")):
                return item
        raise AssertionError(f"no button {label_or_emoji!r} in {self.items!r}")

    def select(self) -> dict:
        (found,) = [item for item in self.items if item.get("type") == 3]
        return found

    def __repr__(self) -> str:
        return f"<Sent {self.kind} content={self.content!r} embeds={len(self.embeds)} components={len(self.items)}>"


def fields(embed: dict) -> dict[str, str]:
    return {f["name"]: f["value"] for f in embed.get("fields", [])}


def item_payload(item: dict) -> dict:
    """The `data` of a component interaction, as Discord would build it from a button or select."""
    return {"id": item["custom_id"], "type": item["type"], "component_type": item["type"], "custom_id": item["custom_id"]}


class FakeGateway:
    """Answers REQUEST_GUILD_MEMBERS the way Discord does for a query without the members intent: a prefix match."""

    open = False
    shard_id = None

    def __init__(self, server: "FakeDiscord"):
        self.server = server
        self.requests: list[dict] = []

    def is_ratelimited(self) -> bool:
        return False

    async def request_chunks(self, guild_id, query=None, *, limit=0, user_ids=None, presences=False, nonce=None):
        self.requests.append({"guild_id": guild_id, "query": query, "limit": limit, "user_ids": user_ids})
        state = self.server.state
        if user_ids is not None:
            found = [p for p in self.server.members.values() if p.id in set(user_ids)]
        else:
            prefix = (query or "").lower()
            found = [
                p for p in self.server.members.values()
                if any((name or "").lower().startswith(prefix) for name in (p.username, p.nick, p.global_name))
            ]
        found = found[: limit or 100]
        chunk = {
            "guild_id": str(guild_id), "members": [p.member(with_user=True) for p in found],
            "chunk_index": 0, "chunk_count": 1, "nonce": nonce,
        }

        async def deliver():
            # MemberConverter registers its waiter right after request_chunks returns; deliver once it has.
            for _ in range(100):
                request = state._chunk_requests.get(nonce)
                if request is not None and request.waiters:
                    break
                await asyncio.sleep(0)
            state.parse_guild_members_chunk(chunk)

        asyncio.get_running_loop().create_task(deliver(), name="fake-gateway-chunk")


class FakeDiscord:
    """The part of Discord the bot talks to: one guild, a few channels, REST and interaction-webhook endpoints."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.state = bot._connection
        self.members = {p.id: p for p in EVERYONE}
        self.messages: dict[int, list[dict]] = {}  # channel id -> message payloads, oldest first
        self.sent: list[Sent] = []
        self.requests: list[tuple[str, str]] = []
        self.unexpected: list[str] = []
        self.failures: dict[tuple[str, str], BaseException] = {}
        self.interactions: dict[str, dict] = {}
        self.gateway = FakeGateway(self)

        bot.http.request = self.request
        bot.ws = self.gateway
        self.state.application_id = APP_ID
        self.state.user = discord.ClientUser(state=self.state, data=BOT_USER.user())
        self.state._users[BOT_USER.id] = self.state.user

        channel_ids = iter(range(820_000_000_000_000_001, 820_000_000_000_000_100))
        self.general_id, self.rules_id, self.voice_id = next(channel_ids), next(channel_ids), next(channel_ids)
        self.guild = self.state._add_guild_from_data({
            "id": str(GUILD_ID), "name": "Test Server", "owner_id": str(ALICE.id), "member_count": len(EVERYONE),
            "unavailable": False, "features": [], "premium_tier": 0, "preferred_locale": "en-US",
            "roles": [{"id": str(GUILD_ID), "name": "@everyone", "permissions": str(GUILD_PERMISSIONS), "position": 0,
                       "color": 0, "hoist": False, "managed": False, "mentionable": False, "flags": 0}],
            "channels": [
                self.text_channel_payload(self.general_id, "general", 0),
                self.text_channel_payload(self.rules_id, "rules", 1),
                {"id": str(self.voice_id), "type": 2, "name": "Music", "position": 2, "permission_overwrites": [],
                 "bitrate": 64000, "user_limit": 0, "rtc_region": None},
            ],
            "members": [BOT_USER.member(with_user=True)],
            "emojis": [], "stickers": [],
        })
        for channel in self.guild.channels:
            self.messages[channel.id] = []
        self.dm = self.state.add_dm_channel({"id": str(DM_CHANNEL_ID), "type": 1, "recipients": [ALICE.user()]})
        self.messages[DM_CHANNEL_ID] = []

    # ---------- the world ----------

    @staticmethod
    def text_channel_payload(channel_id: int, name: str, position: int) -> dict:
        return {"id": str(channel_id), "type": 0, "name": name, "position": position, "permission_overwrites": [],
                "nsfw": False, "topic": None, "parent_id": None, "last_message_id": None, "rate_limit_per_user": 0}

    @property
    def general(self) -> discord.TextChannel:
        return self.guild.get_channel(self.general_id)

    @property
    def rules(self) -> discord.TextChannel:
        return self.guild.get_channel(self.rules_id)

    def add_text_channel(self, name: str, position: int) -> discord.TextChannel:
        channel = discord.TextChannel(guild=self.guild, state=self.state, data=self.text_channel_payload(snowflake(), name, position))
        self.guild._add_channel(channel)
        self.messages[channel.id] = []
        return channel

    def remove_channel(self, channel: discord.TextChannel) -> None:
        self.guild._remove_channel(channel)

    def post(self, channel, author: Person, content: str) -> discord.Message:
        """A plain message somebody posted into a channel's history."""
        return self.incoming(content, author, channel)

    def incoming(self, content: str, author: Person, channel, *, attachments=(), embeds=(), type: int = 0) -> discord.Message:
        """A message somebody posted: stored in the channel's history, and returned as discord.py's Message."""
        in_guild = isinstance(channel, discord.TextChannel)
        payload = {
            "id": str(snowflake()), "channel_id": str(channel.id), "author": author.user(), "content": content,
            "timestamp": now_iso(), "edited_timestamp": None, "tts": False,
            "mention_everyone": "@everyone" in content or "@here" in content,
            "mentions": [], "mention_roles": [], "attachments": list(attachments), "embeds": list(embeds),
            "pinned": False, "type": type, "flags": 0,
        }
        if in_guild:
            payload["guild_id"] = str(GUILD_ID)
            payload["member"] = author.member()
        for match in re.finditer(r"<@!?([0-9]{15,20})>", content):
            person = self.members.get(int(match.group(1))) or (OUTSIDER if int(match.group(1)) == OUTSIDER.id else None)
            if person is not None:
                mention = person.user()
                if in_guild and person.id in self.members:
                    mention["member"] = person.member()
                payload["mentions"].append(mention)
        self.messages[channel.id].append(copy.deepcopy(payload))
        return self.state.create_message(channel=channel, data=payload)

    # ---------- REST ----------

    async def request(self, route, *, files=None, form=None, **kwargs):
        key = (route.method, route.path)
        self.requests.append(key)
        if key in self.failures:
            raise self.failures[key]
        handlers = {
            ("POST", "/channels/{channel_id}/messages"): self._send_message,
            ("GET", "/channels/{channel_id}/messages"): self._history,
            ("GET", "/channels/{channel_id}/messages/{message_id}"): self._get_message,
            ("PATCH", "/channels/{channel_id}/messages/{message_id}"): self._edit_message,
            ("POST", "/channels/{channel_id}/typing"): self._typing,
        }
        handler = handlers.get(key)
        if handler is None:
            self.unexpected.append(f"{route.method} {route.url}")
            raise AssertionError(f"the bot made an HTTP request the fake Discord does not know: {route.method} {route.url}")
        return handler(route, kwargs)

    def make_message(self, channel_id: int, data: dict) -> dict:
        message = {
            "id": str(snowflake()), "channel_id": str(channel_id), "author": BOT_USER.user(),
            "content": data.get("content") or "", "timestamp": now_iso(), "edited_timestamp": None, "tts": bool(data.get("tts")),
            "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [],
            "embeds": copy.deepcopy(data.get("embeds") or []), "pinned": False, "type": 0, "flags": data.get("flags") or 0,
            "components": copy.deepcopy(data.get("components") or []),
        }
        if channel_id != DM_CHANNEL_ID:
            message["guild_id"] = str(GUILD_ID)
        return message

    def _find(self, channel_id: int, message_id: int) -> dict | None:
        return next((m for m in self.messages.get(channel_id, []) if int(m["id"]) == message_id), None)

    def _send_message(self, route, kwargs):
        payload = copy.deepcopy(kwargs["json"])
        channel_id = int(route.channel_id)
        message = self.make_message(channel_id, payload)
        self.messages.setdefault(channel_id, []).append(copy.deepcopy(message))
        self.sent.append(Sent("message", channel_id, payload, message))
        return copy.deepcopy(message)

    def _history(self, route, kwargs):
        key = ("GET", f"/channels/{route.channel_id}/messages")
        if key in self.failures:  # e.g. a channel id outside the guild: Discord answers 400/10003
            raise self.failures.pop(key)
        params = kwargs.get("params") or {}
        everything = self.messages.get(int(route.channel_id), [])
        limit = int(params.get("limit", 50))
        if "after" in params:  # Discord returns the `limit` messages just after the ID, newest first
            page = [m for m in everything if int(m["id"]) > int(params["after"])][:limit]
        elif "before" in params:
            page = [m for m in everything if int(m["id"]) < int(params["before"])][-limit:]
        else:
            page = everything[-limit:]
        return copy.deepcopy(sorted(page, key=lambda m: int(m["id"]), reverse=True))

    def _get_message(self, route, kwargs):
        message_id = int(re.search(r"/messages/(\d+)$", route.url).group(1))
        found = self._find(int(route.channel_id), message_id)
        if found is None:
            raise http_error(404, 10008, "Unknown Message")
        return copy.deepcopy(found)

    def _edit_message(self, route, kwargs):
        message_id = int(re.search(r"/messages/(\d+)$", route.url).group(1))
        found = self._find(int(route.channel_id), message_id)
        if found is None:
            raise http_error(404, 10008, "Unknown Message")
        payload = copy.deepcopy(kwargs["json"])
        for key in ("content", "embeds", "components", "flags"):
            if key in payload:
                found[key] = payload[key] if payload[key] is not None else ""
        self.sent.append(Sent("edit", int(route.channel_id), payload, copy.deepcopy(found)))
        return copy.deepcopy(found)

    def _typing(self, route, kwargs):
        return None

    # ---------- interaction webhooks ----------

    async def webhook_request(self, route, **kwargs):
        key = (route.method, route.path)
        self.requests.append(key)
        if key in self.failures:
            raise self.failures[key]
        token = route.webhook_token
        handlers = {
            ("POST", "/interactions/{webhook_id}/{webhook_token}/callback"): self._callback,
            ("GET", "/webhooks/{webhook_id}/{webhook_token}/messages/@original"): self._get_original,
            ("PATCH", "/webhooks/{webhook_id}/{webhook_token}/messages/@original"): self._edit_original,
            ("POST", "/webhooks/{webhook_id}/{webhook_token}"): self._followup,
            ("PATCH", "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"): self._edit_followup,
        }
        handler = handlers.get(key)
        if handler is None or token not in self.interactions:
            self.unexpected.append(f"{route.method} {route.url}")
            raise AssertionError(f"the bot made a webhook request the fake Discord does not know: {route.method} {route.url}")
        return handler(self.interactions[token], route, kwargs)

    def _publish(self, interaction: dict, kind: str, data: dict) -> dict:
        message = self.make_message(interaction["channel_id"], data)
        if not (message["flags"] & EPHEMERAL):
            self.messages[interaction["channel_id"]].append(copy.deepcopy(message))
        interaction["messages"][message["id"]] = message
        self.sent.append(Sent(kind, interaction["channel_id"], copy.deepcopy(data), copy.deepcopy(message)))
        return message

    def _callback(self, interaction, route, kwargs):
        body = copy.deepcopy(kwargs["payload"])
        kind, data = body["type"], body.get("data") or {}
        info = {"id": str(interaction["id"]), "type": interaction["type"]}
        if kind == 4:  # channel message with source
            message = self._publish(interaction, "response", data)
            interaction["original"] = message
            info.update(response_message_id=message["id"], response_message_loading=False, response_message_ephemeral=bool(message["flags"] & EPHEMERAL))
            return {"interaction": info, "resource": {"type": 4, "message": copy.deepcopy(message)}}
        if kind == 5:  # deferred channel message ("thinking")
            interaction["deferred_flags"] = data.get("flags", 0)
            info.update(response_message_loading=True, response_message_ephemeral=bool(data.get("flags", 0) & EPHEMERAL))
            return {"interaction": info, "resource": {"type": 5}}
        if kind == 6:  # deferred message update (component press)
            return {"interaction": info, "resource": {"type": 6}}
        if kind == 7:  # update the message a component sits on
            message = interaction["component_message"]
            for key in ("content", "embeds", "components", "flags"):
                if key in data:
                    message[key] = data[key] if data[key] is not None else ""
            stored = self._find(interaction["channel_id"], int(message["id"]))
            if stored is not None:
                stored.update(copy.deepcopy(message))
            self.sent.append(Sent("edit", interaction["channel_id"], copy.deepcopy(data), copy.deepcopy(message)))
            info.update(response_message_id=message["id"])
            return {"interaction": info, "resource": {"type": 7, "message": copy.deepcopy(message)}}
        raise AssertionError(f"unhandled interaction callback type {kind}")

    def _get_original(self, interaction, route, kwargs):
        if interaction["original"] is None:
            raise http_error(404, 10015, "Unknown Webhook")
        return copy.deepcopy(interaction["original"])

    def _edit_original(self, interaction, route, kwargs):
        data = copy.deepcopy(kwargs["payload"])
        original = interaction["original"]
        if original is None:  # editing the deferred "thinking" message creates it
            data["flags"] = interaction.get("deferred_flags", 0) | (data.get("flags") or 0)
            original = interaction["original"] = self._publish(interaction, "response", data)
            return copy.deepcopy(original)
        return self._apply_edit(interaction, original, data)

    def _apply_edit(self, interaction, message, data):
        for key in ("content", "embeds", "components", "flags"):
            if key in data:
                message[key] = data[key] if data[key] is not None else ""
        stored = self._find(interaction["channel_id"], int(message["id"]))
        if stored is not None:
            stored.update(copy.deepcopy(message))
        self.sent.append(Sent("edit", interaction["channel_id"], copy.deepcopy(data), copy.deepcopy(message)))
        return copy.deepcopy(message)

    def _followup(self, interaction, route, kwargs):
        data = copy.deepcopy(kwargs["payload"])
        if interaction["original"] is None and "deferred_flags" in interaction:
            # The first followup after a defer replaces the "thinking" message; its ephemeral flag is the defer's.
            data["flags"] = interaction["deferred_flags"]
            message = self._publish(interaction, "followup", data)
            interaction["original"] = message
            return copy.deepcopy(message)
        return copy.deepcopy(self._publish(interaction, "followup", data))

    def _edit_followup(self, interaction, route, kwargs):
        message_id = re.search(r"/messages/(\d+)$", route.url).group(1)
        return self._apply_edit(interaction, interaction["messages"][message_id], copy.deepcopy(kwargs["payload"]))

    async def webhook_adapter_request(self, route, **kwargs):
        return await self.webhook_request(route, **kwargs)

    # ---------- interactions ----------

    def interaction_payload(self, kind: int, data: dict, user: Person, channel, *, message: dict | None = None) -> dict:
        interaction_id = snowflake()
        token = f"fake-interaction-token-{interaction_id}"
        in_guild = isinstance(channel, discord.TextChannel)
        payload = {
            "id": str(interaction_id), "application_id": str(APP_ID), "type": kind, "data": data, "token": token, "version": 1,
            "attachment_size_limit": 10 * 1024 * 1024, "locale": "en-US", "app_permissions": str(GUILD_PERMISSIONS),
            "entitlements": [], "context": 0 if in_guild else 1,
            "channel": {"id": str(channel.id), "type": 0 if in_guild else 1},
        }
        if in_guild:
            payload.update(guild_id=str(GUILD_ID), guild_locale="en-US", authorizing_integration_owners={"0": str(GUILD_ID)})
            payload["member"] = {**user.member(with_user=True), "permissions": str(GUILD_PERMISSIONS)}
        else:
            payload["user"] = user.user()
        if message is not None:
            payload["message"] = copy.deepcopy(message)
        self.interactions[token] = {
            "id": interaction_id, "type": kind, "channel_id": channel.id, "original": None, "messages": {},
            "component_message": copy.deepcopy(message) if message is not None else None,
        }
        return payload


def make_bot() -> botmod.Bot:
    """Build the bot exactly as main() does, but never connect: main() hands it to Bot.run, which we intercept."""
    with mock.patch.dict(os.environ, {"DISCORD_TOKEN": "not-a-real-token"}), mock.patch.object(botmod.Bot, "run", autospec=True) as run:
        botmod.main()
    (bot,) = run.call_args.args[:1]
    return bot


class BotCase(unittest.IsolatedAsyncioTestCase):
    """A bot with all three cogs loaded, talking to a FakeDiscord."""

    load_cogs = True

    async def asyncSetUp(self):
        self.errors: list[tuple[str, BaseException | None]] = []  # exceptions that escaped into discord.py's event/view/tree handlers
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "pokemon.db"
        env = self.enterContext(mock.patch.dict(os.environ, {
            "NAVIDROME_URL": "http://127.0.0.1:9", "NAVIDROME_USER": "test-user", "NAVIDROME_PASS": "test-pass",
            "POKEMON_DB": str(self.db_path),
        }))
        for name in ("GUILD_ID", "RULES_CHANNEL_ID", "RULES_MESSAGE_ID", "DISCORD_TOKEN"):
            os.environ.pop(name, None)
        self.enterContext(mock.patch.object(navidrome.Navidrome, "ping", mock.AsyncMock(return_value=None)))

        self.bot = make_bot()
        await self.bot._async_setup_hook()  # what `async with bot:` / login does: binds the running loop
        self.server = FakeDiscord(self.bot)
        self.enterContext(mock.patch.object(AsyncWebhookAdapter, "request", self.server.webhook_adapter_request))

        async def on_error(event_method, *args, **kwargs):
            self.errors.append((event_method, sys.exc_info()[1]))

        async def tree_error(interaction, error):
            self.errors.append(("app command", error))

        async def view_error(view, interaction, error, item):
            self.errors.append((f"view {type(view).__name__}", error))

        self.bot.on_error = on_error
        self.bot.tree.on_error = tree_error
        self.enterContext(mock.patch.object(discord.ui.view.BaseView, "on_error", view_error))

        self.stdout = io.StringIO()
        if self.load_cogs:
            with contextlib.redirect_stdout(self.stdout):
                for name in botmod.COGS:
                    await self.bot.load_extension(name)
            self.rules_cog = self.bot.get_cog("Rules")
            self.music = self.bot.get_cog("Music")
            self.game = self.bot.get_cog("PokemonGame")
            # load_extension execs a *copy* of each cog module; the top-level imports in this file are a
            # different object with the same __name__. Patches (env constants, clocks) must target the
            # copy the bot actually ran, which is each cog's own __globals__.
            self.rulesmod = vars(sys.modules[self.rules_cog.__class__.__module__])
            self.musicmod = vars(sys.modules[self.music.__class__.__module__])
            self.pkm = vars(sys.modules[self.game.__class__.__module__])
            self.store = self.game.store
            self.dex = self.game.dex
            self.music.reaper.cancel()  # its first act would be to wait for a gateway READY that never comes
            await asyncio.sleep(0)
        self._catches: dict[int, int] = {}

    async def asyncTearDown(self):
        store = getattr(self, "store", None)
        try:
            for name in list(self.bot.extensions):
                await self.bot.unload_extension(name)
            if store is not None:
                self.assertIsNone(store._db, "unloading cogs.pokemon must close the Store")
        finally:
            if store is not None:
                await store.close()  # aiosqlite's worker thread would otherwise keep the process alive
            await self.bot.close()
        self.assertEqual(self.server.unexpected, [], "HTTP requests the fake Discord did not expect")
        self.assertEqual(self.errors, [], "exceptions that escaped into discord.py's handlers")
        self.assert_nothing_pinged_by_accident()

    # ---------- feeding the bot ----------

    async def settle(self) -> None:
        """Wait for the event-handler, command-tree and view-dispatch tasks discord.py spawns (error handlers, buttons)."""
        for _ in range(50):
            me = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not me and not t.done() and t.get_name().startswith(WATCHED_TASKS)]
            if not pending:
                return
            _, still = await asyncio.wait(pending, timeout=10)
            if still:
                raise AssertionError(f"still running after 10s: {[t.get_name() for t in still]}")
        raise AssertionError("event handlers keep spawning more event handlers")

    async def run_message(self, message: discord.Message) -> list[Sent]:
        start = len(self.server.sent)
        async with asyncio.timeout(20):
            await self.bot.process_commands(message)
            await self.settle()
        return self.server.sent[start:]

    async def say(self, content: str, author: Person = ALICE, *, channel=None, **extra) -> list[Sent]:
        """Type `content` in a channel (default #general) and return what the bot sent in response."""
        message = self.server.incoming(content, author, channel or self.server.general, **extra)
        return await self.run_message(message)

    async def dm(self, content: str, author: Person = ALICE) -> list[Sent]:
        return await self.run_message(self.server.incoming(content, author, self.server.dm))

    async def say_one(self, content: str, author: Person = ALICE, **kwargs) -> Sent:
        sent = await self.say(content, author, **kwargs)
        self.assertEqual(len(sent), 1, f"expected exactly one reply to {content!r}, got {sent!r}")
        return sent[0]

    # ---------- seeding the game ----------

    async def give(self, person: Person, name_or_id) -> "Caught":
        """Put a Pokemon in somebody's box without touching their catch cooldown."""
        species = self.dex[name_or_id] if isinstance(name_or_id, int) else self.dex.find(name_or_id)[0]
        n = self._catches[person.id] = self._catches.get(person.id, 0) + 1
        result = await self.store.catch(person.id, species.id, now=10_000 + n * COOLDOWN, cooldown=COOLDOWN, guild_id=GUILD_ID)
        self.assertIsNotNone(result.pokemon)
        return result.pokemon

    # ---------- interactions ----------

    async def tap(self, sent: Sent, user: Person, label_or_emoji: str, *, channel=None) -> list[Sent]:
        """Press a button on a message the bot sent, as `user`, and return what the bot sent back."""
        return await self.dispatch_component(sent, user, item_payload(sent.button(label_or_emoji)), channel=channel)

    async def pick(self, sent: Sent, user: Person, value: str, *, channel=None) -> list[Sent]:
        """Choose a dropdown option on a message the bot sent, as `user`."""
        data = item_payload(sent.select())
        data["values"] = [value]
        return await self.dispatch_component(sent, user, data, channel=channel)

    async def dispatch_component(self, sent: Sent, user: Person, data: dict, *, channel=None) -> list[Sent]:
        start = len(self.server.sent)
        payload = self.server.interaction_payload(3, data, user, channel or self.server.general, message=sent.message)
        self.bot._connection.parse_interaction_create(payload)
        await self.settle()
        return self.server.sent[start:]

    async def slash(self, name: str, user: Person, *, channel=None) -> list[Sent]:
        """Run a slash command the way the command tree would."""
        start = len(self.server.sent)
        data = {"id": 1, "name": name, "type": 1, "options": []}
        payload = self.server.interaction_payload(2, data, user, channel or self.server.general)
        self.bot._connection.parse_interaction_create(payload)
        await self.settle()
        return self.server.sent[start:]

    # ---------- invariants ----------

    def assert_nothing_pinged_by_accident(self) -> None:
        """Nothing the bot sends may ping @everyone, roles or arbitrary users; the one exception is a trade request's single user."""
        for sent in self.server.sent:
            if sent.kind == "edit":
                continue
            allowed = sent.allowed_mentions
            self.assertIsNotNone(allowed, f"no allowed_mentions on the wire for {sent!r}")
            self.assertEqual(allowed.get("parse"), [], f"{sent!r} may ping everyone/roles/users by parse: {allowed}")
            self.assertFalse(allowed.get("roles"), f"{sent!r} may ping roles: {allowed}")
            self.assertFalse(allowed.get("replied_user"), f"{sent!r} may ping the replied-to user: {allowed}")
            users = allowed.get("users") or []
            if users:
                self.assertEqual(len(users), 1, allowed)
                self.assertEqual(sent.content, f"<@{users[0]}>", f"{sent!r} allows a ping the content does not ask for")


# =====================================================================
# rules
# =====================================================================


class Rules(BotCase):
    async def test_plain_message_becomes_an_embed_from_the_oldest(self):
        self.server.incoming("be nice to each other", ALICE, self.server.rules)
        self.server.incoming("a newer rules post", BOB, self.server.rules)
        reply = await self.say_one("!rules")
        embed = reply.embed
        self.assertEqual(embed["title"], "Server Rules")
        self.assertEqual(embed["description"], "be nice to each other")
        self.assertEqual(embed["footer"]["text"], "From #rules")
        self.assertEqual(reply.channel_id, self.server.general_id)

    async def test_an_existing_embed_is_reused_as_is(self):
        original = {"title": "Server Rules", "description": "Be nice", "color": 0x5865F2}
        self.server.incoming("", ALICE, self.server.rules, embeds=[original])
        reply = await self.say_one(";rules")
        self.assertEqual(reply.embed["title"], "Server Rules")
        self.assertEqual(reply.embed["description"], "Be nice")
        self.assertIsNone(reply.embed.get("footer"))  # reused, not rebuilt

    async def test_no_rules_channel_is_explained(self):
        self.server.remove_channel(self.server.rules)
        reply = await self.say_one("!rules")
        self.assertEqual(reply.content, "I couldn't find a rules channel. Set RULES_CHANNEL_ID.")

    async def test_an_empty_rules_channel_is_explained(self):
        reply = await self.say_one("!rules")
        self.assertEqual(reply.content, f"<#{self.server.rules.id}> has no messages yet.")

    async def test_environment_ids_take_priority(self):
        self.server.post(self.server.general, ALICE, "the rules, per env")
        with mock.patch.dict(self.rulesmod, {"RULES_CHANNEL_ID": self.server.general_id, "RULES_MESSAGE_ID": None}):
            reply = await self.say_one("!rules")
        self.assertEqual(reply.embed["description"], "the rules, per env")
        self.assertEqual(reply.embed["footer"]["text"], "From #general")

    async def test_missing_rules_message_is_reported(self):
        with mock.patch.dict(self.rulesmod, {"RULES_CHANNEL_ID": self.server.general_id, "RULES_MESSAGE_ID": 999_999}):
            reply = await self.say_one("!rules")
        self.assertEqual(reply.content, "I couldn't find that rules message.")

    async def test_no_access_to_the_rules_channel(self):
        # A RULES_CHANNEL_ID the bot can't see: Discord answers the history request with 403/50001.
        # The fake server keys injected failures by route template, the same shape the requests use.
        with mock.patch.dict(self.rulesmod, {"RULES_CHANNEL_ID": 88_000_000_000_000_000, "RULES_MESSAGE_ID": None}):
            self.server.failures[("GET", "/channels/{channel_id}/messages")] = http_error(403, 50001, "Missing Permissions")
            reply = await self.say_one("!rules")
        self.assertEqual(reply.content, "I can't read <#820000000000000002>.")


# =====================================================================
# music
# =====================================================================


class Music(BotCase):
    def searching(self, songs):
        return mock.patch.object(self.music.navidrome, "search_songs", mock.AsyncMock(return_value=songs))

    @staticmethod
    def songs(n):
        return [navidrome.Song(id=f"song{i}", title=f"Track {i}", artist="The Test Artist", album="Test Album", duration=61 + 37 * i) for i in range(n)]

    async def test_search_lists_results_with_a_picker(self):
        with self.searching(self.songs(12)):  # the library loves us; the cog keeps the top 10
            reply = await self.say_one("!search track")
        lines = reply.embed["description"].split("\n")
        self.assertEqual(len(lines), musicmod.SEARCH_RESULTS)
        self.assertEqual(lines[0], "`1.` **Track 0** — The Test Artist · Test Album (1:01)")
        options = reply.select()["options"]
        self.assertEqual([o["value"] for o in options], [str(i) for i in range(musicmod.SEARCH_RESULTS)])  # the menu caps at 10
        self.assertEqual(options[0]["label"], "Track 0 — The Test Artist")

    async def test_only_the_person_who_searched_can_pick(self):
        with self.searching(self.songs(3)):
            reply = await self.say_one("!search track")
        wrong = await self.pick(reply, BOB, "0")
        self.assertEqual(wrong[0].kind, "response")
        self.assertTrue(wrong[0].ephemeral)
        self.assertEqual(wrong[0].content, "Only the person who searched can pick.")

    async def test_picking_a_song_without_joining_voice(self):
        with self.searching(self.songs(3)):
            reply = await self.say_one("!search track")
        queued = await self.pick(reply, ALICE, "2")
        self.assertEqual(queued[0].kind, "followup")
        self.assertTrue(queued[0].ephemeral)
        self.assertEqual(queued[0].content, "⚠️ Join a voice channel first.")

    async def test_play_with_no_results(self):
        with self.searching([]):
            reply = await self.say_one("!play nothing")
        self.assertEqual(reply.content, "⚠️ No songs found for **nothing**.")

    async def test_play_without_voice_is_an_error(self):
        with self.searching(self.songs(1)):
            reply = await self.say_one("!play track 0")
        self.assertEqual(reply.content, "⚠️ Join a voice channel first.")

    async def test_control_commands_need_a_playing_bot(self):
        reply = await self.say_one("!q")
        self.assertEqual(reply.content, "The queue is empty.")
        reply = await self.say_one("!np")
        self.assertEqual(reply.content, "Nothing is playing.")
        reply = await self.say_one("!skip")
        self.assertEqual(reply.content, "⚠️ I'm not playing anything.")

    async def test_queue_lists_queued_tracks(self):
        player = musicmod.GuildPlayer(self.music, self.server.guild, self.server.general)
        self.music.players[self.server.guild.id] = player
        player.queue.append(musicmod.Track(self.songs(1)[0], "Alice"))
        reply = await self.say_one("!queue")
        self.assertEqual(reply.embed["title"], "Queue")
        self.assertEqual(reply.embed["description"], "`1.` **Track 0** by The Test Artist (1:01)")


class MusicOff(BotCase):
    load_cogs = False

    async def test_no_settings_means_no_music_cog(self):
        with mock.patch.dict(os.environ, {"NAVIDROME_URL": "", "NAVIDROME_USER": "", "NAVIDROME_PASS": ""}), contextlib.redirect_stdout(self.stdout):
            await self.bot.load_extension("cogs.music")
        self.assertIsNone(self.bot.get_cog("Music"))
        self.assertIn("Music is off", self.stdout.getvalue())


# =====================================================================
# pokemon
# =====================================================================


class Catch(BotCase):
    async def test_catch_reports_rarity_and_cooldown(self):
        pikachu = self.dex[25]
        clock = {"t": 1_000_000}
        with mock.patch.object(pkmmod.time, "time", lambda: clock["t"]), mock.patch.object(self.dex, "roll", return_value=pikachu):
            reply = await self.say_one("!pokemon")
            embed = reply.embed
            self.assertEqual(embed["title"], "Alice caught Pikachu!")
            f = fields(embed)
            self.assertEqual(f["Rarity"], "⚪ Common")
            self.assertEqual(f["Type"], "Electric")
            self.assertEqual(f["Pokédex"], "No. 0025")
            self.assertEqual(f["ID"], "`#1`")
            self.assertIn("✨ New Pokédex entry!", f)  # first catch of a species
            self.assertEqual(embed["footer"]["text"], "Pokédex 1/1025 · you can catch again in 90 minutes")

            # The cooldown bites immediately. (Prefix replies can't be ephemeral: discord.py only
            # honours `ephemeral` for interaction contexts, and silently ignores it otherwise.)
            blocked = await self.say_one(";catch")
            self.assertEqual(blocked.content, "⏳ Your next catch is ready in **1h 30m** (<t:1005400:t>).")
            self.assertEqual((await self.store.profile(ALICE.id)).total_caught, 1)

            # And it lifts on time.
            clock["t"] = 1_005_401
            again = await self.say_one(";pokemon")
            self.assertEqual(again.embed["title"], "Alice caught Pikachu!")
            self.assertNotIn("✨ New Pokédex entry!", fields(again.embed))  # already registered

    async def test_cooldown_is_per_person(self):
        pikachu = self.dex[25]
        with mock.patch.object(self.dex, "roll", return_value=pikachu), mock.patch.object(pkmmod.time, "time", lambda: 1_000_000):
            first = await self.say_one(";pokemon", author=BOB)
            self.assertEqual(fields(first.embed)["ID"], "`#1`")  # Bob owns it, not Alice
            self.assertEqual((await self.store.profile(ALICE.id)).total_caught, 0)
            second = await self.say_one(";pokemon", author=ALICE)
            self.assertEqual(fields(second.embed)["ID"], "`#2`")
            self.assertEqual([c.owner_id for c in await self.store.owned(ALICE.id)], [ALICE.id])
            self.assertEqual([c.owner_id for c in await self.store.owned(BOB.id)], [BOB.id])

    async def test_a_new_species_lands_in_the_trainers_own_pokedex(self):
        pikachu, mew = self.dex[25], self.dex[151]
        clock = {"t": 1_000_000}
        with mock.patch.object(pkmmod.time, "time", lambda: clock["t"]), mock.patch.object(self.dex, "roll", side_effect=[pikachu, pikachu, mew]):
            first = await self.say_one(";pokemon")
            self.assertIn("✨ New Pokédex entry!", fields(first.embed))  # a species Alice didn't have
            self.assertEqual(first.embed["footer"]["text"], "Pokédex 1/1025 · you can catch again in 90 minutes")
            clock["t"] += COOLDOWN
            again = await self.say_one(";catch")  # same species again: a new Pokémon, not a new entry
            self.assertEqual(fields(again.embed)["ID"], "`#2`")
            self.assertNotIn("✨ New Pokédex entry!", fields(again.embed))
            self.assertEqual(again.embed["footer"]["text"], "Pokédex 1/1025 · you can catch again in 90 minutes")
            clock["t"] += COOLDOWN
            new_species = await self.say_one(";catch")
            self.assertIn("✨ New Pokédex entry!", fields(new_species.embed))
            self.assertEqual(new_species.embed["footer"]["text"], "Pokédex 2/1025 · you can catch again in 90 minutes")


class Inventory(BotCase):
    async def test_empty_inventory(self):
        reply = await self.say_one(";inv", author=BOB)
        self.assertEqual(reply.content, "⚠️ You don't have any Pokémon yet. Try `;pokemon`!")

    async def test_single_pokemon_has_no_pages(self):
        p = await self.give(ALICE, "pikachu")
        reply = await self.say_one(";inv")
        self.assertFalse(reply.components)
        embed = reply.embed
        self.assertEqual(embed["title"], "Alice's Pokémon")
        self.assertEqual(embed["description"], f"`#{p.id}` ⚪ **Pikachu** · No. 0025 · Electric · Gen I")
        self.assertEqual(embed["footer"]["text"], "Page 1/1 · 1 Pokémon · Newest first")

    def of(self, sent, kind: str) -> list:
        """The sends of one kind out of a component response (a defer, an edit, a follow-up...)."""
        return [s for s in sent if s.kind == kind]

    async def test_pages_sort_and_filter(self):
        for name in ("pikachu", "charizard", "mew", "mewtwo", "snorlax", "gyarados", "lapras", "eevee", "vaporeon", "aerodactyl", "ditto"):
            await self.give(BOB, name)
        self.server.members[BOB.id] = BOB  # so the menu's replies can resolve the trainer
        first = await self.say_one(";inv", author=BOB)
        self.assertTrue(first.components)  # pagers only get buttons when there is more than one page
        self.assertEqual(first.embed["footer"]["text"], "Page 1/2 · 11 Pokémon · Newest first")
        self.assertIn("ditto", first.embed["description"].lower())  # newest first

        second = await self.tap(first, BOB, "▶️")
        self.assertEqual(second[0].kind, "edit")
        self.assertEqual(second[0].embed["footer"]["text"], "Page 2/2 · 11 Pokémon · Newest first")

        # Rarest first puts the Mythical at the top of page one again. The tap answers with a defer,
        # the page itself is the edit that follows it.
        sorted_reply = await self.pick(first, BOB, "rarity")
        edit = self.of(sorted_reply, "edit")[0]
        self.assertEqual(edit.embed["footer"]["text"], "Page 1/2 · 11 Pokémon · Rarest first")
        self.assertEqual(edit.embed["description"].split("\n")[0], "`#3` 🟡 **Mew** · No. 0151 · Psychic · Gen I")  # the mythical tops a rarity sort (#ID first, as the inventory lists them)

        # Someone else can't drive your menu.
        wrong = await self.pick(first, CARA, "rarity")
        self.assertEqual(wrong[0].content, "That's not your menu. Run the command yourself to get one.")  # ephemeral in real Discord; prefix sends can't be

    async def test_filter_matches_names_and_numbers(self):
        await self.give(BOB, "pikachu")
        await self.give(BOB, "charizard")
        hit = await self.say_one(";inv pika", author=BOB)
        self.assertEqual(hit.embed["description"], "`#1` ⚪ **Pikachu** · No. 0025 · Electric · Gen I")
        self.assertIn("matching “pika”", hit.embed["footer"]["text"])
        miss = await self.say_one(";inv nothing", author=BOB)
        self.assertEqual(miss.content, "⚠️ No Pokémon matching “nothing”.")

    async def test_looking_at_someone_else(self):
        await self.give(BOB, "pikachu")
        reply = await self.say_one(f";inv {BOB.mention}")
        self.assertEqual(reply.embed["title"], "Bob's Pokémon")
        self.assertIn(f"`#{1}` ⚪ **Pikachu**", reply.embed["description"])
        self.assert_nothing_pinged_by_accident()  # looking at someone's page is not a mention


class Info(BotCase):
    async def test_info_shows_who_caught_it_and_who_has_it_now(self):
        p = await self.give(ALICE, "pikachu")
        reply = await self.say_one(";info 1")
        embed = reply.embed
        self.assertEqual(embed["title"], "Pikachu · No. 0025")
        f = fields(embed)
        self.assertEqual(f["Owner"], f"<@{ALICE.id}>")
        self.assertEqual(f["History"], f"Caught by <@{ALICE.id}> on <t:{p.caught_at}:D>")
        self.assertEqual(embed["footer"]["text"], "ID #1")
        self.assert_nothing_pinged_by_accident()  # <@id> only pings when allowed_mentions says so

    async def test_info_by_number_and_missing_id(self):
        await self.give(ALICE, "charizard")  # Alice's one Pokémon; its #ID is 1, its species is No. 0006
        reply = await self.say_one(";info 1")  # the #ID the inventory shows
        self.assertEqual(reply.embed["title"], "Charizard · No. 0006")
        self.assertEqual(reply.embed["footer"]["text"], "ID #1")
        bad = await self.say_one(";info 424242")
        self.assertEqual(bad.content, "⚠️ There's no Pokémon with ID #424242.")

    async def test_a_traded_away_pokemon_keeps_its_history(self):
        p = await self.give(ALICE, "pikachu")
        q = await self.give(BOB, "charizard")
        result = await self.store.swap(GUILD_ID, ALICE.id, [p.id], BOB.id, [q.id], now=50_000)
        self.assertTrue(result.ok)
        reply = await self.say_one(";info 1")
        self.assertEqual(fields(reply.embed)["Owner"], f"<@{BOB.id}>")
        self.assertEqual(fields(reply.embed)["History"], f"Caught by <@{ALICE.id}> on <t:{p.caught_at}:D>")


class DexCommand(BotCase):
    async def test_progress_lists_all_nine_generations(self):
        reply = await self.say_one(";dex")
        embed = reply.embed
        self.assertEqual(embed["title"], "Alice's Pokédex")
        self.assertIn("**0/1025** registered (0.0%)", embed["description"])
        self.assertIn("0 caught in all · 0 owned right now", embed["description"])
        names = [f["name"] for f in embed.get("fields", [])]
        self.assertEqual(names[:3], ["Gen I · Kanto", "Gen II · Johto", "Gen III · Hoenn"])
        self.assertEqual(len(names), 9)

    async def test_lookup_and_multiple_matches(self):
        await self.give(ALICE, "pikachu")
        entry = await self.say_one(";pokedex charizard")
        self.assertEqual(entry.embed["title"], "No. 0006 · Charizard")
        self.assertEqual(entry.embed["footer"]["text"], "❌ Not in your Pokédex yet")
        matches = await self.say_one(";pokedex char")
        self.assertEqual(matches.embed["title"], "Several Pokémon match “char”")
        self.assertIn("0006", matches.embed["description"])
        # A species you caught once stays registered even after you trade it away.
        self.assertEqual((await self.say_one(";pokedex pikachu")).embed["footer"]["text"], "✅ In your Pokédex · you own 1")

    async def test_gen_browse_pages_and_ticks(self):
        await self.give(ALICE, "treecko")
        n = len(self.dex.generation(3))
        pages = (n + 29) // 30
        self.assertEqual(self.dex.generation(3)[0].name, "treecko".capitalize())  # the first species of the generation, as the dex spells it
        first = await self.say_one(";pokedex gen 3")
        self.assertEqual(first.embed["title"], "Generation III · Hoenn")
        self.assertEqual(first.embed["description"].split("\n")[0], "✅ `0252` Treecko")
        self.assertEqual(first.embed["footer"]["text"], f"1/{n} registered · Page 1/{pages}")
        second = await self.tap(first, ALICE, "▶️")
        self.assertEqual(second[0].embed["footer"]["text"], f"1/{n} registered · Page 2/{pages}")
        bad = await self.say_one(";pokedex gen 12")
        self.assertEqual(bad.content, f"⚠️ There's no generation 12. Try 1 to {max(self.dex.generations)}.")


class Trade(BotCase):
    async def test_offer_pings_the_partner_and_waits_for_an_answer(self):
        await self.give(ALICE, "pikachu")
        await self.give(BOB, "charizard")
        reply = await self.say_one(f";trade {BOB.mention} 1 2")
        self.assertEqual(reply.content, BOB.mention)
        self.assertEqual(reply.allowed_mentions.get("users"), [BOB.id])  # the only permitted ping
        embed = reply.embed
        self.assertEqual(embed["title"], "Alice wants to trade with Bob")
        f = fields(embed)
        self.assertEqual(f["Alice gives"], "`#1` ⚪ **Pikachu** · No. 0025")
        self.assertEqual(f["Bob gives"], "`#2` 🔵 **Charizard** · No. 0006")
        self.assertEqual(embed["footer"]["text"], "Bob: press Accept to swap, or Decline. This offer expires in 2 minutes.")
        self.assertEqual([b["label"] for b in reply.buttons], ["Accept", "Decline"])

        wrong = await self.tap(reply, CARA, "Accept")
        self.assertTrue(wrong[0].ephemeral)
        self.assertEqual(wrong[0].content, "This trade is between two other trainers.")
        self_author = await self.tap(reply, ALICE, "Accept")
        self.assertTrue(self_author[0].ephemeral)
        self.assertEqual(self_author[0].content, "Only Bob can accept this trade.")
        self.assertEqual([c.id for c in await self.store.owned(ALICE.id)], [1])  # nothing moved yet

        sent = await self.tap(reply, BOB, "Accept")
        final = sent[0]
        self.assertEqual(final.kind, "response")
        self.assertEqual(final.embed["title"], "Trade complete 🤝")
        f = fields(final.embed)
        self.assertEqual(f["✨ New for Alice's Pokédex"], "Charizard")
        self.assertEqual(f["✨ New for Bob's Pokédex"], "Pikachu")
        self.assertEqual([c.species_id for c in await self.store.owned(ALICE.id)], [6])
        self.assertEqual([c.species_id for c in await self.store.owned(BOB.id)], [25])
        self.assertFalse(final.components)  # the buttons are gone once the trade is done

    async def test_decline_and_cancel(self):
        await self.give(ALICE, "pikachu")
        await self.give(BOB, "charizard")
        reply = await self.say_one(f";trade {BOB.mention} 1 2")
        declined = await self.tap(reply, BOB, "Decline")
        self.assertEqual(declined[0].embed["title"], "Trade declined")
        # One open offer each: the guard holds while it is open, and frees up after it closes.
        again = await self.say_one(f";trade {BOB.mention} 1 2")
        second = await self.tap(again, ALICE, "Decline")
        self.assertEqual(second[0].embed["title"], "Trade cancelled")
        self.assertEqual(again.allowed_mentions.get("users"), [BOB.id])

    async def test_a_frozen_offer_fails_when_something_moved(self):
        p = await self.give(ALICE, "pikachu")
        await self.give(BOB, "charizard")
        q = await self.give(BOB, "bulbasaur")
        r = await self.give(DAN, "squirtle")
        reply = await self.say_one(f";trade {BOB.mention} 1 {q.id}")
        moved = await self.store.swap(GUILD_ID, BOB.id, [q.id], DAN.id, [r.id], now=50_000)
        self.assertTrue(moved.ok)  # Bob handed #3 to Dan outside the offer
        sent = await self.tap(reply, BOB, "Accept")
        self.assertEqual(sent[0].embed["title"], "Trade failed")
        self.assertEqual(sent[0].embed["footer"]["text"], f"Somebody no longer owns a Pokémon in this offer (#{q.id}). Nothing was traded.")
        self.assertEqual([c.id for c in await self.store.owned(ALICE.id)], [p.id])  # Alice keeps hers

    async def test_trade_input_errors(self):
        await self.give(ALICE, "pikachu")  # #1
        await self.give(BOB, "charizard")  # #2
        await self.give(ALICE, "mew")  # #3: exists, but not Bob's
        checks = [
            (f";trade {ROBOT.mention} 1 2", "⚠️ Bots don't trade Pokémon."),
            (f";trade {ALICE.mention} 1 2", "⚠️ You can't trade with yourself."),
            (f";trade {BOB.mention} 1 1", "⚠️ The same Pokémon can't be on both sides of a trade."),
            (f";trade {BOB.mention} 1,1 2", "⚠️ You listed the same Pokémon ID twice."),
            (f";trade {BOB.mention} 1,2,3,4,5,6,7 2", "⚠️ That's too many: at most 6 per side."),
            (f";trade {BOB.mention} 1 999", "⚠️ There's no Pokémon with ID #999."),
            (f";trade {BOB.mention} 2 1", "⚠️ You don't own #2."),  # Alice's side: she owns #1, not #2
            (f";trade {BOB.mention} 1 3", "⚠️ Bob doesn't own #3."),  # #3 exists; it's Alice's
        ]
        for content, expected in checks:
            reply = await self.say_one(content)
            self.assertEqual(reply.content, expected, content)  # prefix replies can't be ephemeral
        self.assertEqual(await self.store.owned(ALICE.id), [c for c in await self.store.owned(ALICE.id)])  # nothing moved


class Slash(BotCase):
    async def test_slash_pokedex_shows_the_progress_card(self):
        await self.give(ALICE, "pikachu")
        sent = await self.slash("pokedex", ALICE)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].kind, "response")
        self.assertEqual(sent[0].embed["title"], "Alice's Pokédex")
        self.assertIn("**1/1025** registered (0.1%)", sent[0].embed["description"])
        self.assertIn("/pokedex", sent[0].embed["footer"]["text"])

    async def test_a_guard_failure_replies_without_pinging_anyone(self):
        # A prefix cooldown reply must not ping anyone, ephemeral or not.
        with mock.patch.dict(self.pkm, {"time": mock.Mock(time=lambda: 1_000_000)}):
            await self.say_one(";pokemon", author=ALICE)  # starts the cooldown
            reply = await self.say_one(";catch")
        self.assertIn("Your next catch is ready in", reply.content)
        self.assertIn(reply.allowed_mentions.get("users"), ([], None))  # nothing user-shaped is allowed through


if __name__ == "__main__":
    unittest.main()
