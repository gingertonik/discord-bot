"""
SQLite persistence for the Pokémon game: who caught what, Pokédex registrations, cooldowns and trades.

There is one connection and one lock. Every public method takes the lock, and the ones that write run
inside a single BEGIN IMMEDIATE transaction, so a catch or a trade either happens completely or not
at all and two commands can never interleave halfway through.

Tables:
    trainers    one row per user; last_catch_at drives the catch cooldown
    caught      one row per individual Pokémon ever caught; its id is the "Pokémon ID" users see and
                it keeps that id when traded; owner_id changes, caught_by never does
    registered  Pokédex entries: species a user has ever caught or received in a trade. Rows are
                never removed, so trading a Pokémon away doesn't un-register it
    trades      a log of completed trades
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE trainers (
    user_id       INTEGER PRIMARY KEY,
    last_catch_at INTEGER              -- NULL until their first catch
);
CREATE TABLE caught (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    species_id INTEGER NOT NULL,
    caught_by  INTEGER NOT NULL,
    caught_at  INTEGER NOT NULL,
    guild_id   INTEGER
);
CREATE INDEX caught_owner ON caught (owner_id);
CREATE INDEX caught_by_idx ON caught (caught_by);
CREATE TABLE registered (
    user_id    INTEGER NOT NULL,
    species_id INTEGER NOT NULL,
    first_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, species_id)
);
CREATE TABLE trades (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    user_a   INTEGER NOT NULL,
    user_b   INTEGER NOT NULL,
    gave_a   TEXT NOT NULL,
    gave_b   TEXT NOT NULL,
    at       INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Caught:
    """One individual Pokémon somebody caught."""
    id: int
    owner_id: int
    species_id: int
    caught_by: int
    caught_at: int
    guild_id: int | None


@dataclass(frozen=True, slots=True)
class CatchResult:
    pokemon: Caught | None  # None while the user is still on cooldown
    new_entry: bool = False  # first time this user has registered the species
    retry_at: int = 0  # unix time the cooldown ends (only set when pokemon is None)


@dataclass(frozen=True, slots=True)
class SwapResult:
    ok: bool
    error: str = ""  # "invalid", "missing" or "not_owner" when ok is False
    bad_ids: tuple[int, ...] = ()  # the Pokémon behind a "missing" / "not_owner" error
    new_for_a: tuple[int, ...] = ()  # species newly registered in A's Pokédex
    new_for_b: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Profile:
    total_caught: int  # every catch ever, including ones since traded away
    owned: int
    registered: int  # distinct species in the Pokédex


_POKEMON_COLUMNS = "id, owner_id, species_id, caught_by, caught_at, guild_id"


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we issue BEGIN/COMMIT ourselves (see _tx).
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA synchronous = NORMAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        async with self._tx() as db:
            version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
            if version == 0:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        await db.execute(statement)
                await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} has schema version {version}, this bot expects {SCHEMA_VERSION}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @asynccontextmanager
    async def _tx(self):
        """Hold the lock and run the body in one transaction: commit if it finishes, roll back if it raises."""
        async with self._lock:
            db = self._db
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                await db.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    await db.execute("ROLLBACK")
                raise

    @asynccontextmanager
    async def _read(self):
        # Reads take the lock too: with a single connection, a read slipped in between another task's
        # BEGIN and COMMIT would see rows that task may still roll back.
        async with self._lock:
            yield self._db

    # ------------------- catching -------------------

    async def catch(self, user_id: int, species_id: int, *, now: int, cooldown: int, guild_id: int | None = None) -> CatchResult:
        """
        Give the user a new Pokémon of this species unless their last catch was less than `cooldown`
        seconds ago. The cooldown check and the write are one statement, so two commands fired at
        the same moment can't both get through.
        """
        async with self._tx() as db:
            await db.execute("INSERT OR IGNORE INTO trainers (user_id) VALUES (?)", (user_id,))
            cur = await db.execute(
                "UPDATE trainers SET last_catch_at = ? WHERE user_id = ? AND (last_catch_at IS NULL OR last_catch_at <= ?)",
                (now, user_id, now - cooldown),
            )
            if cur.rowcount == 0:
                (last,) = await (await db.execute("SELECT last_catch_at FROM trainers WHERE user_id = ?", (user_id,))).fetchone()
                # min() so a clock that jumped backwards can't lock someone out for longer than `cooldown`
                return CatchResult(None, retry_at=min(last + cooldown, now + cooldown))
            cur = await db.execute(
                "INSERT INTO caught (owner_id, species_id, caught_by, caught_at, guild_id) VALUES (?, ?, ?, ?, ?)",
                (user_id, species_id, user_id, now, guild_id),
            )
            pokemon = Caught(cur.lastrowid, user_id, species_id, user_id, now, guild_id)
            cur = await db.execute(
                "INSERT OR IGNORE INTO registered (user_id, species_id, first_at) VALUES (?, ?, ?)",
                (user_id, species_id, now),
            )
            return CatchResult(pokemon, new_entry=cur.rowcount == 1)

    # ------------------- looking things up -------------------

    @staticmethod
    def _row(row) -> Caught:
        return Caught(*row)

    async def pokemon(self, pokemon_id: int) -> Caught | None:
        async with self._read() as db:
            row = await (await db.execute(f"SELECT {_POKEMON_COLUMNS} FROM caught WHERE id = ?", (pokemon_id,))).fetchone()
        return self._row(row) if row else None

    async def pokemon_many(self, ids: list[int]) -> dict[int, Caught]:
        if not ids:
            return {}
        async with self._read() as db:
            rows = await (await db.execute(f"SELECT {_POKEMON_COLUMNS} FROM caught WHERE id IN ({_placeholders(len(ids))})", ids)).fetchall()
        return {r[0]: self._row(r) for r in rows}

    async def owned(self, user_id: int) -> list[Caught]:
        """Everything the user currently owns, newest first."""
        async with self._read() as db:
            rows = await (await db.execute(f"SELECT {_POKEMON_COLUMNS} FROM caught WHERE owner_id = ? ORDER BY id DESC", (user_id,))).fetchall()
        return [self._row(r) for r in rows]

    async def registered(self, user_id: int) -> dict[int, int]:
        """The user's Pokédex: species id -> when it was first registered."""
        async with self._read() as db:
            rows = await (await db.execute("SELECT species_id, first_at FROM registered WHERE user_id = ?", (user_id,))).fetchall()
        return dict(rows)

    async def profile(self, user_id: int) -> Profile:
        async with self._read() as db:
            row = await (await db.execute(
                "SELECT (SELECT COUNT(*) FROM caught WHERE caught_by = ?),"
                " (SELECT COUNT(*) FROM caught WHERE owner_id = ?),"
                " (SELECT COUNT(*) FROM registered WHERE user_id = ?)",
                (user_id, user_id, user_id),
            )).fetchone()
        return Profile(*row)

    # ------------------- trading -------------------

    async def swap(self, guild_id: int | None, user_a: int, ids_a: list[int], user_b: int, ids_b: list[int], *, now: int) -> SwapResult:
        """
        Atomically hand every Pokémon in ids_a from user_a to user_b and every one in ids_b from user_b
        to user_a. Ownership is checked inside the same transaction that moves them, so an offer that
        went stale (someone traded the Pokémon away in the meantime) fails cleanly instead of
        moving the wrong thing.
        """
        ids_a, ids_b = list(dict.fromkeys(ids_a)), list(dict.fromkeys(ids_b))
        if user_a == user_b or not ids_a or not ids_b or set(ids_a) & set(ids_b):
            return SwapResult(False, "invalid")

        async with self._tx() as db:
            everything = ids_a + ids_b
            rows = await (await db.execute(
                f"SELECT id, owner_id, species_id FROM caught WHERE id IN ({_placeholders(len(everything))})", everything
            )).fetchall()
            found = {r[0]: (r[1], r[2]) for r in rows}
            missing = tuple(i for i in everything if i not in found)
            if missing:
                return SwapResult(False, "missing", missing)
            not_owned = tuple(i for i in ids_a if found[i][0] != user_a) + tuple(i for i in ids_b if found[i][0] != user_b)
            if not_owned:
                return SwapResult(False, "not_owner", not_owned)

            for ids, giver, receiver in ((ids_a, user_a, user_b), (ids_b, user_b, user_a)):
                cur = await db.execute(
                    f"UPDATE caught SET owner_id = ? WHERE owner_id = ? AND id IN ({_placeholders(len(ids))})",
                    (receiver, giver, *ids),
                )
                if cur.rowcount != len(ids):  # can't happen after the check above; refuse rather than half-trade
                    raise RuntimeError("trade moved an unexpected number of Pokémon")

            new_for_a = await self._register(db, user_a, [found[i][1] for i in ids_b], now)
            new_for_b = await self._register(db, user_b, [found[i][1] for i in ids_a], now)
            await db.execute(
                "INSERT INTO trades (guild_id, user_a, user_b, gave_a, gave_b, at) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, user_a, user_b, ",".join(map(str, ids_a)), ",".join(map(str, ids_b)), now),
            )
        return SwapResult(True, new_for_a=new_for_a, new_for_b=new_for_b)

    @staticmethod
    async def _register(db, user_id: int, species_ids: list[int], now: int) -> tuple[int, ...]:
        new = []
        for species_id in dict.fromkeys(species_ids):
            cur = await db.execute(
                "INSERT OR IGNORE INTO registered (user_id, species_id, first_at) VALUES (?, ?, ?)",
                (user_id, species_id, now),
            )
            if cur.rowcount == 1:
                new.append(species_id)
        return tuple(new)
