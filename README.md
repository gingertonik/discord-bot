# Discord bot

One bot, three features:

- **`!rules`** reposts your server's existing rules message as an embed.
- **Music**: plays songs from your [Navidrome](https://www.navidrome.org) library in a voice channel.
- **Pokémon game**: `;pokemon` catches a random Pokémon every 90 minutes. Keep a collection, fill a
  Pokédex covering all nine generations, and trade with other people in your server.

Every command works with the `!` prefix, the `;` prefix (so `!pokemon` and `;rules` work too), and as a
slash command.

## Setting it up

1. **Make the bot** at <https://discord.com/developers/applications>: New Application, then *Bot*. Copy the
   token. On the same page switch on **Message Content Intent**. Without it `!` and `;` commands silently do
   nothing.
2. **Invite it** with this link (put your application's ID in place of `YOUR_APP_ID`). It asks for the `bot`
   and `applications.commands` scopes and these permissions: View Channels, Send Messages, Embed Links, Read
   Message History, Connect, Speak:

   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=3230720`
3. **Install** (needs Python 3.10+, and [ffmpeg](https://ffmpeg.org) on your PATH if you want music):

   ```
   cd ~/Projects/discord-bot
   uv venv && uv pip install -r requirements.txt
   ```
   (Without uv: `python -m venv .venv && .venv/bin/pip install -r requirements.txt`.)
4. **Configure**: copy `.env.example` to `.env` and fill it in. It's a plain `KEY=VALUE` file, so there's
   no `export` or `set -x` to worry about, whichever shell you use.
5. **Run**: `.venv/bin/python bot.py`

Only `DISCORD_TOKEN` is required. Leave the Navidrome settings blank and music stays off while everything
else works.

| Setting | What it does |
| --- | --- |
| `DISCORD_TOKEN` | The bot token (required). |
| `GUILD_ID` | Your server's ID. Makes slash commands appear instantly; without it they can take up to an hour. |
| `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASS` | Turn on music. A dedicated Navidrome user is a good idea. |
| `RULES_CHANNEL_ID`, `RULES_MESSAGE_ID` | Which message `!rules` reposts. Default: the channel with "rules" in its name, and that channel's oldest message. |
| `POKEMON_DB` | Where the game keeps its data. Default: `data/pokemon.db`. |

## Commands

### Rules
- `!rules`: posts the rules message as an embed (a message that's already an embed is reused as-is).

### Music
Join a voice channel first; the bot joins you.

| Command | |
| --- | --- |
| `!play <words>` | Search the library and play the best match, or queue it if something is playing. |
| `!search <words>` | Show the top 10 matches and pick which to queue from a dropdown. |
| `!queue` (`!q`), `!nowplaying` (`!np`) | What's playing and what's next. |
| `!skip`, `!pause`, `!resume` | Playback controls. |
| `!stop` (`!leave`) | Stop, clear the queue and leave. |

Playback controls only work for people in the bot's voice channel. The bot leaves by itself after five
minutes of nothing playing or of an empty channel.

### Pokémon
| Command | |
| --- | --- |
| `;pokemon` (`;catch`) | Catch a random Pokémon. **Once every 90 minutes** per person. |
| `;inventory [@user] [name]` (`;inv`, `;box`) | Your Pokémon, 10 per page, newest first. Sort from the dropdown, filter by name or Pokédex number (`;inv pika`), or look at someone else's. |
| `;info <id>` | Everything about one Pokémon, by the `#ID` shown in the inventory. |
| `;pokedex` (`;dex`) | Your Pokédex progress for each generation. |
| `;pokedex <name or number>` | Look up any of the 1,025 species and see whether you've registered it. |
| `;pokedex gen 3` | Browse a generation, with a tick next to what you've registered. |
| `;trade @user <your ids> <their ids>` | Offer a swap, e.g. `;trade @Ash 4821 5003` or `;trade @Ash 12,15 40`. |

**How it works**
- Every catch is its own Pokémon with a permanent ID. Catch two Pikachu and you have two.
- Your Pokédex records every species you've ever caught or received in a trade. Trading a Pokémon away
  doesn't remove it from your Pokédex.
- Your Pokémon, Pokédex and cooldown belong to *you*, not to a server, so hopping between servers doesn't
  skip the cooldown.
- **Odds** (per catch): Common 55%, Uncommon 28%, Rare 14%, Legendary 2.5%, Mythical 0.5%. A species'
  tier comes from its mainline-games catch rate; every species in a tier is equally likely. Change
  `TIER_ODDS` in `pokedex.py` to retune.
- **Trading**: you name the Pokémon on both sides (up to 6 each). The other person gets a message with
  **Accept** and **Decline** buttons and two minutes to answer; only they can accept, and either of you can
  cancel. Nothing moves until they accept, and ownership is re-checked at that moment, so an offer that's
  gone stale just fails and nothing is traded. Both people must be in the server where it's proposed.
- **The Pokédex** holds all **1,025** species of generations I-IX (National Dex #1-1025), built from
  [PokeAPI](https://pokeapi.co)'s data. To refresh it when new Pokémon come out:
  `python -I scripts/build_pokedex.py <new empty dir> --download`

## Files

```
bot.py                  entry point: loads the cogs, registers slash commands, reports errors
cogs/rules.py           !rules
cogs/music.py           music player
cogs/pokemon.py         the Pokémon game's commands and buttons
pokedex.py              the species data, name lookup and the catch roll
pokestore.py            SQLite storage; catches and trades are single transactions
navidrome.py            tiny Navidrome (Subsonic API) client
data/pokedex.json       the 1,025 species (generated)
data/pokemon.db         your game data, created on first run (back this up!)
tests/                  .venv/bin/python -m unittest
```

## Status — October 2026

All 98 tests pass: `.venv/bin/python -m unittest discover -s tests`. Four suites cover the config, the
Navidrome client, the music player's plumbing, and a command-layer harness (`tests/test_commands.py`)
that drives the real discord.py against a fake Discord server: real cogs, real slash and prefix dispatch,
real button and dropdown clicks, a fake clock for the catch cooldown.

The test pass found **one real bug**, now fixed: `!search` put *every* match into its dropdown. Discord
caps select menus at 10 options and rejects the whole message, so a search with 11+ hits would have failed
in production. `cogs/music.py` now cuts the list to the top 10.

Things learned the hard way, worth remembering before touching this code again:

- **Prefix replies can't be ephemeral.** discord.py honours `ephemeral=True` only for slash-command
  interactions and silently ignores it elsewhere — so `!play` with no song in a channel posts the
  warning in the channel. That is by design now.
- **10 options is a hard limit** on every dropdown, not a style choice.
- A catch counts toward the **trainer's own** Pokédex, not a shared one: the "✨ New Pokédex entry!" line
  means *you* registered that species for the first time.
- The inventory's `#ID` is a per-Pokémon serial, so `;info 1` opens *your first catch* — any species.
  To look at a species by Pokédex number, use `;pokedex 6`.
- The test harness keeps notes in `.worknotes/` — how the fake server works and what to watch for.
