from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class QueueState(StrEnum):
    OPEN = "open"
    LOCKED = "locked"
    DRAFTING = "drafting"


@dataclass(frozen=True, slots=True)
class QueueEntry:
    guild_id: int
    user_id: int
    joined_at: datetime
    is_ready: bool = False


@dataclass(frozen=True, slots=True)
class QueueJoinResult:
    entry: QueueEntry
    joined: bool


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    guild_id: int
    state: QueueState
    entries: tuple[QueueEntry, ...]
    updated_at: datetime

    @property
    def total_players(self) -> int:
        return len(self.entries)

    @property
    def ready_players(self) -> int:
        return sum(1 for entry in self.entries if entry.is_ready)

