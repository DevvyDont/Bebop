from __future__ import annotations

import asyncio
import contextlib
import hmac
import io
import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import uuid4

import discord
from aiohttp import web
from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from bot.models.deadlock import (
    DeadlockMatchMetadataResponse,
    DeadlockMatchStartedCallback,
    DeadlockSettingsUpdatedCallback,
)
from bot.models.live_match import LiveMatchPostRecord, LiveMatchPostStatus
from bot.models.match_history import MatchHistoryRecord
from bot.services.deadlock_api import DeadlockApiConfigurationError, DeadlockApiRequestError
from bot.views.live_match_post import LiveMatchPostView

log = logging.getLogger(__name__)

CALLBACK_SECRET_HEADER = "X-Callback-Secret"
MAX_SETTINGS_PAYLOAD_CHARS = 1500
MATCH_HISTORY_COLLECTION_NAME = "match_history"
LIVE_MATCH_POSTS_COLLECTION_NAME = "live_match_posts"
TEAM_HIDDEN_KING_LABEL = "Hidden King"
TEAM_ARCHMOTHER_LABEL = "Archmother"
LIVELOCK_MATCH_URL_TEMPLATE = "https://livelock.gg/matches/{match_id}"
UNASSIGNED_HERO_LABEL = "Unassigned"
LIVE_MATCH_REFRESH_COOLDOWN_SECONDS = 300
LIVE_MATCH_HEARTBEAT_INTERVAL_SECONDS = 60
LIVE_MATCH_HEARTBEAT_JITTER_SECONDS = 15
TEAM_INDEX_HIDDEN_KING = 0
TEAM_INDEX_ARCHMOTHER = 1
MAX_SETTINGS_SUMMARY_KEYS = 6
SETTINGS_ATTACHMENT_FILENAME_TEMPLATE = "match-{match_number}-settings-{timestamp}.json"
MIN_AUTO_LEAVE_PLAYER_THRESHOLD = 1
MIN_AUTO_LEAVE_RETRY_COOLDOWN_SECONDS = 1
MAX_RECURSIVE_SEARCH_DEPTH = 8
PLAYER_COUNT_KEYS: tuple[str, ...] = (
    "player_count",
    "players_count",
    "active_player_count",
    "connected_players",
    "lobby_player_count",
    "roster_size",
    "num_players",
    "numplayers",
)
PLAYER_COLLECTION_KEY_TOKENS: tuple[str, ...] = ("player", "players", "roster", "members", "participants")
REMAKE_COMMAND_GUIDANCE = (
    "If party creation breaks or the game fails to start cleanly, use `/remake` to vote for a remake "
    "during the first 15 minutes."
)


@dataclass(frozen=True, slots=True)
class PendingCallbackContext:
    guild_id: int
    match_number: int
    match_text_channel_id: int
    matches_channel_id: int | None


@dataclass(frozen=True, slots=True)
class ActiveCallbackContext:
    token: str
    guild_id: int
    match_number: int
    match_text_channel_id: int
    matches_channel_id: int | None
    party_id: str
    party_code: str
    callback_secret: str
    team_a_ids: tuple[int, ...]
    team_b_ids: tuple[int, ...]
    assigned_heroes: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class CallbackTrackingResetSummary:
    pending_callback_count: int
    active_callback_count: int
    deleted_live_match_message_count: int
    cleared_live_match_post_count: int
    cleared_match_history_count: int


class MatchIdRemapStatus(StrEnum):
    SUCCESS = "success"
    INVALID_INPUT = "invalid_input"
    DATABASE_UNAVAILABLE = "database_unavailable"
    OLD_MATCH_NOT_FOUND = "old_match_not_found"
    NEW_MATCH_ALREADY_TRACKED = "new_match_already_tracked"
    PERSISTENCE_ERROR = "persistence_error"


@dataclass(frozen=True, slots=True)
class MatchIdRemapSummary:
    status: MatchIdRemapStatus
    old_match_id: int
    new_match_id: int
    updated_live_match_post_count: int = 0
    updated_match_history_count: int = 0
    synced_live_match_post: bool = False


class LiveMatchTrackStatus(StrEnum):
    SUCCESS = "success"
    INVALID_INPUT = "invalid_input"
    DATABASE_UNAVAILABLE = "database_unavailable"
    MATCH_NOT_TRACKED = "match_not_tracked"
    API_VALIDATION_FAILED = "api_validation_failed"
    PERSISTENCE_ERROR = "persistence_error"


@dataclass(frozen=True, slots=True)
class LiveMatchTrackSummary:
    status: LiveMatchTrackStatus
    match_id: int
    resulting_status: LiveMatchPostStatus | None = None
    api_status_code: int | None = None
    api_retry_after_seconds: int | None = None


@dataclass(slots=True)
class PartyAutoLeaveState:
    last_observed_player_count: int | None = None
    leave_requested: bool = False
    leave_succeeded: bool = False
    last_attempt_at: datetime | None = None


class DeadlockCallbackServer:
    def __init__(
        self,
        bot: BebopBot,
        *,
        enabled: bool,
        public_base_url: str | None,
        bind_host: str,
        bind_port: int,
        path_prefix: str,
        auto_leave_enabled: bool,
        auto_leave_min_players: int,
        auto_leave_retry_cooldown_seconds: int,
    ) -> None:
        self._bot = bot
        self._enabled = enabled
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._path_prefix = path_prefix.rstrip("/")
        self._auto_leave_enabled = auto_leave_enabled
        self._auto_leave_min_players = max(auto_leave_min_players, MIN_AUTO_LEAVE_PLAYER_THRESHOLD)
        self._auto_leave_retry_cooldown_seconds = max(
            auto_leave_retry_cooldown_seconds,
            MIN_AUTO_LEAVE_RETRY_COOLDOWN_SECONDS,
        )
        self._pending_by_token: dict[str, PendingCallbackContext] = {}
        self._active_by_token: dict[str, ActiveCallbackContext] = {}
        self._active_token_by_party_id: dict[str, str] = {}
        self._auto_leave_by_party_id: dict[str, PartyAutoLeaveState] = {}
        self._state_lock = asyncio.Lock()
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._live_match_heartbeat_task: asyncio.Task[None] | None = None

        callback_base_path = self._path_prefix
        self._app.router.add_post(f"{callback_base_path}/{{token}}", self._handle_match_started_callback)
        self._app.router.add_post(f"{callback_base_path}/{{token}}/settings", self._handle_settings_callback)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def can_generate_callback_url(self) -> bool:
        return self._enabled and self._public_base_url is not None

    async def start(self) -> None:
        if not self._enabled:
            log.info("Deadlock callback server is disabled.")
            return

        if not self._public_base_url:
            log.warning("Deadlock callback server enabled but DEADLOCK_CALLBACK_PUBLIC_BASE_URL is not configured.")

        if self._runner is not None:
            return

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self._bind_host, port=self._bind_port)
        await self._site.start()
        self._start_live_match_heartbeat()
        log.info("Deadlock callback server listening on %s:%s", self._bind_host, self._bind_port)

    async def close(self) -> None:
        if self._live_match_heartbeat_task is not None:
            self._live_match_heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._live_match_heartbeat_task
            self._live_match_heartbeat_task = None

        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

        async with self._state_lock:
            self._pending_by_token.clear()
            self._active_by_token.clear()
            self._active_token_by_party_id.clear()
            self._auto_leave_by_party_id.clear()

    async def prepare_match_callback(
        self,
        guild_id: int,
        match_number: int,
        match_text_channel_id: int,
        matches_channel_id: int | None,
    ) -> tuple[str, str] | None:
        if not self.can_generate_callback_url():
            return None

        token = uuid4().hex
        callback_url = self._build_callback_url(token)
        pending_context = PendingCallbackContext(
            guild_id=guild_id,
            match_number=match_number,
            match_text_channel_id=match_text_channel_id,
            matches_channel_id=matches_channel_id,
        )

        async with self._state_lock:
            self._pending_by_token[token] = pending_context

        return token, callback_url

    async def activate_match_callback(
        self,
        token: str,
        party_id: str,
        party_code: str,
        callback_secret: str,
        team_a_ids: tuple[int, ...],
        team_b_ids: tuple[int, ...],
        assigned_heroes: tuple[tuple[int, str], ...],
    ) -> None:
        async with self._state_lock:
            pending_context = self._pending_by_token.pop(token, None)
            if pending_context is None:
                log.warning("Deadlock callback token %s was not found in pending map", token)
                return

            active_context = ActiveCallbackContext(
                token=token,
                guild_id=pending_context.guild_id,
                match_number=pending_context.match_number,
                match_text_channel_id=pending_context.match_text_channel_id,
                matches_channel_id=pending_context.matches_channel_id,
                party_id=party_id,
                party_code=party_code,
                callback_secret=callback_secret,
                team_a_ids=team_a_ids,
                team_b_ids=team_b_ids,
                assigned_heroes=assigned_heroes,
            )
            self._active_by_token[token] = active_context
            self._active_token_by_party_id[party_id] = token
            self._auto_leave_by_party_id[party_id] = PartyAutoLeaveState()

    async def discard_pending_callback(self, token: str) -> None:
        async with self._state_lock:
            self._pending_by_token.pop(token, None)

    async def unregister_party_id(self, party_id: str) -> None:
        async with self._state_lock:
            token = self._active_token_by_party_id.pop(party_id, None)
            if token is None:
                self._auto_leave_by_party_id.pop(party_id, None)
                return
            self._active_by_token.pop(token, None)
            self._auto_leave_by_party_id.pop(party_id, None)

    async def retire_party_id(self, party_id: str) -> None:
        await self.unregister_party_id(party_id)
        await self._delete_live_match_artifacts_for_party_id(party_id)

    async def reset_tracking_state(self) -> CallbackTrackingResetSummary:
        live_match_records = await self._get_all_live_match_posts()
        deleted_live_match_message_count = 0
        for live_match_record in live_match_records:
            if await self._delete_live_match_post_message(live_match_record):
                deleted_live_match_message_count += 1

        cleared_live_match_post_count = 0
        collection = self._get_live_match_posts_collection()
        if collection is not None:
            try:
                delete_result = await collection.delete_many({})
                cleared_live_match_post_count = delete_result.deleted_count
            except PyMongoError:
                log.exception("Failed to clear persisted live match post records during reset.")

        cleared_match_history_count = 0
        database = self._bot.database.db
        if database is not None:
            try:
                delete_result = await database[MATCH_HISTORY_COLLECTION_NAME].delete_many({})
                cleared_match_history_count = delete_result.deleted_count
            except PyMongoError:
                log.exception("Failed to clear persisted match history records during reset.")

        async with self._state_lock:
            pending_callback_count = len(self._pending_by_token)
            active_callback_count = len(self._active_by_token)
            self._pending_by_token.clear()
            self._active_by_token.clear()
            self._active_token_by_party_id.clear()
            self._auto_leave_by_party_id.clear()

        return CallbackTrackingResetSummary(
            pending_callback_count=pending_callback_count,
            active_callback_count=active_callback_count,
            deleted_live_match_message_count=deleted_live_match_message_count,
            cleared_live_match_post_count=cleared_live_match_post_count,
            cleared_match_history_count=cleared_match_history_count,
        )

    async def remap_tracked_match_id(self, guild_id: int, old_match_id: int, new_match_id: int) -> MatchIdRemapSummary:
        if old_match_id <= 0 or new_match_id <= 0 or old_match_id == new_match_id:
            return MatchIdRemapSummary(
                status=MatchIdRemapStatus.INVALID_INPUT,
                old_match_id=old_match_id,
                new_match_id=new_match_id,
            )

        database = self._bot.database.db
        if database is None:
            return MatchIdRemapSummary(
                status=MatchIdRemapStatus.DATABASE_UNAVAILABLE,
                old_match_id=old_match_id,
                new_match_id=new_match_id,
            )

        live_match_collection = database[LIVE_MATCH_POSTS_COLLECTION_NAME]
        match_history_collection = database[MATCH_HISTORY_COLLECTION_NAME]

        old_live_match_record = await live_match_collection.find_one(
            {"guild_id": guild_id, "match_id": old_match_id}
        )
        old_match_history_record = await match_history_collection.find_one(
            {"guild_id": guild_id, "match_id": old_match_id}
        )
        if not isinstance(old_live_match_record, dict) and not isinstance(old_match_history_record, dict):
            return MatchIdRemapSummary(
                status=MatchIdRemapStatus.OLD_MATCH_NOT_FOUND,
                old_match_id=old_match_id,
                new_match_id=new_match_id,
            )

        new_live_match_record = await live_match_collection.find_one(
            {"guild_id": guild_id, "match_id": new_match_id}
        )
        new_match_history_record = await match_history_collection.find_one(
            {"guild_id": guild_id, "match_id": new_match_id}
        )
        if isinstance(new_live_match_record, dict) or isinstance(new_match_history_record, dict):
            return MatchIdRemapSummary(
                status=MatchIdRemapStatus.NEW_MATCH_ALREADY_TRACKED,
                old_match_id=old_match_id,
                new_match_id=new_match_id,
            )

        updated_live_match_post_count = 0
        updated_match_history_count = 0
        try:
            live_update_result = await live_match_collection.update_one(
                {"guild_id": guild_id, "match_id": old_match_id},
                {"$set": {"match_id": new_match_id}},
            )
            updated_live_match_post_count = live_update_result.modified_count

            history_update_result = await match_history_collection.update_one(
                {"guild_id": guild_id, "match_id": old_match_id},
                {"$set": {"match_id": new_match_id}},
            )
            updated_match_history_count = history_update_result.modified_count
        except PyMongoError:
            log.exception(
                "Failed to remap tracked match_id from %s to %s in guild %s",
                old_match_id,
                new_match_id,
                guild_id,
            )
            return MatchIdRemapSummary(
                status=MatchIdRemapStatus.PERSISTENCE_ERROR,
                old_match_id=old_match_id,
                new_match_id=new_match_id,
                updated_live_match_post_count=updated_live_match_post_count,
                updated_match_history_count=updated_match_history_count,
            )

        remapped_live_record = await self._get_live_match_post_by_guild_and_match_id(guild_id, new_match_id)
        synced_live_match_post = False
        if remapped_live_record is not None:
            await self._sync_live_match_post_message(remapped_live_record)
            synced_live_match_post = True

        return MatchIdRemapSummary(
            status=MatchIdRemapStatus.SUCCESS,
            old_match_id=old_match_id,
            new_match_id=new_match_id,
            updated_live_match_post_count=updated_live_match_post_count,
            updated_match_history_count=updated_match_history_count,
            synced_live_match_post=synced_live_match_post,
        )

    async def track_existing_live_match(self, guild_id: int, match_id: int) -> LiveMatchTrackSummary:
        if match_id <= 0:
            return LiveMatchTrackSummary(status=LiveMatchTrackStatus.INVALID_INPUT, match_id=match_id)

        database = self._bot.database.db
        if database is None:
            return LiveMatchTrackSummary(status=LiveMatchTrackStatus.DATABASE_UNAVAILABLE, match_id=match_id)

        existing_record = await self._get_live_match_post_by_guild_and_match_id(guild_id, match_id)
        if existing_record is None:
            return LiveMatchTrackSummary(status=LiveMatchTrackStatus.MATCH_NOT_TRACKED, match_id=match_id)

        try:
            metadata = await self._bot.deadlock_api.get_match_metadata(match_id, is_custom=True)
        except DeadlockApiConfigurationError:
            return LiveMatchTrackSummary(status=LiveMatchTrackStatus.API_VALIDATION_FAILED, match_id=match_id)
        except DeadlockApiRequestError as error:
            return LiveMatchTrackSummary(
                status=LiveMatchTrackStatus.API_VALIDATION_FAILED,
                match_id=match_id,
                api_status_code=error.status_code,
                api_retry_after_seconds=error.retry_after_seconds,
            )

        refreshed_at = datetime.now(UTC)
        seeded_record = existing_record.model_copy(
            update={
                "status": LiveMatchPostStatus.IN_PROGRESS,
                "cleanup_completed_at": None,
            }
        )
        updated_record = self._apply_match_metadata_to_record(seeded_record, metadata, refreshed_at)

        try:
            await self._upsert_live_match_post_record(updated_record)
            await self._sync_live_match_post_message(updated_record)
        except (PyMongoError, discord.HTTPException):
            log.exception("Failed to re-arm tracking for guild=%s match_id=%s", guild_id, match_id)
            return LiveMatchTrackSummary(status=LiveMatchTrackStatus.PERSISTENCE_ERROR, match_id=match_id)

        return LiveMatchTrackSummary(
            status=LiveMatchTrackStatus.SUCCESS,
            match_id=match_id,
            resulting_status=updated_record.status,
        )

    def _start_live_match_heartbeat(self) -> None:
        if self._live_match_heartbeat_task is not None and not self._live_match_heartbeat_task.done():
            return
        self._live_match_heartbeat_task = asyncio.create_task(self._run_live_match_heartbeat())

    async def _run_live_match_heartbeat(self) -> None:
        while True:
            try:
                await self._heartbeat_live_matches_once()
            except Exception:
                log.exception("Live match heartbeat iteration failed unexpectedly.")

            heartbeat_delay_seconds = LIVE_MATCH_HEARTBEAT_INTERVAL_SECONDS + random.uniform(
                0,
                LIVE_MATCH_HEARTBEAT_JITTER_SECONDS,
            )
            await asyncio.sleep(heartbeat_delay_seconds)

    async def _heartbeat_live_matches_once(self) -> None:
        in_progress_records = await self._get_in_progress_live_match_posts()
        if not in_progress_records:
            return

        heartbeat_timestamp = datetime.now(UTC)
        for live_match_record in in_progress_records:
            await self._heartbeat_single_live_match(live_match_record, heartbeat_timestamp)

    async def _heartbeat_single_live_match(
        self,
        live_match_record: LiveMatchPostRecord,
        heartbeat_timestamp: datetime,
    ) -> None:
        if not await self._is_live_match_record_still_active(live_match_record):
            return

        try:
            latest_metadata = await self._bot.deadlock_api.get_match_metadata(
                live_match_record.match_id,
                is_custom=True,
            )
        except DeadlockApiRequestError as error:
            log.warning(
                "Heartbeat metadata refresh failed for match_id=%s (status=%s, retry_after=%s)",
                live_match_record.match_id,
                error.status_code,
                error.retry_after_seconds,
            )
            return

        if not await self._is_live_match_record_still_active(live_match_record):
            return

        updated_record = self._apply_match_metadata_to_record(live_match_record, latest_metadata, heartbeat_timestamp)
        await self._upsert_live_match_post_record(updated_record)
        await self._sync_live_match_post_message(updated_record)

        if updated_record.status == LiveMatchPostStatus.FINISHED and updated_record.cleanup_completed_at is None:
            await self._handle_match_completion_cleanup(updated_record)

    async def _get_in_progress_live_match_posts(self) -> tuple[LiveMatchPostRecord, ...]:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return ()

        raw_records = await collection.find({"status": LiveMatchPostStatus.IN_PROGRESS.value}).to_list(length=None)
        return self._parse_live_match_post_records(raw_records)

    async def _get_all_live_match_posts(self) -> tuple[LiveMatchPostRecord, ...]:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return ()

        raw_records = await collection.find({}).to_list(length=None)
        return self._parse_live_match_post_records(raw_records)

    @staticmethod
    def _parse_live_match_post_records(raw_records: list[object]) -> tuple[LiveMatchPostRecord, ...]:
        parsed_records: list[LiveMatchPostRecord] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue

            try:
                parsed_records.append(LiveMatchPostRecord.model_validate(raw_record))
            except ValidationError:
                log.warning("Skipping invalid live match post record during persistence load.")
        return tuple(parsed_records)

    async def _is_live_match_record_still_active(self, record: LiveMatchPostRecord) -> bool:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return False

        raw_record = await collection.find_one(
            {
                "match_id": record.match_id,
                "party_id": record.party_id,
                "status": LiveMatchPostStatus.IN_PROGRESS.value,
            }
        )
        return isinstance(raw_record, dict)

    async def _delete_live_match_artifacts_for_party_id(self, party_id: str) -> None:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return

        raw_records = await collection.find({"party_id": party_id}).to_list(length=None)
        parsed_records = self._parse_live_match_post_records(raw_records)

        if not parsed_records:
            return

        for record in parsed_records:
            await self._delete_live_match_post_message(record)
            await self._delete_live_match_post_record(record.match_id)
            await self._delete_match_history_record(record.match_id)

    async def _delete_live_match_post_message(self, record: LiveMatchPostRecord) -> bool:
        if record.message_id is None:
            return False

        channel = await self._resolve_message_channel(record.matches_channel_id)
        if channel is None:
            return False

        try:
            message = await channel.fetch_message(record.message_id)
            await message.delete()
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden:
            log.warning(
                "Missing permissions to delete stale live match message %s for party %s",
                record.message_id,
                record.party_id,
            )
            return False
        except discord.HTTPException:
            log.warning(
                "Failed to delete stale live match message %s for party %s",
                record.message_id,
                record.party_id,
            )
            return False

    async def _delete_live_match_post_record(self, match_id: int) -> None:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return

        try:
            await collection.delete_one({"match_id": match_id})
        except PyMongoError:
            log.exception("Failed to delete stale live match post for match_id=%s", match_id)

    async def _delete_match_history_record(self, match_id: int) -> None:
        database = self._bot.database.db
        if database is None:
            return

        collection = database[MATCH_HISTORY_COLLECTION_NAME]
        try:
            await collection.delete_one({"match_id": match_id})
        except PyMongoError:
            log.exception("Failed to delete stale match history for match_id=%s", match_id)

    async def _handle_match_completion_cleanup(self, record: LiveMatchPostRecord) -> None:
        queue_cog = self._bot.get_cog("Queue")
        if queue_cog is None or not hasattr(queue_cog, "handle_match_finished"):
            log.warning(
                "Queue cleanup hook is unavailable; cannot auto-clean finished match %s.",
                record.match_number,
            )
            return

        cleanup_succeeded = await queue_cog.handle_match_finished(record.guild_id, record.match_number)
        if not cleanup_succeeded:
            log.warning(
                "Queue cleanup did not confirm completion for guild=%s match=%s.",
                record.guild_id,
                record.match_number,
            )
            return

        updated_record = record.model_copy(update={"cleanup_completed_at": datetime.now(UTC)})
        await self._upsert_live_match_post_record(updated_record)

    def _build_callback_url(self, token: str) -> str:
        if self._public_base_url is None:
            raise RuntimeError("Deadlock callback URL requested without configured public base URL")
        return f"{self._public_base_url}{self._path_prefix}/{token}"

    async def _handle_match_started_callback(self, request: web.Request) -> web.Response:
        context = await self._get_authorized_context(request)
        if context is None:
            return web.Response(status=401, text="Unauthorized")

        raw_body = await request.read()
        callback_payload = self._parse_match_started_payload(raw_body)
        match_started_at = datetime.now(UTC)
        match_id = callback_payload.match_id
        if match_id is None:
            try:
                match_id = await self._bot.deadlock_api.get_custom_match_id(context.party_id)
            except DeadlockApiRequestError as error:
                log.warning(
                    "Failed to fetch match_id for party %s after callback (status=%s, body=%s)",
                    context.party_id,
                    error.status_code,
                    error.response_body,
                )

        await self._persist_match_history(context, match_id, match_started_at)
        match_started_embed = self._build_match_in_progress_embed(context, match_id, match_started_at)
        await self._send_match_channel_message(
            context.match_text_channel_id,
            content=None,
            embed=match_started_embed,
        )

        if match_id is not None and context.matches_channel_id is not None:
            await self._create_or_update_live_match_post(context, match_id, match_started_at)

        return web.Response(status=200, text="ok")

    async def _handle_settings_callback(self, request: web.Request) -> web.Response:
        context = await self._get_authorized_context(request)
        if context is None:
            return web.Response(status=401, text="Unauthorized")

        raw_body = await request.read()
        settings_payload = self._parse_settings_payload(raw_body)
        await self._maybe_auto_leave_custom_lobby(context, settings_payload, raw_body)

        settings_payload_text = raw_body.decode("utf-8", errors="replace")
        compact_summary = self._build_settings_update_summary(settings_payload_text)
        if not compact_summary:
            return web.Response(status=200, text="ok")
        attachment = self._build_settings_payload_attachment(context.match_number, settings_payload_text)
        message = (
            f"⚙️ **Match {context.match_number}** settings were updated. "
            f"See attached file for full details.\n{compact_summary}"
        )
        await self._send_match_channel_message(
            context.match_text_channel_id,
            content=message,
            attachment=attachment,
        )
        return web.Response(status=200, text="ok")

    async def _maybe_auto_leave_custom_lobby(
        self,
        context: ActiveCallbackContext,
        settings_payload: DeadlockSettingsUpdatedCallback,
        raw_payload: bytes,
    ) -> None:
        if not self._auto_leave_enabled:
            return

        observed_player_count = self._resolve_active_player_count(settings_payload, raw_payload)
        if observed_player_count is None:
            return

        now = datetime.now(UTC)
        async with self._state_lock:
            auto_leave_state = self._auto_leave_by_party_id.get(context.party_id)
            if auto_leave_state is None:
                auto_leave_state = PartyAutoLeaveState()
                self._auto_leave_by_party_id[context.party_id] = auto_leave_state

            previous_player_count = auto_leave_state.last_observed_player_count
            auto_leave_state.last_observed_player_count = observed_player_count

            if auto_leave_state.leave_succeeded:
                return

            if auto_leave_state.leave_requested:
                return

            if observed_player_count < self._auto_leave_min_players:
                return

            crossed_threshold = previous_player_count is None or previous_player_count < self._auto_leave_min_players
            if not crossed_threshold:
                return

            if auto_leave_state.last_attempt_at is not None:
                retry_at = auto_leave_state.last_attempt_at + timedelta(
                    seconds=self._auto_leave_retry_cooldown_seconds
                )
                if now < retry_at:
                    return

            auto_leave_state.leave_requested = True
            auto_leave_state.last_attempt_at = now

        try:
            await self._bot.deadlock_api.leave_custom_match(context.party_id)
        except DeadlockApiConfigurationError:
            async with self._state_lock:
                auto_leave_state = self._auto_leave_by_party_id.get(context.party_id)
                if auto_leave_state is not None:
                    auto_leave_state.leave_requested = False
                    auto_leave_state.last_attempt_at = datetime.now(UTC)

            log.warning("Auto-leave is enabled but DEADLOCK_API_KEY is not configured.")
            return
        except DeadlockApiRequestError as error:
            async with self._state_lock:
                auto_leave_state = self._auto_leave_by_party_id.get(context.party_id)
                if auto_leave_state is not None:
                    auto_leave_state.leave_requested = False
                    auto_leave_state.last_attempt_at = datetime.now(UTC)

            log.warning(
                "Auto-leave failed for party_id=%s at player_count=%s (status=%s, body=%s)",
                context.party_id,
                observed_player_count,
                error.status_code,
                error.response_body,
            )
            return

        async with self._state_lock:
            auto_leave_state = self._auto_leave_by_party_id.get(context.party_id)
            if auto_leave_state is not None:
                auto_leave_state.leave_requested = False
                auto_leave_state.leave_succeeded = True

        log.info(
            "Auto-left custom lobby for party_id=%s after settings callback reported %s player(s).",
            context.party_id,
            observed_player_count,
        )

    def _resolve_active_player_count(
        self,
        settings_payload: DeadlockSettingsUpdatedCallback,
        raw_payload: bytes,
    ) -> int | None:
        top_level_count = settings_payload.resolved_top_level_player_count()
        if top_level_count is not None:
            return top_level_count

        payload_text = raw_payload.decode("utf-8", errors="replace")
        if not payload_text.strip():
            return None

        try:
            parsed_payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None

        recursive_count = self._extract_player_count_from_json(parsed_payload, depth=0)
        if recursive_count is None or recursive_count < 0:
            return None
        return recursive_count

    def _extract_player_count_from_json(self, payload_node: object, depth: int) -> int | None:
        if depth > MAX_RECURSIVE_SEARCH_DEPTH:
            return None

        if isinstance(payload_node, int) and payload_node >= 0:
            return None

        candidates: list[int] = []
        if isinstance(payload_node, dict):
            for key, value in payload_node.items():
                normalized_key = key.casefold().replace("-", "_").replace(" ", "_")
                if normalized_key in PLAYER_COUNT_KEYS and isinstance(value, int) and value >= 0:
                    candidates.append(value)

                if isinstance(value, list | tuple):
                    key_looks_like_players = any(token in normalized_key for token in PLAYER_COLLECTION_KEY_TOKENS)
                    if key_looks_like_players:
                        candidates.append(len(value))

                nested_count = self._extract_player_count_from_json(value, depth + 1)
                if nested_count is not None:
                    candidates.append(nested_count)

        elif isinstance(payload_node, list | tuple):
            for list_item in payload_node:
                nested_count = self._extract_player_count_from_json(list_item, depth + 1)
                if nested_count is not None:
                    candidates.append(nested_count)

        if not candidates:
            return None
        return max(candidates)

    @staticmethod
    def _build_settings_update_summary(settings_payload_text: str) -> str:
        return  # temp
        if not settings_payload_text.strip():
            return "Summary: empty payload."

        try:
            parsed_payload = json.loads(settings_payload_text)
        except json.JSONDecodeError:
            clipped_payload = settings_payload_text.strip()
            if len(clipped_payload) > MAX_SETTINGS_PAYLOAD_CHARS:
                clipped_payload = f"{clipped_payload[:MAX_SETTINGS_PAYLOAD_CHARS]}..."
            return f"Summary: non-JSON payload received (`{len(settings_payload_text)} chars`). `{clipped_payload}`"

        if not isinstance(parsed_payload, dict):
            payload_type = type(parsed_payload).__name__
            return f"Summary: payload type `{payload_type}` (`{len(settings_payload_text)} chars`)."

        payload_keys = sorted(parsed_payload)
        if not payload_keys:
            return "Summary: no settings keys were present."

        shown_keys = payload_keys[:MAX_SETTINGS_SUMMARY_KEYS]
        remaining_key_count = max(len(payload_keys) - len(shown_keys), 0)
        key_list = ", ".join(f"`{key}`" for key in shown_keys)
        if remaining_key_count == 0:
            return f"Summary: keys updated: {key_list}."
        return f"Summary: keys updated: {key_list}, and {remaining_key_count} more."

    @staticmethod
    def _build_settings_payload_attachment(match_number: int, settings_payload_text: str) -> discord.File:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        filename = SETTINGS_ATTACHMENT_FILENAME_TEMPLATE.format(match_number=match_number, timestamp=timestamp)

        payload_for_file = settings_payload_text
        if not payload_for_file.strip():
            payload_for_file = "{}"

        try:
            parsed_payload = json.loads(payload_for_file)
            payload_for_file = json.dumps(parsed_payload, indent=2, sort_keys=True)
        except json.JSONDecodeError:
            pass

        file_buffer = io.BytesIO(payload_for_file.encode("utf-8"))
        return discord.File(file_buffer, filename=filename)

    @staticmethod
    def _build_match_in_progress_embed(
        context: ActiveCallbackContext,
        match_id: int | None,
        match_started_at: datetime,
    ) -> discord.Embed:
        team_a_mentions = " ".join(f"<@{user_id}>" for user_id in context.team_a_ids)
        team_b_mentions = " ".join(f"<@{user_id}>" for user_id in context.team_b_ids)

        embed = discord.Embed(
            title=f"🏁 Match {context.match_number} In Progress",
            description="The custom lobby has started. Use the details below for tracking and post-match stats.",
            color=discord.Color.green(),
        )
        embed.add_field(name=TEAM_HIDDEN_KING_LABEL, value=team_a_mentions or "*No players*", inline=False)
        embed.add_field(name=TEAM_ARCHMOTHER_LABEL, value=team_b_mentions or "*No players*", inline=False)
        embed.add_field(name="Party Code", value=f"`{context.party_code}`", inline=False)
        embed.add_field(
            name="Started",
            value=(
                f"{discord.utils.format_dt(match_started_at, style='F')}\n"
                f"{discord.utils.format_dt(match_started_at, style='R')}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Match ID",
            value=f"`{match_id}`" if match_id is not None else "Unavailable (API lookup failed)",
            inline=False,
        )
        embed.add_field(name="Need a remake?", value=REMAKE_COMMAND_GUIDANCE, inline=False)
        return embed

    @staticmethod
    def _format_team_roster(team_user_ids: tuple[int, ...], assigned_heroes: dict[int, str]) -> str:
        if not team_user_ids:
            return "*No players*"

        return "\n".join(
            f"<@{user_id}> - **{assigned_heroes.get(user_id, UNASSIGNED_HERO_LABEL)}**" for user_id in team_user_ids
        )

    @staticmethod
    def _format_duration(duration_seconds: int) -> str:
        hours, remainder = divmod(duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    @staticmethod
    def _resolve_winning_team_label(winning_team: str | int | None) -> str | None:
        if winning_team is None:
            return None

        if isinstance(winning_team, int):
            if winning_team == TEAM_INDEX_HIDDEN_KING:
                return TEAM_HIDDEN_KING_LABEL
            if winning_team == TEAM_INDEX_ARCHMOTHER:
                return TEAM_ARCHMOTHER_LABEL
            return f"Team {winning_team}"

        normalized_team = winning_team.strip().casefold().replace("-", "_").replace(" ", "_")
        hidden_king_aliases = {"hidden_king", "team_a", "team_0", "team0", "blue", "left"}
        archmother_aliases = {"archmother", "team_b", "team_1", "team1", "orange", "right"}
        if normalized_team in hidden_king_aliases:
            return TEAM_HIDDEN_KING_LABEL
        if normalized_team in archmother_aliases:
            return TEAM_ARCHMOTHER_LABEL
        return winning_team.replace("_", " ").title()

    @staticmethod
    def _build_live_match_embed(record: LiveMatchPostRecord) -> discord.Embed:
        assigned_heroes = dict(record.assigned_heroes)
        livelock_url = LIVELOCK_MATCH_URL_TEMPLATE.format(match_id=record.match_id)

        title = f"📡 Match {record.match_number} Live"
        description = "The lobby is underway. Spectators can track the match live using the details below."
        color = discord.Color.green()
        if record.status == LiveMatchPostStatus.FINISHED:
            title = f"🏆 Match {record.match_number} Final"
            description = "The match has finished. Final result and live match details are below."
            color = discord.Color.gold()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )
        embed.add_field(
            name=TEAM_HIDDEN_KING_LABEL,
            value=DeadlockCallbackServer._format_team_roster(record.team_a_ids, assigned_heroes),
            inline=False,
        )
        embed.add_field(
            name=TEAM_ARCHMOTHER_LABEL,
            value=DeadlockCallbackServer._format_team_roster(record.team_b_ids, assigned_heroes),
            inline=False,
        )
        embed.add_field(
            name="Started",
            value=(
                f"{discord.utils.format_dt(record.match_started_at, style='F')}\n"
                f"{discord.utils.format_dt(record.match_started_at, style='R')}"
            ),
            inline=False,
        )
        if record.status == LiveMatchPostStatus.FINISHED and record.winning_team_label is not None:
            embed.add_field(name="Winner", value=record.winning_team_label, inline=False)
        if record.duration_seconds is not None:
            embed.add_field(
                name="Duration",
                value=DeadlockCallbackServer._format_duration(record.duration_seconds),
                inline=False,
            )
        if record.last_refresh_at is not None:
            embed.add_field(
                name="Last Refresh",
                value=discord.utils.format_dt(record.last_refresh_at, style="R"),
                inline=False,
            )
        embed.add_field(name="Match ID", value=f"`{record.match_id}`", inline=False)
        embed.add_field(name="Livelock", value=livelock_url, inline=False)
        return embed

    async def _create_or_update_live_match_post(
        self,
        context: ActiveCallbackContext,
        match_id: int,
        match_started_at: datetime,
    ) -> None:
        existing_record = await self._get_live_match_post_by_match_id(match_id)
        live_match_record = LiveMatchPostRecord(
            guild_id=context.guild_id,
            match_number=context.match_number,
            party_id=context.party_id,
            party_code=context.party_code,
            match_id=match_id,
            match_text_channel_id=context.match_text_channel_id,
            matches_channel_id=context.matches_channel_id or 0,
            message_id=existing_record.message_id if existing_record is not None else None,
            status=existing_record.status if existing_record is not None else LiveMatchPostStatus.IN_PROGRESS,
            match_started_at=existing_record.match_started_at if existing_record is not None else match_started_at,
            match_finished_at=existing_record.match_finished_at if existing_record is not None else None,
            team_a_ids=context.team_a_ids,
            team_b_ids=context.team_b_ids,
            assigned_heroes=context.assigned_heroes,
            winning_team_label=existing_record.winning_team_label if existing_record is not None else None,
            duration_seconds=existing_record.duration_seconds if existing_record is not None else None,
            last_refresh_at=existing_record.last_refresh_at if existing_record is not None else None,
            last_refresh_requested_by_user_id=(
                existing_record.last_refresh_requested_by_user_id if existing_record is not None else None
            ),
            last_heartbeat_at=existing_record.last_heartbeat_at if existing_record is not None else None,
            cleanup_completed_at=existing_record.cleanup_completed_at if existing_record is not None else None,
        )
        await self._upsert_live_match_post_record(live_match_record)
        await self._sync_live_match_post_message(live_match_record)

    async def _upsert_live_match_post_record(self, record: LiveMatchPostRecord) -> None:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return

        try:
            await collection.update_one(
                {"match_id": record.match_id},
                {"$set": record.model_dump(mode="python")},
                upsert=True,
            )
        except PyMongoError:
            log.exception("Failed to persist live match post for match_id=%s", record.match_id)

    def _get_live_match_posts_collection(self):
        database = self._bot.database.db
        if database is None:
            log.warning("Live match post persistence is unavailable because the MongoDB connection is unavailable.")
            return None
        return database[LIVE_MATCH_POSTS_COLLECTION_NAME]

    async def _get_live_match_post_by_match_id(self, match_id: int) -> LiveMatchPostRecord | None:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return None

        raw_record = await collection.find_one({"match_id": match_id})
        if not isinstance(raw_record, dict):
            return None

        try:
            return LiveMatchPostRecord.model_validate(raw_record)
        except ValidationError:
            log.warning("Skipping invalid live match post record for match_id=%s", match_id)
            return None

    async def _get_live_match_post_by_guild_and_match_id(
        self,
        guild_id: int,
        match_id: int,
    ) -> LiveMatchPostRecord | None:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return None

        raw_record = await collection.find_one({"guild_id": guild_id, "match_id": match_id})
        if not isinstance(raw_record, dict):
            return None

        try:
            return LiveMatchPostRecord.model_validate(raw_record)
        except ValidationError:
            log.warning("Skipping invalid live match post record for guild=%s match_id=%s", guild_id, match_id)
            return None

    async def _get_live_match_post_by_message(
        self,
        channel_id: int,
        message_id: int,
    ) -> LiveMatchPostRecord | None:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return None

        raw_record = await collection.find_one({"matches_channel_id": channel_id, "message_id": message_id})
        if not isinstance(raw_record, dict):
            return None

        try:
            return LiveMatchPostRecord.model_validate(raw_record)
        except ValidationError:
            log.warning(
                "Skipping invalid live match post record for channel_id=%s message_id=%s",
                channel_id,
                message_id,
            )
            return None

    async def _sync_live_match_post_message(self, record: LiveMatchPostRecord) -> None:
        channel = await self._resolve_message_channel(record.matches_channel_id)
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            log.warning(
                "Could not resolve matches channel %s for live match %s",
                record.matches_channel_id,
                record.match_id,
            )
            return

        embed = self._build_live_match_embed(record)
        view = LiveMatchPostView(self._bot)

        if record.message_id is not None:
            try:
                existing_message = await channel.fetch_message(record.message_id)
                await existing_message.edit(embed=embed, view=view)
                return
            except discord.HTTPException:
                log.warning(
                    "Could not edit live match post %s for match_id=%s; sending a replacement message.",
                    record.message_id,
                    record.match_id,
                )

        created_message = await channel.send(embed=embed, view=view)
        updated_record = record.model_copy(update={"message_id": created_message.id})
        await self._upsert_live_match_post_record(updated_record)

    async def handle_live_match_refresh(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.message is None or interaction.channel_id is None:
            await interaction.response.send_message("❌ This match post could not be refreshed.", ephemeral=True)
            return

        live_match_record = await self._get_live_match_post_by_message(interaction.channel_id, interaction.message.id)
        if live_match_record is None:
            await interaction.response.send_message("❌ This live match post is no longer tracked.", ephemeral=True)
            return

        refresh_started_at = datetime.now(UTC)
        reserved_record, remaining_cooldown = await self._reserve_refresh_cooldown(
            live_match_record,
            interaction.user.id,
            refresh_started_at,
        )
        if reserved_record is None:
            if remaining_cooldown <= timedelta(0):
                await interaction.response.send_message("❌ This live match post is no longer tracked.", ephemeral=True)
                return

            remaining_seconds = max(int(remaining_cooldown.total_seconds()), 1)
            retry_timestamp = refresh_started_at + remaining_cooldown
            await interaction.response.send_message(
                (
                    "⏳ Match refresh is on cooldown. "
                    f"Try again {discord.utils.format_dt(retry_timestamp, style='R')} "
                    f"({remaining_seconds}s remaining)."
                ),
                ephemeral=True,
            )
            return

        try:
            latest_metadata = await self._bot.deadlock_api.get_match_metadata(
                reserved_record.match_id,
                is_custom=True,
            )
        except DeadlockApiRequestError as error:
            await self._restore_refresh_cooldown(
                live_match_record.match_id,
                live_match_record.last_refresh_at,
                live_match_record.last_refresh_requested_by_user_id,
            )
            message = "❌ Match refresh failed."
            if error.status_code is not None:
                message = f"❌ Match refresh failed (status {error.status_code})."
            if error.status_code == 429 and error.retry_after_seconds is not None:
                message = (
                    f"❌ Match refresh is currently rate limited. Try again in about {error.retry_after_seconds}s."
                )
            await interaction.response.send_message(message, ephemeral=True)
            return

        updated_record = self._apply_match_metadata_to_record(reserved_record, latest_metadata, refresh_started_at)
        await self._upsert_live_match_post_record(updated_record)
        await self._sync_live_match_post_message(updated_record)

        if updated_record.status == LiveMatchPostStatus.FINISHED and updated_record.cleanup_completed_at is None:
            await self._handle_match_completion_cleanup(updated_record)
            persisted_record = await self._get_live_match_post_by_match_id(updated_record.match_id)
            if persisted_record is not None:
                updated_record = persisted_record

        refresh_message = "🔄 Match data refreshed."
        if updated_record.status == LiveMatchPostStatus.FINISHED and updated_record.winning_team_label is not None:
            refresh_message = f"🏆 Match finished — {updated_record.winning_team_label} won."

        await interaction.response.send_message(refresh_message, ephemeral=True)

    async def _reserve_refresh_cooldown(
        self,
        record: LiveMatchPostRecord,
        requested_by_user_id: int,
        requested_at: datetime,
    ) -> tuple[LiveMatchPostRecord | None, timedelta]:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return None, timedelta(0)

        cooldown_cutoff = requested_at - timedelta(seconds=LIVE_MATCH_REFRESH_COOLDOWN_SECONDS)
        raw_record = await collection.find_one_and_update(
            {
                "match_id": record.match_id,
                "$or": [
                    {"last_refresh_at": None},
                    {"last_refresh_at": {"$exists": False}},
                    {"last_refresh_at": {"$lte": cooldown_cutoff}},
                ],
            },
            {
                "$set": {
                    "last_refresh_at": requested_at,
                    "last_refresh_requested_by_user_id": requested_by_user_id,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if isinstance(raw_record, dict):
            return LiveMatchPostRecord.model_validate(raw_record), timedelta(0)

        current_record = await self._get_live_match_post_by_match_id(record.match_id)
        if current_record is None:
            return None, timedelta(0)

        if current_record.last_refresh_at is None:
            return None, timedelta(seconds=LIVE_MATCH_REFRESH_COOLDOWN_SECONDS)

        last_refresh_at = self._as_utc_aware_datetime(current_record.last_refresh_at)
        requested_at_utc = self._as_utc_aware_datetime(requested_at)
        retry_at = last_refresh_at + timedelta(seconds=LIVE_MATCH_REFRESH_COOLDOWN_SECONDS)
        return None, max(retry_at - requested_at_utc, timedelta(0))

    @staticmethod
    def _as_utc_aware_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _restore_refresh_cooldown(
        self,
        match_id: int,
        last_refresh_at: datetime | None,
        last_refresh_requested_by_user_id: int | None,
    ) -> None:
        collection = self._get_live_match_posts_collection()
        if collection is None:
            return

        await collection.update_one(
            {"match_id": match_id},
            {
                "$set": {
                    "last_refresh_at": last_refresh_at,
                    "last_refresh_requested_by_user_id": last_refresh_requested_by_user_id,
                }
            },
        )

    @staticmethod
    def _apply_match_metadata_to_record(
        record: LiveMatchPostRecord,
        metadata: DeadlockMatchMetadataResponse,
        refreshed_at: datetime,
    ) -> LiveMatchPostRecord:
        winning_team_label = DeadlockCallbackServer._resolve_winning_team_label(metadata.resolved_winning_team())
        match_status = record.status
        match_finished_at = record.match_finished_at
        if winning_team_label is not None:
            match_status = LiveMatchPostStatus.FINISHED
            if match_finished_at is None:
                match_finished_at = refreshed_at

        duration_seconds = metadata.resolved_duration_seconds()
        if duration_seconds is None:
            duration_seconds = record.duration_seconds

        return record.model_copy(
            update={
                "status": match_status,
                "match_finished_at": match_finished_at,
                "winning_team_label": winning_team_label or record.winning_team_label,
                "duration_seconds": duration_seconds,
                "last_refresh_at": refreshed_at,
                "last_heartbeat_at": refreshed_at,
            }
        )

    async def _persist_match_history(
        self,
        context: ActiveCallbackContext,
        match_id: int | None,
        match_started_at: datetime,
    ) -> None:
        if match_id is None:
            log.warning(
                "Skipping match history persistence for party %s because match_id could not be resolved.",
                context.party_id,
            )
            return

        database = self._bot.database.db
        if database is None:
            log.warning("Skipping match history persistence because the MongoDB connection is unavailable.")
            return

        history_record = MatchHistoryRecord(
            guild_id=context.guild_id,
            match_id=match_id,
            match_started_at=match_started_at,
            hidden_king_player_ids=context.team_a_ids,
            archmother_player_ids=context.team_b_ids,
        )

        collection = database[MATCH_HISTORY_COLLECTION_NAME]
        try:
            await collection.update_one(
                {"match_id": history_record.match_id},
                {"$set": history_record.model_dump(mode="python")},
                upsert=True,
            )
        except PyMongoError:
            log.exception("Failed to persist match history for match_id=%s", history_record.match_id)

    async def _get_authorized_context(self, request: web.Request) -> ActiveCallbackContext | None:
        token = request.match_info["token"]
        async with self._state_lock:
            context = self._active_by_token.get(token)

        if context is None:
            log.warning("Received Deadlock callback for unknown token %s", token)
            return None

        incoming_secret = request.headers.get(CALLBACK_SECRET_HEADER)
        if incoming_secret is None:
            log.warning("Received Deadlock callback without %s header", CALLBACK_SECRET_HEADER)
            return None

        if not hmac.compare_digest(incoming_secret, context.callback_secret):
            log.warning("Deadlock callback secret mismatch for token %s", token)
            return None

        return context

    @staticmethod
    def _parse_match_started_payload(raw_body: bytes) -> DeadlockMatchStartedCallback:
        if not raw_body.strip():
            return DeadlockMatchStartedCallback()

        try:
            return DeadlockMatchStartedCallback.model_validate_json(raw_body)
        except ValueError:
            payload_dict = json.loads(raw_body.decode("utf-8"))
            return DeadlockMatchStartedCallback.model_validate(payload_dict)

    @staticmethod
    def _parse_settings_payload(raw_body: bytes) -> DeadlockSettingsUpdatedCallback:
        if not raw_body.strip():
            return DeadlockSettingsUpdatedCallback()

        try:
            return DeadlockSettingsUpdatedCallback.model_validate_json(raw_body)
        except ValueError:
            payload_dict = json.loads(raw_body.decode("utf-8"))
            return DeadlockSettingsUpdatedCallback.model_validate(payload_dict)

    async def _resolve_message_channel(self, channel_id: int) -> discord.TextChannel | discord.Thread | None:
        channel = self._bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel | discord.Thread):
            return channel

        try:
            fetched_channel = await self._bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("Could not resolve match channel %s for callback notification", channel_id)
            return None

        if isinstance(fetched_channel, discord.TextChannel | discord.Thread):
            return fetched_channel
        return None

    async def _send_match_channel_message(
        self,
        channel_id: int,
        *,
        content: str | None,
        embed: discord.Embed | None = None,
        attachment: discord.File | None = None,
    ) -> None:
        if content is None and embed is None and attachment is None:
            return

        channel = await self._resolve_message_channel(channel_id)
        if channel is None:
            return

        await channel.send(content=content, embed=embed, file=attachment)


if TYPE_CHECKING:
    from bot.bot import BebopBot
