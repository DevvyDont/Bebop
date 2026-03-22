FROM python:3.11-slim AS base

RUN pip install --no-cache-dir poetry==2.1.1

WORKDIR /app

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

COPY bot/ bot/

CMD ["poetry", "run", "python", "-m", "bot"]