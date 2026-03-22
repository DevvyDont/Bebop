# AGENTS.md

This file provides guidance to AI Chat Agents when working with code in this repository.

## Project Overview

This is a repository for a Discord bot that manages Deadlock PUGs.
It is a Python project (MIT licensed) built with discord.py, Motor (async MongoDB), and pydantic-settings.

## Build & Run Commands

- **Install dependencies**: `poetry install`
- **Run the bot**: `poetry run python -m bot`
- **Lint**: `poetry run ruff check .`
- **Format check**: `poetry run ruff format --check .`
- **Format fix**: `poetry run ruff format .`

## Architecture

```
bot/
├── __main__.py          Entry point (python -m bot)
├── bot.py               BebopBot subclass of commands.Bot
├── config.py            pydantic-settings Settings from .env
├── log.py               Logging setup
├── database.py          Async Motor client manager
└── cogs/
    └── error_handler.py Global error handler cog
```

- **BebopBot** owns the `Database` instance; connected in `setup_hook`, closed in `close`.
- Cogs in `bot/cogs/` are auto-discovered and loaded (files prefixed with `_` are skipped).
- Configuration is loaded from `.env` via pydantic-settings (`bot/config.py`).

## Cog Conventions

- Each cog file must have an `async def setup(bot)` at module level.
- Use `from __future__ import annotations` in every file.
- Import `BebopBot` under `TYPE_CHECKING` to avoid circular imports.
- Ruff is configured with line-length 120 and target Python 3.11.

## Coding Standards

This project enforces professional-grade code quality. All contributions must follow these conventions.

### Type Hints

- Every function signature must have full type annotations — parameters and return types, no exceptions.
- Use `from __future__ import annotations` in every file for modern union syntax (`X | None` instead of `Optional[X]`).
- Annotate class and instance attributes, including collection types (`list[str]`, `dict[str, int]`, not bare `list` or `dict`).
- Use `typing.TYPE_CHECKING` for imports only needed by type checkers to avoid circular imports and runtime overhead.
- Prefer precise types over `Any`. Use `Any` only when truly unavoidable and leave a comment explaining why.

### No Magic Values

- **No magic numbers.** Define named constants (module-level `UPPER_SNAKE_CASE`) for any literal number that isn't immediately self-documenting (0, 1, and simple booleans are fine in context).
- **No magic strings.** Use `StrEnum`, constants, or config values instead of raw string literals for keys, identifiers, or categories.
- **No magic dict lookups.** Use dataclasses, `NamedTuple`, Pydantic models, or typed `TypedDict` instead of untyped dictionaries for structured data. Access fields via attributes, not string keys.

### OOP & Architecture

- Follow single-responsibility — each class and module should have one clear purpose.
- Prefer composition over inheritance unless the framework requires it (e.g., `commands.Cog`, `commands.Bot`).
- Keep public APIs small: prefix internal methods and attributes with `_`.
- Use `@dataclass`, `NamedTuple`, or Pydantic `BaseModel` for data containers — never plain dicts or tuples for structured data.
- Enums (`enum.Enum`, `enum.IntEnum`, `enum.StrEnum`) for any finite set of related constants.

### Code Clarity

- Write self-documenting code with descriptive names. Avoid abbreviations unless they are universally understood (e.g., `db`, `ctx`, `msg`).
- Keep functions short and focused. If a function needs a comment explaining a block, that block is a candidate for extraction.
- No commented-out code. Remove dead code entirely.
- No bare `except:` — always catch specific exception types.
- Use early returns to reduce nesting.

### PyCharm / IDE Compatibility

- Code must produce zero warnings in PyCharm with default inspections enabled.
- No unused imports, variables, or parameters.
- No shadowing of built-in names or outer scope variables.
- No unresolved references — ensure all type stubs and dependencies are available.
- Use `# noinspection` or `# type: ignore[code]` sparingly and only with a justifying comment.
