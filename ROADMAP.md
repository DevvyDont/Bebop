# ROADMAP

This document tracks the planned build-out of `Bebop`, a Discord bot for managing Deadlock pickup games for a competitive community.

## Status Legend

- [ ] Not started
- [~] In progress
- [x] Done
- [!] Blocked / needs validation

## Product Goals

- Create a reliable Discord-first matchmaking flow for organized Deadlock PUGs.
- Keep queueing, team creation, draft decisions, and match reporting inside Discord.
- Track persistent player history such as wins, losses, match participation, and teammate history.
- Support a more competitive but community-friendly experience than ad hoc voice-channel coordination.

## Current Foundation

The current repository already gives us a strong starting point:

- `discord.py` bot skeleton with cog auto-loading
- `Motor` for async MongoDB access
- `pydantic-settings` for environment configuration
- shared logging and error handling
- room to add models, services, repositories, and feature cogs without reworking the core bot bootstrap

## Phase 0 — Discovery, Research, and Technical Decisions

### Goal

Validate the external tools we can rely on and document the product decisions that affect every later phase.

### Tasks

- [x] Research and document Deadlock-related APIs, clients, and limitations. See `DEADLOCK_API_RESEARCH.md`.
  - [x] Confirm the third-party Deadlock API docs are reachable: `https://api.deadlock-api.com/docs`
  - [x] Confirm the OpenAPI client repository is reachable: `https://github.com/deadlock-api/openapi-clients`
  - [x] Identify which endpoints are useful for `Bebop`:
    - [x] player lookup (`/v1/players/steam-search`, `/v1/players/steam`)
    - [x] match lookup (`/v1/matches/{match_id}/metadata`)
    - [x] hero / character metadata (`https://assets.deadlock-api.com/v2/heroes`)
    - [x] custom match creation (`POST /v1/matches/custom/create` — full lifecycle confirmed)
    - [x] party code retrieval (returned directly in the create response as `party_code`)
  - [x] Document authentication requirements, rate limits, uptime expectations, and error behavior.
    - Custom match endpoints require an API key (`X-API-KEY` header or `api_key` query param).
    - Rate limit: 100 req / 30 min per key, 1,000 req / hr global for custom match endpoints.
    - All read-only player / match endpoints are free with IP-based rate limiting.
  - [x] Verify whether the API truly supports custom match creation — **YES, confirmed with full lifecycle.**
  - [x] Fallback defined: if API is unavailable, admin manually enters party code and bot records it.
- [x] Decide how to integrate with the Deadlock API.
  - [x] Generated Python client evaluated — maintenance quality unclear; a thin typed service layer is preferred.
  - [x] Build a typed `DeadlockApiClient` in `bot/services/deadlock_api.py` using `aiohttp`.
  - [x] All external API access sits behind a service boundary; cogs never call the API directly.
- [ ] Document Discord UX choices before feature work begins.
  - [ ] Slash commands vs. buttons vs. select menus vs. modal forms
  - [ ] Which actions should be public vs. ephemeral
  - [ ] Which channels/roles are needed for queue, admin, and match operations
- [ ] Define the first version of the game flow.
  - [ ] Queue size and match-ready threshold
  - [ ] Whether captains are manual, voted on, or automatically selected
  - [ ] How character selection should work in the first release
  - [ ] What players vote on during draft (draft mode, captains, team side, rule variants, etc.)

### Initial Tool / API Candidates

- **Discord interactions (`discord.py`)**
  - Slash commands for queue and stats
  - Buttons/select menus for ready checks and draft actions
  - Ephemeral messages for confirmations and sensitive admin operations
- **MongoDB + Motor**
  - Persistent storage for players, queue state, draft sessions, matches, and stats
- **Pydantic / pydantic-settings**
  - Typed settings and typed external API payload validation where useful
- **Deadlock API (third party)**
  - Candidate source for player, match, and possibly lobby-related data
  - Needs validation for custom match / party code capabilities before we design around it
- **Deadlock OpenAPI clients**
  - Candidate source for generated API clients or OpenAPI schema references
  - Needs evaluation for Python support, maintenance quality, and typing quality
- **Ruff**
  - Keep code style and linting consistent from the beginning
- **Docker / Docker Compose**
  - Useful for local Mongo-backed development and eventual deployment

## Phase 1 — Core Domain Modeling and Project Foundation

### Goal

Define the data model and service boundaries before implementing gameplay features.

### Tasks

- [ ] Create core domain models in `bot/models/`.
  - [ ] `PlayerProfile`
  - [ ] `QueueEntry`
  - [ ] `QueueSnapshot`
  - [ ] `DraftSession`
  - [ ] `DraftVote`
  - [ ] `TeamAssignment`
  - [ ] `CharacterSelection`
  - [ ] `MatchRecord`
  - [ ] `MatchResult`
- [ ] Create repository and service layers in `bot/services/`.
  - [ ] Player repository
  - [ ] Queue repository/service
  - [ ] Draft service
  - [ ] Match service
  - [ ] Stats service
- [ ] Define enums and constants for all finite game concepts.
  - [ ] queue states
  - [ ] draft phases
  - [ ] vote options
  - [ ] match statuses
  - [ ] result states
- [ ] Decide which values should be configurable in environment vs. code constants.
  - [ ] admin role IDs
  - [ ] queue channel IDs
  - [ ] log channel IDs
  - [ ] match size / team size
- [ ] Add MongoDB indexes once the initial models are chosen.

## Phase 2 — Queue MVP

### Goal

Ship the first playable vertical slice: players can join a queue, see queue state, and progress toward a match-ready lobby.

### Tasks

- [ ] Implement a queue cog.
- [ ] Add commands for the first queue flow.
  - [ ] `/queue join`
  - [ ] `/queue leave`
  - [ ] `/queue status`
  - [ ] `/queue ready`
  - [ ] `/queue lock` or admin-only queue controls
- [ ] Support durable queue state in MongoDB.
- [ ] Prevent duplicate joins and stale queue entries.
- [ ] Add a ready-check flow once enough players are present.
- [ ] Handle inactivity, timeouts, and automatic cleanup.
- [ ] Add admin override tools.
  - [ ] remove player from queue
  - [ ] force ready
  - [ ] reset queue
- [ ] Publish queue status using embeds or a persistent queue message.

### Queue MVP Exit Criteria

- [ ] Players can reliably join and leave without state corruption.
- [ ] Queue state persists across bot restarts.
- [ ] A full lobby can be assembled and confirmed.
- [ ] Admins can recover from common failure states without database surgery.

## Phase 3 — Draft System and Match Assembly

### Goal

Turn a ready lobby into an organized match with voting and clear team / player assignments.

### Tasks

- [ ] Define the first draft format.
  - [ ] vote-based mode selection
  - [ ] captain selection strategy
  - [ ] team assignment strategy
  - [ ] character-selection strategy
- [ ] Implement a draft session state machine.
  - [ ] lobby locked
  - [ ] vote collection
  - [ ] decision resolution
  - [ ] team finalization
  - [ ] character finalization
- [ ] Support draft voting in Discord.
  - [ ] buttons or select menus for vote capture
  - [ ] timer handling and vote deadlines
  - [ ] tie-breaking rules
- [ ] Decide how teams are formed in the first version.
  - [ ] captains draft players
  - [ ] automatic balancing from stored performance data
  - [ ] randomized fallback when insufficient data exists
- [ ] Decide how characters are handled in the first version.
  - [ ] free declaration
  - [ ] drafted picks
  - [ ] optional bans / restrictions if the community wants them later
- [ ] Create a draft summary artifact.
  - [ ] final teams
  - [ ] final characters
  - [ ] captains
  - [ ] timestamps and draft decisions for auditability

### Draft Exit Criteria

- [ ] A ready queue can always progress into a deterministic draft flow.
- [ ] The bot records how teams and characters were decided.
- [ ] Draft outcomes are visible and easy for players to confirm.

## Phase 4 — Match Creation and External Integration

### Goal

Convert a completed draft into an actual playable match and distribute the information players need to join.

### Tasks

- [x] Research whether the Deadlock API can create or manage custom games directly — **YES, confirmed.**
- [ ] If supported, build an integration for match creation.
  - [ ] send the necessary API request
  - [ ] persist external match identifiers
  - [ ] capture and distribute party / lobby codes
  - [ ] handle retries and partial failures safely
- [ ] If not supported, build a manual assisted workflow.
  - [ ] admin enters party code
  - [ ] bot posts the code to approved players only
  - [ ] bot records who received the match info
- [ ] Add a match room workflow in Discord.
  - [ ] match announcement message
  - [ ] team rosters
  - [ ] party code distribution
  - [ ] start timestamp
- [ ] Track match lifecycle state.
  - [ ] created
  - [ ] code distributed
  - [ ] in progress
  - [ ] awaiting result
  - [ ] completed
  - [ ] canceled

### Match Creation Exit Criteria

- [ ] A drafted match can be turned into a real playable lobby with minimal manual effort.
- [ ] Players receive the information they need without confusion.
- [ ] External integration failure has a safe fallback path.

## Phase 5 — Post-Match Results, Stats, and History

### Goal

Record outcomes and turn match participation into useful competitive and social data.

### Tasks

- [ ] Implement result submission workflow.
  - [ ] captain report
  - [ ] admin override
  - [ ] optional player confirmation or dispute flow
- [ ] Persist match history and player statistics.
  - [ ] wins and losses
  - [ ] matches played
  - [ ] teammate frequency
  - [ ] opponent frequency
  - [ ] character usage
  - [ ] character win rate
  - [ ] recent form / streaks
- [ ] Add player-facing stats commands.
  - [ ] `/stats`
  - [ ] `/history`
  - [ ] `/teammates`
  - [ ] `/leaderboard`
- [ ] Add leaderboards and ranking views.
  - [ ] win rate leaderboard
  - [ ] total wins leaderboard
  - [ ] games played leaderboard
  - [ ] character-specific stats if enough data exists
- [ ] Decide whether a rating system belongs in v1 or later.
  - [ ] simple W/L only in early versions
  - [ ] MMR / Elo / TrueSkill as a later enhancement

### Post-Match Exit Criteria

- [ ] Every completed match has a stored result.
- [ ] Players can inspect their own history and stats.
- [ ] The community has at least one useful leaderboard.

## Phase 6 — Admin Tooling, Moderation, and Quality-of-Life

### Goal

Make the bot manageable for moderators and resilient for repeated use.

### Tasks

- [ ] Add admin-only commands for corrections and moderation.
  - [ ] void match
  - [ ] edit result
  - [ ] remove bad stats entry
  - [ ] lock queue during maintenance
- [ ] Add role-based permissions and channel restrictions.
- [ ] Add audit logging for sensitive actions.
- [ ] Add rate-limiting / abuse prevention for player commands.
- [ ] Add player profile notes or flags if the community needs moderation support.

## Phase 7 — Reliability, Testing, and Deployment

### Goal

Keep the project maintainable as features expand.

### Tasks

- [ ] Add unit tests for services and state transitions.
- [ ] Add integration tests for repositories where practical.
- [ ] Add smoke-test commands or test guild workflows.
- [ ] Improve logging around queue, draft, and result transitions.
- [ ] Add backup / recovery notes for MongoDB.
- [ ] Document deployment expectations for a hosted environment.
- [ ] Review secrets handling and environment configuration.

## Suggested Build Order

1. [ ] Complete Phase 0 research and document what the Deadlock API can actually do.
2. [ ] Implement Phase 1 models, enums, and repositories.
3. [ ] Build the Queue MVP from Phase 2.
4. [ ] Add Draft System support from Phase 3.
5. [ ] Implement Match Creation flow from Phase 4 with fallback support.
6. [ ] Add post-match stats and history from Phase 5.
7. [ ] Tighten admin tooling, testing, and deployment readiness.

## Immediate Next Steps

- [x] Create `DEADLOCK_API_RESEARCH.md` with full Deadlock API endpoint inventory. ✅
- [ ] Obtain a Deadlock API key for custom match creation (required before Phase 4). Add to `.env` as `DEADLOCK_API_KEY`.
- [ ] Decide the first supported match format and queue size for the community (answers open questions above).
- [ ] Design the first set of models in `bot/models/` (Phase 1).
- [ ] Design the first queue commands and queue-state persistence rules (Phase 2).

## Open Questions

- [x] Can the available Deadlock API create custom games, or only read game data? — **Fully confirmed. Complete lobby lifecycle supported. Requires API key.**
- [ ] What is the best first draft mode for your community: captains, auto-balance, or vote-to-select?
- [ ] Should character selection be fully managed by the bot in v1, or only tracked after players self-select?
- [ ] Should post-match confirmation require both captains, or only one trusted reporter plus admin overrides?
- [ ] Do you want rankings/MMR in the first release, or should v1 focus on queueing, drafting, and clean match history first?

