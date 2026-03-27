from __future__ import annotations

from enum import StrEnum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class DeadlockGameMode(StrEnum):
    NORMAL = "normal"
    STREET_BRAWL = "street_brawl"
    EXPLORE_N_Y_C = "explore_n_y_c"


class DeadlockServerRegion(StrEnum):
    EUROPE = "europe"
    EU_AMSTERDAM = "eu_amsterdam"
    EU_POLAND = "eu_poland"
    EU_STOCKHOLM = "eu_stockholm"
    EU_HELSINKI = "eu_helsinki"
    EU_FALKENSTEIN = "eu_falkenstein"
    EU_SPAIN = "eu_spain"
    EU_EAST = "eu_east"
    EU_LONDON = "eu_london"
    SOUTH_AFRICA = "south_africa"
    US_WEST = "us_west"
    US_EAST = "us_east"
    US_NORTH_CENTRAL = "us_north_central"
    US_SOUTH_CENTRAL = "us_south_central"
    US_SOUTH_EAST = "us_south_east"
    US_SOUTH_WEST = "us_south_west"
    AUSTRALIA = "australia"
    SINGAPORE = "singapore"
    JAPAN = "japan"
    HONG_KONG = "hong_kong"
    MP_HONG_KONG = "mp_hong_kong"
    SEOUL = "seoul"
    CHILE = "chile"
    PERU = "peru"
    ARGENTINA = "argentina"
    SOUTH_AMERICA = "south_america"


class DeadlockCustomMatchCreateRequest(BaseModel):
    callback_url: str | None = None
    cheats_enabled: bool | None = None
    disable_auto_ready: bool | None = None
    duplicate_heroes_enabled: bool | None = None
    game_mode: DeadlockGameMode | None = None
    is_publicly_visible: bool | None = None
    min_roster_size: int | None = None
    randomize_lanes: bool | None = None
    server_region: DeadlockServerRegion | None = None


class DeadlockCustomMatchCreateResponse(BaseModel):
    party_id: str
    party_code: str
    callback_secret: str | None = None


class DeadlockCustomMatchIdResponse(BaseModel):
    match_id: int


class DeadlockMatchMetadataInfo(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    duration_seconds: int | None = Field(
        default=None,
        validation_alias=AliasChoices("duration_seconds", "duration_s", "match_duration_s", "duration"),
    )
    winning_team: str | int | None = Field(
        default=None,
        validation_alias=AliasChoices("winning_team", "winningTeam", "winner", "winner_team", "winning_side"),
    )


class DeadlockMatchMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    match_id: int | None = Field(default=None, validation_alias=AliasChoices("match_id", "matchId"))
    duration_seconds: int | None = Field(
        default=None,
        validation_alias=AliasChoices("duration_seconds", "duration_s", "match_duration_s", "duration"),
    )
    winning_team: str | int | None = Field(
        default=None,
        validation_alias=AliasChoices("winning_team", "winningTeam", "winner", "winner_team", "winning_side"),
    )
    metadata: DeadlockMatchMetadataInfo | None = Field(
        default=None,
        validation_alias=AliasChoices("metadata", "match_info", "matchInfo", "match"),
    )

    def resolved_duration_seconds(self) -> int | None:
        if self.duration_seconds is not None:
            return self.duration_seconds
        if self.metadata is not None:
            return self.metadata.duration_seconds
        return None

    def resolved_winning_team(self) -> str | int | None:
        if self.winning_team is not None:
            return self.winning_team
        if self.metadata is not None:
            return self.metadata.winning_team
        return None


class DeadlockMatchStartedCallback(BaseModel):
    model_config = ConfigDict(extra="allow")

    match_id: int | None = None


class DeadlockSettingsUpdatedCallback(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    player_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "player_count",
            "players_count",
            "active_player_count",
            "connected_players",
            "lobby_player_count",
            "roster_size",
            "num_players",
            "numPlayers",
        ),
    )

    def resolved_top_level_player_count(self) -> int | None:
        if self.player_count is None or self.player_count < 0:
            return None
        return self.player_count
