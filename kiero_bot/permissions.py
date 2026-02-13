from typing import Optional, Tuple

import discord
from discord.ext import commands

from kiero_bot.common import send_message


def get_bot_member(bot: commands.Bot, guild: discord.Guild) -> Optional[discord.Member]:
    if bot.user is None:
        return None
    return guild.get_member(bot.user.id)


async def validate_permission(
    bot: commands.Bot,
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

    bot_member = get_bot_member(bot, guild)
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


async def validate_moderation(
    bot: commands.Bot,
    interaction: discord.Interaction,
    target: discord.Member,
    *,
    permission_name: str,
) -> Optional[Tuple[discord.Guild, discord.Member, discord.Member]]:
    validated = await validate_permission(bot, interaction, permission_name=permission_name)
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

