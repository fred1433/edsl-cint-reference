"""Small Demand API client (api.cint.com/v1): bearer token and Cint-API-Version.

Only what the launch path needs. Token acquisition (auth.cint.com/oauth/token) is
out of scope; a token is passed in.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import httpx


class DemandError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"Demand API {status}: {body}")
        self.status, self.body = status, body


class PreconditionFailed(DemandError):
    """412: the ETag sent in If-Match is stale."""


class InvalidState(DemandError):
    """422: the fielding run is not in a state that allows the operation."""


class DemandClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        api_version: str = "2025-12-18",
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Cint-API-Version": api_version,
                "Accept": "application/json",
            },
            transport=transport,
            timeout=10.0,
        )

    def _check(self, r: httpx.Response, *ok: int) -> httpx.Response:
        if r.status_code in ok:
            return r
        body = r.json() if r.content else None
        if r.status_code == 412:
            raise PreconditionFailed(412, body)
        if r.status_code == 422:
            raise InvalidState(422, body)
        raise DemandError(r.status_code, body)

    # create_project: 202 + id, asynchronous, Idempotency-Key required.
    def create_project(self, account_id: int, name: str, project_manager_id: str,
                       idempotency_key: Optional[str] = None) -> str:
        r = self._http.post(
            f"/demand/accounts/{account_id}/projects",
            json={"name": name, "project_manager_id": project_manager_id},
            headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
        )
        return self._check(r, 202).json()["id"]

    # create_target_group: 201 + id, status draft.
    def create_draft_target_group(self, account_id: int, project_id: str,
                                  payload: dict[str, Any],
                                  idempotency_key: Optional[str] = None) -> str:
        r = self._http.post(
            f"/demand/accounts/{account_id}/projects/{project_id}/target-groups",
            json=payload,
            headers={"Idempotency-Key": idempotency_key or str(uuid.uuid4())},
        )
        return self._check(r, 201).json()["id"]

    # create_launch_fielding_run_from_draft_job: 201 means the job exists, not that
    # the target group is live. The job URL is in Location.
    def start_launch_job(self, account_id: int, project_id: str, target_group_id: str,
                         end_fielding_at: str, idempotency_key: str) -> str:
        r = self._http.post(
            f"/demand/accounts/{account_id}/projects/{project_id}/target-groups/"
            f"{target_group_id}/fielding-run-jobs/launch-from-draft",
            json={"end_fielding_at": end_fielding_at},
            headers={"Idempotency-Key": idempotency_key},
        )
        location = self._check(r, 201).headers["location"]
        return location.rstrip("/").rsplit("/", 1)[-1]

    # get_launch_fielding_run_from_draft_job: Processing | Completed | Failed.
    def get_launch_job(self, account_id: int, project_id: str, target_group_id: str,
                       job_id: str) -> dict[str, Any]:
        r = self._http.get(
            f"/demand/accounts/{account_id}/projects/{project_id}/target-groups/"
            f"{target_group_id}/fielding-run-jobs/launch-from-draft/{job_id}"
        )
        return self._check(r, 200).json()

    def _run_path(self, account_id, project_id, target_group_id, fielding_run_id) -> str:
        return (f"/demand/accounts/{account_id}/projects/{project_id}/target-groups/"
                f"{target_group_id}/fielding-runs/{fielding_run_id}")

    # get_fielding_run: body plus the ETag needed for any change.
    def get_fielding_run(self, account_id, project_id, target_group_id,
                         fielding_run_id) -> tuple[dict[str, Any], str]:
        r = self._check(self._http.get(
            self._run_path(account_id, project_id, target_group_id, fielding_run_id)), 200)
        return r.json(), r.headers["etag"]

    # pause_fielding_run / resume_fielding_run / complete_fielding_run: If-Match, 204.
    def fielding_run_action(self, action: str, account_id, project_id, target_group_id,
                            fielding_run_id, etag: str) -> Optional[str]:
        assert action in ("pause", "resume", "complete")
        r = self._http.post(
            self._run_path(account_id, project_id, target_group_id, fielding_run_id) + f"/{action}",
            headers={"If-Match": etag},
        )
        return self._check(r, 204).headers.get("etag")
