from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bot.models.queue import QueueEntry, QueueState


@dataclass(slots=True)
class _GuildQueueState:
    state: QueueState = QueueState.OPEN
    entries: dict[int, QueueEntry] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)


class QueueService:
    def __init__(self) -> None:
        self._guild_states: dict[int, _GuildQueueState] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def join(self, guild_id: int, user_id: int) -> bool:
        async with self._get_guild_lock(guild_id):
            guild_state = self._ensure_guild_state(guild_id)
            existing = guild_state.entries.get(user_id)
            if existing is not None:
                return False

            entry = QueueEntry(
                guild_id=guild_id,
                user_id=user_id,
                joined_at=datetime.now(UTC),
            )
            guild_state.entries[user_id] = entry
            guild_state.touch()
            return True

    async def leave(self, guild_id: int, user_id: int) -> bool:
        async with self._get_guild_lock(guild_id):
            guild_state = self._ensure_guild_state(guild_id)
            removed_entry = guild_state.entries.pop(user_id, None)
            if removed_entry is None:
                return False
            guild_state.touch()
            return True

    async def pop_next_match(self, guild_id: int, match_size: int) -> tuple[QueueEntry, ...]:
        async with self._get_guild_lock(guild_id):
            guild_state = self._ensure_guild_state(guild_id)
            ordered_entries = sorted(guild_state.entries.values(), key=lambda entry: entry.joined_at)
            if len(ordered_entries) < match_size:
                return ()

            selected_entries = tuple(ordered_entries[:match_size])
            for entry in selected_entries:
                guild_state.entries.pop(entry.user_id, None)

            guild_state.touch()
            return selected_entries

    async def clear(self, guild_id: int) -> None:
        async with self._get_guild_lock(guild_id):
            guild_state = self._ensure_guild_state(guild_id)
            guild_state.entries.clear()
            guild_state.state = QueueState.OPEN
            guild_state.touch()

    async def set_state(self, guild_id: int, state: QueueState) -> QueueState:
        async with self._get_guild_lock(guild_id):
            guild_state = self._ensure_guild_state(guild_id)
            guild_state.state = state
            guild_state.touch()
            return state

    async def get_queue_state(self, guild_id: int) -> tuple[QueueState, tuple[QueueEntry, ...], datetime]:
        async with self._get_guild_lock(guild_id):
            guild_state = self._ensure_guild_state(guild_id)
            entries = tuple(guild_state.entries.values())
            return guild_state.state, entries, guild_state.updated_at

    def _ensure_guild_state(self, guild_id: int) -> _GuildQueueState:
        existing_state = self._guild_states.get(guild_id)
        if existing_state is not None:
            return existing_state

        guild_state = _GuildQueueState()
        self._guild_states[guild_id] = guild_state
        return guild_state

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is not None:
            return lock

        lock = asyncio.Lock()
        self._guild_locks[guild_id] = lock
        return lock
