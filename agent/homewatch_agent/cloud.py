"""Client for the HomeWatch cloud API.

Failures are sorted by what the caller should do about them:
    RetryableError  network trouble, timeout, 429, 5xx  -> back off and retry
    AuthError       401 / 403                           -> fix configuration; retrying won't help
    BatchRejected   413 / 422 for the whole request     -> shrink the batch / quarantine
"""

from typing import Any

import aiohttp


class CloudError(Exception):
    pass


class RetryableError(CloudError):
    pass


class AuthError(CloudError):
    pass


class BatchRejected(CloudError):
    pass


class CloudClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str, timeout_s: float = 15) -> None:
        self._session = session
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def heartbeat(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/heartbeat", body)

    async def send_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._post("/v1/events", {"events": events})

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                self._base_url + path, json=body, headers=self._headers, timeout=self._timeout
            ) as response:
                if response.status in (401, 403):
                    raise AuthError(f"HTTP {response.status}")
                if response.status in (413, 422):
                    raise BatchRejected(f"HTTP {response.status}")
                if response.status >= 400:
                    raise RetryableError(f"HTTP {response.status}")
                return await response.json()
        except aiohttp.ClientError as exc:
            raise RetryableError(type(exc).__name__) from exc
        except TimeoutError as exc:
            raise RetryableError("timeout") from exc
