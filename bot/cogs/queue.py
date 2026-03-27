from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from pydantic import ValidationError

from bot.config import settings
from bot.models.deadlock import DeadlockCustomMatchCreateRequest
from bot.models.match_history import MatchHistoryRecord
from bot.models.queue import QueueState
from bot.services.deadlock_api import DeadlockApiConfigurationError, DeadlockApiRequestError
from bot.services.deadlock_callback_server import LiveMatchTrackStatus, MatchIdRemapStatus
from bot.services.hero_roster import list_playable_heroes, resolve_hero_alias

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from bot.bot import BebopBot
    from bot.models.queue import QueueEntry

log = logging.getLogger(__name__)

PUGS_CATEGORY_NAME = "PUGS"
QUEUE_CHANNEL_NAME = "queue"
COMMANDS_CHANNEL_NAME = "commands"
WAITING_ROOM_CHANNEL_NAME = "waiting-room"
MATCHES_CHANNEL_NAME = "matches"
MATCH_TEXT_CHANNEL_PREFIX = "match-"
MATCH_TEAM_A_VOICE_TEMPLATE = "match-{match_number}-hidden-king"
MATCH_TEAM_B_VOICE_TEMPLATE = "match-{match_number}-archmother"

QUEUE_MESSAGE_CONTENT = (
    "The queue is live — use the buttons below (or `/queue` commands) to claim your spot.\n"
    "When the queue fills, a new match thread will open automatically."
)

BUTTON_ID_JOIN = "bebop_queue_join"
BUTTON_ID_LEAVE = "bebop_queue_leave"

CUSTOM_LOBBY_MANUAL_MESSAGE = (
    "Lobby details were not created automatically. A match admin will post the lobby info shortly."
)
CUSTOM_LOBBY_CREATED_MESSAGE = "Party is live — join up and get ready."
LIVE_MATCH_POSTS_COLLECTION_NAME = "live_match_posts"
REMAKE_COMMAND_GUIDANCE = (
    "If party creation breaks or the game fails to start cleanly, use `/remake` to vote for a remake "
    "during the first 15 minutes."
)
REMAKE_VOTE_RECORDED_MESSAGE = "Your remake vote is recorded."
REMAKE_READY_MESSAGE = "Remake approved. Recreating the custom lobby now."
MATCH_HISTORY_COLLECTION_NAME = "match_history"
DEFAULT_MATCH_HISTORY_LIMIT = 10
MAX_MATCH_HISTORY_LIMIT = 20
MIN_MATCH_ID = 1
OPENING_TURN_PICK_COUNT = 1
STANDARD_TURN_PICK_COUNT = 2
MIN_HERO_PICK_COUNT = 3
MAX_HERO_CHOICES_DISPLAY = 6
HERO_ROUND_TWO_REMINDER_SECONDS = 90
REMAKE_WINDOW_SECONDS = 900  # 15 minutes — players may vote to remake within this window after party creation
MAX_REMAKE_COUNT = 2  # Number of successful remakes allowed before the next vote cancels the match entirely


class QueueAction(StrEnum):
    JOIN = "join"
    LEAVE = "leave"


class TeamAssignmentMode(StrEnum):
    RANDOM_TEAMS = "random_teams"
    CAPTAIN_DRAFT = "captain_draft"


class CaptainSelectionMode(StrEnum):
    RANDOM = "random"
    QUEUE_ORDER = "queue_order"


class DraftTeam(StrEnum):
    HIDDEN_KING = "hidden_king"
    ARCHMOTHER = "archmother"

    @property
    def label(self) -> str:
        if self == DraftTeam.HIDDEN_KING:
            return "Hidden King"
        return "Archmother"


@dataclass(frozen=True, slots=True)
class TeamAssignmentResult:
    team_a_ids: tuple[int, ...]
    team_b_ids: tuple[int, ...]
    mode: TeamAssignmentMode
    captain_selection_mode: CaptainSelectionMode | None = None
    captain_a_id: int | None = None
    captain_b_id: int | None = None


@dataclass(frozen=True, slots=True)
class DraftPickRecord:
    pick_number: int
    captain_id: int
    drafted_player_id: int
    drafted_team: DraftTeam


@dataclass(slots=True)
class CaptainDraftSession:
    guild_id: int
    match_number: int
    text_channel_id: int
    captain_a_id: int
    captain_b_id: int
    available_player_ids: list[int]
    team_a_ids: list[int]
    team_b_ids: list[int]
    turn_team: DraftTeam = DraftTeam.HIDDEN_KING
    turn_pick_target: int = OPENING_TURN_PICK_COUNT
    turn_picks_made: int = 0
    opening_turn_completed: bool = False
    pick_records: list[DraftPickRecord] = field(default_factory=list)
    draft_message_id: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class HeroSelectionSession:
    guild_id: int
    match_number: int
    text_channel_id: int
    team_a_ids: tuple[int, ...]
    team_b_ids: tuple[int, ...]
    pick_order: tuple[int, ...]
    picks_by_user: dict[int, tuple[str, ...]] = field(default_factory=dict)
    assigned_hero_by_user: dict[int, str] = field(default_factory=dict)
    status_message_id: int | None = None
    resolution_started: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class RemakeSession:
    """Tracks in-flight remake votes for a single match party creation attempt."""

    guild_id: int
    match_number: int
    text_channel_id: int
    all_player_ids: frozenset[int]
    votes: set[int] = field(default_factory=set)
    majority_triggered: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class QueueDraftSettings:
    team_assignment_mode: TeamAssignmentMode = TeamAssignmentMode.CAPTAIN_DRAFT
    captain_selection_mode: CaptainSelectionMode = CaptainSelectionMode.RANDOM


@dataclass(frozen=True, slots=True)
class ActiveMatch:
    match_number: int
    team_a_ids: tuple[int, ...]
    team_b_ids: tuple[int, ...]
    text_channel_id: int
    team_a_voice_channel_id: int | None = None
    team_b_voice_channel_id: int | None = None
    deadlock_party_id: str | None = None
    deadlock_party_code: str | None = None
    callback_token: str | None = None
    captain_a_id: int | None = None
    captain_b_id: int | None = None
    drafted_player_order: tuple[int, ...] = ()
    assigned_heroes: tuple[tuple[int, str], ...] = ()
    remake_count: int = 0
    party_created_at: datetime | None = None


@dataclass(slots=True)
class MatchChannels:
    match_number: int
    text_channel: discord.TextChannel | None = None
    team_a_voice_channel: discord.VoiceChannel | None = None
    team_b_voice_channel: discord.VoiceChannel | None = None


# Display constants

COLOR_OPEN = discord.Color.blue()
COLOR_LOCKED = discord.Color.from_rgb(230, 126, 34)
COLOR_DRAFTING = discord.Color.purple()
COLOR_LOBBY_READY = discord.Color.gold()

_STATE_META: dict[QueueState, tuple[str, discord.Color]] = {
    QueueState.OPEN: ("🔓 Open", COLOR_OPEN),
    QueueState.LOCKED: ("🔒 Locked", COLOR_LOCKED),
    QueueState.DRAFTING: ("⚔️ Drafting", COLOR_DRAFTING),
}

# ── Embed builders ────────────────────────────────────────────────────────────


def _format_player_list(entries: tuple[QueueEntry, ...]) -> str:
    if not entries:
        return "*No players in queue. Use* `/queue join` *to get started!*"
    return "\n".join(f"{i}. <@{entry.user_id}>" for i, entry in enumerate(entries, start=1))


def _build_status_embed(state: QueueState, entries: tuple[QueueEntry, ...], updated_at: datetime) -> discord.Embed:
    state_label, color = _STATE_META[state]
    total_players = len(entries)
    if total_players == settings.queue_size:
        color = COLOR_LOBBY_READY

    embed = discord.Embed(title="🎮 Bebop Queue", color=color)
    embed.add_field(
        name=f"Players — {total_players} / {settings.queue_size}",
        value=_format_player_list(entries),
        inline=False,
    )
    embed.add_field(
        name="Queue Status",
        value=f"{state_label}\nUpdated {discord.utils.format_dt(updated_at, style='R')}",
        inline=False,
    )
    embed.timestamp = updated_at
    embed.set_footer(text="Bebop Queue")
    return embed


def _build_match_started_embed(entries: tuple[QueueEntry, ...], match_number: int) -> discord.Embed:
    mentions = " ".join(f"<@{entry.user_id}>" for entry in entries)
    return discord.Embed(
        title=f"🎉 Match {match_number} Started",
        description=(
            f"All **{settings.queue_size}** players are in. Team reveals, hero picks, and lobby info "
            f"will be posted here.\n\n"
            f"{mentions}"
        ),
        color=COLOR_LOBBY_READY,
    )


def _find_category(guild: discord.Guild, name: str) -> discord.CategoryChannel | None:
    target = name.casefold()
    for category in guild.categories:
        if category.name.casefold() == target:
            return category
    return None


def _find_text_channel(category: discord.CategoryChannel, name: str) -> discord.TextChannel | None:
    target = name.casefold()
    for channel in category.text_channels:
        if channel.name.casefold() == target:
            return channel
    return None


def _find_voice_channel(category: discord.CategoryChannel, name: str) -> discord.VoiceChannel | None:
    target = name.casefold()
    for channel in category.voice_channels:
        if channel.name.casefold() == target:
            return channel
    return None


def _extract_match_number_from_text_channel(channel_name: str) -> int | None:
    if not channel_name.startswith(MATCH_TEXT_CHANNEL_PREFIX):
        return None

    match_number_text = channel_name.removeprefix(MATCH_TEXT_CHANNEL_PREFIX)
    if not match_number_text.isdigit():
        return None
    return int(match_number_text)


def _extract_match_number_from_voice_channel(channel_name: str) -> tuple[int, str] | None:
    team_suffix_by_name: dict[str, str] = {
        "-hidden-king": "hidden_king",
        "-archmother": "archmother",
    }
    for candidate_suffix, team_key in team_suffix_by_name.items():
        if not channel_name.endswith(candidate_suffix):
            continue

        match_number_text = channel_name.removesuffix(candidate_suffix).removeprefix(MATCH_TEXT_CHANNEL_PREFIX)
        if not match_number_text.isdigit():
            return None
        return int(match_number_text), team_key

    return None


def _split_teams(entries: tuple[QueueEntry, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    shuffled_user_ids = [entry.user_id for entry in entries]
    random.shuffle(shuffled_user_ids)
    midpoint = len(shuffled_user_ids) // 2
    team_a = tuple(shuffled_user_ids[:midpoint])
    team_b = tuple(shuffled_user_ids[midpoint:])
    return team_a, team_b


def _build_settings_embed(draft_settings: QueueDraftSettings) -> discord.Embed:
    embed = discord.Embed(title="Queue Draft Settings", color=discord.Color.blurple())
    embed.add_field(name="Team Assignment", value=f"`{draft_settings.team_assignment_mode.value}`", inline=False)
    embed.add_field(name="Captain Selection", value=f"`{draft_settings.captain_selection_mode.value}`", inline=False)
    return embed


# ── Permission helpers ────────────────────────────────────────────────────────


def _is_admin(member: discord.Member) -> bool:
    """Return True if the member holds the configured admin role or Manage Server permission."""
    if settings.admin_role_name is not None:
        return any(role.name.lower() == settings.admin_role_name.lower() for role in member.roles)
    return member.guild_permissions.manage_guild


async def _admin_check(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        raise app_commands.CheckFailure("This command can only be used inside a server.")
    if not _is_admin(interaction.user):
        raise app_commands.CheckFailure(
            "❌ You need the **Admin** role or **Manage Server** permission to use this command."
        )
    return True


class QueueMessageView(discord.ui.View):
    def __init__(self, cog: QueueCog) -> None:
        super().__init__(timeout=None)
        self._cog = cog

    @discord.ui.button(label="Join Queue", style=discord.ButtonStyle.success, custom_id=BUTTON_ID_JOIN)
    async def join_button(
        self,
        interaction: discord.Interaction[BebopBot],
        _: discord.ui.Button[QueueMessageView],
    ) -> None:
        await self._cog._handle_button_action(interaction, QueueAction.JOIN)

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.danger, custom_id=BUTTON_ID_LEAVE)
    async def leave_button(
        self,
        interaction: discord.Interaction[BebopBot],
        _: discord.ui.Button[QueueMessageView],
    ) -> None:
        await self._cog._handle_button_action(interaction, QueueAction.LEAVE)


class DraftPlayerSelect(discord.ui.Select["CaptainDraftView"]):
    def __init__(self, cog: QueueCog, guild_id: int, match_number: int, session: CaptainDraftSession) -> None:
        self._cog = cog
        self._guild_id = guild_id
        self._match_number = match_number
        options = [
            discord.SelectOption(
                label=cog._format_member_label(guild_id, user_id),
                value=str(user_id),
            )
            for user_id in session.available_player_ids[:25]
        ]
        super().__init__(
            placeholder="Select the next drafted player",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction[BebopBot]) -> None:
        drafted_user_id = int(self.values[0])
        await self._cog._handle_captain_draft_pick(interaction, self._guild_id, self._match_number, drafted_user_id)


class CaptainDraftView(discord.ui.View):
    def __init__(self, cog: QueueCog, guild_id: int, match_number: int, session: CaptainDraftSession) -> None:
        super().__init__(timeout=None)
        self.add_item(DraftPlayerSelect(cog, guild_id, match_number, session))


# Cog


class QueueCog(commands.Cog, name="Queue"):
    def __init__(self, bot: BebopBot) -> None:
        self.bot = bot
        self._ready_bootstrap_lock = asyncio.Lock()
        self._queue_bootstrapped = False
        self._queue_channel_id: int | None = None
        self._commands_channel_id: int | None = None
        self._matches_channel_id: int | None = None
        self._queue_message_id: int | None = None
        self._active_matches_by_guild: dict[int, dict[int, ActiveMatch]] = {}
        self._next_match_number_by_guild: dict[int, int] = {}
        self._match_creation_locks: dict[int, asyncio.Lock] = {}
        self._draft_settings_by_guild: dict[int, QueueDraftSettings] = {}
        self._draft_sessions_by_guild: dict[int, dict[int, CaptainDraftSession]] = {}
        self._hero_selection_sessions_by_guild: dict[int, dict[int, HeroSelectionSession]] = {}
        self._remake_sessions_by_guild: dict[int, dict[int, RemakeSession]] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _get_draft_session(self, guild_id: int, match_number: int) -> CaptainDraftSession | None:
        guild_sessions = self._draft_sessions_by_guild.get(guild_id)
        if guild_sessions is None:
            return None
        return guild_sessions.get(match_number)

    def _set_draft_session(self, session: CaptainDraftSession) -> None:
        guild_sessions = self._draft_sessions_by_guild.setdefault(session.guild_id, {})
        guild_sessions[session.match_number] = session

    def _pop_draft_session(self, guild_id: int, match_number: int) -> CaptainDraftSession | None:
        guild_sessions = self._draft_sessions_by_guild.get(guild_id)
        if guild_sessions is None:
            return None

        removed_session = guild_sessions.pop(match_number, None)
        if guild_sessions:
            return removed_session

        self._draft_sessions_by_guild.pop(guild_id, None)
        return removed_session

    def _set_hero_selection_session(self, session: HeroSelectionSession) -> None:
        guild_sessions = self._hero_selection_sessions_by_guild.setdefault(session.guild_id, {})
        guild_sessions[session.match_number] = session

    def _get_hero_selection_session(self, guild_id: int, match_number: int) -> HeroSelectionSession | None:
        guild_sessions = self._hero_selection_sessions_by_guild.get(guild_id)
        if guild_sessions is None:
            return None
        return guild_sessions.get(match_number)

    def _pop_hero_selection_session(self, guild_id: int, match_number: int) -> HeroSelectionSession | None:
        guild_sessions = self._hero_selection_sessions_by_guild.get(guild_id)
        if guild_sessions is None:
            return None

        removed_session = guild_sessions.pop(match_number, None)
        if guild_sessions:
            return removed_session

        self._hero_selection_sessions_by_guild.pop(guild_id, None)
        return removed_session

    def _get_hero_selection_session_by_channel(self, guild_id: int, channel_id: int) -> HeroSelectionSession | None:
        guild_sessions = self._hero_selection_sessions_by_guild.get(guild_id)
        if guild_sessions is None:
            return None

        for session in guild_sessions.values():
            if session.text_channel_id == channel_id:
                return session
        return None

    def _get_remake_session(self, guild_id: int, match_number: int) -> RemakeSession | None:
        guild_sessions = self._remake_sessions_by_guild.get(guild_id)
        if guild_sessions is None:
            return None
        return guild_sessions.get(match_number)

    def _set_remake_session(self, session: RemakeSession) -> None:
        guild_sessions = self._remake_sessions_by_guild.setdefault(session.guild_id, {})
        guild_sessions[session.match_number] = session

    def _pop_remake_session(self, guild_id: int, match_number: int) -> RemakeSession | None:
        guild_sessions = self._remake_sessions_by_guild.get(guild_id)
        if guild_sessions is None:
            return None

        removed_session = guild_sessions.pop(match_number, None)
        if guild_sessions:
            return removed_session

        self._remake_sessions_by_guild.pop(guild_id, None)
        return removed_session

    def _create_background_task(self, coro: Coroutine[object, object, None]) -> None:
        """Schedule a fire-and-forget coroutine, keeping a strong reference to prevent GC."""
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _get_active_match_by_channel(self, guild_id: int, channel_id: int) -> ActiveMatch | None:
        matches_by_number = self._active_matches_by_guild.get(guild_id)
        if matches_by_number is None:
            return None

        for active_match in matches_by_number.values():
            if active_match.text_channel_id == channel_id:
                return active_match
        return None

    @staticmethod
    def _match_player_ids(active_match: ActiveMatch) -> tuple[int, ...]:
        return (*active_match.team_a_ids, *active_match.team_b_ids)

    @staticmethod
    def _required_remake_votes(total_players: int) -> int:
        return (total_players // 2) + 1

    @staticmethod
    def _remake_window_seconds_remaining(active_match: ActiveMatch, now: datetime) -> int:
        if active_match.party_created_at is None:
            return 0
        elapsed_seconds = int((now - active_match.party_created_at).total_seconds())
        return max(REMAKE_WINDOW_SECONDS - elapsed_seconds, 0)

    def _ensure_remake_session(self, active_match: ActiveMatch, guild_id: int) -> RemakeSession:
        all_player_ids = frozenset(self._match_player_ids(active_match))
        existing_session = self._get_remake_session(guild_id, active_match.match_number)
        if existing_session is not None and existing_session.all_player_ids == all_player_ids:
            return existing_session

        remake_session = RemakeSession(
            guild_id=guild_id,
            match_number=active_match.match_number,
            text_channel_id=active_match.text_channel_id,
            all_player_ids=all_player_ids,
        )
        self._set_remake_session(remake_session)
        return remake_session

    @staticmethod
    def _build_remake_vote_momentum_message(
        voter_id: int,
        vote_count: int,
        required_votes: int,
        remaining_window_seconds: int,
    ) -> str:
        remaining_votes = max(required_votes - vote_count, 0)
        if remaining_votes == 0:
            return (
                f"🗳️ <@{voter_id}> voted to remake. Vote is now **{vote_count}/{required_votes}** and "
                "has reached majority."
            )

        return (
            f"🗳️ <@{voter_id}> voted to remake. Vote is now **{vote_count}/{required_votes}** "
            f"({remaining_votes} more needed, {remaining_window_seconds}s left)."
        )

    async def _send_match_text_channel_message(self, channel_id: int, message: str) -> None:
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        with contextlib.suppress(discord.HTTPException):
            await channel.send(message)

    async def _run_remake_for_match(self, guild_id: int, active_match: ActiveMatch) -> tuple[bool, str]:
        if active_match.deadlock_party_id is None:
            return False, "❌ This match does not have an active custom lobby to remake."

        if not active_match.assigned_heroes:
            return False, "❌ Hero assignments are missing, so remake cannot recreate this lobby safely."

        if active_match.remake_count >= MAX_REMAKE_COUNT:
            old_party_id = active_match.deadlock_party_id
            try:
                await self.bot.deadlock_api.leave_custom_match(old_party_id)
            except DeadlockApiConfigurationError:
                log.warning(
                    "Skipping Deadlock lobby leave during remake cancellation because API key is not configured."
                )
            except DeadlockApiRequestError as error:
                log.warning(
                    "Deadlock lobby leave failed during remake cancellation (status=%s, body=%s): %s",
                    error.status_code,
                    error.response_body,
                    error.message,
                )

            await self._delete_active_match_channels(guild_id, active_match)
            return True, "🧹 Remake limit reached, so this match was cancelled and cleaned up."

        old_party_id = active_match.deadlock_party_id
        try:
            await self.bot.deadlock_api.leave_custom_match(old_party_id)
        except DeadlockApiConfigurationError:
            return False, "❌ Remake failed because the Deadlock API key is not configured."
        except DeadlockApiRequestError as error:
            return False, (
                "❌ Remake failed while closing the previous custom lobby"
                f" (status={error.status_code})."
            )

        await self.bot.deadlock_callbacks.retire_party_id(old_party_id)
        if active_match.callback_token is not None:
            await self.bot.deadlock_callbacks.discard_pending_callback(active_match.callback_token)

        replacement_lobby = await self._create_deadlock_custom_lobby(
            guild_id,
            active_match.match_number,
            active_match.text_channel_id,
            active_match.team_a_ids,
            active_match.team_b_ids,
            active_match.assigned_heroes,
        )

        replacement_party_id: str | None = None
        replacement_party_code: str | None = None
        replacement_callback_token: str | None = None
        if replacement_lobby is not None:
            replacement_party_id, replacement_party_code, replacement_callback_token = replacement_lobby

        replacement_party_created_at: datetime | None = None
        if replacement_party_id is not None:
            replacement_party_created_at = datetime.now(UTC)

        updated_match = ActiveMatch(
            match_number=active_match.match_number,
            team_a_ids=active_match.team_a_ids,
            team_b_ids=active_match.team_b_ids,
            text_channel_id=active_match.text_channel_id,
            team_a_voice_channel_id=active_match.team_a_voice_channel_id,
            team_b_voice_channel_id=active_match.team_b_voice_channel_id,
            deadlock_party_id=replacement_party_id,
            deadlock_party_code=replacement_party_code,
            callback_token=replacement_callback_token,
            captain_a_id=active_match.captain_a_id,
            captain_b_id=active_match.captain_b_id,
            drafted_player_order=active_match.drafted_player_order,
            assigned_heroes=active_match.assigned_heroes,
            remake_count=active_match.remake_count + 1,
            party_created_at=replacement_party_created_at,
        )
        self._active_matches_by_guild.setdefault(guild_id, {})[active_match.match_number] = updated_match

        self._pop_remake_session(guild_id, active_match.match_number)
        if replacement_party_id is not None:
            self._set_remake_session(
                RemakeSession(
                    guild_id=guild_id,
                    match_number=active_match.match_number,
                    text_channel_id=active_match.text_channel_id,
                    all_player_ids=frozenset(self._match_player_ids(updated_match)),
                )
            )

        if replacement_party_id is None or replacement_party_code is None:
            return True, (
                "⚠️ Remake passed and the previous lobby was closed, but a replacement lobby could not be created. "
                "A match admin should post manual lobby details."
            )

        return True, (
            f"✅ Remake complete. New party code: `{replacement_party_code}` "
            f"(Lobby ID: `{replacement_party_id}`)."
        )

    def _build_remake_lobby_ready_embed(self, active_match: ActiveMatch) -> discord.Embed:
        embed = discord.Embed(
            title=f"Match {active_match.match_number} Lobby Remade",
            description=(
                "Join the new party using the code below. Teams and hero picks are unchanged, and the remake "
                "window is still active."
            ),
            color=COLOR_LOBBY_READY,
        )
        hidden_king_lines = [
            f"<@{user_id}> - **{dict(active_match.assigned_heroes).get(user_id, 'Unassigned')}**"
            for user_id in active_match.team_a_ids
        ]
        archmother_lines = [
            f"<@{user_id}> - **{dict(active_match.assigned_heroes).get(user_id, 'Unassigned')}**"
            for user_id in active_match.team_b_ids
        ]
        embed.add_field(name="Hidden King", value="\n".join(hidden_king_lines) or "*No players*", inline=False)
        embed.add_field(name="Archmother", value="\n".join(archmother_lines) or "*No players*", inline=False)

        if active_match.deadlock_party_code is not None:
            embed.add_field(name="Party Code", value=f"`{active_match.deadlock_party_code}`", inline=False)
        if active_match.deadlock_party_id is not None:
            embed.add_field(name="Lobby ID", value=f"`{active_match.deadlock_party_id}`", inline=False)

        embed.add_field(name="Need a remake?", value=REMAKE_COMMAND_GUIDANCE, inline=False)

        embed.add_field(
            name="Remakes Used",
            value=f"{active_match.remake_count}/{MAX_REMAKE_COUNT}",
            inline=False,
        )
        return embed

    @staticmethod
    def _parse_hero_preferences(message_content: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        parsed_heroes: list[str] = []
        invalid_tokens: list[str] = []
        for token in message_content.split():
            resolved_hero = resolve_hero_alias(token)
            if resolved_hero is None:
                invalid_tokens.append(token)
                continue
            if resolved_hero in parsed_heroes:
                continue
            parsed_heroes.append(resolved_hero)

        return tuple(parsed_heroes), tuple(invalid_tokens)

    @staticmethod
    def _format_hero_choices(choices: tuple[str, ...]) -> str:
        if not choices:
            return "Pending"
        shown_choices = choices[:MAX_HERO_CHOICES_DISPLAY]
        return ", ".join(shown_choices)

    def _build_hero_selection_embed(self, session: HeroSelectionSession) -> discord.Embed:
        all_players = (*session.team_a_ids, *session.team_b_ids)
        submitted_count = sum(1 for user_id in all_players if user_id in session.picks_by_user)
        total_count = len(all_players)
        embed = discord.Embed(
            title=f"Match {session.match_number} Hero Selection",
            description=(
                "Hero selection is live.\n"
                f"Players locked in: **{submitted_count}/{total_count}**\n"
                "Send your hero preferences in priority order with spaces between picks.\n"
                f"Minimum picks per player: **{MIN_HERO_PICK_COUNT}**.\n"
                "Use concatenated names for multi-word heroes (example: `ladygeist`, `greytalon`)."
            ),
            color=discord.Color.teal(),
        )

        for team_name, team_user_ids in (("Hidden King", session.team_a_ids), ("Archmother", session.team_b_ids)):
            team_lines: list[str] = []
            for user_id in team_user_ids:
                assigned_hero = session.assigned_hero_by_user.get(user_id)
                if assigned_hero is not None:
                    team_lines.append(f"<@{user_id}> - **{assigned_hero}**")
                    continue

                choices = session.picks_by_user.get(user_id)
                team_lines.append(f"<@{user_id}> - {self._format_hero_choices(choices or ())}")

            embed.add_field(name=team_name, value="\n".join(team_lines) or "*No players*", inline=False)

        return embed

    @staticmethod
    def _current_turn_captain_id(session: CaptainDraftSession) -> int:
        if session.turn_team == DraftTeam.HIDDEN_KING:
            return session.captain_a_id
        return session.captain_b_id

    @staticmethod
    def _current_turn_team_ids(session: CaptainDraftSession) -> list[int]:
        if session.turn_team == DraftTeam.HIDDEN_KING:
            return session.team_a_ids
        return session.team_b_ids

    @staticmethod
    def _advance_draft_turn(session: CaptainDraftSession) -> None:
        if not session.opening_turn_completed:
            session.opening_turn_completed = True
            session.turn_team = DraftTeam.ARCHMOTHER
        else:
            if session.turn_team == DraftTeam.HIDDEN_KING:
                session.turn_team = DraftTeam.ARCHMOTHER
            else:
                session.turn_team = DraftTeam.HIDDEN_KING

        session.turn_picks_made = 0
        session.turn_pick_target = min(STANDARD_TURN_PICK_COUNT, len(session.available_player_ids))

    @staticmethod
    def _format_player_mentions(user_ids: list[int] | tuple[int, ...]) -> str:
        if not user_ids:
            return "*None*"
        return " ".join(f"<@{user_id}>" for user_id in user_ids)

    def _format_member_label(self, guild_id: int, user_id: int) -> str:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return str(user_id)

        member = guild.get_member(user_id)
        if member is None:
            return str(user_id)
        return member.display_name

    async def _configure_read_only_text_channel(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        await channel.set_permissions(
            guild.default_role,
            view_channel=True,
            send_messages=False,
            reason="Managed channel should be read-only for regular users",
        )
        if guild.me is not None:
            await channel.set_permissions(
                guild.me,
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
                embed_links=True,
                reason="Bot needs to post updates in managed read-only channels",
            )

    def _build_captain_draft_embed(self, session: CaptainDraftSession) -> discord.Embed:
        picks_remaining_this_turn = max(session.turn_pick_target - session.turn_picks_made, 0)
        embed = discord.Embed(
            title=f"Match {session.match_number} Captain Draft Live",
            color=discord.Color.purple(),
            description=(
                f"On the clock: **{session.turn_team.label}** (<@{self._current_turn_captain_id(session)}>)\n"
                f"Picks remaining this turn: **{picks_remaining_this_turn}**"
            ),
        )
        embed.add_field(name="Hidden King", value=self._format_player_mentions(session.team_a_ids), inline=False)
        embed.add_field(name="Archmother", value=self._format_player_mentions(session.team_b_ids), inline=False)
        embed.add_field(
            name="Available Players",
            value=self._format_player_mentions(session.available_player_ids),
            inline=False,
        )

        if session.pick_records:
            pick_lines = [
                f"{record.pick_number}. {record.drafted_team.label} picked <@{record.drafted_player_id}>"
                for record in session.pick_records[-8:]
            ]
            embed.add_field(name="Recent Picks", value="\n".join(pick_lines), inline=False)

        return embed

    def _get_guild_draft_settings(self, guild_id: int) -> QueueDraftSettings:
        existing_settings = self._draft_settings_by_guild.get(guild_id)
        if existing_settings is not None:
            return existing_settings

        created_settings = QueueDraftSettings()
        self._draft_settings_by_guild[guild_id] = created_settings
        return created_settings

    def _set_guild_team_assignment_mode(self, guild_id: int, mode: TeamAssignmentMode) -> QueueDraftSettings:
        draft_settings = self._get_guild_draft_settings(guild_id)
        draft_settings.team_assignment_mode = mode
        return draft_settings

    def _set_guild_captain_selection_mode(self, guild_id: int, mode: CaptainSelectionMode) -> QueueDraftSettings:
        draft_settings = self._get_guild_draft_settings(guild_id)
        draft_settings.captain_selection_mode = mode
        return draft_settings

    @staticmethod
    def _select_captains(
        entries: tuple[QueueEntry, ...],
        *,
        match_number: int,
        captain_selection_mode: CaptainSelectionMode,
    ) -> tuple[int, int]:
        if len(entries) < 2:
            msg = "Captain selection requires at least two queued players."
            raise ValueError(msg)

        if captain_selection_mode == CaptainSelectionMode.QUEUE_ORDER:
            return entries[0].user_id, entries[1].user_id

        user_ids = [entry.user_id for entry in entries]
        random_generator = random.Random(match_number)
        captain_a_id, captain_b_id = random_generator.sample(user_ids, k=2)
        return captain_a_id, captain_b_id

    @staticmethod
    def _assign_teams_for_match(
        entries: tuple[QueueEntry, ...],
        *,
        match_number: int,
        draft_settings: QueueDraftSettings,
    ) -> TeamAssignmentResult:
        if draft_settings.team_assignment_mode == TeamAssignmentMode.RANDOM_TEAMS:
            team_a_ids, team_b_ids = _split_teams(entries)
            return TeamAssignmentResult(
                team_a_ids=team_a_ids,
                team_b_ids=team_b_ids,
                mode=TeamAssignmentMode.RANDOM_TEAMS,
            )

        captain_a_id, captain_b_id = QueueCog._select_captains(
            entries,
            match_number=match_number,
            captain_selection_mode=draft_settings.captain_selection_mode,
        )
        return TeamAssignmentResult(
            team_a_ids=(captain_a_id,),
            team_b_ids=(captain_b_id,),
            mode=TeamAssignmentMode.CAPTAIN_DRAFT,
            captain_selection_mode=draft_settings.captain_selection_mode,
            captain_a_id=captain_a_id,
            captain_b_id=captain_b_id,
        )

    def _get_match_creation_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._match_creation_locks.get(guild_id)
        if lock is not None:
            return lock

        created_lock = asyncio.Lock()
        self._match_creation_locks[guild_id] = created_lock
        return created_lock

    def _next_match_number(self, guild_id: int) -> int:
        discovered_match_numbers = self._discover_match_channels(guild_id).keys()
        highest_discovered_match_number = max(discovered_match_numbers, default=0)
        highest_active_match_number = max(self._active_matches_by_guild.get(guild_id, {}), default=0)
        next_available_match_number = max(highest_discovered_match_number, highest_active_match_number) + 1
        stored_match_number = self._next_match_number_by_guild.get(guild_id, 1)
        match_number = max(stored_match_number, next_available_match_number)
        self._next_match_number_by_guild[guild_id] = match_number + 1
        return match_number

    def _forget_active_match(self, guild_id: int, match_number: int) -> ActiveMatch | None:
        matches_by_number = self._active_matches_by_guild.get(guild_id)
        if matches_by_number is None:
            return None

        removed_match = matches_by_number.pop(match_number, None)
        if matches_by_number:
            return removed_match

        self._active_matches_by_guild.pop(guild_id, None)
        return removed_match

    def _discover_match_channels(self, guild_id: int) -> dict[int, MatchChannels]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {}

        category = _find_category(guild, PUGS_CATEGORY_NAME)
        if category is None:
            return {}

        matches_by_number: dict[int, MatchChannels] = {}

        for text_channel in category.text_channels:
            match_number = _extract_match_number_from_text_channel(text_channel.name)
            if match_number is None:
                continue

            match_channels = matches_by_number.setdefault(match_number, MatchChannels(match_number=match_number))
            match_channels.text_channel = text_channel

        for voice_channel in category.voice_channels:
            voice_match_info = _extract_match_number_from_voice_channel(voice_channel.name)
            if voice_match_info is None:
                continue

            match_number, team_key = voice_match_info
            match_channels = matches_by_number.setdefault(match_number, MatchChannels(match_number=match_number))
            if team_key == "hidden_king":
                match_channels.team_a_voice_channel = voice_channel
                continue

            match_channels.team_b_voice_channel = voice_channel

        return matches_by_number

    async def _delete_discovered_match_channels(self, guild_id: int, match_channels: MatchChannels) -> int:
        deleted_channel_count = 0
        channels_to_delete = (
            match_channels.text_channel,
            match_channels.team_a_voice_channel,
            match_channels.team_b_voice_channel,
        )
        for channel in channels_to_delete:
            if channel is None:
                continue

            await channel.delete(reason="Match cleanup")
            deleted_channel_count += 1

        removed_match = self._forget_active_match(guild_id, match_channels.match_number)
        self._pop_draft_session(guild_id, match_channels.match_number)
        self._pop_hero_selection_session(guild_id, match_channels.match_number)
        self._pop_remake_session(guild_id, match_channels.match_number)
        if removed_match is not None and removed_match.deadlock_party_id is not None:
            await self.bot.deadlock_callbacks.retire_party_id(removed_match.deadlock_party_id)

        if removed_match is not None and removed_match.callback_token is not None:
            await self.bot.deadlock_callbacks.discard_pending_callback(removed_match.callback_token)

        return deleted_channel_count

    async def _resolve_guild_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is not None:
            return member

        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            log.warning("Could not resolve member %s in guild %s for team voice permissions", user_id, guild.id)
        except discord.Forbidden:
            log.warning("Missing permission to fetch members in guild %s", guild.id)
        except discord.HTTPException:
            log.exception("Failed to fetch member %s in guild %s", user_id, guild.id)
        return None

    async def _bootstrap_queue_channel_and_message(self) -> None:
        guild = self.bot.get_guild(settings.guild_id)
        if guild is None:
            log.warning("Queue bootstrap skipped because guild %s is not cached yet", settings.guild_id)
            return

        category = _find_category(guild, PUGS_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(PUGS_CATEGORY_NAME)
            log.info("Created category: %s", PUGS_CATEGORY_NAME)

        queue_channel = _find_text_channel(category, QUEUE_CHANNEL_NAME)
        if queue_channel is None:
            queue_channel = await guild.create_text_channel(QUEUE_CHANNEL_NAME, category=category)
            log.info("Created queue channel: %s", QUEUE_CHANNEL_NAME)

        commands_channel = _find_text_channel(category, COMMANDS_CHANNEL_NAME)
        if commands_channel is None:
            commands_channel = await guild.create_text_channel(COMMANDS_CHANNEL_NAME, category=category)
            log.info("Created commands channel: %s", COMMANDS_CHANNEL_NAME)

        matches_channel = _find_text_channel(category, MATCHES_CHANNEL_NAME)
        if matches_channel is None:
            matches_channel = await guild.create_text_channel(MATCHES_CHANNEL_NAME, category=category)
            log.info("Created matches channel: %s", MATCHES_CHANNEL_NAME)

        await self._configure_read_only_text_channel(guild, queue_channel)
        await self._configure_read_only_text_channel(guild, matches_channel)

        waiting_room_channel = _find_voice_channel(category, WAITING_ROOM_CHANNEL_NAME)
        if waiting_room_channel is None:
            await guild.create_voice_channel(WAITING_ROOM_CHANNEL_NAME, category=category)
            log.info("Created waiting room voice channel: %s", WAITING_ROOM_CHANNEL_NAME)

        self._queue_channel_id = queue_channel.id
        self._commands_channel_id = commands_channel.id
        self._matches_channel_id = matches_channel.id

        try:
            await queue_channel.purge(limit=None)
        except discord.Forbidden:
            log.warning("Missing permissions to purge queue channel %s", queue_channel.id)

        state, entries, updated_at = await self.bot.queue_repository.get_queue_state(guild.id)
        queue_message = await queue_channel.send(
            content=QUEUE_MESSAGE_CONTENT,
            embed=_build_status_embed(state, entries, updated_at),
            view=QueueMessageView(self),
        )
        self._queue_message_id = queue_message.id
        log.info("Published queue message in #%s (%s)", queue_channel.name, queue_channel.id)

    async def _sync_queue_message(self, guild_id: int) -> None:
        if self._queue_channel_id is None or self._queue_message_id is None:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(self._queue_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await channel.fetch_message(self._queue_message_id)
        except (discord.NotFound, discord.Forbidden):
            await self._bootstrap_queue_channel_and_message()
            return

        state, entries, updated_at = await self.bot.queue_repository.get_queue_state(guild_id)
        await message.edit(embed=_build_status_embed(state, entries, updated_at), view=QueueMessageView(self))

    async def _delete_active_match_channels(self, guild_id: int, match: ActiveMatch) -> None:
        discovered_match_channels = self._discover_match_channels(guild_id).get(match.match_number)
        if discovered_match_channels is not None:
            await self._delete_discovered_match_channels(guild_id, discovered_match_channels)
            return

        removed_match = self._forget_active_match(guild_id, match.match_number)
        self._pop_draft_session(guild_id, match.match_number)
        self._pop_hero_selection_session(guild_id, match.match_number)
        self._pop_remake_session(guild_id, match.match_number)
        if removed_match is not None and removed_match.deadlock_party_id is not None:
            await self.bot.deadlock_callbacks.retire_party_id(removed_match.deadlock_party_id)

        if removed_match is not None and removed_match.callback_token is not None:
            await self.bot.deadlock_callbacks.discard_pending_callback(removed_match.callback_token)

    async def _delete_all_match_channels(self, guild_id: int) -> int:
        deleted_channel_count = 0
        discovered_matches = self._discover_match_channels(guild_id)
        for match_channels in discovered_matches.values():
            deleted_channel_count += await self._delete_discovered_match_channels(guild_id, match_channels)

        active_matches = self._active_matches_by_guild.pop(guild_id, {})
        for active_match in active_matches.values():
            if active_match.deadlock_party_id is not None:
                await self.bot.deadlock_callbacks.retire_party_id(active_match.deadlock_party_id)

            if active_match.callback_token is not None:
                await self.bot.deadlock_callbacks.discard_pending_callback(active_match.callback_token)

        self._draft_sessions_by_guild.pop(guild_id, None)
        self._hero_selection_sessions_by_guild.pop(guild_id, None)
        self._remake_sessions_by_guild.pop(guild_id, None)

        self._next_match_number_by_guild.pop(guild_id, None)
        return deleted_channel_count

    async def _delete_match_by_number(self, guild_id: int, match_number: int) -> int:
        discovered_match_channels = self._discover_match_channels(guild_id).get(match_number)
        if discovered_match_channels is None:
            return 0

        return await self._delete_discovered_match_channels(guild_id, discovered_match_channels)

    async def _get_waiting_room_voice_channel(self, guild_id: int) -> discord.VoiceChannel | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None

        category = _find_category(guild, PUGS_CATEGORY_NAME)
        if category is None:
            return None

        return _find_voice_channel(category, WAITING_ROOM_CHANNEL_NAME)

    async def _move_match_members_to_waiting_room(self, guild_id: int, match_channels: MatchChannels) -> None:
        waiting_room = await self._get_waiting_room_voice_channel(guild_id)
        if waiting_room is None:
            return

        members_to_move: dict[int, discord.Member] = {}
        for voice_channel in (match_channels.team_a_voice_channel, match_channels.team_b_voice_channel):
            if voice_channel is None:
                continue
            for member in voice_channel.members:
                members_to_move[member.id] = member

        for member in members_to_move.values():
            try:
                await member.move_to(waiting_room, reason="Move player back to waiting room after match completion")
            except discord.Forbidden:
                log.warning("Missing permission to move member %s to waiting room in guild %s", member.id, guild_id)
            except discord.HTTPException:
                log.exception("Failed to move member %s to waiting room in guild %s", member.id, guild_id)

    async def handle_match_finished(self, guild_id: int, match_number: int) -> bool:
        discovered_match_channels = self._discover_match_channels(guild_id).get(match_number)
        if discovered_match_channels is None:
            # Channels may already be gone; still release active match/callback tracking if present.
            removed_match = self._forget_active_match(guild_id, match_number)
            self._pop_draft_session(guild_id, match_number)
            self._pop_hero_selection_session(guild_id, match_number)
            self._pop_remake_session(guild_id, match_number)

            if removed_match is not None and removed_match.deadlock_party_id is not None:
                await self.bot.deadlock_callbacks.retire_party_id(removed_match.deadlock_party_id)
            if removed_match is not None and removed_match.callback_token is not None:
                await self.bot.deadlock_callbacks.discard_pending_callback(removed_match.callback_token)
            return True

        await self._move_match_members_to_waiting_room(guild_id, discovered_match_channels)
        deleted_channel_count = await self._delete_discovered_match_channels(guild_id, discovered_match_channels)
        return deleted_channel_count > 0

    async def _create_match_text_channel(
        self,
        guild_id: int,
        match_number: int,
        member_ids: tuple[int, ...],
    ) -> discord.TextChannel | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None

        category = _find_category(guild, PUGS_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(PUGS_CATEGORY_NAME)

        match_text_channel_name = f"{MATCH_TEXT_CHANNEL_PREFIX}{match_number}"
        text_overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if guild.me is not None:
            text_overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        for user_id in member_ids:
            member = await self._resolve_guild_member(guild, user_id)
            if member is not None:
                text_overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        match_text_channel = await guild.create_text_channel(
            match_text_channel_name,
            category=category,
            overwrites=text_overwrites,
            reason="Create match text channel",
        )

        return match_text_channel

    async def _create_team_voice_channels(
        self,
        guild_id: int,
        match_number: int,
        team_a_ids: tuple[int, ...],
        team_b_ids: tuple[int, ...],
    ) -> tuple[discord.VoiceChannel, discord.VoiceChannel] | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None

        category = _find_category(guild, PUGS_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(PUGS_CATEGORY_NAME)

        team_a_channel_name = MATCH_TEAM_A_VOICE_TEMPLATE.format(match_number=match_number)
        team_a_channel = await guild.create_voice_channel(
            team_a_channel_name,
            category=category,
            reason="Create match team A voice channel",
        )

        team_b_channel_name = MATCH_TEAM_B_VOICE_TEMPLATE.format(match_number=match_number)
        team_b_channel = await guild.create_voice_channel(
            team_b_channel_name,
            category=category,
            reason="Create match team B voice channel",
        )

        team_a_overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        }
        team_b_overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        }

        if guild.me is not None:
            bot_permissions = discord.PermissionOverwrite(view_channel=True, connect=True)
            team_a_overwrites[guild.me] = bot_permissions
            team_b_overwrites[guild.me] = bot_permissions

        for user_id in team_a_ids:
            member = await self._resolve_guild_member(guild, user_id)
            if member is not None:
                team_a_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        for user_id in team_b_ids:
            member = await self._resolve_guild_member(guild, user_id)
            if member is not None:
                team_b_overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        await team_a_channel.edit(overwrites=team_a_overwrites, reason="Set team assignment permissions")
        await team_b_channel.edit(overwrites=team_b_overwrites, reason="Set team assignment permissions")

        return team_a_channel, team_b_channel

    async def _create_match_channels(
        self,
        guild_id: int,
        match_number: int,
        team_a_ids: tuple[int, ...],
        team_b_ids: tuple[int, ...],
    ) -> tuple[discord.TextChannel, discord.VoiceChannel, discord.VoiceChannel] | None:
        member_ids = (*team_a_ids, *team_b_ids)
        match_text_channel = await self._create_match_text_channel(guild_id, match_number, member_ids)
        if match_text_channel is None:
            return None

        voice_channels = await self._create_team_voice_channels(guild_id, match_number, team_a_ids, team_b_ids)
        if voice_channels is None:
            return None

        team_a_channel, team_b_channel = voice_channels
        return match_text_channel, team_a_channel, team_b_channel

    async def _move_members_to_team_voice_channels(
        self,
        guild_id: int,
        team_a_ids: tuple[int, ...],
        team_b_ids: tuple[int, ...],
        team_a_channel: discord.VoiceChannel,
        team_b_channel: discord.VoiceChannel,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        for user_id in team_a_ids:
            member = await self._resolve_guild_member(guild, user_id)
            if member is None or member.voice is None:
                continue

            try:
                await member.move_to(team_a_channel, reason="Move player to assigned team voice channel")
            except discord.Forbidden:
                log.warning("Missing permission to move member %s in guild %s", user_id, guild_id)
            except discord.HTTPException:
                log.exception("Failed to move member %s to team A voice channel in guild %s", user_id, guild_id)

        for user_id in team_b_ids:
            member = await self._resolve_guild_member(guild, user_id)
            if member is None or member.voice is None:
                continue

            try:
                await member.move_to(team_b_channel, reason="Move player to assigned team voice channel")
            except discord.Forbidden:
                log.warning("Missing permission to move member %s in guild %s", user_id, guild_id)
            except discord.HTTPException:
                log.exception("Failed to move member %s to team B voice channel in guild %s", user_id, guild_id)

    async def _create_deadlock_custom_lobby(
        self,
        guild_id: int,
        match_number: int,
        match_text_channel_id: int,
        team_a_ids: tuple[int, ...],
        team_b_ids: tuple[int, ...],
        assigned_heroes: tuple[tuple[int, str], ...],
    ) -> tuple[str, str, str | None] | None:
        min_roster_size = settings.deadlock_custom_min_roster_size
        if min_roster_size is None:
            min_roster_size = max(settings.queue_size // 2, 1)

        callback_token: str | None = None
        callback_url: str | None = None
        callback_config = await self.bot.deadlock_callbacks.prepare_match_callback(
            guild_id,
            match_number,
            match_text_channel_id,
            self._matches_channel_id,
        )
        if callback_config is not None:
            callback_token, callback_url = callback_config

        request_payload = DeadlockCustomMatchCreateRequest(
            callback_url=callback_url,
            disable_auto_ready=settings.deadlock_custom_disable_auto_ready,
            game_mode=settings.deadlock_custom_game_mode,
            is_publicly_visible=True,
            min_roster_size=min_roster_size,
            server_region=settings.deadlock_custom_server_region,
        )

        try:
            response = await self.bot.deadlock_api.create_custom_match(request_payload)
            if callback_token is not None and response.callback_secret is not None:
                await self.bot.deadlock_callbacks.activate_match_callback(
                    callback_token,
                    response.party_id,
                    response.party_code,
                    response.callback_secret,
                    team_a_ids,
                    team_b_ids,
                    assigned_heroes,
                )
            elif callback_token is not None:
                await self.bot.deadlock_callbacks.discard_pending_callback(callback_token)
                log.warning(
                    "Deadlock callback URL was used, but no callback_secret was returned for party %s",
                    response.party_id,
                )
                callback_token = None

            return response.party_id, response.party_code, callback_token
        except DeadlockApiConfigurationError:
            log.warning("Skipping Deadlock custom lobby creation because API key is not configured.")
        except DeadlockApiRequestError as error:
            log.warning(
                "Deadlock custom lobby creation failed (status=%s, body=%s): %s",
                error.status_code,
                error.response_body,
                error.message,
            )

        if callback_token is not None:
            await self.bot.deadlock_callbacks.discard_pending_callback(callback_token)

        return None

    async def _sync_captain_draft_message(self, session: CaptainDraftSession) -> None:
        channel = self.bot.get_channel(session.text_channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched_channel = await self.bot.fetch_channel(session.text_channel_id)
            except discord.HTTPException:
                return
            if not isinstance(fetched_channel, discord.TextChannel):
                return
            channel = fetched_channel

        draft_embed = self._build_captain_draft_embed(session)
        draft_view = CaptainDraftView(self, session.guild_id, session.match_number, session)
        if session.draft_message_id is None:
            draft_message = await channel.send(embed=draft_embed, view=draft_view)
            session.draft_message_id = draft_message.id
            return

        try:
            draft_message = await channel.fetch_message(session.draft_message_id)
        except discord.HTTPException:
            draft_message = await channel.send(embed=draft_embed, view=draft_view)
            session.draft_message_id = draft_message.id
            return

        await draft_message.edit(embed=draft_embed, view=draft_view)

    async def _start_captain_draft(
        self,
        guild_id: int,
        entries: tuple[QueueEntry, ...],
        match_number: int,
        assignment_result: TeamAssignmentResult,
    ) -> bool:
        captain_a_id = assignment_result.captain_a_id
        captain_b_id = assignment_result.captain_b_id
        if captain_a_id is None or captain_b_id is None:
            return False

        match_member_ids = tuple(entry.user_id for entry in entries)
        match_text_channel = await self._create_match_text_channel(guild_id, match_number, match_member_ids)
        if match_text_channel is None:
            return False

        remaining_player_ids = [entry.user_id for entry in entries if entry.user_id not in {captain_a_id, captain_b_id}]
        session = CaptainDraftSession(
            guild_id=guild_id,
            match_number=match_number,
            text_channel_id=match_text_channel.id,
            captain_a_id=captain_a_id,
            captain_b_id=captain_b_id,
            available_player_ids=remaining_player_ids,
            team_a_ids=[captain_a_id],
            team_b_ids=[captain_b_id],
        )
        self._set_draft_session(session)

        matches_by_number = self._active_matches_by_guild.setdefault(guild_id, {})
        matches_by_number[match_number] = ActiveMatch(
            match_number=match_number,
            team_a_ids=(captain_a_id,),
            team_b_ids=(captain_b_id,),
            text_channel_id=match_text_channel.id,
            captain_a_id=captain_a_id,
            captain_b_id=captain_b_id,
        )

        intro_embed = discord.Embed(
            title=f"Match {match_number} Captain Draft Underway",
            description=(
                "Captains are building the teams now.\n"
                "Draft order: first captain picks 1, then second captain picks 2, then teams "
                "alternate 2 picks each turn."
            ),
            color=discord.Color.purple(),
        )
        intro_embed.add_field(name="Hidden King Captain", value=f"<@{captain_a_id}>", inline=False)
        intro_embed.add_field(name="Archmother Captain", value=f"<@{captain_b_id}>", inline=False)
        if assignment_result.captain_selection_mode is not None:
            intro_embed.add_field(
                name="Captain Selection",
                value=f"`{assignment_result.captain_selection_mode.value}`",
                inline=False,
            )

        await match_text_channel.send(
            content=f"||{' '.join(f'<@{entry.user_id}>' for entry in entries)}||",
            embed=intro_embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

        was_auto_completed = await self._auto_assign_last_remaining_player(session)
        if was_auto_completed:
            return True

        if not session.available_player_ids:
            self._pop_draft_session(guild_id, match_number)
            await self._finalize_captain_draft(session)
            return True

        await self._sync_captain_draft_message(session)
        return True

    async def _finalize_captain_draft(self, session: CaptainDraftSession) -> None:
        team_a_ids = tuple(session.team_a_ids)
        team_b_ids = tuple(session.team_b_ids)

        voice_channels = await self._create_team_voice_channels(
            session.guild_id,
            session.match_number,
            team_a_ids,
            team_b_ids,
        )
        if voice_channels is None:
            return

        team_a_channel, team_b_channel = voice_channels
        await self._move_members_to_team_voice_channels(
            session.guild_id,
            team_a_ids,
            team_b_ids,
            team_a_channel,
            team_b_channel,
        )

        drafted_player_order = tuple(record.drafted_player_id for record in session.pick_records)
        updated_match = ActiveMatch(
            match_number=session.match_number,
            team_a_ids=team_a_ids,
            team_b_ids=team_b_ids,
            text_channel_id=session.text_channel_id,
            team_a_voice_channel_id=team_a_channel.id,
            team_b_voice_channel_id=team_b_channel.id,
            captain_a_id=session.captain_a_id,
            captain_b_id=session.captain_b_id,
            drafted_player_order=drafted_player_order,
        )
        matches_by_number = self._active_matches_by_guild.setdefault(session.guild_id, {})
        matches_by_number[session.match_number] = updated_match

        channel = self.bot.get_channel(session.text_channel_id)
        if isinstance(channel, discord.TextChannel):
            summary_embed = discord.Embed(
                title=f"Match {session.match_number} Teams Locked In",
                description="The draft is complete. Hero selection is up next.",
                color=discord.Color.purple(),
            )
            summary_embed.add_field(name="Hidden King", value=self._format_player_mentions(team_a_ids), inline=False)
            summary_embed.add_field(name="Archmother", value=self._format_player_mentions(team_b_ids), inline=False)
            summary_embed.add_field(
                name="Voice Channels",
                value=f"Hidden King: {team_a_channel.mention}\nArchmother: {team_b_channel.mention}",
                inline=False,
            )
            summary_embed.add_field(
                name="Draft Order",
                value=self._format_player_mentions(drafted_player_order),
                inline=False,
            )
            await channel.send(embed=summary_embed)

        # Captains drafted first; reverse order gives last-drafted player the highest hero-pick priority.
        hero_pick_order = (session.captain_a_id, session.captain_b_id, *drafted_player_order)
        await self._begin_hero_selection_for_match(updated_match, hero_pick_order)

    async def _auto_assign_last_remaining_player(self, session: CaptainDraftSession) -> bool:
        if len(session.available_player_ids) != 1:
            return False

        current_captain_id = self._current_turn_captain_id(session)
        drafted_user_id = session.available_player_ids.pop()
        self._current_turn_team_ids(session).append(drafted_user_id)
        session.turn_picks_made += 1
        session.pick_records.append(
            DraftPickRecord(
                pick_number=len(session.pick_records) + 1,
                captain_id=current_captain_id,
                drafted_player_id=drafted_user_id,
                drafted_team=session.turn_team,
            )
        )

        if not session.available_player_ids:
            self._pop_draft_session(session.guild_id, session.match_number)
            await self._finalize_captain_draft(session)
            return True

        if session.turn_picks_made >= session.turn_pick_target:
            self._advance_draft_turn(session)

        return False

    async def _sync_hero_selection_message(self, session: HeroSelectionSession) -> None:
        channel = self.bot.get_channel(session.text_channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched_channel = await self.bot.fetch_channel(session.text_channel_id)
            except discord.HTTPException:
                return
            if not isinstance(fetched_channel, discord.TextChannel):
                return
            channel = fetched_channel

        embed = self._build_hero_selection_embed(session)
        if session.status_message_id is None:
            status_message = await channel.send(embed=embed)
            session.status_message_id = status_message.id
            return

        try:
            status_message = await channel.fetch_message(session.status_message_id)
        except discord.HTTPException:
            status_message = await channel.send(embed=embed)
            session.status_message_id = status_message.id
            return

        await status_message.edit(embed=embed)

    async def _begin_hero_selection_for_match(self, match: ActiveMatch, pick_order: tuple[int, ...]) -> None:
        session = HeroSelectionSession(
            guild_id=settings.guild_id,
            match_number=match.match_number,
            text_channel_id=match.text_channel_id,
            team_a_ids=match.team_a_ids,
            team_b_ids=match.team_b_ids,
            pick_order=pick_order,
        )
        self._set_hero_selection_session(session)

        channel = self.bot.get_channel(match.text_channel_id)
        if isinstance(channel, discord.TextChannel):
            instructions_embed = discord.Embed(
                title=f"Match {match.match_number} Hero Picks",
                description=(
                    "Hero selection is open. Send your preferred heroes in priority order, separated by spaces.\n"
                    f"Minimum picks per player: **{MIN_HERO_PICK_COUNT}**\n"
                    "If a hero name has multiple words, send it without spaces.\n"
                    "Example: `rem haze ladygeist greytalon vindicta`\n"
                    "Your message will be removed once your picks are recorded."
                ),
                color=discord.Color.teal(),
            )
            await channel.send(embed=instructions_embed)

        await self._sync_hero_selection_message(session)

    @staticmethod
    def _assign_heroes_from_preferences(
        pick_order: tuple[int, ...],
        picks_by_user: dict[int, tuple[str, ...]],
    ) -> tuple[dict[int, str], tuple[int, ...]]:
        assigned_by_user: dict[int, str] = {}
        used_heroes: set[str] = set()
        unresolved_players: list[int] = []

        for user_id in reversed(pick_order):
            picks = picks_by_user.get(user_id, ())
            selected_hero: str | None = None
            for hero_name in picks:
                if hero_name in used_heroes:
                    continue
                selected_hero = hero_name
                break

            if selected_hero is None:
                unresolved_players.append(user_id)
                continue

            assigned_by_user[user_id] = selected_hero
            used_heroes.add(selected_hero)

        return assigned_by_user, tuple(unresolved_players)

    async def _run_round_two_prompts(self, session: HeroSelectionSession, unresolved_players: tuple[int, ...]) -> None:
        channel = self.bot.get_channel(session.text_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        playable_heroes = set(list_playable_heroes())
        for user_id in unresolved_players:
            while True:
                unavailable_heroes = sorted(set(session.assigned_hero_by_user.values()))
                unavailable_text = ", ".join(unavailable_heroes) if unavailable_heroes else "None"
                await channel.send(
                    f"<@{user_id}> you're up for the next hero call. Send your next choice now. "
                    f"Unavailable right now: {unavailable_text}"
                )

                try:
                    response_message = await self.bot.wait_for(
                        "message",
                        timeout=HERO_ROUND_TWO_REMINDER_SECONDS,
                        check=lambda msg, _uid=user_id: (
                            msg.guild is not None
                            and msg.guild.id == session.guild_id
                            and msg.channel.id == session.text_channel_id
                            and msg.author.id == _uid
                            and not msg.author.bot
                        ),
                    )
                except TimeoutError:
                    await channel.send(f"<@{user_id}> we're still waiting on your next hero pick.")
                    continue

                selected_heroes, _ = self._parse_hero_preferences(response_message.content)
                with contextlib.suppress(discord.HTTPException):
                    await response_message.delete()

                selected_hero: str | None = None
                for hero_name in selected_heroes:
                    if hero_name not in playable_heroes:
                        continue
                    if hero_name in session.assigned_hero_by_user.values():
                        continue
                    selected_hero = hero_name
                    break

                if selected_hero is None:
                    await channel.send(f"<@{user_id}> that hero is already off the board. Send another choice.")
                    continue

                session.assigned_hero_by_user[user_id] = selected_hero
                await self._sync_hero_selection_message(session)
                break

    async def _complete_match_after_hero_selection(self, session: HeroSelectionSession) -> None:
        active_match = self._active_matches_by_guild.get(session.guild_id, {}).get(session.match_number)
        if active_match is None:
            return

        custom_lobby = await self._create_deadlock_custom_lobby(
            session.guild_id,
            session.match_number,
            session.text_channel_id,
            active_match.team_a_ids,
            active_match.team_b_ids,
            tuple((user_id, hero_name) for user_id, hero_name in session.assigned_hero_by_user.items()),
        )

        party_id: str | None = None
        callback_token: str | None = None
        party_code: str | None = None
        if custom_lobby is not None:
            party_id, party_code, callback_token = custom_lobby

        updated_match = ActiveMatch(
            match_number=active_match.match_number,
            team_a_ids=active_match.team_a_ids,
            team_b_ids=active_match.team_b_ids,
            text_channel_id=active_match.text_channel_id,
            team_a_voice_channel_id=active_match.team_a_voice_channel_id,
            team_b_voice_channel_id=active_match.team_b_voice_channel_id,
            deadlock_party_id=party_id,
            deadlock_party_code=party_code,
            callback_token=callback_token,
            captain_a_id=active_match.captain_a_id,
            captain_b_id=active_match.captain_b_id,
            drafted_player_order=active_match.drafted_player_order,
            assigned_heroes=tuple((user_id, hero_name) for user_id, hero_name in session.assigned_hero_by_user.items()),
            remake_count=active_match.remake_count,
            party_created_at=datetime.now(UTC) if party_id is not None else None,
        )
        self._active_matches_by_guild.setdefault(session.guild_id, {})[session.match_number] = updated_match
        self._pop_remake_session(session.guild_id, session.match_number)
        if party_id is not None:
            self._set_remake_session(
                RemakeSession(
                    guild_id=session.guild_id,
                    match_number=session.match_number,
                    text_channel_id=session.text_channel_id,
                    all_player_ids=frozenset(self._match_player_ids(updated_match)),
                )
            )

        channel = self.bot.get_channel(session.text_channel_id)
        if isinstance(channel, discord.TextChannel):
            match_mentions = self._format_player_mentions((*updated_match.team_a_ids, *updated_match.team_b_ids))
            final_description = "Hero assignments are locked in. The match is ready to go."
            if custom_lobby is not None:
                final_description = (
                    "Join the party using the party code below.\n"
                    "Hero assignments are locked in — you're ready to load in."
                )

            final_embed = discord.Embed(
                title=f"Match {session.match_number} Ready",
                description=final_description,
                color=COLOR_LOBBY_READY,
            )
            hidden_king_lines = [
                f"<@{user_id}> - **{session.assigned_hero_by_user.get(user_id, 'Unassigned')}**"
                for user_id in updated_match.team_a_ids
            ]
            archmother_lines = [
                f"<@{user_id}> - **{session.assigned_hero_by_user.get(user_id, 'Unassigned')}**"
                for user_id in updated_match.team_b_ids
            ]
            final_embed.add_field(
                name="Hidden King",
                value="\n".join(hidden_king_lines) or "*No players*",
                inline=False,
            )
            final_embed.add_field(
                name="Archmother",
                value="\n".join(archmother_lines) or "*No players*",
                inline=False,
            )

            if custom_lobby is None:
                final_embed.add_field(name="Custom Lobby", value=CUSTOM_LOBBY_MANUAL_MESSAGE, inline=False)
            else:
                final_embed.add_field(name="Party Code", value=f"`{party_code}`", inline=False)
                final_embed.add_field(name="Lobby ID", value=f"`{party_id}`", inline=False)
                final_embed.add_field(name="Custom Lobby", value=CUSTOM_LOBBY_CREATED_MESSAGE, inline=False)
                final_embed.add_field(name="Need a remake?", value=REMAKE_COMMAND_GUIDANCE, inline=False)

            await channel.send(
                content=f"||{match_mentions}||",
                embed=final_embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )

        self._pop_hero_selection_session(session.guild_id, session.match_number)

    async def _resolve_hero_selection(self, session: HeroSelectionSession) -> None:
        async with session.lock:
            assigned_by_user, unresolved_players = self._assign_heroes_from_preferences(
                session.pick_order,
                session.picks_by_user,
            )
            session.assigned_hero_by_user = assigned_by_user
            await self._sync_hero_selection_message(session)

            if unresolved_players:
                await self._run_round_two_prompts(session, unresolved_players)

            await self._sync_hero_selection_message(session)
            await self._complete_match_after_hero_selection(session)

    @staticmethod
    async def _delete_message_after_delay(message: discord.Message, *, delay: float) -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(discord.HTTPException):
            await message.delete()

    async def _handle_captain_draft_pick(
        self,
        interaction: discord.Interaction[BebopBot],
        guild_id: int,
        match_number: int,
        drafted_user_id: int,
    ) -> None:
        if interaction.guild_id != guild_id:
            await interaction.response.send_message("This draft pick is not valid in this server.", ephemeral=True)
            return

        session = self._get_draft_session(guild_id, match_number)
        if session is None:
            await interaction.response.send_message("This draft session is no longer active.", ephemeral=True)
            return

        if interaction.channel_id != session.text_channel_id:
            await interaction.response.send_message("Use draft controls inside the match channel.", ephemeral=True)
            return

        async with session.lock:
            current_captain_id = self._current_turn_captain_id(session)
            if interaction.user.id != current_captain_id:
                await interaction.response.send_message("It is not your draft turn.", ephemeral=True)
                return

            if drafted_user_id not in session.available_player_ids:
                await interaction.response.send_message("That player is no longer available.", ephemeral=True)
                return

            session.available_player_ids.remove(drafted_user_id)
            self._current_turn_team_ids(session).append(drafted_user_id)
            session.turn_picks_made += 1
            session.pick_records.append(
                DraftPickRecord(
                    pick_number=len(session.pick_records) + 1,
                    captain_id=current_captain_id,
                    drafted_player_id=drafted_user_id,
                    drafted_team=session.turn_team,
                )
            )

            if session.available_player_ids and session.turn_picks_made >= session.turn_pick_target:
                self._advance_draft_turn(session)

            if interaction.response.is_done():
                await interaction.followup.send("Draft pick locked in.", ephemeral=True)
            else:
                await interaction.response.send_message("Draft pick locked in.", ephemeral=True)

            if not session.available_player_ids:
                self._pop_draft_session(guild_id, match_number)
                await self._finalize_captain_draft(session)
                return

            was_auto_completed = await self._auto_assign_last_remaining_player(session)
            if was_auto_completed:
                return

            await self._sync_captain_draft_message(session)

    async def _create_match_from_entries(self, guild_id: int, entries: tuple[QueueEntry, ...]) -> bool:
        match_number = self._next_match_number(guild_id)
        draft_settings = self._get_guild_draft_settings(guild_id)
        assignment_result = self._assign_teams_for_match(
            entries,
            match_number=match_number,
            draft_settings=draft_settings,
        )

        if assignment_result.mode == TeamAssignmentMode.CAPTAIN_DRAFT:
            return await self._start_captain_draft(
                guild_id,
                entries,
                match_number,
                assignment_result,
            )

        team_a_ids = assignment_result.team_a_ids
        team_b_ids = assignment_result.team_b_ids
        channels = await self._create_match_channels(guild_id, match_number, team_a_ids, team_b_ids)
        if channels is None:
            return False

        match_text_channel, team_a_channel, team_b_channel = channels
        await self._move_members_to_team_voice_channels(
            guild_id,
            team_a_ids,
            team_b_ids,
            team_a_channel,
            team_b_channel,
        )

        new_match = ActiveMatch(
            match_number=match_number,
            team_a_ids=team_a_ids,
            team_b_ids=team_b_ids,
            text_channel_id=match_text_channel.id,
            team_a_voice_channel_id=team_a_channel.id,
            team_b_voice_channel_id=team_b_channel.id,
            captain_a_id=assignment_result.captain_a_id,
            captain_b_id=assignment_result.captain_b_id,
        )
        matches_by_number = self._active_matches_by_guild.setdefault(guild_id, {})
        matches_by_number[match_number] = new_match

        embed = _build_match_started_embed(entries, match_number)
        embed.add_field(name="Hidden King", value=self._format_player_mentions(team_a_ids), inline=False)
        embed.add_field(name="Archmother", value=self._format_player_mentions(team_b_ids), inline=False)
        embed.add_field(
            name="Voice Channels",
            value=f"Hidden King: {team_a_channel.mention}\nArchmother: {team_b_channel.mention}",
            inline=False,
        )
        embed.add_field(name="Team Assignment", value=f"`{assignment_result.mode.value}`", inline=False)

        match_mentions = " ".join(f"<@{entry.user_id}>" for entry in entries)
        await match_text_channel.send(
            content=f"||{match_mentions}||",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

        # For random teams the pick order is arbitrary; reverse during assignment gives the last
        # entry their top pick guaranteed, consistent with the captain-draft path.
        hero_pick_order = (*team_a_ids, *team_b_ids)
        await self._begin_hero_selection_for_match(new_match, hero_pick_order)
        return True

    async def _create_available_matches(self, guild_id: int) -> bool:
        async with self._get_match_creation_lock(guild_id):
            state, _, _ = await self.bot.queue_repository.get_queue_state(guild_id)
            if state != QueueState.OPEN:
                return False

            created_any_match = False
            while True:
                next_entries = await self.bot.queue_repository.pop_next_match(guild_id, settings.queue_size)
                if not next_entries:
                    break

                created_any_match = True
                await self._create_match_from_entries(guild_id, next_entries)

            return created_any_match

    @staticmethod
    def _is_user_in_match_record(record: MatchHistoryRecord, user_id: int) -> bool:
        all_player_ids = (*record.hidden_king_player_ids, *record.archmother_player_ids)
        return user_id in all_player_ids

    @staticmethod
    def _resolve_user_team_label(record: MatchHistoryRecord, user_id: int) -> str:
        if user_id in record.hidden_king_player_ids:
            return "Hidden King"
        if user_id in record.archmother_player_ids:
            return "Archmother"
        return "Unknown"

    @staticmethod
    def _match_history_sort_key(record: MatchHistoryRecord) -> tuple[datetime, int]:
        # Keep legacy records without a timestamp at the bottom while preserving deterministic ordering.
        started_at = record.match_started_at or datetime.min.replace(tzinfo=UTC)
        return started_at, record.match_id

    async def _get_user_match_history(self, guild_id: int, user_id: int) -> tuple[MatchHistoryRecord, ...]:
        database = self.bot.database.db
        if database is None:
            return ()

        collection = database[MATCH_HISTORY_COLLECTION_NAME]
        raw_documents = await collection.find({"guild_id": guild_id}).to_list(length=None)
        matched_records: list[MatchHistoryRecord] = []

        for raw_document in raw_documents:
            if not isinstance(raw_document, dict):
                continue

            try:
                record = MatchHistoryRecord.model_validate(raw_document)
            except ValidationError:
                log.warning("Skipping invalid match history record in guild %s", guild_id)
                continue

            if not self._is_user_in_match_record(record, user_id):
                continue

            matched_records.append(record)

        matched_records.sort(key=self._match_history_sort_key, reverse=True)
        return tuple(matched_records)

    async def _build_user_history_embed(
        self,
        user: discord.abc.User,
        records: tuple[MatchHistoryRecord, ...],
        *,
        limit: int,
        is_self_query: bool,
    ) -> discord.Embed:
        requested_count = min(limit, len(records))
        description = f"Showing your latest {requested_count} match(es)."
        if not is_self_query:
            description = f"Showing latest {requested_count} match(es) for {user.mention}."

        embed = discord.Embed(
            title="Match History",
            description=description,
            color=discord.Color.blurple(),
        )

        recent_records = records[:limit]
        for record in recent_records:
            user_team = self._resolve_user_team_label(record, user.id)
            started_at_label = "Unknown"
            if record.match_started_at is not None:
                started_at_label = discord.utils.format_dt(record.match_started_at, style="R")

            embed.add_field(
                name=f"Match `{record.match_id}`",
                value=f"Team: **{user_team}**\nStarted: {started_at_label}",
                inline=False,
            )

        return embed

    async def _handle_button_action(self, interaction: discord.Interaction[BebopBot], action: QueueAction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
            return

        if self._queue_channel_id is not None and interaction.channel_id != self._queue_channel_id:
            await interaction.response.send_message(
                f"❌ Queue buttons can only be used in <#{self._queue_channel_id}>.",
                ephemeral=True,
            )
            return

        if action == QueueAction.JOIN:
            await self._handle_join(interaction)
            return

        await self._handle_leave(interaction)

    async def _handle_join(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        state, _, _ = await self.bot.queue_repository.get_queue_state(interaction.guild_id)
        if state != QueueState.OPEN:
            state_label = _STATE_META[state][0]
            await interaction.response.send_message(
                f"❌ The queue is not open right now ({state_label}).", ephemeral=True
            )
            return

        joined = await self.bot.queue_repository.join(interaction.guild_id, interaction.user.id)
        if not joined:
            await interaction.response.send_message("❌ You are already in the queue.", ephemeral=True)
            return

        await interaction.response.send_message("✅ You joined the queue!", ephemeral=True)
        await self._create_available_matches(interaction.guild_id)
        await self._sync_queue_message(interaction.guild_id)

    async def _handle_leave(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        removed = await self.bot.queue_repository.leave(interaction.guild_id, interaction.user.id)
        if not removed:
            await interaction.response.send_message("❌ You are not in the queue.", ephemeral=True)
            return

        await interaction.response.send_message("✅ You left the queue.", ephemeral=True)
        await self._sync_queue_message(interaction.guild_id)

    # Cog-level guards

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return

        guild_id = message.guild.id
        session = self._get_hero_selection_session_by_channel(guild_id, message.channel.id)
        if session is None:
            return

        all_player_ids = (*session.team_a_ids, *session.team_b_ids)
        if message.author.id not in all_player_ids:
            return

        valid_picks, invalid_tokens = self._parse_hero_preferences(message.content)

        with contextlib.suppress(discord.HTTPException):
            await message.delete()

        if len(valid_picks) < MIN_HERO_PICK_COUNT:
            token_note = f" Unrecognized: {', '.join(f'`{t}`' for t in invalid_tokens)}." if invalid_tokens else ""
            warning = await message.channel.send(
                f"<@{message.author.id}> your hero list needs at least **{MIN_HERO_PICK_COUNT}** recognized picks."
                f"{token_note} Send a fresh list when you're ready."
            )
            self._create_background_task(self._delete_message_after_delay(warning, delay=10.0))
            return

        should_resolve = False
        async with session.lock:
            if session.resolution_started:
                return
            session.picks_by_user[message.author.id] = valid_picks
            if all(uid in session.picks_by_user for uid in all_player_ids):
                session.resolution_started = True
                should_resolve = True

        if invalid_tokens:
            warning = await message.channel.send(
                f"<@{message.author.id}> hero list locked in. These entries were not recognized and were ignored: "
                f"{', '.join(f'`{t}`' for t in invalid_tokens)}."
            )
            self._create_background_task(self._delete_message_after_delay(warning, delay=12.0))

        await self._sync_hero_selection_message(session)

        if should_resolve:
            self._create_background_task(self._resolve_hero_selection(session))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self._ready_bootstrap_lock:
            if self._queue_bootstrapped:
                return
            await self._bootstrap_queue_channel_and_message()
            self._queue_bootstrapped = (
                self._queue_channel_id is not None
                and self._commands_channel_id is not None
                and self._matches_channel_id is not None
                and self._queue_message_id is not None
            )

    async def interaction_check(self, interaction: discord.Interaction[BebopBot]) -> bool:
        """Restrict slash commands to the commands channel when configured or bootstrapped."""
        restricted_channel_id = settings.commands_channel_id or self._commands_channel_id
        if interaction.type != discord.InteractionType.application_command:
            return True

        if restricted_channel_id is not None and interaction.channel_id != restricted_channel_id:
            await interaction.response.send_message(
                f"❌ Queue commands can only be used in <#{restricted_channel_id}>.",
                ephemeral=True,
            )
            return False
        return True

    async def cog_app_command_error(
        self, interaction: discord.Interaction[BebopBot], error: app_commands.AppCommandError
    ) -> None:
        """Handle CheckFailure cleanly; forward everything else to the global handler."""
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "❌ You don't have permission to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        await self.bot.tree.on_error(interaction, error)

    # Command group

    queue_group = app_commands.Group(name="queue", description="Manage the PUG queue.")

    # Player commands

    @queue_group.command(name="join", description="Join the PUG queue.")
    async def queue_join(self, interaction: discord.Interaction[BebopBot]) -> None:
        await self._handle_join(interaction)

    @queue_group.command(name="leave", description="Leave the PUG queue.")
    async def queue_leave(self, interaction: discord.Interaction[BebopBot]) -> None:
        await self._handle_leave(interaction)

    @queue_group.command(name="status", description="View the current queue status.")
    async def queue_status(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        state, entries, updated_at = await self.bot.queue_repository.get_queue_state(interaction.guild_id)
        await interaction.response.send_message(embed=_build_status_embed(state, entries, updated_at), ephemeral=True)

    @app_commands.command(name="remake", description="Vote to remake the current match lobby.")
    async def queue_remake(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            return

        active_match = self._get_active_match_by_channel(interaction.guild_id, interaction.channel_id)
        if active_match is None:
            await interaction.response.send_message(
                "❌ This command can only be used in an active match text channel.",
                ephemeral=True,
            )
            return

        all_player_ids = self._match_player_ids(active_match)
        if interaction.user.id not in all_player_ids:
            await interaction.response.send_message(
                "❌ Only players assigned to this match can vote to remake.",
                ephemeral=True,
            )
            return

        if active_match.deadlock_party_id is None or active_match.party_created_at is None:
            await interaction.response.send_message(
                "❌ This match does not have an active custom lobby to remake.",
                ephemeral=True,
            )
            return

        now = datetime.now(UTC)
        remaining_window_seconds = self._remake_window_seconds_remaining(active_match, now)
        if remaining_window_seconds <= 0:
            await interaction.response.send_message(
                "❌ The remake window has expired for this lobby.",
                ephemeral=True,
            )
            return

        remake_session = self._ensure_remake_session(active_match, interaction.guild_id)
        required_votes = self._required_remake_votes(len(remake_session.all_player_ids))
        should_execute_remake = False
        vote_count_after_submit = 0
        async with remake_session.lock:
            if remake_session.majority_triggered:
                await interaction.response.send_message(
                    "⏳ A remake vote already passed and is being processed.",
                    ephemeral=True,
                )
                return

            if interaction.user.id in remake_session.votes:
                await interaction.response.send_message(
                    "Info: your remake vote is already counted.",
                    ephemeral=True,
                )
                return

            remake_session.votes.add(interaction.user.id)
            vote_count_after_submit = len(remake_session.votes)
            if vote_count_after_submit >= required_votes:
                remake_session.majority_triggered = True
                should_execute_remake = True

        momentum_message = self._build_remake_vote_momentum_message(
            interaction.user.id,
            vote_count_after_submit,
            required_votes,
            remaining_window_seconds,
        )

        if not should_execute_remake:
            await interaction.response.send_message(
                (
                    f"✅ {REMAKE_VOTE_RECORDED_MESSAGE} "
                    f"Current votes: **{vote_count_after_submit}/{required_votes}**."
                ),
                ephemeral=True,
            )
            await self._send_match_text_channel_message(active_match.text_channel_id, momentum_message)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._send_match_text_channel_message(active_match.text_channel_id, momentum_message)
        succeeded, result_message = await self._run_remake_for_match(interaction.guild_id, active_match)
        if succeeded:
            await self._send_match_text_channel_message(active_match.text_channel_id, f"🔁 {REMAKE_READY_MESSAGE}")
            refreshed_match = self._active_matches_by_guild.get(interaction.guild_id, {}).get(active_match.match_number)
            if (
                refreshed_match is not None
                and refreshed_match.deadlock_party_id is not None
                and refreshed_match.deadlock_party_code is not None
            ):
                match_mentions = self._format_player_mentions(self._match_player_ids(refreshed_match))
                await interaction.followup.send(result_message, ephemeral=True)
                channel = self.bot.get_channel(active_match.text_channel_id)
                if isinstance(channel, discord.TextChannel):
                    await channel.send(
                        content=f"||{match_mentions}||",
                        embed=self._build_remake_lobby_ready_embed(refreshed_match),
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                return

            await self._send_match_text_channel_message(active_match.text_channel_id, result_message)
        else:
            remake_session = self._get_remake_session(interaction.guild_id, active_match.match_number)
            if remake_session is not None:
                async with remake_session.lock:
                    remake_session.majority_triggered = False

        await interaction.followup.send(result_message, ephemeral=True)

    @queue_group.command(name="remake-status", description="View remake vote status for the current match lobby.")
    async def queue_remake_status(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            return

        active_match = self._get_active_match_by_channel(interaction.guild_id, interaction.channel_id)
        if active_match is None:
            await interaction.response.send_message(
                "❌ This command can only be used in an active match text channel.",
                ephemeral=True,
            )
            return

        all_player_ids = self._match_player_ids(active_match)
        if interaction.user.id not in all_player_ids:
            await interaction.response.send_message(
                "❌ Only players assigned to this match can view remake status.",
                ephemeral=True,
            )
            return

        remaining_window_seconds = self._remake_window_seconds_remaining(active_match, datetime.now(UTC))
        if active_match.deadlock_party_id is None or active_match.party_created_at is None:
            await interaction.response.send_message(
                "Info: this match does not currently have an active custom lobby.",
                ephemeral=True,
            )
            return

        remake_session = self._ensure_remake_session(active_match, interaction.guild_id)
        required_votes = self._required_remake_votes(len(remake_session.all_player_ids))
        vote_count = len(remake_session.votes)
        remaining_votes = max(required_votes - vote_count, 0)
        status_embed = discord.Embed(
            title=f"Match {active_match.match_number} Remake Status",
            color=discord.Color.orange(),
        )
        status_embed.add_field(name="Votes", value=f"{vote_count}/{required_votes}", inline=False)
        status_embed.add_field(name="Votes Needed", value=str(remaining_votes), inline=False)
        status_embed.add_field(
            name="Window Remaining",
            value=f"{remaining_window_seconds}s",
            inline=False,
        )
        status_embed.add_field(
            name="Successful Remakes",
            value=f"{active_match.remake_count}/{MAX_REMAKE_COUNT}",
            inline=False,
        )

        await interaction.response.send_message(embed=status_embed, ephemeral=True)

    @queue_group.command(name="history", description="View recent match history for yourself or another user.")
    async def queue_history(
        self,
        interaction: discord.Interaction[BebopBot],
        user: discord.Member | None = None,
        limit: app_commands.Range[int, 1, MAX_MATCH_HISTORY_LIMIT] = DEFAULT_MATCH_HISTORY_LIMIT,
    ) -> None:
        if interaction.guild_id is None:
            return

        target_user = user or interaction.user
        is_self_query = target_user.id == interaction.user.id

        if self.bot.database.db is None:
            await interaction.response.send_message(
                "❌ Match history is unavailable right now because the database is not connected.",
                ephemeral=True,
            )
            return

        user_history_records = await self._get_user_match_history(interaction.guild_id, target_user.id)
        if not user_history_records:
            empty_message = "Info: you do not have any recorded matches yet."
            if not is_self_query:
                empty_message = f"Info: {target_user.mention} does not have any recorded matches yet."

            await interaction.response.send_message(
                empty_message,
                ephemeral=True,
            )
            return

        history_embed = await self._build_user_history_embed(
            target_user,
            user_history_records,
            limit=limit,
            is_self_query=is_self_query,
        )
        await interaction.response.send_message(embed=history_embed, ephemeral=True)

    @queue_group.command(name="settings", description="[Admin] View queue draft settings.")
    @app_commands.check(_admin_check)
    async def queue_settings(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        draft_settings = self._get_guild_draft_settings(interaction.guild_id)
        await interaction.response.send_message(embed=_build_settings_embed(draft_settings), ephemeral=True)

    @queue_group.command(name="set-team-assignment", description="[Admin] Set team assignment behavior.")
    @app_commands.check(_admin_check)
    async def queue_set_team_assignment(
        self,
        interaction: discord.Interaction[BebopBot],
        mode: TeamAssignmentMode,
    ) -> None:
        if interaction.guild_id is None:
            return

        draft_settings = self._set_guild_team_assignment_mode(interaction.guild_id, mode)
        await interaction.response.send_message(
            content=f"✅ Team assignment mode set to `{draft_settings.team_assignment_mode.value}`.",
            embed=_build_settings_embed(draft_settings),
            ephemeral=True,
        )

    @queue_group.command(name="set-captain-selection", description="[Admin] Set captain selection behavior.")
    @app_commands.check(_admin_check)
    async def queue_set_captain_selection(
        self,
        interaction: discord.Interaction[BebopBot],
        mode: CaptainSelectionMode,
    ) -> None:
        if interaction.guild_id is None:
            return

        draft_settings = self._set_guild_captain_selection_mode(interaction.guild_id, mode)
        await interaction.response.send_message(
            content=f"✅ Captain selection mode set to `{draft_settings.captain_selection_mode.value}`.",
            embed=_build_settings_embed(draft_settings),
            ephemeral=True,
        )

    # Admin commands

    @queue_group.command(name="lock", description="[Admin] Lock the queue to prevent new players from joining.")
    @app_commands.check(_admin_check)
    async def queue_lock(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        await self.bot.queue_repository.set_state(interaction.guild_id, QueueState.LOCKED)
        await interaction.response.send_message("🔒 Queue locked.", ephemeral=True)
        await self._sync_queue_message(interaction.guild_id)

    @queue_group.command(name="unlock", description="[Admin] Unlock the queue to allow players to join.")
    @app_commands.check(_admin_check)
    async def queue_unlock(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        await self.bot.queue_repository.set_state(interaction.guild_id, QueueState.OPEN)
        await interaction.response.send_message("🔓 Queue unlocked.", ephemeral=True)
        await self._create_available_matches(interaction.guild_id)
        await self._sync_queue_message(interaction.guild_id)

    @queue_group.command(name="remove", description="[Admin] Remove a player from the queue.")
    @app_commands.check(_admin_check)
    async def queue_remove(self, interaction: discord.Interaction[BebopBot], player: discord.Member) -> None:
        if interaction.guild_id is None:
            return

        removed = await self.bot.queue_repository.leave(interaction.guild_id, player.id)
        if not removed:
            await interaction.response.send_message(f"❌ {player.mention} is not in the queue.", ephemeral=True)
            return

        await interaction.response.send_message(f"✅ Removed {player.mention} from the queue.", ephemeral=True)
        await self._sync_queue_message(interaction.guild_id)

    @queue_group.command(name="remake-force", description="[Admin] Force a remake for an active match.")
    @app_commands.check(_admin_check)
    async def queue_remake_force(
        self,
        interaction: discord.Interaction[BebopBot],
        match_number: int | None = None,
    ) -> None:
        if interaction.guild_id is None:
            return

        active_match: ActiveMatch | None = None
        if match_number is None:
            if interaction.channel_id is None:
                return
            active_match = self._get_active_match_by_channel(interaction.guild_id, interaction.channel_id)
        else:
            active_match = self._active_matches_by_guild.get(interaction.guild_id, {}).get(match_number)

        if active_match is None:
            await interaction.response.send_message(
                "❌ Could not find an active match for remake-force.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        succeeded, result_message = await self._run_remake_for_match(interaction.guild_id, active_match)
        if succeeded:
            await self._send_match_text_channel_message(
                active_match.text_channel_id,
                f"🛠️ Admin forced a remake for match `{active_match.match_number}`.",
            )
            await self._send_match_text_channel_message(active_match.text_channel_id, result_message)

        await interaction.followup.send(result_message, ephemeral=True)

    @queue_group.command(name="cancel-match", description="[Admin] Cancel a specific match and delete its channels.")
    @app_commands.check(_admin_check)
    async def queue_cancel_match(self, interaction: discord.Interaction[BebopBot], match_number: int) -> None:
        if interaction.guild_id is None:
            return

        deleted_channel_count = await self._delete_match_by_number(interaction.guild_id, match_number)
        if deleted_channel_count == 0:
            await interaction.response.send_message(
                f"❌ Could not find managed channels for match `{match_number}`.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🧹 Cancelled match `{match_number}` and deleted {deleted_channel_count} channel(s).",
            ephemeral=True,
        )

    @queue_group.command(name="remap", description="[Admin] Remap a tracked match ID to a new match ID.")
    @app_commands.check(_admin_check)
    async def queue_remap(
        self,
        interaction: discord.Interaction[BebopBot],
        old_match_id: int,
        new_match_id: int,
    ) -> None:
        if interaction.guild_id is None:
            return

        if old_match_id < MIN_MATCH_ID or new_match_id < MIN_MATCH_ID:
            await interaction.response.send_message(
                f"❌ Match IDs must be integers greater than or equal to `{MIN_MATCH_ID}`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        remap_summary = await self.bot.deadlock_callbacks.remap_tracked_match_id(
            interaction.guild_id,
            old_match_id,
            new_match_id,
        )

        status = remap_summary.status
        if status == MatchIdRemapStatus.SUCCESS:
            await interaction.followup.send(
                (
                    f"✅ Remapped match ID `{old_match_id}` -> `{new_match_id}`.\n"
                    f"• Updated live match post record(s): **{remap_summary.updated_live_match_post_count}**\n"
                    f"• Updated match history record(s): **{remap_summary.updated_match_history_count}**\n"
                    f"• Live match post synced: **{'yes' if remap_summary.synced_live_match_post else 'no'}**"
                ),
                ephemeral=True,
            )
            return

        if status == MatchIdRemapStatus.OLD_MATCH_NOT_FOUND:
            await interaction.followup.send(
                f"❌ No tracked records were found for old match ID `{old_match_id}` in this server.",
                ephemeral=True,
            )
            return

        if status == MatchIdRemapStatus.NEW_MATCH_ALREADY_TRACKED:
            await interaction.followup.send(
                f"❌ Match ID `{new_match_id}` is already tracked in this server. Choose a different new match ID.",
                ephemeral=True,
            )
            return

        if status == MatchIdRemapStatus.DATABASE_UNAVAILABLE:
            await interaction.followup.send(
                "❌ Remap failed because the database connection is unavailable.",
                ephemeral=True,
            )
            return

        if status == MatchIdRemapStatus.INVALID_INPUT:
            await interaction.followup.send(
                "❌ Remap failed because the supplied IDs were invalid. Use two different positive integers.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "❌ Remap failed due to a database write error. Check logs and try again.",
            ephemeral=True,
        )

    @queue_group.command(name="track", description="[Admin] Re-enable heartbeat tracking for an existing match ID.")
    @app_commands.check(_admin_check)
    async def queue_track(self, interaction: discord.Interaction[BebopBot], match_id: int) -> None:
        if interaction.guild_id is None:
            return

        if match_id < MIN_MATCH_ID:
            await interaction.response.send_message(
                f"❌ Match ID must be an integer greater than or equal to `{MIN_MATCH_ID}`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        track_summary = await self.bot.deadlock_callbacks.track_existing_live_match(interaction.guild_id, match_id)
        if track_summary.status == LiveMatchTrackStatus.SUCCESS:
            if track_summary.resulting_status is not None and track_summary.resulting_status.value == "in_progress":
                await interaction.followup.send(
                    f"✅ Match `{match_id}` is now tracked for heartbeat updates.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                f"Info: match `{match_id}` was found, but metadata indicates it is already finished.",
                ephemeral=True,
            )
            return

        if track_summary.status == LiveMatchTrackStatus.MATCH_NOT_TRACKED:
            await interaction.followup.send(
                (
                    f"❌ Match `{match_id}` is not currently stored in `{LIVE_MATCH_POSTS_COLLECTION_NAME}` "
                    "for this server, so it cannot be tracked."
                ),
                ephemeral=True,
            )
            return

        if track_summary.status == LiveMatchTrackStatus.API_VALIDATION_FAILED:
            if track_summary.api_status_code == 429 and track_summary.api_retry_after_seconds is not None:
                await interaction.followup.send(
                    (
                        "❌ Could not validate that match right now because the API is rate limited. "
                        f"Try again in about {track_summary.api_retry_after_seconds}s."
                    ),
                    ephemeral=True,
                )
                return

            if track_summary.api_status_code is not None:
                await interaction.followup.send(
                    (
                        f"❌ Could not validate match `{match_id}` with the API (status "
                        f"{track_summary.api_status_code})."
                    ),
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                f"❌ Could not validate match `{match_id}` with the API.",
                ephemeral=True,
            )
            return

        if track_summary.status == LiveMatchTrackStatus.DATABASE_UNAVAILABLE:
            await interaction.followup.send(
                "❌ Tracking failed because the database connection is unavailable.",
                ephemeral=True,
            )
            return

        if track_summary.status == LiveMatchTrackStatus.INVALID_INPUT:
            await interaction.followup.send(
                "❌ Tracking failed because the supplied match ID was invalid.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "❌ Tracking failed while updating match tracking state. Check logs and try again.",
            ephemeral=True,
        )

    @queue_group.command(name="cleanup-matches", description="[Admin] Delete all managed match channels.")
    @app_commands.check(_admin_check)
    async def queue_cleanup_matches(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        deleted_channel_count = await self._delete_all_match_channels(interaction.guild_id)
        if deleted_channel_count == 0:
            await interaction.response.send_message("Info: no managed match channels were found.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"🧹 Deleted {deleted_channel_count} managed match channel(s).",
            ephemeral=True,
        )

    @queue_group.command(name="reset", description="[Admin] Clear queue, matches, and persisted bot state.")
    @app_commands.check(_admin_check)
    async def queue_reset(self, interaction: discord.Interaction[BebopBot]) -> None:
        if interaction.guild_id is None:
            return

        await self.bot.queue_repository.clear(interaction.guild_id)
        deleted_channel_count = await self._delete_all_match_channels(interaction.guild_id)
        reset_summary = await self.bot.deadlock_callbacks.reset_tracking_state()
        await interaction.response.send_message(
            (
                "🗑️ Bebop has been reset.\n"
                f"• Deleted **{deleted_channel_count}** managed match channel(s).\n"
                f"• Removed **{reset_summary.deleted_live_match_message_count}** tracked live match post(s).\n"
                f"• Cleared **{reset_summary.cleared_live_match_post_count}** live match record(s) from "
                f"`{LIVE_MATCH_POSTS_COLLECTION_NAME}`.\n"
                f"• Cleared **{reset_summary.cleared_match_history_count}** match history record(s) from "
                f"`{MATCH_HISTORY_COLLECTION_NAME}`.\n"
                f"• Discarded **{reset_summary.pending_callback_count}** pending callback(s) and "
                f"**{reset_summary.active_callback_count}** active callback(s)."
            ),
            ephemeral=True,
        )
        await self._sync_queue_message(interaction.guild_id)


async def setup(bot: BebopBot) -> None:
    await bot.add_cog(QueueCog(bot))
