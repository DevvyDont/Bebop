from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

log = logging.getLogger(__name__)


class Database:
    def __init__(self, uri: str, db_name: str) -> None:
        self._uri = uri
        self._db_name = db_name
        self.client: AsyncIOMotorClient | None = None
        self.db: AsyncIOMotorDatabase | None = None

    async def connect(self) -> None:
        client = AsyncIOMotorClient(self._uri)

        try:
            await client.admin.command("ping")
        except Exception:
            log.exception("Failed to connect to MongoDB")
            client.close()
            return

        self.client = client
        self.db = client[self._db_name]
        log.info("Connected to MongoDB (%s)", self._db_name)

    async def close(self) -> None:
        if self.client:
            self.client.close()
            log.info("Closed MongoDB connection")
