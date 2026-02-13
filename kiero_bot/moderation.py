import asyncio
import logging
import sqlite3
from typing import Any, Coroutine, Dict, List, Tuple, Optional

import discord
from discord.ext import commands

from kiero_bot.common import current_timestamp
from kiero_bot.config import ACTION_BAN, ACTION_MUTE, DATABASE_PATH, RETRY_DELAY_SECONDS


TEMP_ACTION_TASKS: Dict[Tuple[str, int, int], asyncio.Task] = {}


def action_key(action_type: str, guild_id: int, user_id: int) -> Tuple[str, int, int]:
    return action_type, guild_id, user_id


def create_background_task(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    task.add_done_callback(log_background_error)
    return task


def log_background_error(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logging.exception("Background task failed")


def save_temporary_action(
    action_type: str,
    guild_id: int,
    user_id: int,
    expires_at: int,
    reason: str,
) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO temporary_actions (action_type, guild_id, user_id, expires_at, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(action_type, guild_id, user_id) DO UPDATE SET
                expires_at = excluded.expires_at,
                reason = excluded.reason,
                created_at = excluded.created_at
            """,
            (action_type, guild_id, user_id, expires_at, reason, current_timestamp()),
        )
        connection.commit()


def delete_temporary_action(action_type: str, guild_id: int, user_id: int) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            DELETE FROM temporary_actions
            WHERE action_type = ? AND guild_id = ? AND user_id = ?
            """,
            (action_type, guild_id, user_id),
        )
        connection.commit()


def load_temporary_actions() -> List[Tuple[str, int, int, int, str]]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            SELECT action_type, guild_id, user_id, expires_at, reason
            FROM temporary_actions
            """
        )
        rows = cursor.fetchall()
    return [(row[0], int(row[1]), int(row[2]), int(row[3]), str(row[4])) for row in rows]


def cancel_temporary_action(
    action_type: str,
    guild_id: int,
    user_id: int,
    *,
    delete_from_db: bool,
) -> None:
    key = action_key(action_type, guild_id, user_id)
    task = TEMP_ACTION_TASKS.pop(key, None)
    if task is not None and not task.done():
        task.cancel()
    if delete_from_db:
        delete_temporary_action(action_type, guild_id, user_id)


def schedule_temporary_action(
    bot: commands.Bot,
    action_type: str,
    guild_id: int,
    user_id: int,
    expires_at: int,
    reason: str,
) -> None:
    cancel_temporary_action(action_type, guild_id, user_id, delete_from_db=False)
    key = action_key(action_type, guild_id, user_id)
    task = create_background_task(
        process_temporary_action(
            bot=bot,
            action_type=action_type,
            guild_id=guild_id,
            user_id=user_id,
            expires_at=expires_at,
            reason=reason,
        )
    )
    TEMP_ACTION_TASKS[key] = task
    task.add_done_callback(
        lambda finished, key=key: TEMP_ACTION_TASKS.pop(key, None)
        if TEMP_ACTION_TASKS.get(key) is finished
        else None
    )


async def resolve_guild(bot: commands.Bot, guild_id: int) -> Optional[discord.Guild]:
    guild = bot.get_guild(guild_id)
    if guild is not None:
        return guild
    try:
        return await bot.fetch_guild(guild_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return None


async def perform_automatic_unban(
    bot: commands.Bot,
    guild_id: int,
    user_id: int,
    reason: str,
) -> bool:
    guild = await resolve_guild(bot, guild_id)
    if guild is None:
        logging.warning("Cannot resolve guild %s for automatic unban of %s", guild_id, user_id)
        return False

    try:
        await guild.unban(discord.Object(id=user_id), reason=reason)
    except discord.NotFound:
        return True
    except discord.Forbidden:
        logging.warning("Missing permissions to unban user %s in guild %s", user_id, guild_id)
        return False
    except discord.HTTPException:
        logging.exception("Failed to unban user %s in guild %s", user_id, guild_id)
        return False
    return True


async def perform_automatic_unmute(
    bot: commands.Bot,
    guild_id: int,
    user_id: int,
    reason: str,
) -> bool:
    guild = await resolve_guild(bot, guild_id)
    if guild is None:
        logging.warning("Cannot resolve guild %s for automatic unmute of %s", guild_id, user_id)
        return False

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return True
        except discord.Forbidden:
            logging.warning("Missing permissions to fetch member %s in guild %s", user_id, guild_id)
            return False
        except discord.HTTPException:
            logging.exception("Failed to fetch member %s in guild %s", user_id, guild_id)
            return False

    try:
        await member.timeout(None, reason=reason)
    except discord.NotFound:
        return True
    except discord.Forbidden:
        logging.warning("Missing permissions to unmute user %s in guild %s", user_id, guild_id)
        return False
    except discord.HTTPException:
        logging.exception("Failed to unmute user %s in guild %s", user_id, guild_id)
        return False
    return True


async def process_temporary_action(
    bot: commands.Bot,
    action_type: str,
    guild_id: int,
    user_id: int,
    expires_at: int,
    reason: str,
) -> None:
    next_attempt = expires_at
    while True:
        delay = max(0, next_attempt - current_timestamp())
        if delay > 0:
            await asyncio.sleep(delay)

        if action_type == ACTION_BAN:
            is_done = await perform_automatic_unban(bot, guild_id, user_id, reason)
        elif action_type == ACTION_MUTE:
            is_done = await perform_automatic_unmute(bot, guild_id, user_id, reason)
        else:
            logging.warning("Unknown temporary action type: %s", action_type)
            delete_temporary_action(action_type, guild_id, user_id)
            return

        if is_done:
            delete_temporary_action(action_type, guild_id, user_id)
            return

        next_attempt = current_timestamp() + RETRY_DELAY_SECONDS
        save_temporary_action(action_type, guild_id, user_id, next_attempt, reason)
        logging.warning(
            "Retrying temporary action %s for guild %s user %s in %s seconds",
            action_type,
            guild_id,
            user_id,
            RETRY_DELAY_SECONDS,
        )


async def restore_temporary_actions(bot: commands.Bot) -> None:
    rows = load_temporary_actions()
    for action_type, guild_id, user_id, expires_at, reason in rows:
        schedule_temporary_action(
            bot=bot,
            action_type=action_type,
            guild_id=guild_id,
            user_id=user_id,
            expires_at=expires_at,
            reason=reason,
        )
    if rows:
        logging.info("Restored %s temporary action(s) from database.", len(rows))

