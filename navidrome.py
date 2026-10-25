"""
Minimal async client for the Subsonic API that Navidrome speaks: search for songs and build stream URLs.

Subsonic's token auth sends md5(password + salt) and the salt instead of the password itself.
Stream URLs carry that token in their query string, so never show or log them.
"""

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

API_VERSION = "1.16.1"
CLIENT_NAME = "discord-bot"


class NavidromeError(Exception):
    """Something went wrong talking to Navidrome. The message is safe to show in chat."""


@dataclass(frozen=True, slots=True)
class Song:
    id: str
    title: str
    artist: str
    album: str
    duration: int  # seconds, 0 if unknown


class Navidrome:
    def __init__(self, url: str, user: str, password: str):
        self.base = url.rstrip("/")
        self.user = user
        self.password = password
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _auth_params(self) -> dict:
        salt = secrets.token_hex(8)
        # md5 is what the Subsonic protocol specifies here, not a choice of ours
        token = hashlib.md5((self.password + salt).encode(), usedforsecurity=False).hexdigest()
        return {"u": self.user, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME}

    async def _get(self, endpoint: str, **params) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            async with self._session.get(f"{self.base}/rest/{endpoint}", params={**self._auth_params(), "f": "json", **params}) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        # `from None` throughout: aiohttp's own errors carry the full URL, token included.
        except aiohttp.ClientResponseError as e:
            raise NavidromeError(f"Navidrome answered HTTP {e.status}.") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise NavidromeError("Couldn't reach Navidrome.") from None
        except ValueError:
            raise NavidromeError("Navidrome sent a reply I couldn't read. Is NAVIDROME_URL right?") from None

        body = data.get("subsonic-response") if isinstance(data, dict) else None
        if not isinstance(body, dict):
            raise NavidromeError("That doesn't look like a Navidrome server. Is NAVIDROME_URL right?")
        # Subsonic reports failures (bad login, missing song...) inside an HTTP 200.
        if body.get("status") != "ok":
            message = (body.get("error") or {}).get("message", "unknown error")
            raise NavidromeError(f"Navidrome said: {message}")
        return body

    async def ping(self) -> None:
        await self._get("ping")

    async def search_songs(self, query: str, count: int = 10) -> list[Song]:
        body = await self._get("search3", query=query, songCount=count, artistCount=0, albumCount=0)
        songs = (body.get("searchResult3") or {}).get("song") or []  # no hits: searchResult3 has no "song" key
        if isinstance(songs, dict):
            songs = [songs]
        return [
            Song(
                id=str(s["id"]),
                title=s.get("title") or "Unknown title",
                artist=s.get("artist") or "Unknown artist",
                album=s.get("album") or "",
                duration=int(s.get("duration") or 0),
            )
            for s in songs
            if isinstance(s, dict) and s.get("id") is not None
        ]

    def stream_url(self, song_id: str) -> str:
        return f"{self.base}/rest/stream?{urlencode({**self._auth_params(), 'id': song_id})}"
