"""Respondent flow: admission, response binding, durable finalization, recovery.

Proposed attachment points on the survey host (not inspected, proposed by analogy
with EDSL's public client):

1. survey entry           -> RespondentFlow.admit
2. response persistence   -> RespondentFlow.save_response
3. outcome finalization   -> RespondentFlow.finalize
4. status reporting       -> RespondentFlow.recover / session_state
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from psycopg.types.json import Jsonb

from .config import Settings
from .db import Database
from .outcomes import Outcome, s2s_get_allows_entry, to_s2s_transition_code
from .s2s_client import S2SAmbiguous, S2SClient, S2SNotSent, S2SResponse


class AdmissionRefused(Exception):
    pass


class NoSavedResponse(Exception):
    pass


class ResponseConflict(Exception):
    pass


@dataclass
class SurveyBinding:
    """Links one human survey to its Cint target group entry link."""

    human_survey_uuid: str
    # Survey logic that decides a screenout from the recorded entries. Runs on the
    # server after the response is saved; the browser never chooses the outcome.
    screenout_rule: Optional[Callable[[dict[str, Any]], bool]] = None

    def entry_path(self) -> str:
        return f"/cint/s/{self.human_survey_uuid}/entry"

    def live_url(self, public_base_url: str) -> str:
        """The live_url to put on the target group; Cint fills [%RID%]."""
        return f"{public_base_url}{self.entry_path()}?rid=[%RID%]"

    def entry_url(self, public_base_url: str, rid: str) -> str:
        return f"{public_base_url}{self.entry_path()}?rid={rid}"


@dataclass
class FinalizeResult:
    state: str
    redirect_url: Optional[str] = None  # set only after a confirmed transition
    detail: str = ""


# Optional rule that may turn an 'unknown' transition into a confirmed one from a
# GET observation. None by default: Cint has not published which GET value, if any,
# proves that a transition was applied (README question 1). Plug a Cint-confirmed
# rule here; until then an operator decides with operator_resolve().
RecoveryRule = Callable[[dict[str, Any], S2SResponse], Optional[bool]]


def _href_matches(href: str, expected: str, rid: str) -> bool:
    """Local policy: same scheme, host and path, and rid query value equal to the RID.

    The guide says to check that links[].href "is the accurate entry link"; the exact
    comparison Cint expects is not specified."""
    a, b = urlsplit(href), urlsplit(expected)
    if (a.scheme, a.netloc.lower(), a.path) != (b.scheme, b.netloc.lower(), b.path):
        return False
    got = parse_qs(a.query).get("rid", [""])[0]
    return got.lower() == rid.lower()


class RespondentFlow:
    def __init__(
        self,
        db: Database,
        s2s: S2SClient,
        settings: Settings,
        bindings: dict[str, SurveyBinding],
        recovery_rule: Optional[RecoveryRule] = None,
    ):
        self.db, self.s2s, self.settings = db, s2s, settings
        self.bindings = bindings
        self.recovery_rule = recovery_rule

    # 1. Entry -------------------------------------------------------------------
    def admit(self, human_survey_uuid: str, raw_rid: str) -> dict[str, Any]:
        binding = self.bindings.get(human_survey_uuid)
        if binding is None:
            raise AdmissionRefused("unknown survey")
        try:
            rid = str(uuid.UUID(raw_rid))
        except (ValueError, TypeError):
            raise AdmissionRefused("malformed rid")

        existing = self.session_state(rid)
        if existing is not None:
            return self._check_existing(existing, human_survey_uuid)

        try:
            r = self.s2s.get_respondent(rid)
        except (S2SNotSent, S2SAmbiguous) as e:
            raise AdmissionRefused(f"cint unreachable: {e}")
        if r.http_status == 404:
            raise AdmissionRefused("unknown rid")
        if r.http_status != 200 or not r.body:
            raise AdmissionRefused(f"s2s validation returned {r.http_status}")
        if not s2s_get_allows_entry(r.body.get("status")):
            raise AdmissionRefused(f"rid status {r.body.get('status')} is not in-survey")
        expected = binding.entry_url(self.settings.public_base_url, rid)
        hrefs = [l.get("href", "") for l in r.body.get("links") or []]
        match = next((h for h in hrefs if _href_matches(h, expected, rid)), None)
        if match is None:
            raise AdmissionRefused("entry link does not match this survey")

        with self.db.tx() as conn:
            row = conn.execute(
                """INSERT INTO cint_sessions (rid, human_survey_uuid, entry_href)
                   VALUES (%s, %s, %s) ON CONFLICT (rid) DO NOTHING RETURNING *""",
                (rid, human_survey_uuid, match),
            ).fetchone()
        if row is None:  # concurrent admission of the same RID
            return self._check_existing(self.session_state(rid), human_survey_uuid)
        return row

    @staticmethod
    def _check_existing(row: dict[str, Any], human_survey_uuid: str) -> dict[str, Any]:
        if row["human_survey_uuid"] != human_survey_uuid:
            raise AdmissionRefused("rid already bound to another survey")
        if row["response_saved_at"] is not None:
            raise AdmissionRefused("response already recorded for this rid")
        return row  # reload before answering: resume the same session

    # 2. Response persistence ------------------------------------------------------
    def save_response(
        self,
        human_survey_uuid: str,
        raw_rid: str,
        response_uuid: str,
        entries: dict[str, Any],
        scenario: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        rid = str(uuid.UUID(raw_rid))
        binding = self.bindings[human_survey_uuid]
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM cint_sessions WHERE rid = %s FOR UPDATE", (rid,)
            ).fetchone()
            if row is None or row["human_survey_uuid"] != human_survey_uuid:
                raise AdmissionRefused("no admitted session for this rid and survey")
            if row["response_uuid"] is not None:
                if row["response_uuid"] == response_uuid and row["response_entries"] == entries:
                    return row  # same submission replayed
                raise ResponseConflict("a different response is already recorded")
            conn.execute(
                """UPDATE cint_sessions SET response_uuid = %s, response_entries = %s,
                       scenario = %s, response_saved_at = now(), updated_at = now()
                   WHERE rid = %s""",
                (response_uuid, Jsonb(entries), Jsonb(scenario) if scenario is not None else None, rid),
            )
            outcome = (
                Outcome.SCREENOUT
                if binding.screenout_rule and binding.screenout_rule(entries)
                else Outcome.COMPLETE
            )
            self._record_outcome(conn, rid, outcome, "survey_logic")
            return conn.execute("SELECT * FROM cint_sessions WHERE rid = %s", (rid,)).fetchone()

    def record_outcome(self, raw_rid: str, outcome: Outcome, decided_by: str) -> dict[str, Any]:
        """Server-side decision (survey logic, quality check). First decision wins."""
        rid = str(uuid.UUID(raw_rid))
        with self.db.tx() as conn:
            return self._record_outcome(conn, rid, Outcome(outcome), decided_by)

    @staticmethod
    def _record_outcome(conn, rid: str, outcome: Outcome, decided_by: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM cint_sessions WHERE rid = %s FOR UPDATE", (rid,)).fetchone()
        if row is None or row["response_saved_at"] is None:
            raise NoSavedResponse("no recorded response: no outcome can be decided")
        if row["outcome"] is None:
            return conn.execute(
                """UPDATE cint_sessions SET outcome = %s, outcome_decided_by = %s,
                       transition_state = 'pending', updated_at = now()
                   WHERE rid = %s RETURNING *""",
                (outcome.value, decided_by, rid),
            ).fetchone()
        if row["outcome"] == outcome.value:
            return row
        conflict = {"outcome": outcome.value, "decided_by": decided_by}
        return conn.execute(
            """UPDATE cint_sessions
                  SET outcome_conflicts = outcome_conflicts || %s::jsonb, updated_at = now()
                WHERE rid = %s RETURNING *""",
            (Jsonb([conflict]), rid),
        ).fetchone()

    # 3. Finalization --------------------------------------------------------------
    def finalize(self, raw_rid: str) -> FinalizeResult:
        """Send the decided outcome to Cint once, durably. Ignores any browser status."""
        rid = str(uuid.UUID(raw_rid))
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM cint_sessions WHERE rid = %s FOR UPDATE", (rid,)
            ).fetchone()
            if row is None:
                raise AdmissionRefused("unknown session")
            if row["response_saved_at"] is None:
                raise NoSavedResponse("no recorded response: no transition is sent")
            state = row["transition_state"]
            if state == "confirmed":
                return FinalizeResult("confirmed", self._callback(rid), "already confirmed")
            if state in ("in_flight", "failed"):
                return FinalizeResult(state, None, "no new attempt from this caller")
            if row["transition_attempts"] >= self.settings.max_transition_attempts:
                return FinalizeResult(state, None, "attempts exhausted: operator decision needed")
            previous = state  # 'pending' or 'unknown'
            row = conn.execute(
                """UPDATE cint_sessions SET transition_state = 'in_flight',
                       transition_attempts = transition_attempts + 1,
                       in_flight_since = now(), updated_at = now()
                   WHERE rid = %s RETURNING *""",
                (rid,),
            ).fetchone()
        # The claim is committed before any network call: a crash from here on leaves
        # an 'in_flight' row that recover() turns into 'unknown', never into success.
        return self._send_transition(rid, Outcome(row["outcome"]), previous)

    def _send_transition(self, rid: str, outcome: Outcome, previous: str) -> FinalizeResult:
        code = to_s2s_transition_code(outcome)
        http: Optional[int] = None
        try:
            r = self.s2s.transition(rid, code)
            http = r.http_status
        except S2SNotSent as e:
            return self._settle(rid, previous, None, f"not sent: {e}")
        except S2SAmbiguous as e:
            return self._settle(rid, "unknown", None, f"no response after send: {e}")

        if http == 200:
            body = r.body or {}
            if body.get("id", rid).lower() == rid and body.get("status", code) == code:
                return self._settle(rid, "confirmed", http, None)
            return self._settle(rid, "unknown", http, f"200 with unexpected body {body}")
        if http in (404, 422):
            # After an ambiguous attempt, an error cannot be told apart from
            # "already applied" (the guide says a same-status retry errors), so the
            # state stays unknown. On a first clean attempt it is a failure, never
            # a success.
            new_state = "unknown" if previous == "unknown" else "failed"
            return self._settle(rid, new_state, http, f"s2s returned {http}")
        return self._settle(rid, "unknown", http, f"s2s returned {http}")

    def _settle(self, rid: str, state: str, http: Optional[int], error: Optional[str]) -> FinalizeResult:
        with self.db.tx() as conn:
            conn.execute(
                """UPDATE cint_sessions SET transition_state = %s,
                       last_transition_http = %s, last_transition_error = %s,
                       confirmed_at = CASE WHEN %s = 'confirmed' THEN now() END,
                       confirmed_by = CASE WHEN %s = 'confirmed' THEN 's2s_200' END,
                       in_flight_since = NULL, updated_at = now()
                   WHERE rid = %s AND transition_state = 'in_flight'""",
                (state, http, error, state, state, rid),
            )
        redirect = self._callback(rid) if state == "confirmed" else None
        return FinalizeResult(state, redirect, error or "")

    def _callback(self, rid: str) -> str:
        return self.settings.callback_url_template.format(rid=rid)

    # 4. Recovery and status -------------------------------------------------------
    def recover(self) -> dict[str, list[str]]:
        """Run by a worker after a restart, or periodically."""
        report: dict[str, list[str]] = {"stale_in_flight": [], "sent": [], "observed": []}
        with self.db.tx() as conn:
            rows = conn.execute(
                """UPDATE cint_sessions SET transition_state = 'unknown',
                       last_transition_error = 'process stopped during the S2S call',
                       in_flight_since = NULL, updated_at = now()
                   WHERE transition_state = 'in_flight'
                     AND in_flight_since < now() - make_interval(secs => %s)
                   RETURNING rid""",
                (self.settings.in_flight_lease_seconds,),
            ).fetchall()
            report["stale_in_flight"] = [str(r["rid"]) for r in rows]
            todo = conn.execute(
                """SELECT rid, transition_state FROM cint_sessions
                   WHERE transition_state IN ('pending','unknown') ORDER BY updated_at"""
            ).fetchall()
        for r in todo:
            rid = str(r["rid"])
            result = self.finalize(rid)
            report["sent"].append(f"{rid}:{result.state}")
            if result.state == "unknown":
                self.observe(rid)
                report["observed"].append(rid)
        return report

    def observe(self, rid: str) -> Optional[S2SResponse]:
        """Record what GET says. Evidence only: it never confirms a transition."""
        try:
            r = self.s2s.get_respondent(rid)
        except (S2SNotSent, S2SAmbiguous):
            return None
        status = (r.body or {}).get("status") if r.http_status == 200 else None
        with self.db.tx() as conn:
            row = conn.execute(
                """UPDATE cint_sessions SET observed_s2s_status = %s, observed_s2s_at = now()
                   WHERE rid = %s RETURNING *""",
                (status, rid),
            ).fetchone()
        if self.recovery_rule is not None and row is not None and row["transition_state"] == "unknown":
            verdict = self.recovery_rule(row, r)
            if verdict is True:
                self._resolve(rid, "confirmed", "cint_rule")
        return r

    def operator_resolve(self, raw_rid: str, confirmed: bool, operator: str) -> dict[str, Any]:
        """Human decision for an unknown or failed transition, recorded with its author."""
        rid = str(uuid.UUID(raw_rid))
        return self._resolve(rid, "confirmed" if confirmed else "failed", f"operator:{operator}")

    def _resolve(self, rid: str, state: str, by: str) -> dict[str, Any]:
        with self.db.tx() as conn:
            row = conn.execute(
                """UPDATE cint_sessions SET transition_state = %s,
                       confirmed_at = CASE WHEN %s = 'confirmed' THEN now() END,
                       confirmed_by = CASE WHEN %s = 'confirmed' THEN %s END,
                       updated_at = now()
                   WHERE rid = %s AND transition_state IN ('unknown','failed') RETURNING *""",
                (state, state, state, by, rid),
            ).fetchone()
        if row is None:
            raise ValueError("only an unknown or failed transition can be resolved")
        return row

    def session_state(self, rid: str) -> Optional[dict[str, Any]]:
        with self.db.tx() as conn:
            return conn.execute("SELECT * FROM cint_sessions WHERE rid = %s", (rid,)).fetchone()
