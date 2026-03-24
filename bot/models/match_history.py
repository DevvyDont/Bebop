from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from datetime import datetime


class MatchHistoryRecord(BaseModel):
    guild_id: int
    match_id: int
    match_started_at: datetime | None = None
    hidden_king_player_ids: tuple[int, ...]
    archmother_player_ids: tuple[int, ...]

