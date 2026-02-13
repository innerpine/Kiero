from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from kiero_bot.common import send_message


class GeneralCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="hello", description="Simple hello command")
    async def hello(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"Hello, {interaction.user.mention}!")

    @app_commands.command(name="help", description="Show available bot commands")
    async def help_command(self, interaction: discord.Interaction) -> None:
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

    @app_commands.command(name="ping", description="Show bot latency")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await send_message(interaction, f"Pong: `{latency_ms} ms`", ephemeral=True)

    @app_commands.command(name="avatar", description="Show user avatar")
    @app_commands.describe(user="User to show avatar for")
    async def avatar(self, interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
        target = user or interaction.user
        embed = discord.Embed(
            title=f"Avatar: {target}",
            color=discord.Color.blurple(),
        )
        embed.set_image(url=target.display_avatar.url)
        await send_message(interaction, embed=embed)

    @app_commands.command(name="userinfo", description="Show information about a server member")
    @app_commands.describe(user="Member to inspect")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
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

    @app_commands.command(name="serverinfo", description="Show information about the current server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GeneralCog(bot))

