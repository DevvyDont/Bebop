from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from datetime import datetime


class LiveMatchPostStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"


class LiveMatchPostRecord(BaseModel):
    guild_id: int
    match_number: int
    party_id: str
    party_code: str
    match_id: int
    match_text_channel_id: int
    matches_channel_id: int
    message_id: int | None = None
    status: LiveMatchPostStatus = LiveMatchPostStatus.IN_PROGRESS
    match_started_at: datetime
    match_finished_at: datetime | None = None
    team_a_ids: tuple[int, ...]
    team_b_ids: tuple[int, ...]
    assigned_heroes: tuple[tuple[int, str], ...]
    winning_team_label: str | None = None
    duration_seconds: int | None = None
    last_refresh_at: datetime | None = None
    last_refresh_requested_by_user_id: int | None = None
    last_heartbeat_at: datetime | None = None
    cleanup_completed_at: datetime | None = None
