from __future__ import annotations

import logging
import pathlib

import discord
from discord.ext import commands

from bot.config import settings
from bot.database import Database
from bot.services.deadlock_api import DeadlockApiClient
from bot.services.deadlock_callback_server import DeadlockCallbackServer
from bot.services.queue_service import QueueService
from bot.views.live_match_post import LiveMatchPostView

log = logging.getLogger(__name__)


class BebopBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(command_prefix=settings.command_prefix, intents=intents)

        self._guild = discord.Object(id=settings.guild_id)
        self.database = Database(settings.mongo_uri, settings.mongo_db_name)
        self.queue_repository = QueueService()
        self.deadlock_api = DeadlockApiClient(
            base_url=settings.deadlock_api_base_url,
            api_key=settings.deadlock_api_key,
            timeout_seconds=settings.deadlock_api_timeout_seconds,
        )
        self.deadlock_callbacks = DeadlockCallbackServer(
            self,
            enabled=settings.deadlock_callback_enabled,
            public_base_url=settings.deadlock_callback_public_base_url,
            bind_host=settings.deadlock_callback_bind_host,
            bind_port=settings.deadlock_callback_bind_port,
            path_prefix=settings.deadlock_callback_path_prefix,
        )

    async def setup_hook(self) -> None:
        await self.database.connect()
        await self.deadlock_api.start()
        await self.deadlock_callbacks.start()
        self.add_view(LiveMatchPostView(self))
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
        await self.deadlock_callbacks.close()
        await self.deadlock_api.close()
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        if not self.user:
            return

        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %d guild(s)", len(self.guilds))
