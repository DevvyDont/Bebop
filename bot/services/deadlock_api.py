from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from bot.models.deadlock import (
    DeadlockCustomMatchCreateRequest,
    DeadlockCustomMatchCreateResponse,
    DeadlockCustomMatchIdResponse,
    DeadlockMatchMetadataResponse,
)

log = logging.getLogger(__name__)

API_KEY_HEADER_NAME = "X-API-Key"
CUSTOM_MATCH_CREATE_PATH = "/v1/matches/custom/create"
CUSTOM_MATCH_ID_PATH_TEMPLATE = "/v1/matches/custom/{party_id}/match-id"
MATCH_METADATA_PATH_TEMPLATE = "/v1/matches/{match_id}/metadata"


class DeadlockApiError(Exception):
    """Base class for Deadlock API related errors."""


class DeadlockApiConfigurationError(DeadlockApiError):
    """Raised when required API configuration is missing."""


@dataclass(frozen=True, slots=True)
class DeadlockApiRequestError(DeadlockApiError):
    message: str
    status_code: int | None = None
    response_body: str | None = None


class DeadlockApiClient:
    def __init__(self, base_url: str, api_key: str | None, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return

        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session is None:
            return

        if self._session.closed:
            self._session = None
            return

        await self._session.close()
        self._session = None

    async def create_custom_match(
        self,
        payload: DeadlockCustomMatchCreateRequest,
    ) -> DeadlockCustomMatchCreateResponse:
        if not self._api_key:
            raise DeadlockApiConfigurationError("DEADLOCK_API_KEY is not configured.")

        session = self._require_session()
        request_url = f"{self._base_url}{CUSTOM_MATCH_CREATE_PATH}"
        request_headers = {API_KEY_HEADER_NAME: self._api_key}
        request_body = payload.model_dump(mode="json", exclude_none=True)

        try:
            async with session.post(
                request_url,
                headers=request_headers,
                json=request_body,
            ) as response:
                response_text = await response.text()
                if response.status != 200:
                    raise DeadlockApiRequestError(
                        message="Deadlock custom match create request failed.",
                        status_code=response.status,
                        response_body=response_text,
                    )

                return DeadlockCustomMatchCreateResponse.model_validate_json(response_text)
        except DeadlockApiRequestError:
            raise
        except aiohttp.ClientError as error:
            raise DeadlockApiRequestError(message=f"Deadlock API network error: {error}") from error

    async def get_custom_match_id(self, party_id: str) -> int:
        if not self._api_key:
            raise DeadlockApiConfigurationError("DEADLOCK_API_KEY is not configured.")

        session = self._require_session()
        request_url = f"{self._base_url}{CUSTOM_MATCH_ID_PATH_TEMPLATE.format(party_id=party_id)}"
        request_headers = {API_KEY_HEADER_NAME: self._api_key}

        try:
            async with session.get(request_url, headers=request_headers) as response:
                response_text = await response.text()
                if response.status != 200:
                    raise DeadlockApiRequestError(
                        message="Deadlock custom match-id request failed.",
                        status_code=response.status,
                        response_body=response_text,
                    )

                parsed_response = DeadlockCustomMatchIdResponse.model_validate_json(response_text)
                return parsed_response.match_id
        except DeadlockApiRequestError:
            raise
        except aiohttp.ClientError as error:
            raise DeadlockApiRequestError(message=f"Deadlock API network error: {error}") from error

    async def get_match_metadata(self, match_id: int) -> DeadlockMatchMetadataResponse:
        session = self._require_session()
        request_url = f"{self._base_url}{MATCH_METADATA_PATH_TEMPLATE.format(match_id=match_id)}"

        try:
            async with session.get(request_url) as response:
                response_text = await response.text()
                if response.status != 200:
                    raise DeadlockApiRequestError(
                        message="Deadlock match metadata request failed.",
                        status_code=response.status,
                        response_body=response_text,
                    )

                return DeadlockMatchMetadataResponse.model_validate_json(response_text)
        except DeadlockApiRequestError:
            raise
        except aiohttp.ClientError as error:
            raise DeadlockApiRequestError(message=f"Deadlock API network error: {error}") from error

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session

        log.warning("Deadlock API client session was not started. Creating a new session on demand.")
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
