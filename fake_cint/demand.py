"""Fake api.cint.com/v1: the launch path and fielding run actions."""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from typing import Optional

import httpx

# [published] required fields of CreateDraftTargetGroupRequest in the spec.
TARGET_GROUP_REQUIRED = (
    "name", "business_unit_id", "locale", "project_manager_id", "fielding_specification",
    "fielding_assistant_assignment", "completes_goal", "collects_pii",
    "expected_length_of_interview_minutes", "expected_incidence_rate",
)


@dataclass
class FakeRun:
    id: str
    status: str = "live"
    version: int = 1

    @property
    def etag(self) -> str:
        return f'W/"{self.version}"'  # [published] ETag example format W/"1234"


class FakeDemand:
    def __init__(self, api_version: str = "2025-12-18"):
        self.api_version = api_version
        self._ids = (f"01FAKE{n:020d}" for n in itertools.count(1))
        self.projects_by_key: dict[str, str] = {}
        self.target_groups: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.jobs_by_key: dict[str, str] = {}
        self.runs: dict[str, FakeRun] = {}
        # [fault] scripted outcome for the next launch job and polls before it ends.
        self.next_launch_outcome = "Completed"
        self.polls_before_terminal = 1
        self.calls: list[tuple[str, str]] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def concurrent_change(self, run_id: str, status: Optional[str] = None) -> None:
        """[fault] another user or automation changes the run (new version)."""
        run = self.runs[run_id]
        run.version += 1
        if status:
            run.status = status

    def handle(self, request: httpx.Request) -> httpx.Response:
        # Client check: bearer token and API version on every Demand API call.
        assert request.headers.get("authorization", "").startswith("Bearer ")
        assert request.headers.get("cint-api-version") == self.api_version
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        key = request.headers.get("idempotency-key")

        if m := re.fullmatch(r"/v1/demand/accounts/\d+/projects", path):
            if method == "POST":
                if not key:  # [published] required; [assumption] error code 400
                    return httpx.Response(400, json={"detail": "Idempotency-Key required"})
                pid = self.projects_by_key.setdefault(key, next(self._ids))  # [published]
                return httpx.Response(202, json={"id": pid})  # [published] 202 + id
        if m := re.fullmatch(r"/v1/demand/accounts/\d+/projects/(\w+)/target-groups", path):
            missing = [f for f in TARGET_GROUP_REQUIRED if f not in body]
            if missing:  # [published] required list; [assumption] error body shape
                return httpx.Response(400, json={"detail": f"missing {missing}"})
            tg = next(self._ids)
            self.target_groups[tg] = {"status": "draft", **body}  # [published] draft
            return httpx.Response(201, json={"id": tg})
        if m := re.fullmatch(r"(/v1/demand/accounts/\d+/projects/\w+/target-groups/(\w+))"
                             r"/fielding-run-jobs/launch-from-draft(?:/(\w+))?", path):
            base, tg, job_id = m.groups()
            if method == "POST":
                if not key:
                    return httpx.Response(400, json={"detail": "Idempotency-Key required"})
                if "end_fielding_at" not in body:  # [published] required
                    return httpx.Response(400, json={"detail": "end_fielding_at required"})
                job_id = self.jobs_by_key.get(key)
                if job_id is None:
                    job_id = next(self._ids)
                    self.jobs_by_key[key] = job_id
                    self.jobs[job_id] = {"tg": tg, "polls": 0, "outcome": self.next_launch_outcome}
                # [published] 201 = job created, not live; Location points at the job.
                return httpx.Response(201, headers={
                    "location": f"{base[3:]}/fielding-run-jobs/launch-from-draft/{job_id}"})
            return self._job(job_id)
        if m := re.fullmatch(r"/v1/demand/accounts/\d+/projects/\w+/target-groups/\w+"
                             r"/fielding-runs/(\w+)(?:/(pause|resume|complete))?", path):
            run_id, action = m.groups()
            run = self.runs.get(run_id)
            if run is None:
                return httpx.Response(404)
            if action is None and method == "GET":
                return httpx.Response(200, headers={"etag": run.etag}, json={
                    "id": run.id, "status": run.status, "version": run.version})
            return self._run_action(run, action, request.headers.get("if-match"))
        return httpx.Response(404)

    def _job(self, job_id: str) -> httpx.Response:
        job = self.jobs.get(job_id)
        if job is None:
            return httpx.Response(404)
        job["polls"] += 1
        status = "Processing"
        if job["polls"] > self.polls_before_terminal:
            status = job["outcome"]
        out = {"job_id": job_id, "status": status, "failure_reason": None,
               "created_at": "2026-10-08T00:00:00.000Z", "created_by": "fake"}
        if status == "Completed":
            run_id = job.setdefault("run_id", next(self._ids))
            self.runs.setdefault(run_id, FakeRun(run_id))
            self.target_groups[job["tg"]]["status"] = "live"
            out["created_fielding_run_id"] = run_id
        elif status == "Failed":
            # [published] example failure from the spec
            out["failure_reason"] = {"code": "TargetGroupInvalidState",
                                     "description": "The target group is not in a draft state and cannot be launched."}
        return httpx.Response(200, json=out)

    def _run_action(self, run: FakeRun, action: str, if_match: Optional[str]) -> httpx.Response:
        if if_match != run.etag:
            return httpx.Response(412, json={"error": {"code": "conflict"}})  # [published]
        allowed = {"pause": ("live",), "resume": ("paused",), "complete": ("live", "scheduled")}
        if run.status not in allowed[action]:
            return httpx.Response(422, json={"detail": "invalid state"})  # [published] 422 listed
        run.status = {"pause": "paused", "resume": "live", "complete": "completed"}[action]
        run.version += 1
        return httpx.Response(204, headers={"etag": run.etag})  # [published] 204 + ETag
