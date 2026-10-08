"""Fake s2s.cint.com."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import httpx

IN_SURVEY = 1  # [published] GET status 1 = "In Survey/Drop"
TRANSITION_CODES = {5, 2, 4, 3}  # [published] complete, screenout, quality term, quota full


class ProcessDied(BaseException):
    """Simulates the worker process being killed mid-call (not an Exception)."""


@dataclass
class FakeRespondent:
    rid: str
    href: str
    status: int = IN_SURVEY
    applied: list[int] = field(default_factory=list)  # transitions Cint applied


class FakeS2S:
    def __init__(self, api_key: str, latency: float = 0.0):
        self.api_key = api_key
        self.latency = latency
        self.respondents: dict[str, FakeRespondent] = {}
        self.calls: list[tuple[str, str, Optional[dict]]] = []
        self.faults: deque[str] = deque()
        self._lock = threading.Lock()

    # Test controls ---------------------------------------------------------------
    def add_respondent(self, rid: str, href: str, status: int = IN_SURVEY) -> None:
        self.respondents[rid.lower()] = FakeRespondent(rid.lower(), href, status)

    def inject(self, *faults: str) -> None:
        """Faults for the next transition calls, in order:
        'commit_then_drop'  [fault] apply the transition, then lose the response
        'not_sent'          [fault] connection refused, nothing applied
        'unrelated_422'     [fault] 422 without applying anything
        'crash_before_send' [fault] the calling process dies before the request leaves
        """
        self.faults.extend(faults)

    def transitions_sent(self) -> list[dict]:
        return [b for (m, p, b) in self.calls if p.endswith("/transition")]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # Server ------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        # Client check, not a Cint behaviour: S2S takes the raw key (no "Bearer")
        # and no Cint-API-Version header.
        assert request.headers.get("authorization") == self.api_key, "S2S key must be raw"
        assert "cint-api-version" not in request.headers, "S2S needs no version header"
        body = json.loads(request.content) if request.content else None
        with self._lock:
            self.calls.append((request.method, request.url.path, body))
        if self.latency:
            time.sleep(self.latency)
        if request.method == "GET" and request.url.path.startswith("/fulfillment/respondents/"):
            return self._get(request.url.path.rsplit("/", 1)[-1])
        if request.method == "POST" and request.url.path == "/fulfillment/respondents/transition":
            return self._transition(request, body or {})
        return httpx.Response(404)

    def _get(self, rid: str) -> httpx.Response:
        r = self.respondents.get(rid.lower())
        if r is None:
            return httpx.Response(404)  # [published] 404 respondent not found
        # [published] 200 {id, status, links[].href}.
        # [assumption] after a transition GET returns some non-1 value; the code is not
        # documented, so the fake returns 99 and the application must not interpret it.
        status = r.status if r.status == IN_SURVEY else 99
        return httpx.Response(200, json={"id": r.rid, "status": status, "links": [{"href": r.href}]})

    def _transition(self, request: httpx.Request, body: dict) -> httpx.Response:
        fault = self.faults.popleft() if self.faults else None
        if fault == "crash_before_send":
            raise ProcessDied()
        if fault == "not_sent":
            raise httpx.ConnectError("connection refused", request=request)
        if fault == "unrelated_422":
            return httpx.Response(422, json={"id": body.get("id"), "status": None})
        rid, code = str(body.get("id", "")).lower(), body.get("status")
        r = self.respondents.get(rid)
        if r is None:
            return httpx.Response(404)  # [published]
        if code not in TRANSITION_CODES:
            return httpx.Response(422, json={"id": rid, "status": code})  # [assumption]
        if r.status != IN_SURVEY:
            # [published] "If the RID is already in the requested status, the API
            # returns an error instead of HTTP 200 OK" and lists 404/422.
            # [assumption] that error is 422, and so is a change between two
            # terminal statuses.
            return httpx.Response(422, json={"id": rid, "status": r.applied[-1] if r.applied else None})
        with self._lock:
            r.status = code
            r.applied.append(code)
        if fault == "commit_then_drop":
            raise httpx.ReadTimeout("response lost after Cint applied the transition", request=request)
        return httpx.Response(200, json={"id": rid, "status": code})  # [published]
