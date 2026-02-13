from datetime import timedelta
from typing import Optional

import discord

from kiero_bot.config import TOKEN_PATH


def parse_duration(raw: str) -> timedelta:
    parts = raw.split(":")
    if len(parts) != 4:
        raise ValueError("Duration format must be d:h:m:s")

    try:
        days, hours, minutes, seconds = (int(part) for part in parts)
    except ValueError as error:
        raise ValueError("Duration must contain only numbers") from error

    if any(value < 0 for value in (days, hours, minutes, seconds)):
        raise ValueError("Duration values cannot be negative")
    if hours > 23 or minutes > 59 or seconds > 59:
        raise ValueError("Hours must be <= 23, minutes/seconds <= 59")

    duration = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if duration.total_seconds() <= 0:
        raise ValueError("Duration must be greater than zero")
    return duration


def format_duration(duration: timedelta) -> str:
    total_seconds = int(duration.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


async def send_message(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        return
    await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)


def current_timestamp() -> int:
    return int(discord.utils.utcnow().timestamp())


def load_token() -> str:
    if not TOKEN_PATH.exists():
        raise FileNotFoundError("File token.txt was not found.")

    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("File token.txt is empty.")
    return token

