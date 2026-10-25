"""
Pokédex data and helpers: the 1,025 species of generations I-IX, name lookup, and the random catch roll.

data/pokedex.json is built by scripts/build_pokedex.py from PokeAPI's CSV dump.
"""

import json
import random
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "pokedex.json"
ARTWORK_URL = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/{}.png"
SPRITE_URL = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{}.png"

ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV"]
STAT_NAMES = ["HP", "Atk", "Def", "SpA", "SpD", "Spe"]
REGIONS = {1: "Kanto", 2: "Johto", 3: "Hoenn", 4: "Sinnoh", 5: "Unova", 6: "Kalos", 7: "Alola", 8: "Galar", 9: "Paldea"}

# Chance of each rarity tier per catch, in percent. Within a tier every species is equally likely.
TIER_ODDS = {"common": 55, "uncommon": 28, "rare": 14, "legendary": 2.5, "mythical": 0.5}
TIER_EMOJI = {"common": "⚪", "uncommon": "🟢", "rare": "🔵", "legendary": "🟣", "mythical": "🟡"}
TIER_LABELS = {tier: f"{emoji} {tier.title()}" for tier, emoji in TIER_EMOJI.items()}
TIER_RANK = {"common": 0, "uncommon": 1, "rare": 2, "legendary": 3, "mythical": 4}  # for sorting, rarest highest
# Non-legendary species are sorted into tiers by their games capture rate (255 = easy, 3 = hard).
COMMON_MIN_CAPTURE_RATE = 120
UNCOMMON_MIN_CAPTURE_RATE = 60

TYPE_COLORS = {
    "normal": 0xA8A77A, "fire": 0xEE8130, "water": 0x6390F0, "electric": 0xF7D02C, "grass": 0x7AC74C,
    "ice": 0x96D9D6, "fighting": 0xC22E28, "poison": 0xA33EA1, "ground": 0xE2BF65, "flying": 0xA98FF3,
    "psychic": 0xF95587, "bug": 0xA6B91A, "rock": 0xB6A136, "ghost": 0x735797, "dragon": 0x6F35FC,
    "dark": 0x705746, "steel": 0xB7B7CE, "fairy": 0xD685AD,
}

_GENDER_SIGNS = str.maketrans({"♀": "f", "♂": "m"})


def normalize(text: str) -> str:
    """Lowercase letters and digits only, accents dropped: 'Flabébé' -> 'flabebe', 'Mr. Mime' -> 'mrmime'."""
    text = unicodedata.normalize("NFKD", text.translate(_GENDER_SIGNS)).casefold()
    return "".join(c for c in text if c.isascii() and c.isalnum())


@dataclass(frozen=True, slots=True)
class Species:
    id: int
    name: str
    gen: int
    types: tuple[str, ...]
    genus: str
    height: int  # decimetres
    weight: int  # hectograms
    stats: tuple[int, ...]  # hp, attack, defense, special attack, special defense, speed
    capture_rate: int
    legendary: bool
    mythical: bool
    baby: bool

    @property
    def tier(self) -> str:
        if self.mythical:
            return "mythical"
        if self.legendary:
            return "legendary"
        if self.capture_rate >= COMMON_MIN_CAPTURE_RATE:
            return "common"
        if self.capture_rate >= UNCOMMON_MIN_CAPTURE_RATE:
            return "uncommon"
        return "rare"

    @property
    def tier_label(self) -> str:
        return TIER_LABELS[self.tier]

    @property
    def tier_emoji(self) -> str:
        return TIER_EMOJI[self.tier]

    @property
    def number(self) -> str:
        return f"{self.id:04d}"

    @property
    def gen_roman(self) -> str:
        return ROMAN[self.gen] if self.gen < len(ROMAN) else str(self.gen)

    @property
    def types_text(self) -> str:
        return " / ".join(t.title() for t in self.types)

    @property
    def color(self) -> int:
        return TYPE_COLORS.get(self.types[0], 0x68A090)

    @property
    def height_m(self) -> float:
        return self.height / 10

    @property
    def weight_kg(self) -> float:
        return self.weight / 10

    @property
    def stats_text(self) -> str:
        return " · ".join(f"{n} {v}" for n, v in zip(STAT_NAMES, self.stats)) + f" (total {sum(self.stats)})"

    @property
    def artwork_url(self) -> str:
        return ARTWORK_URL.format(self.id)

    @property
    def sprite_url(self) -> str:
        return SPRITE_URL.format(self.id)


class Pokedex:
    def __init__(self, species: list[Species]):
        ids = [s.id for s in species]
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError("species ids must run 1..N with no gaps")
        self.species = species
        self._by_name: dict[str, Species] = {}
        self.search_names: dict[int, str] = {}  # species id -> normalized name, for matching typed text
        for s in species:
            key = normalize(s.name)
            if key in self._by_name:
                raise ValueError(f"{s.name} and {self._by_name[key].name} normalize to the same name")
            self._by_name[key] = s
            self.search_names[s.id] = key

        self._pools: dict[str, list[Species]] = {tier: [] for tier in TIER_ODDS}
        for s in species:
            self._pools[s.tier].append(s)
        # A tier nobody falls into can't be rolled, so leave it out rather than crash on an empty pool.
        self._tiers = [t for t in TIER_ODDS if self._pools[t]]
        self._weights = [TIER_ODDS[t] for t in self._tiers]

    @classmethod
    def load(cls, path: Path = DATA_PATH) -> "Pokedex":
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)["species"]
        return cls([
            Species(
                id=r["id"], name=r["name"], gen=r["gen"], types=tuple(r["types"]), genus=r["genus"],
                height=r["height"], weight=r["weight"], stats=tuple(r["stats"]), capture_rate=r["capture_rate"],
                legendary=r["legendary"], mythical=r["mythical"], baby=r["baby"],
            )
            for r in raw
        ])

    def __len__(self) -> int:
        return len(self.species)

    def __iter__(self):
        return iter(self.species)

    def get(self, species_id: int) -> Species | None:
        return self.species[species_id - 1] if 1 <= species_id <= len(self.species) else None

    def __getitem__(self, species_id: int) -> Species:
        species = self.get(species_id)
        if species is None:
            raise KeyError(species_id)
        return species

    @property
    def generations(self) -> list[int]:
        return sorted({s.gen for s in self.species})

    def generation(self, gen: int) -> list[Species]:
        return [s for s in self.species if s.gen == gen]

    def find(self, query: str) -> list[Species]:
        """A dex number ('25', '#025') or a name. Exact name first, then names starting with it, then containing it."""
        query = query.strip()[:64].lstrip("#")
        if re.fullmatch(r"[0-9]+", query):
            species = self.get(int(query))
            return [species] if species else []
        key = normalize(query)
        if not key:
            return []
        if key in self._by_name:
            return [self._by_name[key]]
        starts = [s for k, s in self._by_name.items() if k.startswith(key)]
        contains = [s for k, s in self._by_name.items() if key in k and not k.startswith(key)]
        return starts + contains

    def roll(self, rng: random.Random | None = None) -> Species:
        """Pick a tier by TIER_ODDS, then a species from it uniformly."""
        rng = rng or random
        tier = rng.choices(self._tiers, weights=self._weights)[0]
        return rng.choice(self._pools[tier])
