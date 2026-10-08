"""Client for s2s.cint.com (operationIds get_respondent_status, update_respondent_status).

Authentication differs from the Demand API: the S2S key goes raw in Authorization,
and Cint-API-Version is not required (how-to-server-to-server-api, 2025-12-18).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx


class S2SNotSent(Exception):
    """The request never reached Cint (connection refused or connect timeout)."""


class S2SAmbiguous(Exception):
    """The request may have reached Cint, but no response came back."""


@dataclass
class S2SResponse:
    http_status: int
    body: Optional[dict[str, Any]]


class S2SClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 5.0,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": api_key, "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    def _send(self, method: str, path: str, **kw: Any) -> S2SResponse:
        try:
            r = self._http.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise S2SNotSent(str(e)) from e
        except httpx.TransportError as e:
            # Read timeout, reset connection, protocol error: the server may have acted.
            raise S2SAmbiguous(f"{type(e).__name__}: {e}") from e
        body = None
        if r.content:
            try:
                body = r.json()
            except ValueError:
                body = None
        return S2SResponse(r.status_code, body)

    def get_respondent(self, rid: str) -> S2SResponse:
        return self._send("GET", f"/fulfillment/respondents/{rid}")

    def transition(self, rid: str, s2s_code: int) -> S2SResponse:
        return self._send(
            "POST", "/fulfillment/respondents/transition", json={"id": rid, "status": s2s_code}
        )
