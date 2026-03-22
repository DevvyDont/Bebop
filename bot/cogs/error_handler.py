from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from discord.ext import commands

if TYPE_CHECKING:
    import discord
    from discord import app_commands

    from bot.bot import BebopBot

log = logging.getLogger(__name__)

_USER_ERROR_MESSAGE = "An error occurred: {}"


class ErrorHandler(commands.Cog):
    def __init__(self, bot: BebopBot) -> None:
        self.bot = bot
        self._original_tree_error = bot.tree.on_error
        bot.tree.on_error = self.on_app_command_error

    async def cog_unload(self) -> None:
        self.bot.tree.on_error = self._original_tree_error

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context[BebopBot], error: commands.CommandError) -> None:
        if hasattr(ctx.command, "on_error"):
            return

        if ctx.cog and ctx.cog.has_error_handler():
            return

        if isinstance(error, commands.CommandNotFound):
            return

        log.exception("Unhandled command error", exc_info=error)
        await ctx.send(_USER_ERROR_MESSAGE.format(error))

    async def on_app_command_error(
        self, interaction: discord.Interaction[BebopBot], error: app_commands.AppCommandError
    ) -> None:
        log.exception("Unhandled app command error", exc_info=error)

        message = _USER_ERROR_MESSAGE.format(error)

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: BebopBot) -> None:
    await bot.add_cog(ErrorHandler(bot))
