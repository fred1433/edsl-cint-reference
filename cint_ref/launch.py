"""Launch tracking: a study is 'live' only after the launch job completes.

Mirrors publish/pause from EDSL's Prolific surface, with Cint's asynchronous launch
job and ETag-guarded fielding run actions. Every local write is conditional on the
state and job the caller read, so a delayed response cannot undo a later decision.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .db import Database
from .demand_client import DemandClient, PreconditionFailed


class LaunchConflict(Exception):
    """A launch for this target group exists with a different request."""


class LaunchTracker:
    def __init__(self, db: Database, demand: DemandClient):
        self.db, self.demand = db, demand

    def start(self, account_id: int, project_id: str, target_group_id: str,
              end_fielding_at: str) -> dict[str, Any]:
        # The full request and its key are stored before sending, so a retry after a
        # crash resends the same request with the same key; a different request for
        # the same target group is refused.
        request = {"end_fielding_at": end_fielding_at}
        with self.db.tx() as conn:
            row = conn.execute(
                """INSERT INTO cint_launches (target_group_id, account_id, project_id,
                       idempotency_key, launch_request, state)
                   VALUES (%s, %s, %s, %s, %s, 'requested')
                   ON CONFLICT (target_group_id) DO UPDATE SET updated_at = cint_launches.updated_at
                   RETURNING *""",
                (target_group_id, account_id, project_id, uuid.uuid4(), Jsonb(request)),
            ).fetchone()
        if (row["account_id"], row["project_id"], row["launch_request"]) != (account_id, project_id, request):
            raise LaunchConflict("a launch with a different request already exists")
        if row["state"] != "requested":
            return row
        job_id = self.demand.start_launch_job(
            account_id, project_id, target_group_id,
            row["launch_request"]["end_fielding_at"], str(row["idempotency_key"]))
        return self._update(target_group_id, ("requested",), None,
                            state="launching", job_id=job_id) or self.get(target_group_id)

    def poll(self, target_group_id: str) -> dict[str, Any]:
        row = self.get(target_group_id)
        if row["state"] != "launching":
            return row
        job = self.demand.get_launch_job(row["account_id"], row["project_id"],
                                         target_group_id, row["job_id"])
        if job["status"] == "Completed":
            updated = self._update(target_group_id, ("launching",), row["job_id"], state="live",
                                   fielding_run_id=job["created_fielding_run_id"])
        elif job["status"] == "Failed":
            reason = job.get("failure_reason") or {}
            updated = self._update(target_group_id, ("launching",), row["job_id"], state="failed",
                                   failure_code=reason.get("code"))
        else:
            return row  # Processing: still not live
        # None: someone else moved the launch on since we read it; keep their state.
        return updated or self.get(target_group_id)

    def pause(self, target_group_id: str) -> str:
        """Pause with optimistic locking. On 412, reread and decide again."""
        row = self.get(target_group_id)
        ids = (row["account_id"], row["project_id"], target_group_id, row["fielding_run_id"])
        for _ in range(2):
            run, etag = self.demand.get_fielding_run(*ids)
            if run["status"] == "paused":
                self._update(target_group_id, ("live", "paused"), row["job_id"], state="paused")
                return "already_paused"
            if run["status"] != "live":
                return f"not_pausable:{run['status']}"
            try:
                self.demand.fielding_run_action("pause", *ids, etag=etag)
            except PreconditionFailed:
                continue  # someone changed the run: reread before any new attempt
            self._update(target_group_id, ("live", "paused"), row["job_id"], state="paused")
            return "paused"
        return "conflict_persisted"

    def get(self, target_group_id: str) -> Optional[dict[str, Any]]:
        with self.db.tx() as conn:
            return conn.execute("SELECT * FROM cint_launches WHERE target_group_id = %s",
                                (target_group_id,)).fetchone()

    def _update(self, target_group_id: str, expected_states: tuple[str, ...],
                expected_job: Optional[str], **fields: Any) -> Optional[dict[str, Any]]:
        cols = ", ".join(f"{k} = %s" for k in fields)
        with self.db.tx() as conn:
            return conn.execute(
                f"UPDATE cint_launches SET {cols}, updated_at = now() "
                "WHERE target_group_id = %s AND state = ANY(%s) "
                "AND job_id IS NOT DISTINCT FROM %s RETURNING *",
                (*fields.values(), target_group_id, list(expected_states), expected_job),
            ).fetchone()
