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
RETRY_AFTER_HEADER_NAME = "Retry-After"
QUERY_PARAM_IS_CUSTOM = "is_custom"
QUERY_PARAM_TRUE_VALUE = "true"
QUERY_PARAM_FALSE_VALUE = "false"
CUSTOM_MATCH_CREATE_PATH = "/v1/matches/custom/create"
CUSTOM_MATCH_ID_PATH_TEMPLATE = "/v1/matches/custom/{party_id}/match-id"
CUSTOM_MATCH_LEAVE_PATH_TEMPLATE = "/v1/matches/custom/{party_id}/leave"
MATCH_METADATA_PATH_TEMPLATE = "/v1/matches/{match_id}/metadata"
CUSTOM_MATCH_LEAVE_IDEMPOTENT_STATUS_CODES: tuple[int, ...] = (404, 409, 410)


class DeadlockApiError(Exception):
    """Base class for Deadlock API related errors."""


class DeadlockApiConfigurationError(DeadlockApiError):
    """Raised when required API configuration is missing."""


@dataclass(frozen=True, slots=True)
class DeadlockApiRequestError(DeadlockApiError):
    message: str
    status_code: int | None = None
    response_body: str | None = None
    retry_after_seconds: int | None = None


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

    async def leave_custom_match(self, party_id: str) -> None:
        if not self._api_key:
            raise DeadlockApiConfigurationError("DEADLOCK_API_KEY is not configured.")

        session = self._require_session()
        request_url = f"{self._base_url}{CUSTOM_MATCH_LEAVE_PATH_TEMPLATE.format(party_id=party_id)}"
        request_headers = {API_KEY_HEADER_NAME: self._api_key}

        try:
            async with session.post(request_url, headers=request_headers) as response:
                response_text = await response.text()
                if response.status in CUSTOM_MATCH_LEAVE_IDEMPOTENT_STATUS_CODES:
                    log.info(
                        "Treating custom lobby leave as no-op for party_id=%s (status=%s)",
                        party_id,
                        response.status,
                    )
                    return

                if response.status < 200 or response.status >= 300:
                    raise DeadlockApiRequestError(
                        message="Deadlock custom lobby leave request failed.",
                        status_code=response.status,
                        response_body=response_text,
                    )
        except DeadlockApiRequestError:
            raise
        except aiohttp.ClientError as error:
            raise DeadlockApiRequestError(message=f"Deadlock API network error: {error}") from error

    async def get_match_metadata(
        self,
        match_id: int,
        *,
        is_custom: bool | None = None,
    ) -> DeadlockMatchMetadataResponse:
        session = self._require_session()
        request_url = f"{self._base_url}{MATCH_METADATA_PATH_TEMPLATE.format(match_id=match_id)}"
        request_headers = {API_KEY_HEADER_NAME: self._api_key} if self._api_key else None
        request_params: dict[str, str] | None = None
        if is_custom is not None:
            request_params = {
                QUERY_PARAM_IS_CUSTOM: QUERY_PARAM_TRUE_VALUE if is_custom else QUERY_PARAM_FALSE_VALUE,
            }

        try:
            async with session.get(request_url, headers=request_headers, params=request_params) as response:
                response_text = await response.text()
                if response.status != 200:
                    raise DeadlockApiRequestError(
                        message="Deadlock match metadata request failed.",
                        status_code=response.status,
                        response_body=response_text,
                        retry_after_seconds=self._parse_retry_after_seconds(response.headers.get(RETRY_AFTER_HEADER_NAME)),
                    )

                return DeadlockMatchMetadataResponse.model_validate_json(response_text)
        except DeadlockApiRequestError:
            raise
        except aiohttp.ClientError as error:
            raise DeadlockApiRequestError(message=f"Deadlock API network error: {error}") from error

    @staticmethod
    def _parse_retry_after_seconds(retry_after_header_value: str | None) -> int | None:
        if retry_after_header_value is None:
            return None
        try:
            parsed_seconds = int(float(retry_after_header_value))
        except ValueError:
            return None
        if parsed_seconds < 0:
            return None
        return parsed_seconds

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session

        log.warning("Deadlock API client session was not started. Creating a new session on demand.")
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
