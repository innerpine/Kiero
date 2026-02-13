from pathlib import Path

import discord


TOKEN_PATH = Path("token.txt")
DATABASE_PATH = Path("bot_data.sqlite3")
INTENTS = discord.Intents.default()

ACTION_BAN = "ban"
ACTION_MUTE = "mute"
ACTION_TICKET_OPEN = "open"
ACTION_TICKET_CLOSED = "closed"

RETRY_DELAY_SECONDS = 600
DEFAULT_TICKET_PREFIX = "ticket"
MAX_TICKET_SUBJECT_LENGTH = 200
MAX_TICKET_CLOSE_REASON_LENGTH = 300

