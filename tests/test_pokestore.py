import asyncio
import tempfile
import unittest
from pathlib import Path

from pokestore import Store

COOLDOWN = 90 * 60
ALICE, BOB, CARA = 111, 222, 333


class StoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.store = Store(self.path)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()  # aiosqlite's thread would keep the test process alive otherwise
        self.tmp.cleanup()

    async def catch(self, user, species, now=10_000):
        result = await self.store.catch(user, species, now=now, cooldown=COOLDOWN, guild_id=1)
        self.assertIsNotNone(result.pokemon)
        return result.pokemon


class Catching(StoreCase):
    async def test_a_catch_is_recorded_and_registered(self):
        result = await self.store.catch(ALICE, 25, now=10_000, cooldown=COOLDOWN, guild_id=42)
        p = result.pokemon
        self.assertEqual((p.owner_id, p.species_id, p.caught_by, p.caught_at, p.guild_id), (ALICE, 25, ALICE, 10_000, 42))
        self.assertTrue(result.new_entry)
        self.assertEqual(await self.store.owned(ALICE), [p])
        self.assertEqual(await self.store.pokemon(p.id), p)
        self.assertEqual(await self.store.registered(ALICE), {25: 10_000})

    async def test_cooldown_is_exactly_90_minutes(self):
        await self.catch(ALICE, 1, now=10_000)
        early = await self.store.catch(ALICE, 2, now=10_000 + COOLDOWN - 1, cooldown=COOLDOWN)
        self.assertIsNone(early.pokemon)
        self.assertEqual(early.retry_at, 10_000 + COOLDOWN)
        on_time = await self.store.catch(ALICE, 2, now=10_000 + COOLDOWN, cooldown=COOLDOWN)
        self.assertIsNotNone(on_time.pokemon)
        self.assertEqual(len(await self.store.owned(ALICE)), 2)

    async def test_a_blocked_catch_changes_nothing_and_does_not_extend_the_cooldown(self):
        await self.catch(ALICE, 1, now=10_000)
        for t in (10_100, 12_000, 15_000):
            self.assertIsNone((await self.store.catch(ALICE, 2, now=t, cooldown=COOLDOWN)).pokemon)
        self.assertEqual(len(await self.store.owned(ALICE)), 1)
        self.assertIsNotNone((await self.store.catch(ALICE, 2, now=10_000 + COOLDOWN, cooldown=COOLDOWN)).pokemon)

    async def test_cooldowns_are_per_user(self):
        await self.catch(ALICE, 1)
        await self.catch(BOB, 1)

    async def test_cooldown_survives_a_restart(self):
        await self.catch(ALICE, 1, now=10_000)
        await self.store.close()
        self.store = Store(self.path)
        await self.store.open()
        self.assertIsNone((await self.store.catch(ALICE, 2, now=10_500, cooldown=COOLDOWN)).pokemon)
        self.assertEqual(len(await self.store.owned(ALICE)), 1)

    async def test_a_clock_that_jumps_back_cannot_lock_someone_out_for_more_than_the_cooldown(self):
        await self.catch(ALICE, 1, now=1_000_000)
        result = await self.store.catch(ALICE, 2, now=500, cooldown=COOLDOWN)
        self.assertIsNone(result.pokemon)
        self.assertEqual(result.retry_at, 500 + COOLDOWN)

    async def test_simultaneous_catches_let_exactly_one_through(self):
        results = await asyncio.gather(*[self.store.catch(ALICE, n, now=10_000, cooldown=COOLDOWN) for n in range(1, 31)])
        self.assertEqual(sum(r.pokemon is not None for r in results), 1)
        self.assertEqual(len(await self.store.owned(ALICE)), 1)
        self.assertEqual((await self.store.profile(ALICE)).total_caught, 1)

    async def test_duplicates_are_separate_pokemon_but_register_once(self):
        a = await self.store.catch(ALICE, 25, now=1, cooldown=COOLDOWN)
        b = await self.store.catch(ALICE, 25, now=1 + COOLDOWN, cooldown=COOLDOWN)
        self.assertNotEqual(a.pokemon.id, b.pokemon.id)
        self.assertTrue(a.new_entry)
        self.assertFalse(b.new_entry)
        profile = await self.store.profile(ALICE)
        self.assertEqual((profile.total_caught, profile.owned, profile.registered), (2, 2, 1))

    async def test_ids_are_never_reused_and_lookups_cope_with_gaps(self):
        p = await self.catch(ALICE, 1)
        self.assertIsNone(await self.store.pokemon(p.id + 100))
        self.assertEqual(await self.store.pokemon_many([]), {})
        self.assertEqual(set(await self.store.pokemon_many([p.id, p.id + 100])), {p.id})


class Trading(StoreCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.a1 = await self.catch(ALICE, 25)
        self.a2 = await self.catch(ALICE, 26, now=10_000 + COOLDOWN)
        self.b1 = await self.catch(BOB, 4)
        self.b2 = await self.catch(BOB, 5, now=10_000 + COOLDOWN)

    async def owners(self, *pokemon):
        return [(await self.store.pokemon(p.id)).owner_id for p in pokemon]

    async def test_swap_moves_ownership_and_registers_new_species(self):
        result = await self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id], now=50_000)
        self.assertTrue(result.ok)
        self.assertEqual(await self.owners(self.a1, self.b1), [BOB, ALICE])
        self.assertEqual(result.new_for_a, (4,))
        self.assertEqual(result.new_for_b, (25,))
        self.assertIn(4, await self.store.registered(ALICE))
        self.assertIn(25, await self.store.registered(BOB))

    async def test_traded_pokemon_keep_their_id_and_original_catcher(self):
        await self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id], now=50_000)
        moved = await self.store.pokemon(self.a1.id)
        self.assertEqual((moved.id, moved.owner_id, moved.caught_by, moved.caught_at), (self.a1.id, BOB, ALICE, 10_000))

    async def test_trading_away_a_pokemon_keeps_it_in_the_pokedex_and_in_total_caught(self):
        await self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id], now=50_000)
        self.assertIn(25, await self.store.registered(ALICE))
        profile = await self.store.profile(ALICE)
        self.assertEqual((profile.total_caught, profile.owned), (2, 2))

    async def test_multi_pokemon_swap(self):
        result = await self.store.swap(9, ALICE, [self.a1.id, self.a2.id], BOB, [self.b1.id], now=50_000)
        self.assertTrue(result.ok)
        self.assertEqual(await self.owners(self.a1, self.a2, self.b1), [BOB, BOB, ALICE])

    async def test_stale_offer_is_refused_and_nothing_moves(self):
        await self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id], now=50_000)
        replay = await self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id], now=50_001)
        self.assertFalse(replay.ok)
        self.assertEqual(replay.error, "not_owner")
        self.assertEqual(set(replay.bad_ids), {self.a1.id, self.b1.id})
        self.assertEqual(await self.owners(self.a1, self.b1), [BOB, ALICE])

    async def test_swap_is_all_or_nothing(self):
        # Alice's a2 is fine, but Bob doesn't own a1 (it's Alice's): nothing may move, including a2.
        result = await self.store.swap(9, ALICE, [self.a2.id], BOB, [self.b1.id, self.a1.id], now=50_000)
        self.assertFalse(result.ok)
        self.assertEqual(result.bad_ids, (self.a1.id,))
        self.assertEqual(await self.owners(self.a1, self.a2, self.b1), [ALICE, ALICE, BOB])
        self.assertEqual(await self.store.registered(ALICE), {25: 10_000, 26: 10_000 + COOLDOWN})

    async def test_unknown_ids_are_reported(self):
        result = await self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id + 999], now=1)
        self.assertEqual((result.ok, result.error, result.bad_ids), (False, "missing", (self.b1.id + 999,)))

    async def test_invalid_requests(self):
        for args in [(ALICE, [self.a1.id], ALICE, [self.a2.id]),    # with yourself
                     (ALICE, [], BOB, [self.b1.id]),                 # nothing offered
                     (ALICE, [self.a1.id], BOB, []),                 # nothing asked for
                     (ALICE, [self.a1.id], BOB, [self.a1.id])]:      # same Pokémon on both sides
            with self.subTest(args):
                result = await self.store.swap(9, *args, now=1)
                self.assertEqual((result.ok, result.error), (False, "invalid"))

    async def test_duplicate_ids_in_one_side_are_collapsed(self):
        result = await self.store.swap(9, ALICE, [self.a1.id, self.a1.id], BOB, [self.b1.id], now=1)
        self.assertTrue(result.ok)

    async def test_only_one_of_two_competing_offers_for_the_same_pokemon_can_win(self):
        c1 = await self.catch(CARA, 7)
        results = await asyncio.gather(
            self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id], now=50_000),
            self.store.swap(9, ALICE, [self.a1.id], CARA, [c1.id], now=50_000),
        )
        self.assertEqual(sorted(r.ok for r in results), [False, True])
        self.assertEqual((await self.store.profile(ALICE)).owned, 2)  # gave one, got one

    async def test_inventory_follows_the_pokemon(self):
        await self.store.swap(9, ALICE, [self.a1.id], BOB, [self.b1.id], now=50_000)
        self.assertEqual({p.id for p in await self.store.owned(ALICE)}, {self.a2.id, self.b1.id})
        self.assertEqual({p.id for p in await self.store.owned(BOB)}, {self.a1.id, self.b2.id})

    async def test_completed_trades_are_logged(self):
        await self.store.swap(9, ALICE, [self.a1.id, self.a2.id], BOB, [self.b1.id], now=50_000)
        async with self.store._read() as db:
            rows = await (await db.execute("SELECT guild_id, user_a, user_b, gave_a, gave_b, at FROM trades")).fetchall()
        self.assertEqual(rows, [(9, ALICE, BOB, f"{self.a1.id},{self.a2.id}", str(self.b1.id), 50_000)])


class Schema(StoreCase):
    async def test_reopening_an_existing_database_keeps_its_data(self):
        p = await self.catch(ALICE, 25)
        await self.store.close()
        self.store = Store(self.path)
        await self.store.open()
        self.assertEqual(await self.store.owned(ALICE), [p])

    async def test_a_newer_schema_is_refused_rather_than_mangled(self):
        async with self.store._tx() as db:
            await db.execute("PRAGMA user_version = 99")
        await self.store.close()
        self.store = Store(self.path)
        with self.assertRaises(RuntimeError):
            await self.store.open()
        await self.store._db.close()  # open() raised after connecting
        self.store._db = None


if __name__ == "__main__":
    unittest.main()
