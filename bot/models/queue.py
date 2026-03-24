from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class QueueState(StrEnum):
    OPEN = "open"
    LOCKED = "locked"
    DRAFTING = "drafting"


@dataclass(frozen=True, slots=True)
class QueueEntry:
    guild_id: int
    user_id: int
    joined_at: datetime
