from __future__ import annotations

import logging
import pathlib

import discord
from discord.ext import commands

from bot.config import settings
from bot.database import Database

log = logging.getLogger(__name__)


class BebopBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix=settings.command_prefix, intents=intents)

        self._guild = discord.Object(id=settings.guild_id)
        self.database = Database(settings.mongo_uri, settings.mongo_db_name)

    async def setup_hook(self) -> None:
        await self.database.connect()
        await self._load_cogs()

        self.tree.copy_global_to(guild=self._guild)
        synced = await self.tree.sync(guild=self._guild)
        log.info("Synced %d command(s) to guild %s", len(synced), self._guild.id)

    async def _load_cogs(self) -> None:
        cogs_dir = pathlib.Path(__file__).parent / "cogs"

        for path in sorted(cogs_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue

            extension = f"bot.cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                log.info("Loaded cog: %s", extension)
            except Exception:
                log.exception("Failed to load cog: %s", extension)

    async def close(self) -> None:
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        if not self.user:
            return

        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %d guild(s)", len(self.guilds))
