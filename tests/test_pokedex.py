import random
import unittest
from collections import Counter

from pokedex import TIER_ODDS, TYPE_COLORS, Pokedex, normalize


class DexData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dex = Pokedex.load()

    def test_all_nine_generations_are_complete(self):
        self.assertEqual(len(self.dex), 1025)
        self.assertEqual(self.dex.generations, list(range(1, 10)))
        sizes = {g: len(self.dex.generation(g)) for g in self.dex.generations}
        self.assertEqual(sizes, {1: 151, 2: 100, 3: 135, 4: 107, 5: 156, 6: 72, 7: 88, 8: 96, 9: 120})

    def test_known_entries(self):
        for number, name, gen in [(1, "Bulbasaur", 1), (25, "Pikachu", 1), (151, "Mew", 1), (152, "Chikorita", 2),
                                  (252, "Treecko", 3), (387, "Turtwig", 4), (494, "Victini", 5), (650, "Chespin", 6),
                                  (722, "Rowlet", 7), (810, "Grookey", 8), (906, "Sprigatito", 9), (1025, "Pecharunt", 9)]:
            self.assertEqual((self.dex[number].name, self.dex[number].gen), (name, gen))
        self.assertEqual(self.dex[6].types, ("fire", "flying"))
        self.assertEqual(self.dex[1].number, "0001")

    def test_every_species_is_complete(self):
        for s in self.dex:
            with self.subTest(s.name):
                self.assertTrue(1 <= len(s.types) <= 2)
                self.assertTrue(all(t in TYPE_COLORS for t in s.types))
                self.assertEqual(len(s.stats), 6)
                self.assertTrue(all(v > 0 for v in s.stats))
                self.assertGreater(s.height, 0)
                self.assertGreater(s.weight, 0)
                self.assertTrue(s.artwork_url.endswith(f"/{s.id}.png"))

    def test_get_and_getitem_handle_out_of_range(self):
        self.assertIsNone(self.dex.get(0))
        self.assertIsNone(self.dex.get(1026))
        self.assertIsNone(self.dex.get(-1))
        with self.assertRaises(KeyError):
            self.dex[0]


class Lookup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dex = Pokedex.load()

    def names(self, query):
        return [s.name for s in self.dex.find(query)]

    def test_awkward_names_in_any_spelling(self):
        for query, expected in [
            ("mr mime", "Mr. Mime"), ("Mr. Mime", "Mr. Mime"), ("MR.MIME", "Mr. Mime"),
            ("type null", "Type: Null"), ("Type: Null", "Type: Null"),
            ("farfetch'd", "Farfetch’d"), ("farfetch’d", "Farfetch’d"), ("farfetchd", "Farfetch’d"),
            ("sirfetch'd", "Sirfetch’d"), ("mime jr", "Mime Jr."),
            ("nidoran♀", "Nidoran♀"), ("nidoran♂", "Nidoran♂"), ("nidoran f", "Nidoran♀"), ("nidoran-m", "Nidoran♂"),
            ("flabebe", "Flabébé"), ("Flabébé", "Flabébé"), ("porygon-z", "Porygon-Z"), ("ho-oh", "Ho-Oh"),
            ("jangmo-o", "Jangmo-o"), ("PIKACHU", "Pikachu"),
        ]:
            with self.subTest(query):
                self.assertEqual(self.names(query), [expected])

    def test_numbers(self):
        for query in ["25", "#25", "025", "0025", " 25 "]:
            self.assertEqual(self.names(query), ["Pikachu"])
        self.assertEqual(self.names("1025"), ["Pecharunt"])
        for query in ["0", "1026", "99999999999999999999"]:
            self.assertEqual(self.names(query), [])

    def test_partial_matches_put_prefix_matches_first(self):
        self.assertEqual(self.names("nidoran"), ["Nidoran♀", "Nidoran♂"])
        result = self.names("char")
        self.assertEqual(result[:3], ["Charmander", "Charmeleon", "Charizard"])
        self.assertNotIn("Mew", self.names("mew")[1:])  # exact name wins outright
        self.assertEqual(self.names("mew"), ["Mew"])

    def test_nonsense_finds_nothing(self):
        for query in ["", "   ", "zzzz", "!!!", "#", "💥", "a" * 500]:
            with self.subTest(query[:10]):
                self.assertEqual(self.names(query) if query != "a" * 500 else self.dex.find(query), [])

    def test_normalize(self):
        self.assertEqual(normalize("Flabébé"), "flabebe")
        self.assertEqual(normalize("Nidoran♀"), "nidoranf")
        self.assertEqual(normalize("  Mr.  Mime "), "mrmime")


class Rolling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dex = Pokedex.load()

    def test_tier_odds_sum_to_100(self):
        self.assertAlmostEqual(sum(TIER_ODDS.values()), 100)

    def test_every_tier_has_species_and_together_they_cover_the_dex(self):
        sizes = {t: sum(s.tier == t for s in self.dex) for t in TIER_ODDS}
        self.assertTrue(all(sizes.values()), sizes)
        self.assertEqual(sum(sizes.values()), len(self.dex))
        self.assertEqual(sizes["legendary"], sum(s.legendary and not s.mythical for s in self.dex))
        self.assertEqual(sizes["mythical"], sum(s.mythical for s in self.dex))

    def test_roll_follows_the_odds(self):
        rng = random.Random(12345)
        n = 100_000
        counts = Counter(self.dex.roll(rng).tier for _ in range(n))
        for tier, percent in TIER_ODDS.items():
            expected = n * percent / 100
            self.assertAlmostEqual(counts[tier], expected, delta=max(5 * (expected ** 0.5), 50), msg=tier)

    def test_every_species_can_be_caught(self):
        rng = random.Random(7)
        seen = {self.dex.roll(rng).id for _ in range(600_000)}
        self.assertEqual(len(seen), len(self.dex), f"never rolled: {sorted(set(range(1, 1026)) - seen)[:10]}")


if __name__ == "__main__":
    unittest.main()
