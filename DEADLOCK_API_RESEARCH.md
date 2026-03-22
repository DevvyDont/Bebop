# Deadlock API Research

This document records all findings from the third-party Deadlock API that `Bebop` will integrate with.

- **API Docs**: https://api.deadlock-api.com/docs
- **OpenAPI Spec**: https://api.deadlock-api.com/openapi.json
- **OpenAPI Clients**: https://github.com/deadlock-api/openapi-clients
- **Assets (heroes, items, ranks)**: https://assets.deadlock-api.com/v2/heroes

---

## Authentication

Most read-only endpoints have no authentication requirement and work with IP-based rate limiting only.

**Custom match endpoints require an API key.** When called without one, the API returns `403 Forbidden`.

The API key can be passed in two ways:

| Method | Header / Param | Value |
|--------|---------------|-------|
| HTTP header | `X-API-KEY` | `<your key>` |
| Query parameter | `api_key` | `<your key>` |

**How to get a key**: The API is community-run and Patreon-supported. Keys are issued via the project's [Discord server](https://discord.gg/XMF9Xrgfqu) or Patreon. An API key should be added to `bot/config.py` as a new `deadlock_api_key` setting.

> **Action item**: Obtain an API key before implementing Phase 4 (match creation). Store it in `.env` as `DEADLOCK_API_KEY`.

---

## Confirmed: Custom Match Creation is Fully Supported

This is the single most important finding. The API provides a **complete custom match lifecycle**, not just read access.
**Bebop can create, manage, and clean up custom lobbies entirely through API calls.**

### Lifecycle Overview

```
POST /create   → party_code returned → players join in-game
                 ↓
POST /ready    → (if disable_auto_ready was set)
                 ↓
POST /start    → match begins
                 ↓
GET  /{party_id}/match-id  → retrieve the real match_id for post-game stats
                 ↓
POST /leave    → cleanup (or bot auto-leaves after 15 minutes)
```

---

## Custom Match Endpoints

All custom match endpoints are under the `Custom Matches` tag.
**All require an API key.**
**Rate limits: 100 req / 30 min (per key), 1,000 req / hr (global).**

---

### `POST /v1/matches/custom/create` — Create Match

Creates a lobby via a bot account. The bot joins, generates a party code, switches to spectator mode, and readies up automatically.

**Request body** (all fields optional):

| Field | Type | Description |
|-------|------|-------------|
| `callback_url` | `string \| null` | If provided, the API POSTs to `{callback_url}/settings` when lobby settings change, and to `{callback_url}` when the match starts (with the match ID). |
| `cheats_enabled` | `boolean \| null` | Enable cheat commands in the lobby. |
| `disable_auto_ready` | `boolean \| null` | If `true`, the bot does **not** auto-ready. You must call `/ready` manually. |
| `duplicate_heroes_enabled` | `boolean \| null` | Allow multiple players to pick the same hero. |
| `game_mode` | `GameMode \| null` | See game modes below. Defaults to `normal`. |
| `is_publicly_visible` | `boolean \| null` | Whether the lobby appears in the public browser. |
| `min_roster_size` | `int \| null` | Minimum players per team before the match can start. |
| `randomize_lanes` | `boolean \| null` | Randomize lane assignments. |
| `server_region` | `ServerRegion \| null` | See server regions below. |

**Response (HTTP 200)**:

| Field | Type | Description |
|-------|------|-------------|
| `party_id` | `string` | Unique ID for the lobby. Used in all subsequent lifecycle calls. |
| `party_code` | `string` | **The code players enter in-game to join the lobby.** |
| `callback_secret` | `string \| null` | Base64 secret for verifying callbacks via `X-Callback-Secret` header. Only present if `callback_url` was provided. |

**Error responses**:
- `400` — Invalid parameters
- `429` — Rate limit exceeded
- `500` — Bot could not create the match

---

### `POST /v1/matches/custom/{lobby_id}/leave` — Leave Lobby

Forces the bot to leave the lobby immediately. By default the bot auto-leaves **15 minutes after creation** regardless of match state. Call this to clean up early.

> **Important**: The bot leaving does not end the match. Players can continue without a spectator slot being used.

---

### `POST /v1/matches/custom/{lobby_id}/ready` — Ready Up

Readies the bot in the lobby. Only needed if `disable_auto_ready` was set to `true` during creation.

---

### `POST /v1/matches/custom/{lobby_id}/unready` — Unready

Un-readies the bot. Useful for holding the lobby open while waiting on players.

---

### `POST /v1/matches/custom/{lobby_id}/start` — Start Match

Starts the match immediately. All players must be ready for this to succeed.

---

### `GET /v1/matches/custom/{party_id}/match-id` — Get Match ID

After the match has been played, call this to retrieve the real Deadlock `match_id`. This links the PUG result to the global match database, enabling full post-game metadata lookup.

**Response**:

| Field | Type |
|-------|------|
| `match_id` | `int64` |

> This is how we connect a `Bebop` match record to the Deadlock API's match metadata, player stats, and history endpoints.

---

## Enums

### `GameMode`

| Value | Description |
|-------|-------------|
| `normal` | Standard game mode. **Default for Bebop PUGs.** |
| `street_brawl` | Brawl variant. Not recommended for competitive PUGs. |
| `explore_n_y_c` | Explore/sandbox mode. Not relevant for PUGs. |

### `ServerRegion`

Full list of supported server regions:

| Region Group | Values |
|---|---|
| Europe (general) | `europe` |
| Europe (specific) | `eu_amsterdam`, `eu_poland`, `eu_stockholm`, `eu_helsinki`, `eu_falkenstein`, `eu_spain`, `eu_east`, `eu_london` |
| Africa | `south_africa` |
| North America | `us_west`, `us_east`, `us_north_central`, `us_south_central`, `us_south_east`, `us_south_west` |
| Asia-Pacific | `australia`, `singapore`, `japan`, `hong_kong`, `mp_hong_kong`, `seoul` |
| South America | `chile`, `peru`, `argentina`, `south_america` |

> **Recommendation**: Make `server_region` configurable in `bot/config.py` (e.g. `DEADLOCK_SERVER_REGION=us_east`) so the community can self-host in their region.

---

## Callback URL Strategy

The API supports a `callback_url` on match creation. When a match starts, the API sends `POST {callback_url}` with the match ID. This is the cleanest way to know a match has started without polling.

For Bebop, this requires an HTTP server that the Deadlock API can reach. Options:
1. **Webhook listener on the bot host** — simplest approach; run a lightweight HTTP server alongside the bot.
2. **Polling fallback** — call `GET /v1/matches/custom/{party_id}/match-id` every 30 seconds until it returns a match ID. No extra infrastructure needed.
3. **No callback** — rely on an admin or captain to report the match result manually.

> **Recommendation for v1**: Use polling as the default. Add callback support later as an enhancement when deployment infrastructure is settled.

---

## Player Data Endpoints (No API Key Required)

These endpoints are free and have generous rate limits. They are the foundation for player profiles, stat tracking, and match history in `Bebop`.

### Player Match History

```
GET /v1/players/{account_id}/match-history
```

Returns full match history for a player (SteamID3). Combines Steam and ClickHouse data.

- `force_refetch=true` — pull fresh data from Steam (strict rate limit: 1 req/h)
- `only_stored_history=true` — fast ClickHouse-only query, no rate limit

**Use this to seed Bebop player records on first lookup and to sync history after matches.**

---

### Player Hero Stats

```
GET /v1/players/hero-stats?account_ids=...
```

Per-hero performance stats for a list of up to 1,000 players. Returns wins, matches, KDA, damage, net worth per minute, etc. Supports filtering by time range.

**Use this to display per-hero stats on the `/stats` command.**

---

### Player Mate Stats

```
GET /v1/players/{account_id}/mate-stats
```

Returns win rate and games played with each teammate. Sorted by `mate_id`.

**Use this for the `/teammates` command and community social features.**

---

### Player Enemy Stats

```
GET /v1/players/{account_id}/enemy-stats
```

Returns win rate and games played against each opponent.

**Potential future use: fun "nemesis" or "rival" stats display.**

---

### MMR History

```
GET /v1/players/{account_id}/mmr-history
GET /v1/players/{account_id}/mmr-history/{hero_id}
```

Returns the player's computed MMR over time.

> **Note on MMR calculation**: This API uses an estimated badge-based MMR, NOT Valve's official internal rank. It is computed as an exponential moving average (EMA) over the last 50 matches based on team badge averages. **It is not exact and should not be presented to users as an official rank.** It can still be used as a rough internal rating for team balancing.

**Potential use: informational display in `/stats`. Not recommended as a balancing input without testing.**

---

### Steam Profile Lookup

```
GET /v1/players/steam?account_ids=...
GET /v1/players/steam-search?search_query=...
```

Returns Steam persona names, avatars, and profile URLs for a list of SteamID3s.

**Use this to display recognizable player names and avatars in Discord embeds.**

---

### Match Metadata

```
GET /v1/matches/{match_id}/metadata
```

Full match data (parsed JSON from protobuf). Includes:
- Winning team
- Duration
- All player K/D/A, net worth, items, damage, level
- Objective timestamps

**Use this after a match ends (once we have the match_id from `/custom/{party_id}/match-id`) to record detailed stats without requiring manual reporting.**

---

## Patreon-Only Endpoints (Not Planned for v1)

The following endpoints require a paid Patreon subscription. They are noted here for future consideration only.

| Endpoint | Description |
|----------|-------------|
| `GET /v1/players/{account_id}/account-stats` | In-game account stats (Valve-side data) |
| `GET /v1/players/{account_id}/card` | Player profile card |

---

## Analytics Endpoints (Future Use)

All analytics endpoints are free and unauthenticated. They operate on the global match database and support filtering by badge range, time window, hero, and more.

| Endpoint | Bebop Use Case |
|----------|---------------|
| `GET /v1/analytics/hero-stats` | Show global hero stats alongside player stats |
| `GET /v1/analytics/hero-synergy-stats` | Suggest hero combos during draft |
| `GET /v1/analytics/hero-counter-stats` | Suggest counter-picks during draft |
| `GET /v1/analytics/player-stats/metrics` | Benchmark a player's performance against global averages |
| `GET /v1/analytics/scoreboards/players` | Global player rankings (supplement or compare to Bebop internal leaderboard) |

---

## SQL Endpoint (Advanced / Debug)

```
GET /v1/sql?query=...
```

Direct ClickHouse SQL queries against the match database. Rate-limited (5 req/min per IP, 10 req/min with key). Useful for development and ad-hoc data exploration.

**Not intended for production bot use, but handy during development for verifying data.**

---

## External Assets

Hero names, icons, ability data, and item data are served from a separate CDN:

- **Heroes**: https://assets.deadlock-api.com/v2/heroes
- **Items**: https://assets.deadlock-api.com/v2/items
- **Ranks**: https://assets.deadlock-api.com/v2/ranks

These should be fetched once at bot startup and cached in memory (or MongoDB). **Do not call these per-request.**

---

## Integration Design Decisions

### Decision 1: Build a thin typed service layer, not use a generated client

The OpenAPI clients repo exists but Python support and maintenance quality need evaluation. Given our codebase standards (full type hints, Pydantic models, no magic dicts), it is cleaner to build a small `DeadlockApiClient` service in `bot/services/deadlock_api.py` using `aiohttp`. This gives us:

- Full control over retry logic, timeout, and error handling
- Typed Pydantic response models that match our codebase conventions
- No dependency on an external generated client that may fall behind

### Decision 2: Use polling for match-start detection in v1

The callback URL approach requires a reachable HTTP endpoint. For v1, `Bebop` will poll `GET /v1/matches/custom/{party_id}/match-id` every 30 seconds for up to 15 minutes (the bot's auto-leave window). If a `match_id` is returned, the match started successfully. This avoids infrastructure complexity for the first release.

### Decision 3: Treat the Deadlock API as advisory, not authoritative

The Deadlock API is third-party and community-run. All core Bebop data (queue state, draft results, match records) lives in MongoDB. The Deadlock API is used for:

- Creating the lobby and getting the party code
- Enriching post-match records with detailed stats
- Displaying player profile data and history

If the Deadlock API is unavailable, Bebop falls back to:
- Manual party code entry by an admin
- Manual result reporting with no auto-enrichment

---

## Resolved: Open Questions from ROADMAP.md Phase 0

- [x] **Can the API create custom games?** — Yes. Full lifecycle confirmed.
- [x] **Is an API key required?** — Yes, for all custom match endpoints. Key goes in `.env`.
- [x] **Are party codes returned by the API?** — Yes. `party_code` is in the create response.
- [x] **Can we retrieve the match ID after the game?** — Yes. `GET /custom/{party_id}/match-id`.
- [x] **Can we get full post-game stats?** — Yes, via `GET /matches/{match_id}/metadata` once we have the match ID.
- [x] **What game modes exist?** — `normal`, `street_brawl`, `explore_n_y_c`. Use `normal`.
- [x] **Can we look up players by Steam name?** — Yes, via `GET /v1/players/steam-search`.

---

## Summary: What the API Enables for Bebop

| Feature | Endpoint(s) | Notes |
|---------|------------|-------|
| Create a lobby and get party code | `POST /create` | Requires API key |
| Distribute party code to players | (local, from create response) | |
| Start match when all ready | `POST /{lobby_id}/start` | Optional — players can start manually |
| Detect match start | `GET /{party_id}/match-id` (polling) | Or callback URL |
| Get post-game detailed stats | `GET /matches/{match_id}/metadata` | Automatic stat enrichment |
| Player profile lookup | `GET /players/steam-search` | Seed player records |
| Player career stats | `GET /players/{id}/match-history` | Sync history |
| Per-hero stats | `GET /players/hero-stats` | Display on `/stats` |
| Teammate frequency | `GET /players/{id}/mate-stats` | `/teammates` command |
| Hero metadata (names, icons) | `https://assets.deadlock-api.com/v2/heroes` | Cache at startup |
| Global hero analytics (draft hints) | `GET /analytics/hero-synergy-stats` etc. | Future draft phase feature |

