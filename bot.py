import logging
from typing import Tuple

from discord.ext import commands

from kiero_bot.common import load_token
from kiero_bot.config import INTENTS
from kiero_bot.database import init_database


EXTENSIONS: Tuple[str, ...] = (
    "cogs.general",
    "cogs.moderation",
    "cogs.tickets",
)


class KieroBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=INTENTS)

    async def setup_hook(self) -> None:
        init_database()
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        # Registers slash commands globally.
        await self.tree.sync()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = KieroBot()
    bot.run(load_token())


if __name__ == "__main__":
    main()

