from __future__ import annotations

from typing import TYPE_CHECKING

import discord

LIVE_MATCH_REFRESH_BUTTON_ID = "bebop_live_match_refresh"
LIVE_MATCH_REFRESH_BUTTON_LABEL = "Refresh Match Data"

if TYPE_CHECKING:
    from bot.bot import BebopBot


class LiveMatchPostView(discord.ui.View):
    def __init__(self, bot: BebopBot) -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(
        label=LIVE_MATCH_REFRESH_BUTTON_LABEL,
        style=discord.ButtonStyle.secondary,
        custom_id=LIVE_MATCH_REFRESH_BUTTON_ID,
        emoji="🔄",
    )
    async def refresh_match_data(
        self,
        interaction: discord.Interaction[BebopBot],
        _: discord.ui.Button[LiveMatchPostView],
    ) -> None:
        await self._bot.deadlock_callbacks.handle_live_match_refresh(interaction)
