import hashlib
import unittest
from urllib.parse import parse_qs, urlparse

from aiohttp import web
from aiohttp.test_utils import TestServer

from navidrome import Navidrome, NavidromeError

USER, PASSWORD = "bot", "hunter2-secret"
SEEN = web.AppKey("seen", list)


def authed(request: web.Request) -> bool:
    """What a real Subsonic server does: md5(password + salt) must equal the token."""
    q = request.query
    return q.get("u") == USER and q.get("t") == hashlib.md5((PASSWORD + q.get("s", "")).encode()).hexdigest()


def subsonic(**body) -> web.Response:
    return web.json_response({"subsonic-response": {"status": "ok", "version": "1.16.1", **body}})


def song(n: int, **extra) -> dict:
    return {"id": f"song-{n}", "title": f"Title {n}", "artist": f"Artist {n}", "album": f"Album {n}", "duration": 100 + n, **extra}


async def handle_search(request: web.Request) -> web.Response:
    if not authed(request):
        return web.json_response({"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})
    query = request.query["query"]
    request.app[SEEN].append(dict(request.query))
    if query == "nothing":
        return subsonic(searchResult3={})
    if query == "one":
        return subsonic(searchResult3={"song": song(1)})  # a lone result as an object, not a list
    if query == "sparse":
        return subsonic(searchResult3={"song": [{"id": 7}, {"title": "no id, skipped"}, "garbage"]})
    if query == "many":
        return subsonic(searchResult3={"song": [song(n) for n in range(1, int(request.query["songCount"]) + 1)]})
    return subsonic(searchResult3={"artist": [{"id": "a"}]})


async def handle_ping(request: web.Request) -> web.Response:
    return subsonic() if authed(request) else web.Response(status=401, text="nope")


async def handle_html(request: web.Request) -> web.Response:
    return web.Response(text="<html>login page</html>", content_type="text/html")


class NavidromeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app[SEEN] = []
        app.router.add_get("/rest/search3", handle_search)
        app.router.add_get("/rest/ping", handle_ping)
        app.router.add_get("/nd/rest/ping", handle_ping)
        app.router.add_get("/html/rest/ping", handle_html)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = str(self.server.make_url("")).rstrip("/")
        self.nd = Navidrome(self.base, USER, PASSWORD)
        self.seen = app[SEEN]

    async def asyncTearDown(self):
        await self.nd.close()
        await self.server.close()


class Searching(NavidromeCase):
    async def test_results_are_parsed(self):
        songs = await self.nd.search_songs("many", 3)
        self.assertEqual([s.id for s in songs], ["song-1", "song-2", "song-3"])
        self.assertEqual((songs[0].title, songs[0].artist, songs[0].album, songs[0].duration), ("Title 1", "Artist 1", "Album 1", 101))

    async def test_request_asks_for_songs_only_and_signs_with_a_fresh_salt(self):
        await self.nd.search_songs("many", 2)
        await self.nd.search_songs("many", 2)
        first, second = self.seen
        self.assertEqual((first["songCount"], first["artistCount"], first["albumCount"]), ("2", "0", "0"))
        self.assertNotEqual(first["s"], second["s"])
        self.assertNotIn(PASSWORD, str(self.seen))  # the password itself is never sent

    async def test_no_hits_is_an_empty_list_not_an_error(self):
        self.assertEqual(await self.nd.search_songs("nothing"), [])
        self.assertEqual(await self.nd.search_songs("only an artist"), [])

    async def test_a_single_result_object_is_accepted(self):
        (s,) = await self.nd.search_songs("one")
        self.assertEqual(s.id, "song-1")

    async def test_incomplete_entries_are_tolerated_or_skipped(self):
        (s,) = await self.nd.search_songs("sparse")
        self.assertEqual((s.id, s.title, s.artist, s.album, s.duration), ("7", "Unknown title", "Unknown artist", "", 0))

    async def test_unicode_and_odd_characters_in_the_query(self):
        for q in ["Beyoncé", "AC/DC", "100% & more", "日本語", "a b  c", "quote\"s"]:
            await self.nd.search_songs(q)
        self.assertEqual([s["query"] for s in self.seen], ["Beyoncé", "AC/DC", "100% & more", "日本語", "a b  c", 'quote"s'])


class Failing(NavidromeCase):
    async def test_wrong_password_comes_back_as_navidromes_own_message(self):
        bad = Navidrome(self.base, USER, "wrong")
        try:
            with self.assertRaisesRegex(NavidromeError, "Wrong username or password"):
                await bad.search_songs("many")
        finally:
            await bad.close()

    async def test_http_errors_are_explained_and_do_not_leak_the_token(self):
        bad = Navidrome(self.base, USER, "wrong")
        try:
            with self.assertRaises(NavidromeError) as caught:
                await bad.ping()
        finally:
            await bad.close()
        text = repr(caught.exception) + str(caught.exception)
        self.assertIn("401", text)
        for secret in ("wrong", "t=", "&s=", "127.0.0.1", "http"):
            self.assertNotIn(secret, text)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)  # no chained exception carrying the URL into a traceback

    async def test_a_server_that_is_not_navidrome(self):
        other = Navidrome(self.base + "/html", USER, PASSWORD)
        try:
            with self.assertRaises(NavidromeError):
                await other.ping()
        finally:
            await other.close()

    async def test_unreachable_server(self):
        dead = Navidrome("http://127.0.0.1:1", USER, PASSWORD)
        try:
            with self.assertRaisesRegex(NavidromeError, "Couldn't reach Navidrome"):
                await dead.search_songs("x")
        finally:
            await dead.close()


class Urls(NavidromeCase):
    async def test_base_paths_and_trailing_slashes(self):
        nested = Navidrome(self.base + "/nd/", USER, PASSWORD)
        try:
            await nested.ping()
        finally:
            await nested.close()

    async def test_stream_url_is_signed_and_names_the_song(self):
        url = Navidrome("http://music.local:4533/", USER, PASSWORD).stream_url("abc 123&x")
        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/rest/stream")
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(q["id"], "abc 123&x")  # survives URL-encoding
        self.assertEqual(q["u"], USER)
        self.assertEqual(q["t"], hashlib.md5((PASSWORD + q["s"]).encode()).hexdigest())
        self.assertNotIn(PASSWORD, url)

    async def test_each_stream_url_has_its_own_salt(self):
        nd = Navidrome("http://x", USER, PASSWORD)
        self.assertNotEqual(nd.stream_url("1"), nd.stream_url("1"))


if __name__ == "__main__":
    unittest.main()
