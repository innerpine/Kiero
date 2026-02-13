import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Coroutine, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands


TOKEN_PATH = Path("token.txt")
DATABASE_PATH = Path("bot_data.sqlite3")
INTENTS = discord.Intents.default()
ACTION_BAN = "ban"
ACTION_MUTE = "mute"
RETRY_DELAY_SECONDS = 600
TEMP_ACTION_TASKS: Dict[Tuple[str, int, int], asyncio.Task] = {}
ACTION_TICKET_OPEN = "open"
ACTION_TICKET_CLOSED = "closed"
DEFAULT_TICKET_PREFIX = "ticket"
MAX_TICKET_SUBJECT_LENGTH = 200
MAX_TICKET_CLOSE_REASON_LENGTH = 300


@dataclass
class TicketSettings:
    guild_id: int
    category_id: int
    support_role_id: int
    log_channel_id: Optional[int]
    panel_channel_id: Optional[int]
    max_open_tickets: int
    ticket_name_prefix: str


@dataclass
class TicketRecord:
    channel_id: int
    guild_id: int
    ticket_number: int
    owner_id: int
    status: str
    subject: str
    created_at: int


class KieroBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=INTENTS)

    async def setup_hook(self) -> None:
        init_database()
        await restore_temporary_actions()
        self.add_view(TicketPanelView())
        self.add_view(TicketCloseView())
        # Registers slash commands globally.
        await self.tree.sync()


bot = KieroBot()
ticket_group = app_commands.Group(name="ticket", description="Ticket commands")
ticket_config_group = app_commands.Group(name="ticketconfig", description="Ticket configuration commands")
bot.tree.add_command(ticket_group)
bot.tree.add_command(ticket_config_group)


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


def get_bot_member(guild: discord.Guild) -> Optional[discord.Member]:
    if bot.user is None:
        return None
    return guild.get_member(bot.user.id)


async def validate_moderation(
    interaction: discord.Interaction,
    target: discord.Member,
    *,
    permission_name: str,
) -> Optional[Tuple[discord.Guild, discord.Member, discord.Member]]:
    validated = await validate_permission(interaction, permission_name=permission_name)
    if validated is None:
        return None

    guild, actor, bot_member = validated
    if target.id == actor.id:
        await send_message(interaction, "You cannot use this command on yourself.", ephemeral=True)
        return None
    if target.id == bot_member.id:
        await send_message(interaction, "You cannot target the bot.", ephemeral=True)
        return None
    if target.id == guild.owner_id:
        await send_message(interaction, "You cannot target the server owner.", ephemeral=True)
        return None

    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        await send_message(interaction, "You can only target members below your top role.", ephemeral=True)
        return None
    if target.top_role >= bot_member.top_role:
        await send_message(interaction, "I can only target members below my top role.", ephemeral=True)
        return None

    return guild, actor, bot_member


async def validate_permission(
    interaction: discord.Interaction,
    *,
    permission_name: str,
    require_bot_permission: bool = True,
) -> Optional[Tuple[discord.Guild, discord.Member, discord.Member]]:
    guild = interaction.guild
    actor = interaction.user
    if guild is None or not isinstance(actor, discord.Member):
        await send_message(interaction, "This command can only be used in a server.", ephemeral=True)
        return None

    bot_member = get_bot_member(guild)
    if bot_member is None:
        await send_message(interaction, "I cannot resolve my member data in this guild.", ephemeral=True)
        return None

    if not getattr(actor.guild_permissions, permission_name):
        await send_message(interaction, "You do not have permission to use this command.", ephemeral=True)
        return None
    if require_bot_permission and not getattr(bot_member.guild_permissions, permission_name):
        await send_message(interaction, "I do not have the required permission for this command.", ephemeral=True)
        return None

    return guild, actor, bot_member


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


def action_key(action_type: str, guild_id: int, user_id: int) -> Tuple[str, int, int]:
    return action_type, guild_id, user_id


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


async def resolve_guild(guild_id: int) -> Optional[discord.Guild]:
    guild = bot.get_guild(guild_id)
    if guild is not None:
        return guild
    try:
        return await bot.fetch_guild(guild_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return None


async def perform_automatic_unban(guild_id: int, user_id: int, reason: str) -> bool:
    guild = await resolve_guild(guild_id)
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


async def perform_automatic_unmute(guild_id: int, user_id: int, reason: str) -> bool:
    guild = await resolve_guild(guild_id)
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
            is_done = await perform_automatic_unban(guild_id, user_id, reason)
        elif action_type == ACTION_MUTE:
            is_done = await perform_automatic_unmute(guild_id, user_id, reason)
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


async def restore_temporary_actions() -> None:
    rows = load_temporary_actions()
    for action_type, guild_id, user_id, expires_at, reason in rows:
        schedule_temporary_action(
            action_type=action_type,
            guild_id=guild_id,
            user_id=user_id,
            expires_at=expires_at,
            reason=reason,
        )
    if rows:
        logging.info("Restored %s temporary action(s) from database.", len(rows))


def sanitize_ticket_prefix(raw_prefix: str) -> Optional[str]:
    normalized = raw_prefix.strip().lower().replace(" ", "-")
    normalized = re.sub(r"[^a-z0-9-]", "", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if not normalized:
        return None
    return normalized[:24]


def save_ticket_settings(settings: TicketSettings) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO ticket_settings (
                guild_id, category_id, support_role_id, log_channel_id, panel_channel_id, max_open_tickets, ticket_name_prefix
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                category_id = excluded.category_id,
                support_role_id = excluded.support_role_id,
                log_channel_id = excluded.log_channel_id,
                panel_channel_id = excluded.panel_channel_id,
                max_open_tickets = excluded.max_open_tickets,
                ticket_name_prefix = excluded.ticket_name_prefix
            """,
            (
                settings.guild_id,
                settings.category_id,
                settings.support_role_id,
                settings.log_channel_id,
                settings.panel_channel_id,
                settings.max_open_tickets,
                settings.ticket_name_prefix,
            ),
        )
        connection.commit()


def get_ticket_settings(guild_id: int) -> Optional[TicketSettings]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            SELECT guild_id, category_id, support_role_id, log_channel_id, panel_channel_id, max_open_tickets, ticket_name_prefix
            FROM ticket_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return TicketSettings(
        guild_id=int(row[0]),
        category_id=int(row[1]),
        support_role_id=int(row[2]),
        log_channel_id=int(row[3]) if row[3] is not None else None,
        panel_channel_id=int(row[4]) if row[4] is not None else None,
        max_open_tickets=int(row[5]),
        ticket_name_prefix=str(row[6]),
    )


def update_ticket_panel_channel(guild_id: int, panel_channel_id: int) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            UPDATE ticket_settings
            SET panel_channel_id = ?
            WHERE guild_id = ?
            """,
            (panel_channel_id, guild_id),
        )
        connection.commit()


def next_ticket_number(guild_id: int) -> int:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            "SELECT last_number FROM ticket_counters WHERE guild_id = ?",
            (guild_id,),
        )
        row = cursor.fetchone()
        if row is None:
            next_number = 1
            connection.execute(
                "INSERT INTO ticket_counters (guild_id, last_number) VALUES (?, ?)",
                (guild_id, next_number),
            )
        else:
            next_number = int(row[0]) + 1
            connection.execute(
                "UPDATE ticket_counters SET last_number = ? WHERE guild_id = ?",
                (next_number, guild_id),
            )
        connection.commit()
    return next_number


def create_ticket_record(
    channel_id: int,
    guild_id: int,
    ticket_number: int,
    owner_id: int,
    subject: str,
) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO tickets (channel_id, guild_id, ticket_number, owner_id, status, subject, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, guild_id, ticket_number, owner_id, ACTION_TICKET_OPEN, subject, current_timestamp()),
        )
        connection.commit()


def get_open_ticket_by_channel(channel_id: int) -> Optional[TicketRecord]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            SELECT channel_id, guild_id, ticket_number, owner_id, status, subject, created_at
            FROM tickets
            WHERE channel_id = ? AND status = ?
            """,
            (channel_id, ACTION_TICKET_OPEN),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return TicketRecord(
        channel_id=int(row[0]),
        guild_id=int(row[1]),
        ticket_number=int(row[2]),
        owner_id=int(row[3]),
        status=str(row[4]),
        subject=str(row[5]),
        created_at=int(row[6]),
    )


def count_open_tickets_for_owner(guild_id: int, owner_id: int) -> int:
    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            SELECT COUNT(*)
            FROM tickets
            WHERE guild_id = ? AND owner_id = ? AND status = ?
            """,
            (guild_id, owner_id, ACTION_TICKET_OPEN),
        )
        row = cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])


def close_ticket_record(channel_id: int, closed_by: int, close_reason: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            UPDATE tickets
            SET status = ?, closed_at = ?, closed_by = ?, closed_reason = ?
            WHERE channel_id = ? AND status = ?
            """,
            (
                ACTION_TICKET_CLOSED,
                current_timestamp(),
                closed_by,
                close_reason,
                channel_id,
                ACTION_TICKET_OPEN,
            ),
        )
        connection.commit()


def resolve_ticket_assets(
    guild: discord.Guild,
    settings: TicketSettings,
) -> Tuple[Optional[discord.CategoryChannel], Optional[discord.Role], Optional[discord.TextChannel]]:
    category = guild.get_channel(settings.category_id)
    if not isinstance(category, discord.CategoryChannel):
        category = None

    support_role = guild.get_role(settings.support_role_id)
    log_channel: Optional[discord.TextChannel] = None
    if settings.log_channel_id is not None:
        maybe_log_channel = guild.get_channel(settings.log_channel_id)
        if isinstance(maybe_log_channel, discord.TextChannel):
            log_channel = maybe_log_channel

    return category, support_role, log_channel


def member_is_ticket_support(member: discord.Member, settings: Optional[TicketSettings]) -> bool:
    if settings is None:
        return False
    support_role = member.guild.get_role(settings.support_role_id)
    if support_role is None:
        return False
    return support_role in member.roles


async def send_ticket_log(
    guild: discord.Guild,
    settings: Optional[TicketSettings],
    embed: discord.Embed,
) -> None:
    if settings is None or settings.log_channel_id is None:
        return

    log_channel = guild.get_channel(settings.log_channel_id)
    if not isinstance(log_channel, discord.TextChannel):
        return

    try:
        await log_channel.send(embed=embed)
    except discord.HTTPException:
        logging.exception("Failed to send ticket log in guild %s", guild.id)


def build_ticket_channel_name(prefix: str, ticket_number: int) -> str:
    return f"{prefix}-{ticket_number:04d}"[:90]


async def create_ticket_for_interaction(interaction: discord.Interaction, subject: Optional[str]) -> None:
    guild = interaction.guild
    actor = interaction.user
    if guild is None or not isinstance(actor, discord.Member):
        await send_message(interaction, "This command can only be used in a server.", ephemeral=True)
        return

    settings = get_ticket_settings(guild.id)
    if settings is None:
        await send_message(
            interaction,
            "Ticket system is not configured. Use `/ticketconfig setup` first.",
            ephemeral=True,
        )
        return

    category, support_role, _ = resolve_ticket_assets(guild, settings)
    if category is None:
        await send_message(interaction, "Configured ticket category no longer exists.", ephemeral=True)
        return
    if support_role is None:
        await send_message(interaction, "Configured support role no longer exists.", ephemeral=True)
        return

    current_open_tickets = count_open_tickets_for_owner(guild.id, actor.id)
    if current_open_tickets >= settings.max_open_tickets:
        await send_message(
            interaction,
            f"You already have the maximum number of open tickets: `{settings.max_open_tickets}`.",
            ephemeral=True,
        )
        return

    cleaned_subject = (subject or "No subject provided.").strip()
    if not cleaned_subject:
        cleaned_subject = "No subject provided."
    if len(cleaned_subject) > MAX_TICKET_SUBJECT_LENGTH:
        await send_message(
            interaction,
            f"Subject is too long. Max length: `{MAX_TICKET_SUBJECT_LENGTH}`.",
            ephemeral=True,
        )
        return

    bot_member = get_bot_member(guild)
    if bot_member is None:
        await send_message(interaction, "I cannot resolve my member data in this guild.", ephemeral=True)
        return

    ticket_number = next_ticket_number(guild.id)
    channel_name = build_ticket_channel_name(settings.ticket_name_prefix, ticket_number)
    reason = f"Ticket #{ticket_number} created by {actor} ({actor.id})"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        actor: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        support_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            manage_messages=True,
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        ),
    }

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            reason=reason,
            overwrites=overwrites,
        )
    except discord.Forbidden:
        await send_message(interaction, "I do not have permission to create ticket channels.", ephemeral=True)
        return
    except discord.HTTPException:
        await send_message(interaction, "Failed to create ticket channel due to a Discord API error.", ephemeral=True)
        return

    create_ticket_record(
        channel_id=ticket_channel.id,
        guild_id=guild.id,
        ticket_number=ticket_number,
        owner_id=actor.id,
        subject=cleaned_subject,
    )

    ticket_embed = discord.Embed(
        title=f"Ticket #{ticket_number}",
        description=cleaned_subject,
        color=discord.Color.orange(),
    )
    ticket_embed.add_field(name="Owner", value=actor.mention, inline=True)
    ticket_embed.add_field(name="Opened", value=f"<t:{current_timestamp()}:F>", inline=True)
    ticket_embed.set_footer(text="Use /ticket close or the button below when resolved.")

    ping_content = "{0} {1}".format(actor.mention, support_role.mention)
    try:
        await ticket_channel.send(
            content=ping_content,
            embed=ticket_embed,
            view=TicketCloseView(),
        )
    except discord.HTTPException:
        logging.exception("Failed to send initial message in ticket channel %s", ticket_channel.id)

    log_embed = discord.Embed(
        title="Ticket Opened",
        color=discord.Color.orange(),
        description=f"Ticket #{ticket_number}: {ticket_channel.mention}",
    )
    log_embed.add_field(name="Owner", value=f"{actor.mention} (`{actor.id}`)", inline=False)
    log_embed.add_field(name="Subject", value=cleaned_subject, inline=False)
    await send_ticket_log(guild, settings, log_embed)

    await send_message(
        interaction,
        f"Ticket created: {ticket_channel.mention}",
        ephemeral=True,
    )


async def resolve_ticket_close_context(
    interaction: discord.Interaction,
) -> Optional[Tuple[discord.Guild, discord.Member, discord.TextChannel, TicketRecord, Optional[TicketSettings]]]:
    guild = interaction.guild
    actor = interaction.user
    channel = interaction.channel
    if guild is None or not isinstance(actor, discord.Member):
        await send_message(interaction, "This command can only be used in a server.", ephemeral=True)
        return None
    if not isinstance(channel, discord.TextChannel):
        await send_message(interaction, "This command can only be used in a ticket text channel.", ephemeral=True)
        return None

    ticket = get_open_ticket_by_channel(channel.id)
    if ticket is None:
        await send_message(interaction, "This channel is not an open ticket.", ephemeral=True)
        return None

    settings = get_ticket_settings(guild.id)
    is_owner = actor.id == ticket.owner_id
    is_support = member_is_ticket_support(actor, settings)
    can_manage = actor.guild_permissions.manage_channels
    if not is_owner and not is_support and not can_manage:
        await send_message(interaction, "You do not have permission to close this ticket.", ephemeral=True)
        return None

    return guild, actor, channel, ticket, settings


async def close_ticket_for_interaction(interaction: discord.Interaction, reason: Optional[str]) -> None:
    context = await resolve_ticket_close_context(interaction)
    if context is None:
        return
    guild, actor, channel, ticket, settings = context

    close_reason = (reason or "No reason provided.").strip()
    if not close_reason:
        close_reason = "No reason provided."
    if len(close_reason) > MAX_TICKET_CLOSE_REASON_LENGTH:
        await send_message(
            interaction,
            f"Close reason is too long. Max length: `{MAX_TICKET_CLOSE_REASON_LENGTH}`.",
            ephemeral=True,
        )
        return
    close_ticket_record(channel.id, actor.id, close_reason)

    await send_message(interaction, "Ticket will be closed in 3 seconds.", ephemeral=True)

    close_notice = "Ticket closed by {0}. Reason: {1}".format(actor.mention, close_reason)
    try:
        await channel.send(close_notice)
    except discord.HTTPException:
        pass

    log_embed = discord.Embed(
        title="Ticket Closed",
        color=discord.Color.red(),
        description="Ticket #{0}: <#{1}>".format(ticket.ticket_number, channel.id),
    )
    log_embed.add_field(name="Closed by", value=f"{actor.mention} (`{actor.id}`)", inline=False)
    log_embed.add_field(name="Reason", value=close_reason, inline=False)
    await send_ticket_log(guild, settings, log_embed)

    await asyncio.sleep(3)
    try:
        await channel.delete(reason=f"Ticket closed by {actor} ({actor.id})")
    except discord.Forbidden:
        logging.warning("Missing permission to delete ticket channel %s", channel.id)
    except discord.HTTPException:
        logging.exception("Failed to delete ticket channel %s", channel.id)


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open Ticket",
        style=discord.ButtonStyle.green,
        custom_id="ticket:panel:open",
    )
    async def open_ticket_button(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await create_ticket_for_interaction(interaction, "Opened from ticket panel.")


class TicketCloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    close_reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Write the reason for closing this ticket",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=MAX_TICKET_CLOSE_REASON_LENGTH,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.close_reason).strip()
        await close_ticket_for_interaction(interaction, reason)


class TicketCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.red,
        custom_id="ticket:channel:close",
    )
    async def close_ticket_button(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        context = await resolve_ticket_close_context(interaction)
        if context is None:
            return
        await interaction.response.send_modal(TicketCloseReasonModal())


@bot.tree.command(name="hello", description="Simple hello command")
async def hello(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f"Hello, {interaction.user.mention}!")


@bot.tree.command(name="help", description="Show available bot commands")
async def help_command(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Kiero Bot Commands",
        color=discord.Color.blurple(),
        description="Main commands for moderation and tickets.",
    )
    embed.add_field(
        name="General",
        value="/hello, /ping, /avatar, /userinfo, /serverinfo",
        inline=False,
    )
    embed.add_field(
        name="Moderation",
        value="/ban, /mute, /unban, /unmute, /kick, /purge",
        inline=False,
    )
    embed.add_field(
        name="Tickets",
        value="/ticket open, /ticket close, /ticket info",
        inline=False,
    )
    embed.add_field(
        name="Ticket Config (Admins)",
        value="/ticketconfig setup, /ticketconfig panel, /ticketconfig show",
        inline=False,
    )
    embed.set_footer(text="Duration format for punishments: d:h:m:s")
    await send_message(interaction, embed=embed, ephemeral=True)


@ticket_group.command(name="open", description="Open a support ticket")
@app_commands.describe(subject="Short ticket subject")
@app_commands.guild_only()
async def ticket_open(interaction: discord.Interaction, subject: Optional[str] = None) -> None:
    await create_ticket_for_interaction(interaction, subject)


@ticket_group.command(name="close", description="Close the current ticket")
@app_commands.describe(reason="Why this ticket is being closed")
@app_commands.guild_only()
async def ticket_close(interaction: discord.Interaction, reason: Optional[str] = None) -> None:
    await close_ticket_for_interaction(interaction, reason)


@ticket_group.command(name="info", description="Show info about the current ticket")
@app_commands.guild_only()
async def ticket_info(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    channel = interaction.channel
    if guild is None or not isinstance(channel, discord.TextChannel):
        await send_message(interaction, "This command can only be used in a ticket text channel.", ephemeral=True)
        return

    ticket = get_open_ticket_by_channel(channel.id)
    if ticket is None:
        await send_message(interaction, "This channel is not an open ticket.", ephemeral=True)
        return

    owner_member = guild.get_member(ticket.owner_id)
    owner_value = f"<@{ticket.owner_id}>"
    if owner_member is not None:
        owner_value = f"{owner_member.mention} (`{ticket.owner_id}`)"

    embed = discord.Embed(
        title=f"Ticket #{ticket.ticket_number}",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Owner", value=owner_value, inline=False)
    embed.add_field(name="Created", value=f"<t:{ticket.created_at}:F>", inline=False)
    embed.add_field(name="Subject", value=ticket.subject, inline=False)
    await send_message(interaction, embed=embed, ephemeral=True)


@ticket_config_group.command(name="setup", description="Configure ticket system for this server")
@app_commands.describe(
    category="Category where ticket channels are created",
    support_role="Role that can access all tickets",
    max_open_tickets="How many tickets one user can have open at once",
    ticket_prefix="Channel name prefix, example: ticket",
    log_channel="Optional channel for ticket open/close logs",
)
@app_commands.guild_only()
async def ticket_config_setup(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    support_role: discord.Role,
    max_open_tickets: app_commands.Range[int, 1, 5] = 1,
    ticket_prefix: str = DEFAULT_TICKET_PREFIX,
    log_channel: Optional[discord.TextChannel] = None,
) -> None:
    validated = await validate_permission(
        interaction,
        permission_name="manage_guild",
        require_bot_permission=False,
    )
    if validated is None:
        return

    guild, _, _ = validated
    normalized_prefix = sanitize_ticket_prefix(ticket_prefix)
    if normalized_prefix is None:
        await send_message(
            interaction,
            "Ticket prefix must contain at least one letter or digit (allowed: a-z, 0-9, -).",
            ephemeral=True,
        )
        return

    existing_settings = get_ticket_settings(guild.id)
    panel_channel_id = existing_settings.panel_channel_id if existing_settings is not None else None
    settings = TicketSettings(
        guild_id=guild.id,
        category_id=category.id,
        support_role_id=support_role.id,
        log_channel_id=log_channel.id if log_channel is not None else None,
        panel_channel_id=panel_channel_id,
        max_open_tickets=int(max_open_tickets),
        ticket_name_prefix=normalized_prefix,
    )
    save_ticket_settings(settings)

    embed = discord.Embed(
        title="Ticket Configuration Saved",
        color=discord.Color.green(),
    )
    embed.add_field(name="Category", value=category.mention, inline=False)
    embed.add_field(name="Support role", value=support_role.mention, inline=False)
    embed.add_field(name="Max open per user", value=str(max_open_tickets), inline=True)
    embed.add_field(name="Prefix", value=normalized_prefix, inline=True)
    if log_channel is not None:
        embed.add_field(name="Log channel", value=log_channel.mention, inline=False)
    else:
        embed.add_field(name="Log channel", value="Not set", inline=False)
    await send_message(interaction, embed=embed, ephemeral=True)


@ticket_config_group.command(name="panel", description="Send ticket panel with button")
@app_commands.describe(channel="Channel where the panel message should be sent")
@app_commands.guild_only()
async def ticket_config_panel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    validated = await validate_permission(
        interaction,
        permission_name="manage_guild",
        require_bot_permission=False,
    )
    if validated is None:
        return

    guild, _, _ = validated
    settings = get_ticket_settings(guild.id)
    if settings is None:
        await send_message(
            interaction,
            "Ticket system is not configured. Use `/ticketconfig setup` first.",
            ephemeral=True,
        )
        return

    target_channel = channel
    if target_channel is None:
        if isinstance(interaction.channel, discord.TextChannel):
            target_channel = interaction.channel
        else:
            await send_message(interaction, "Please specify a text channel for the panel.", ephemeral=True)
            return

    panel_embed = discord.Embed(
        title="Support Tickets",
        color=discord.Color.blue(),
        description="Press the button below to open a ticket.",
    )
    panel_embed.add_field(name="How it works", value="A private channel will be created for you and support.", inline=False)

    try:
        await target_channel.send(embed=panel_embed, view=TicketPanelView())
    except discord.Forbidden:
        await send_message(interaction, "I do not have permission to send messages in that channel.", ephemeral=True)
        return
    except discord.HTTPException:
        await send_message(interaction, "Failed to send ticket panel due to a Discord API error.", ephemeral=True)
        return

    update_ticket_panel_channel(guild.id, target_channel.id)
    await send_message(interaction, f"Ticket panel sent to {target_channel.mention}.", ephemeral=True)


@ticket_config_group.command(name="show", description="Show current ticket configuration")
@app_commands.guild_only()
async def ticket_config_show(interaction: discord.Interaction) -> None:
    validated = await validate_permission(
        interaction,
        permission_name="manage_guild",
        require_bot_permission=False,
    )
    if validated is None:
        return

    guild, _, _ = validated
    settings = get_ticket_settings(guild.id)
    if settings is None:
        await send_message(
            interaction,
            "Ticket system is not configured. Use `/ticketconfig setup` first.",
            ephemeral=True,
        )
        return

    category = guild.get_channel(settings.category_id)
    support_role = guild.get_role(settings.support_role_id)
    log_channel = guild.get_channel(settings.log_channel_id) if settings.log_channel_id is not None else None
    panel_channel = guild.get_channel(settings.panel_channel_id) if settings.panel_channel_id is not None else None

    embed = discord.Embed(
        title="Ticket Configuration",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Category",
        value=category.mention if isinstance(category, discord.CategoryChannel) else f"Missing ({settings.category_id})",
        inline=False,
    )
    embed.add_field(
        name="Support role",
        value=support_role.mention if support_role is not None else f"Missing ({settings.support_role_id})",
        inline=False,
    )
    embed.add_field(
        name="Log channel",
        value=log_channel.mention if isinstance(log_channel, discord.TextChannel) else "Not set",
        inline=False,
    )
    embed.add_field(
        name="Panel channel",
        value=panel_channel.mention if isinstance(panel_channel, discord.TextChannel) else "Not set",
        inline=False,
    )
    embed.add_field(name="Max open per user", value=str(settings.max_open_tickets), inline=True)
    embed.add_field(name="Prefix", value=settings.ticket_name_prefix, inline=True)
    await send_message(interaction, embed=embed, ephemeral=True)


@bot.tree.command(name="ban", description="Temporarily ban a member")
@app_commands.describe(
    user="Member to ban",
    duration="Duration in d:h:m:s format",
    reason="Why this member is being banned",
)
@app_commands.guild_only()
async def ban(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    reason: str,
) -> None:
    validated = await validate_moderation(interaction, user, permission_name="ban_members")
    if validated is None:
        return

    guild, actor, _ = validated

    try:
        parsed_duration = parse_duration(duration)
    except ValueError as error:
        await send_message(interaction, f"Invalid duration: {error}", ephemeral=True)
        return

    duration_text = format_duration(parsed_duration)
    audit_reason = (
        f"Temporary ban by {actor} ({actor.id}) for {duration_text}. "
        f"Reason: {reason}"
    )

    try:
        await guild.ban(user, reason=audit_reason, delete_message_days=0)
    except discord.Forbidden:
        await send_message(interaction, "I do not have permission to ban this member.", ephemeral=True)
        return
    except discord.HTTPException:
        await send_message(interaction, "Failed to ban member due to a Discord API error.", ephemeral=True)
        return

    expires_at = current_timestamp() + int(parsed_duration.total_seconds())
    unban_reason = f"Temporary ban expired. Original reason: {reason}"
    save_temporary_action(
        action_type=ACTION_BAN,
        guild_id=guild.id,
        user_id=user.id,
        expires_at=expires_at,
        reason=unban_reason,
    )
    schedule_temporary_action(
        action_type=ACTION_BAN,
        guild_id=guild.id,
        user_id=user.id,
        expires_at=expires_at,
        reason=unban_reason,
    )

    await send_message(
        interaction,
        f"{user.mention} was banned for `{duration_text}` (until <t:{expires_at}:F>). Reason: {reason}",
    )


@bot.tree.command(name="mute", description="Temporarily mute a member")
@app_commands.describe(
    user="Member to mute",
    duration="Duration in d:h:m:s format (max 28 days)",
    reason="Why this member is being muted",
)
@app_commands.guild_only()
async def mute(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    reason: str,
) -> None:
    validated = await validate_moderation(interaction, user, permission_name="moderate_members")
    if validated is None:
        return

    _, actor, _ = validated

    if user.guild_permissions.administrator:
        await send_message(interaction, "You cannot mute a member with administrator permission.", ephemeral=True)
        return

    try:
        parsed_duration = parse_duration(duration)
    except ValueError as error:
        await send_message(interaction, f"Invalid duration: {error}", ephemeral=True)
        return

    if parsed_duration > timedelta(days=28):
        await send_message(interaction, "Mute duration cannot exceed 28 days.", ephemeral=True)
        return

    timeout_until = discord.utils.utcnow() + parsed_duration
    expires_at = int(timeout_until.timestamp())
    duration_text = format_duration(parsed_duration)
    audit_reason = (
        f"Temporary mute by {actor} ({actor.id}) for {duration_text}. "
        f"Reason: {reason}"
    )

    try:
        await user.timeout(timeout_until, reason=audit_reason)
    except discord.Forbidden:
        await send_message(interaction, "I do not have permission to mute this member.", ephemeral=True)
        return
    except discord.HTTPException:
        await send_message(interaction, "Failed to mute member due to a Discord API error.", ephemeral=True)
        return

    unmute_reason = f"Temporary mute expired. Original reason: {reason}"
    save_temporary_action(
        action_type=ACTION_MUTE,
        guild_id=user.guild.id,
        user_id=user.id,
        expires_at=expires_at,
        reason=unmute_reason,
    )
    schedule_temporary_action(
        action_type=ACTION_MUTE,
        guild_id=user.guild.id,
        user_id=user.id,
        expires_at=expires_at,
        reason=unmute_reason,
    )

    await send_message(
        interaction,
        f"{user.mention} was muted for `{duration_text}` (until <t:{expires_at}:F>). Reason: {reason}",
    )


@bot.tree.command(name="unban", description="Unban a member by user ID")
@app_commands.describe(
    user_id="User ID to unban",
    reason="Why this user is being unbanned",
)
@app_commands.guild_only()
async def unban(interaction: discord.Interaction, user_id: str, reason: str) -> None:
    validated = await validate_permission(interaction, permission_name="ban_members")
    if validated is None:
        return

    guild, actor, _ = validated
    try:
        target_user_id = int(user_id)
    except ValueError:
        await send_message(interaction, "User ID must be a number.", ephemeral=True)
        return

    audit_reason = f"Manual unban by {actor} ({actor.id}). Reason: {reason}"
    try:
        await guild.unban(discord.Object(id=target_user_id), reason=audit_reason)
    except discord.NotFound:
        await send_message(interaction, "This user is not banned.", ephemeral=True)
        return
    except discord.Forbidden:
        await send_message(interaction, "I do not have permission to unban this user.", ephemeral=True)
        return
    except discord.HTTPException:
        await send_message(interaction, "Failed to unban user due to a Discord API error.", ephemeral=True)
        return

    cancel_temporary_action(
        action_type=ACTION_BAN,
        guild_id=guild.id,
        user_id=target_user_id,
        delete_from_db=True,
    )
    await send_message(interaction, f"User `{target_user_id}` was unbanned. Reason: {reason}")


@bot.tree.command(name="unmute", description="Remove timeout from a member")
@app_commands.describe(
    user="Member to unmute",
    reason="Why this member is being unmuted",
)
@app_commands.guild_only()
async def unmute(interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
    validated = await validate_moderation(interaction, user, permission_name="moderate_members")
    if validated is None:
        return

    _, actor, _ = validated
    if user.timed_out_until is None or user.timed_out_until <= discord.utils.utcnow():
        await send_message(interaction, f"{user.mention} is not muted right now.", ephemeral=True)
        return

    audit_reason = f"Manual unmute by {actor} ({actor.id}). Reason: {reason}"
    try:
        await user.timeout(None, reason=audit_reason)
    except discord.Forbidden:
        await send_message(interaction, "I do not have permission to unmute this member.", ephemeral=True)
        return
    except discord.HTTPException:
        await send_message(interaction, "Failed to unmute member due to a Discord API error.", ephemeral=True)
        return

    cancel_temporary_action(
        action_type=ACTION_MUTE,
        guild_id=user.guild.id,
        user_id=user.id,
        delete_from_db=True,
    )
    await send_message(interaction, f"{user.mention} was unmuted. Reason: {reason}")


@bot.tree.command(name="kick", description="Kick a member from the server")
@app_commands.describe(
    user="Member to kick",
    reason="Why this member is being kicked",
)
@app_commands.guild_only()
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
    validated = await validate_moderation(interaction, user, permission_name="kick_members")
    if validated is None:
        return

    guild, actor, _ = validated
    audit_reason = f"Kick by {actor} ({actor.id}). Reason: {reason}"
    try:
        await guild.kick(user, reason=audit_reason)
    except discord.Forbidden:
        await send_message(interaction, "I do not have permission to kick this member.", ephemeral=True)
        return
    except discord.HTTPException:
        await send_message(interaction, "Failed to kick member due to a Discord API error.", ephemeral=True)
        return

    await send_message(interaction, f"{user.mention} was kicked. Reason: {reason}")


@bot.tree.command(name="purge", description="Delete recent messages in the current channel")
@app_commands.describe(
    amount="How many recent messages to scan (1-200)",
    user="Optional user filter",
)
@app_commands.guild_only()
async def purge(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 200],
    user: Optional[discord.Member] = None,
) -> None:
    validated = await validate_permission(interaction, permission_name="manage_messages")
    if validated is None:
        return

    _, actor, bot_member = validated
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await send_message(interaction, "This command can only be used in text channels or threads.", ephemeral=True)
        return

    actor_channel_permissions = channel.permissions_for(actor)
    bot_channel_permissions = channel.permissions_for(bot_member)
    if not actor_channel_permissions.manage_messages:
        await send_message(interaction, "You need Manage Messages permission in this channel.", ephemeral=True)
        return
    if not bot_channel_permissions.manage_messages or not bot_channel_permissions.read_message_history:
        await send_message(
            interaction,
            "I need Manage Messages and Read Message History permissions in this channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    def message_check(message: discord.Message) -> bool:
        if user is None:
            return True
        return message.author.id == user.id

    audit_reason = f"Purge by {actor} ({actor.id})"
    try:
        deleted = await channel.purge(limit=amount, check=message_check, bulk=False, reason=audit_reason)
    except discord.Forbidden:
        await interaction.followup.send("I do not have permission to delete messages here.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.followup.send("Failed to delete messages due to a Discord API error.", ephemeral=True)
        return

    if user is None:
        await interaction.followup.send(f"Deleted `{len(deleted)}` message(s).", ephemeral=True)
        return
    await interaction.followup.send(
        f"Deleted `{len(deleted)}` message(s) from {user.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="ping", description="Show bot latency")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    await send_message(interaction, f"Pong: `{latency_ms} ms`", ephemeral=True)


@bot.tree.command(name="avatar", description="Show user avatar")
@app_commands.describe(user="User to show avatar for")
async def avatar(interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
    target = user or interaction.user
    embed = discord.Embed(
        title=f"Avatar: {target}",
        color=discord.Color.blurple(),
    )
    embed.set_image(url=target.display_avatar.url)
    await send_message(interaction, embed=embed)


@bot.tree.command(name="userinfo", description="Show information about a server member")
@app_commands.describe(user="Member to inspect")
@app_commands.guild_only()
async def userinfo(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    guild = interaction.guild
    if guild is None:
        await send_message(interaction, "This command can only be used in a server.", ephemeral=True)
        return

    target = user or interaction.user
    if not isinstance(target, discord.Member):
        await send_message(interaction, "Could not resolve that member.", ephemeral=True)
        return

    timed_out_until = target.timed_out_until
    if timed_out_until is None or timed_out_until <= discord.utils.utcnow():
        timeout_value = "No active timeout"
    else:
        timeout_value = f"<t:{int(timed_out_until.timestamp())}:F>"

    embed = discord.Embed(
        title=f"User info: {target}",
        color=target.color if target.color.value else discord.Color.blurple(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="ID", value=str(target.id), inline=True)
    embed.add_field(name="Bot", value="Yes" if target.bot else "No", inline=True)
    embed.add_field(name="Top role", value=target.top_role.mention, inline=True)
    embed.add_field(name="Account created", value=f"<t:{int(target.created_at.timestamp())}:F>", inline=False)
    if target.joined_at is not None:
        embed.add_field(name="Joined server", value=f"<t:{int(target.joined_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Roles", value=str(max(0, len(target.roles) - 1)), inline=True)
    embed.add_field(name="Timeout", value=timeout_value, inline=True)
    await send_message(interaction, embed=embed)


@bot.tree.command(name="serverinfo", description="Show information about the current server")
@app_commands.guild_only()
async def serverinfo(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await send_message(interaction, "This command can only be used in a server.", ephemeral=True)
        return

    owner_value = "Unknown"
    if guild.owner is not None:
        owner_value = f"{guild.owner.mention} (`{guild.owner.id}`)"

    embed = discord.Embed(
        title=f"Server info: {guild.name}",
        color=discord.Color.green(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="ID", value=str(guild.id), inline=True)
    embed.add_field(name="Owner", value=owner_value, inline=True)
    embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="Boost tier", value=str(guild.premium_tier), inline=True)
    await send_message(interaction, embed=embed)


def load_token() -> str:
    if not TOKEN_PATH.exists():
        raise FileNotFoundError("File token.txt was not found.")

    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("File token.txt is empty.")
    return token


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot.run(load_token())


if __name__ == "__main__":
    main()
