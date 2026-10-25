"""Environment handling shared by the bot and its cogs."""

import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    """
    Read KEY=VALUE lines from a .env file into os.environ. Real environment variables win, so
    `DISCORD_TOKEN=... python bot.py` still overrides the file. Blank lines and # comments are skipped.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def env_int(name: str) -> int | None:
    """An optional numeric setting such as a channel ID. Unset or empty is None; anything else must be a number."""
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} must be a number (a Discord ID), but it is {value!r}") from None
