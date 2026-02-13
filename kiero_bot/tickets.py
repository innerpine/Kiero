import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple

import discord
from discord.ext import commands

from kiero_bot.common import current_timestamp, send_message
from kiero_bot.config import (
    ACTION_TICKET_CLOSED,
    ACTION_TICKET_OPEN,
    DATABASE_PATH,
    MAX_TICKET_CLOSE_REASON_LENGTH,
    MAX_TICKET_SUBJECT_LENGTH,
)
from kiero_bot.permissions import get_bot_member


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


async def create_ticket_for_interaction(
    bot: commands.Bot,
    interaction: discord.Interaction,
    subject: Optional[str],
) -> None:
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

    bot_member = get_bot_member(bot, guild)
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
            view=TicketCloseView(bot),
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
    _bot: commands.Bot,
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


async def close_ticket_for_interaction(
    bot: commands.Bot,
    interaction: discord.Interaction,
    reason: Optional[str],
) -> None:
    context = await resolve_ticket_close_context(bot, interaction)
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
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

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
        await create_ticket_for_interaction(self.bot, interaction, "Opened from ticket panel.")


class TicketCloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    close_reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Write the reason for closing this ticket",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=MAX_TICKET_CLOSE_REASON_LENGTH,
    )

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.close_reason).strip()
        await close_ticket_for_interaction(self.bot, interaction, reason)


class TicketCloseView(discord.ui.View):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

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
        context = await resolve_ticket_close_context(self.bot, interaction)
        if context is None:
            return
        await interaction.response.send_modal(TicketCloseReasonModal(self.bot))
