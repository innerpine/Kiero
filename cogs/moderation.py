from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from kiero_bot import moderation as moderation_tasks
from kiero_bot.common import current_timestamp, format_duration, parse_duration, send_message
from kiero_bot.config import ACTION_BAN, ACTION_MUTE
from kiero_bot.permissions import validate_moderation, validate_permission


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await moderation_tasks.restore_temporary_actions(self.bot)

    @app_commands.command(name="ban", description="Temporarily ban a member")
    @app_commands.describe(
        user="Member to ban",
        duration="Duration in d:h:m:s format",
        reason="Why this member is being banned",
    )
    @app_commands.guild_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str,
    ) -> None:
        validated = await validate_moderation(self.bot, interaction, user, permission_name="ban_members")
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
        moderation_tasks.save_temporary_action(
            action_type=ACTION_BAN,
            guild_id=guild.id,
            user_id=user.id,
            expires_at=expires_at,
            reason=unban_reason,
        )
        moderation_tasks.schedule_temporary_action(
            bot=self.bot,
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

    @app_commands.command(name="mute", description="Temporarily mute a member")
    @app_commands.describe(
        user="Member to mute",
        duration="Duration in d:h:m:s format (max 28 days)",
        reason="Why this member is being muted",
    )
    @app_commands.guild_only()
    async def mute(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str,
    ) -> None:
        validated = await validate_moderation(self.bot, interaction, user, permission_name="moderate_members")
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
        moderation_tasks.save_temporary_action(
            action_type=ACTION_MUTE,
            guild_id=user.guild.id,
            user_id=user.id,
            expires_at=expires_at,
            reason=unmute_reason,
        )
        moderation_tasks.schedule_temporary_action(
            bot=self.bot,
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

    @app_commands.command(name="unban", description="Unban a member by user ID")
    @app_commands.describe(
        user_id="User ID to unban",
        reason="Why this user is being unbanned",
    )
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str) -> None:
        validated = await validate_permission(self.bot, interaction, permission_name="ban_members")
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

        moderation_tasks.cancel_temporary_action(
            action_type=ACTION_BAN,
            guild_id=guild.id,
            user_id=target_user_id,
            delete_from_db=True,
        )
        await send_message(interaction, f"User `{target_user_id}` was unbanned. Reason: {reason}")

    @app_commands.command(name="unmute", description="Remove timeout from a member")
    @app_commands.describe(
        user="Member to unmute",
        reason="Why this member is being unmuted",
    )
    @app_commands.guild_only()
    async def unmute(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        validated = await validate_moderation(self.bot, interaction, user, permission_name="moderate_members")
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

        moderation_tasks.cancel_temporary_action(
            action_type=ACTION_MUTE,
            guild_id=user.guild.id,
            user_id=user.id,
            delete_from_db=True,
        )
        await send_message(interaction, f"{user.mention} was unmuted. Reason: {reason}")

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(
        user="Member to kick",
        reason="Why this member is being kicked",
    )
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        validated = await validate_moderation(self.bot, interaction, user, permission_name="kick_members")
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

    @app_commands.command(name="purge", description="Delete recent messages in the current channel")
    @app_commands.describe(
        amount="How many recent messages to scan (1-200)",
        user="Optional user filter",
    )
    @app_commands.guild_only()
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 200],
        user: Optional[discord.Member] = None,
    ) -> None:
        validated = await validate_permission(self.bot, interaction, permission_name="manage_messages")
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))

