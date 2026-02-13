import sqlite3

from kiero_bot.config import DATABASE_PATH


def init_database() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS temporary_actions (
                action_type TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (action_type, guild_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                support_role_id INTEGER NOT NULL,
                log_channel_id INTEGER,
                panel_channel_id INTEGER,
                max_open_tickets INTEGER NOT NULL DEFAULT 1,
                ticket_name_prefix TEXT NOT NULL DEFAULT 'ticket'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_counters (
                guild_id INTEGER PRIMARY KEY,
                last_number INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                ticket_number INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                subject TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                closed_at INTEGER,
                closed_by INTEGER,
                closed_reason TEXT,
                UNIQUE(guild_id, ticket_number)
            )
            """
        )
        connection.commit()

