"""
Regenerate data/pokedex.json from the PokeAPI CSV dump.

Usage:
    python -I scripts/build_pokedex.py CSV_DIR [--out data/pokedex.json] [--download]

CSV_DIR holds pokemon_species.csv, pokemon_species_names.csv, pokemon.csv, pokemon_types.csv,
types.csv and pokemon_stats.csv from https://github.com/PokeAPI/pokeapi/tree/master/data/v2/csv.
With --download, any missing file is fetched into CSV_DIR first (use a new, empty directory).
Only the standard library is needed.
"""

import argparse
import csv
import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

CSV_BASE = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv"
CSV_FILES = ["pokemon_species", "pokemon_species_names", "pokemon", "pokemon_types", "types", "pokemon_stats"]
ENGLISH = "9"  # local_language_id for English in PokeAPI
STAT_ORDER = [1, 2, 3, 4, 5, 6]  # hp, attack, defense, special-attack, special-defense, speed


def download(csv_dir: Path) -> None:
    for name in CSV_FILES:
        target = csv_dir / f"{name}.csv"
        if target.exists():
            continue
        print(f"downloading {name}.csv")
        req = urllib.request.Request(f"{CSV_BASE}/{name}.csv", headers={"User-Agent": "discord-bot-pokedex-builder"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            target.write_bytes(resp.read())


def read(csv_dir: Path, name: str) -> list[dict]:
    with open(csv_dir / f"{name}.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build(csv_dir: Path) -> list[dict]:
    species = {int(r["id"]): r for r in read(csv_dir, "pokemon_species")}
    names = {int(r["pokemon_species_id"]): r for r in read(csv_dir, "pokemon_species_names") if r["local_language_id"] == ENGLISH}
    type_names = {r["id"]: r["identifier"] for r in read(csv_dir, "types")}

    # Default form of each species. In PokeAPI the default form's id equals the species id for every
    # species so far, but look it up instead of assuming it.
    default_form = {int(r["species_id"]): r for r in read(csv_dir, "pokemon") if r["is_default"] == "1"}
    form_to_species = {int(r["id"]): sid for sid, r in default_form.items()}

    types: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for r in read(csv_dir, "pokemon_types"):
        sid = form_to_species.get(int(r["pokemon_id"]))
        if sid is not None:
            types[sid].append((int(r["slot"]), type_names[r["type_id"]]))

    stats: dict[int, dict[int, int]] = defaultdict(dict)
    for r in read(csv_dir, "pokemon_stats"):
        sid = form_to_species.get(int(r["pokemon_id"]))
        if sid is not None:
            stats[sid][int(r["stat_id"])] = int(r["base_stat"])

    out = []
    for sid in sorted(species):
        s, form = species[sid], default_form[sid]
        out.append({
            "id": sid,
            "name": names[sid]["name"],
            "gen": int(s["generation_id"]),
            "types": [t for _, t in sorted(types[sid])],
            "genus": names[sid]["genus"],
            "height": int(form["height"]),  # decimetres
            "weight": int(form["weight"]),  # hectograms
            "stats": [stats[sid][i] for i in STAT_ORDER],
            "capture_rate": int(s["capture_rate"]),
            "legendary": s["is_legendary"] == "1",
            "mythical": s["is_mythical"] == "1",
            "baby": s["is_baby"] == "1",
        })
    return out


def check(entries: list[dict]) -> None:
    """Fail loudly instead of writing a dex the bot would choke on."""
    ids = [e["id"] for e in entries]
    assert ids == list(range(1, len(ids) + 1)), "species ids are not contiguous from 1"
    for e in entries:
        assert e["name"], f"#{e['id']} has no English name"
        assert 1 <= len(e["types"]) <= 2, f"#{e['id']} {e['name']} has {len(e['types'])} types"
        assert len(e["stats"]) == 6 and all(v > 0 for v in e["stats"]), f"#{e['id']} {e['name']} has bad stats"
        assert 1 <= e["gen"] <= 15, f"#{e['id']} {e['name']} has generation {e['gen']}"
    gens = [e["gen"] for e in entries]
    assert gens == sorted(gens), "generations are not in dex order"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "pokedex.json")
    ap.add_argument("--download", action="store_true", help="fetch missing CSVs into csv_dir")
    args = ap.parse_args()

    if args.download:
        args.csv_dir.mkdir(parents=True, exist_ok=True)
        download(args.csv_dir)
    entries = build(args.csv_dir)
    check(entries)

    # One species per line keeps the file diffable.
    lines = ",\n".join("  " + json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in entries)
    doc = '{"source":"https://github.com/PokeAPI/pokeapi/tree/master/data/v2/csv","count":%d,"species":[\n%s\n]}\n' % (len(entries), lines)
    json.loads(doc)  # make sure what we wrote parses
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(doc, encoding="utf-8")
    print(f"wrote {len(entries)} species to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
