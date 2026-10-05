"""The Field Notebook look for embeds (matches layout.py's palette)."""

import discord

FOREST = 0x42A979  # the one pen: live, active things
MUTED = 0xABB0BA  # closed, finished things


def label(*parts: str) -> str:
    """Mono-style wall label for footers: label('squad-up', 'Valorant') -> 'SQUAD-UP · VALORANT'."""
    return " · ".join(p.upper() for p in parts if p)


def embed(title: str | None = None, description: str | None = None,
          footer: str | None = None, color: int = FOREST) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    if footer:
        e.set_footer(text=footer)
    return e
