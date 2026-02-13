from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from kiero_bot.common import send_message
from kiero_bot.config import DEFAULT_TICKET_PREFIX
from kiero_bot.permissions import validate_permission
from kiero_bot.tickets import (
    TicketCloseView,
    TicketPanelView,
    TicketSettings,
    close_ticket_for_interaction,
    create_ticket_for_interaction,
    get_open_ticket_by_channel,
    get_ticket_settings,
    sanitize_ticket_prefix,
    save_ticket_settings,
    update_ticket_panel_channel,
)


class TicketCog(commands.GroupCog, name="ticket", description="Ticket commands"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanelView(self.bot))
        self.bot.add_view(TicketCloseView(self.bot))

    @app_commands.command(name="open", description="Open a support ticket")
    @app_commands.describe(subject="Short ticket subject")
    @app_commands.guild_only()
    async def open(self, interaction: discord.Interaction, subject: Optional[str] = None) -> None:
        await create_ticket_for_interaction(self.bot, interaction, subject)

    @app_commands.command(name="close", description="Close the current ticket")
    @app_commands.describe(reason="Why this ticket is being closed")
    @app_commands.guild_only()
    async def close(self, interaction: discord.Interaction, reason: Optional[str] = None) -> None:
        await close_ticket_for_interaction(self.bot, interaction, reason)

    @app_commands.command(name="info", description="Show info about the current ticket")
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction) -> None:
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


class TicketConfigCog(commands.GroupCog, name="ticketconfig", description="Ticket configuration commands"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="setup", description="Configure ticket system for this server")
    @app_commands.describe(
        category="Category where ticket channels are created",
        support_role="Role that can access all tickets",
        max_open_tickets="How many tickets one user can have open at once",
        ticket_prefix="Channel name prefix, example: ticket",
        log_channel="Optional channel for ticket open/close logs",
    )
    @app_commands.guild_only()
    async def setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        support_role: discord.Role,
        max_open_tickets: app_commands.Range[int, 1, 5] = 1,
        ticket_prefix: str = DEFAULT_TICKET_PREFIX,
        log_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        validated = await validate_permission(
            self.bot,
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

    @app_commands.command(name="panel", description="Send ticket panel with button")
    @app_commands.describe(channel="Channel where the panel message should be sent")
    @app_commands.guild_only()
    async def panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        validated = await validate_permission(
            self.bot,
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
            await target_channel.send(embed=panel_embed, view=TicketPanelView(self.bot))
        except discord.Forbidden:
            await send_message(interaction, "I do not have permission to send messages in that channel.", ephemeral=True)
            return
        except discord.HTTPException:
            await send_message(interaction, "Failed to send ticket panel due to a Discord API error.", ephemeral=True)
            return

        update_ticket_panel_channel(guild.id, target_channel.id)
        await send_message(interaction, f"Ticket panel sent to {target_channel.mention}.", ephemeral=True)

    @app_commands.command(name="show", description="Show current ticket configuration")
    @app_commands.guild_only()
    async def show(self, interaction: discord.Interaction) -> None:
        validated = await validate_permission(
            self.bot,
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketCog(bot))
    await bot.add_cog(TicketConfigCog(bot))

