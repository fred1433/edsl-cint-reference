"""Respondent flow: admission, response persistence, host decision, durable reporting.

Proposed attachment points on the survey host (not inspected, proposed by analogy
with EDSL's public client):

1. survey entry           -> RespondentFlow.admit
2. response persistence   -> RespondentFlow.save_response   (stores, decides nothing)
3. host decision          -> SurveyBinding.decide / RespondentFlow.record_outcome
4. outcome reporting      -> RespondentFlow.finalize, recover, session_state

Not implemented: returning a respondent refused at entry to Cint. That path needs a
host decision and a security-termination code that Cint has not specified (README,
questions for Cint).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

import psycopg
from edsl.results.human_responses import ENTRIES
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .config import Settings
from .db import Database
from .outcomes import Outcome, s2s_get_allows_entry, to_s2s_transition_code
from .s2s_client import S2SAmbiguous, S2SClient, S2SNotSent, S2SResponse
from .webhooks import replay_unmatched_session_events


class AdmissionRefused(Exception):
    pass


class NoSavedResponse(Exception):
    pass


class ResponseConflict(Exception):
    pass


class InvalidResponse(Exception):
    pass


def parse_rid(raw_rid: object) -> str:
    """Canonical RID string, or AdmissionRefused for anything that is not a UUID."""
    try:
        return str(uuid.UUID(str(raw_rid)))
    except (ValueError, TypeError):
        raise AdmissionRefused("malformed rid")


# Trusted host decision: given the stored session row, return a final outcome, or
# None while the response is not ready to be reported. It runs on the server; the
# browser never supplies it.
HostDecision = Callable[[dict[str, Any]], Optional[Outcome]]


def complete_when_answered(required: Iterable[str],
                           screenout_rule: Optional[Callable[[dict], bool]] = None) -> HostDecision:
    """EXAMPLE policy used by the tests and the example. The real decision belongs to
    the host's survey engine: completion state, quality review, quota logic."""
    required = list(required)

    def decide(row: dict[str, Any]) -> Optional[Outcome]:
        entries = row["response_entries"] or {}
        if screenout_rule and screenout_rule(entries):
            return Outcome.SCREENOUT
        done = all((entries.get(q) or {}).get("question_presented") is True
                   and (entries.get(q) or {}).get("answer") is not None for q in required)
        return Outcome.COMPLETE if done else None

    return decide


@dataclass
class SurveyBinding:
    """Links one human survey to its Cint target group entry link."""

    human_survey_uuid: str
    decide: Optional[HostDecision] = None  # None: only record_outcome() decides

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
    redirect_url: Optional[str] = None  # set only when the return is authorized
    detail: str = ""


# Optional rule that may turn an 'unknown' transition into a confirmed one from a
# GET observation. None by default: Cint has not published which GET value, if any,
# proves that a transition was applied (README question). Until then an operator
# classifies the case with operator_resolve().
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

    def _binding(self, human_survey_uuid: str) -> SurveyBinding:
        binding = self.bindings.get(human_survey_uuid)
        if binding is None:
            raise AdmissionRefused("unknown survey")
        return binding

    # 1. Entry -------------------------------------------------------------------
    def admit(self, human_survey_uuid: str, raw_rid: str) -> dict[str, Any]:
        binding = self._binding(human_survey_uuid)
        rid = parse_rid(raw_rid)

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
            if row is not None:
                # Session webhooks that arrived before admission are projected now.
                replay_unmatched_session_events(conn, rid)
        if row is None:  # concurrent admission of the same RID
            return self._check_existing(self.session_state(rid), human_survey_uuid)
        return self.session_state(rid)

    @staticmethod
    def _check_existing(row: dict[str, Any], human_survey_uuid: str) -> dict[str, Any]:
        if row["human_survey_uuid"] != human_survey_uuid:
            raise AdmissionRefused("rid already bound to another survey")
        if row["response_saved_at"] is not None:
            raise AdmissionRefused("response already recorded for this rid")
        return row  # reload before answering: resume the same session

    # 2. Response persistence (no decision) ---------------------------------------
    def save_response(
        self,
        human_survey_uuid: str,
        raw_rid: str,
        response_uuid: str,
        entries: dict[str, Any],
        scenario: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        self._binding(human_survey_uuid)
        rid = parse_rid(raw_rid)
        try:
            # The exact parser EDSL applies to response_json_string when it builds
            # Results; a response it would reject is not accepted here.
            ENTRIES.validate_json(json.dumps(entries))
        except ValidationError as e:
            raise InvalidResponse(f"entries rejected by EDSL's HumanResponseEntry: {e.error_count()} error(s)")
        try:
            with self.db.tx() as conn:
                row = conn.execute(
                    "SELECT * FROM cint_sessions WHERE rid = %s FOR UPDATE", (rid,)
                ).fetchone()
                if row is None or row["human_survey_uuid"] != human_survey_uuid:
                    raise AdmissionRefused("no admitted session for this rid and survey")
                if row["response_uuid"] is not None:
                    same = (row["response_uuid"], row["response_entries"], row["scenario"]) == (
                        response_uuid, entries, scenario)
                    if same:
                        return row  # the same submission replayed
                    raise ResponseConflict("a different response is already recorded")
                return conn.execute(
                    """UPDATE cint_sessions SET response_uuid = %s, response_entries = %s,
                           scenario = %s, response_saved_at = now(), updated_at = now()
                       WHERE rid = %s RETURNING *""",
                    (response_uuid, Jsonb(entries),
                     Jsonb(scenario) if scenario is not None else None, rid),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise ResponseConflict("response_uuid already used by another session")

    # 3. Host decision -------------------------------------------------------------
    def decide(self, raw_rid: str) -> dict[str, Any]:
        """Ask the host's decision hook, once a response is saved. First decision wins."""
        rid = parse_rid(raw_rid)
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM cint_sessions WHERE rid = %s FOR UPDATE", (rid,)).fetchone()
            if row is None:
                raise AdmissionRefused("unknown session")
            if row["outcome"] is not None or row["response_saved_at"] is None:
                return row
            hook = self.bindings[row["human_survey_uuid"]].decide
            outcome = hook(row) if hook else None
            if outcome is None:
                return row  # not ready
            return self._record_outcome(conn, rid, Outcome(outcome), "host_decision")

    def record_outcome(self, raw_rid: str, outcome: Outcome, decided_by: str) -> dict[str, Any]:
        """A trusted host decision (quality review, quota logic). First decision wins;
        a different later decision is kept as a visible conflict."""
        rid = parse_rid(raw_rid)
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

    # 4. Reporting -----------------------------------------------------------------
    def finalize(self, raw_rid: str) -> FinalizeResult:
        """Report the host's decision to Cint once, durably. Ignores any browser status."""
        rid = parse_rid(raw_rid)
        self.decide(rid)
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM cint_sessions WHERE rid = %s FOR UPDATE", (rid,)
            ).fetchone()
            if row is None:
                raise AdmissionRefused("unknown session")
            if row["response_saved_at"] is None:
                raise NoSavedResponse("no recorded response: no transition is sent")
            if row["outcome"] is None:
                return FinalizeResult("not_ready", None, "no host decision yet")
            state = row["transition_state"]
            if state == "confirmed":
                return self._result(row, "already confirmed")
            if state in ("in_flight", "failed"):
                return FinalizeResult(state, None, "no new attempt from this caller")
            if row["transition_attempts"] >= self.settings.max_transition_attempts:
                return FinalizeResult(state, None, "attempts exhausted: operator decision needed")
            previous, token = state, uuid.uuid4()  # 'pending' or 'unknown'
            conn.execute(
                """UPDATE cint_sessions SET transition_state = 'in_flight', attempt_token = %s,
                       transition_attempts = transition_attempts + 1,
                       in_flight_since = now(), updated_at = now()
                   WHERE rid = %s""",
                (token, rid),
            )
        # The claim is committed before any network call: a crash from here on leaves
        # an 'in_flight' row that recover() turns into 'unknown', never into success.
        return self._send_transition(rid, Outcome(row["outcome"]), previous, token)

    def _send_transition(self, rid: str, outcome: Outcome, previous: str,
                         token: uuid.UUID) -> FinalizeResult:
        code = to_s2s_transition_code(outcome)
        try:
            r = self.s2s.transition(rid, code)
        except S2SNotSent as e:
            # Nothing reached Cint: back to the previous state, and the attempt is not
            # counted, so an outage cannot exhaust the retry budget.
            return self._settle(rid, token, previous, None, f"not sent: {e}", count_attempt=False)
        except S2SAmbiguous as e:
            return self._settle(rid, token, "unknown", None, f"no response after send: {e}")

        http = r.http_status
        if http == 200:
            body = r.body or {}
            if str(body.get("id", rid)).lower() == rid and body.get("status", code) == code:
                return self._settle(rid, token, "confirmed", http, None)
            return self._settle(rid, token, "unknown", http, f"200 with unexpected body {body}")
        if http in (404, 422):
            # After an ambiguous attempt, an error cannot be told apart from
            # "already applied" (the guide says a same-status retry errors), so the
            # state stays unknown. On a first clean attempt it is a failure, never
            # a success.
            new_state = "unknown" if previous == "unknown" else "failed"
            return self._settle(rid, token, new_state, http, f"s2s returned {http}")
        return self._settle(rid, token, "unknown", http, f"s2s returned {http}")

    def _settle(self, rid: str, token: uuid.UUID, state: str, http: Optional[int],
                error: Optional[str], count_attempt: bool = True) -> FinalizeResult:
        """Apply this attempt's result only if the attempt still owns the session.
        The answer given to the caller is derived from the row actually committed."""
        confirmed = state == "confirmed"
        with self.db.tx() as conn:
            row = conn.execute(
                """UPDATE cint_sessions SET transition_state = %s,
                       transition_attempts = transition_attempts - %s,
                       last_transition_http = %s, last_transition_error = %s,
                       confirmed_at = CASE WHEN %s THEN now() END,
                       confirmed_by = CASE WHEN %s THEN 's2s_200' END,
                       return_authorized = %s,
                       return_authorized_by = CASE WHEN %s THEN 's2s_200' END,
                       attempt_token = NULL, in_flight_since = NULL, updated_at = now()
                   WHERE rid = %s AND transition_state = 'in_flight' AND attempt_token = %s
                   RETURNING *""",
                (state, 0 if count_attempt else 1, http, error,
                 confirmed, confirmed, confirmed, confirmed, rid, token),
            ).fetchone()
            if row is None:
                # Late reply: this attempt no longer owns the session (expired by
                # recover(), resolved by an operator, or claimed by a newer attempt).
                late = {"attempt": str(token), "would_be": state, "http": http, "error": error}
                row = conn.execute(
                    """UPDATE cint_sessions SET late_replies = late_replies || %s::jsonb
                       WHERE rid = %s RETURNING *""",
                    (Jsonb([late]), rid),
                ).fetchone()
                return FinalizeResult(row["transition_state"], None,
                                      "late reply recorded, not applied: attempt no longer owns the session")
        return self._result(row, error or "")

    def _result(self, row: dict[str, Any], detail: str = "") -> FinalizeResult:
        authorized = row["transition_state"] == "confirmed" and row["return_authorized"]
        redirect = self._callback(str(row["rid"])) if authorized else None
        if row["transition_state"] == "confirmed" and not authorized:
            detail = "confirmed, but return to Cint not authorized"
        return FinalizeResult(row["transition_state"], redirect, detail)

    def _callback(self, rid: str) -> str:
        return self.settings.callback_url_template.format(rid=rid)

    def recover(self, only_rids: Optional[list[str]] = None) -> dict[str, list[str]]:
        """Run by a worker after a restart, or periodically. only_rids limits the scan."""
        scope = [parse_rid(r) for r in only_rids] if only_rids is not None else None
        report: dict[str, list[str]] = {"stale_in_flight": [], "sent": [], "observed": []}
        with self.db.tx() as conn:
            rows = conn.execute(
                """UPDATE cint_sessions SET transition_state = 'unknown', attempt_token = NULL,
                       last_transition_error = 'attempt expired: process stopped or stalled during the S2S call',
                       in_flight_since = NULL, updated_at = now()
                   WHERE transition_state = 'in_flight'
                     AND in_flight_since < now() - make_interval(secs => %s)
                     AND (%s::uuid[] IS NULL OR rid = ANY(%s::uuid[]))
                   RETURNING rid""",
                (self.settings.in_flight_lease_seconds, scope, scope),
            ).fetchall()
            report["stale_in_flight"] = [str(r["rid"]) for r in rows]
            todo = conn.execute(
                """SELECT rid FROM cint_sessions
                   WHERE transition_state IN ('pending','unknown')
                     AND (%s::uuid[] IS NULL OR rid = ANY(%s::uuid[]))
                   ORDER BY updated_at""",
                (scope, scope),
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
            if self.recovery_rule(row, r) is True:
                self._resolve(rid, True, "cint_rule", authorize_return=True)
        return r

    def operator_resolve(self, raw_rid: str, confirmed: bool, operator: str) -> dict[str, Any]:
        """Human classification of an unknown or failed transition, with author and time.
        It does NOT authorize returning the respondent to Cint (see authorize_return)."""
        return self._resolve(parse_rid(raw_rid), confirmed, f"operator:{operator}", authorize_return=False)

    def authorize_return(self, raw_rid: str, operator: str) -> dict[str, Any]:
        """LOCAL POLICY, NOT VALIDATED BY CINT: let an operator send a respondent back
        to Cint without a 200 from the transition call. Cint's guide requires a
        successful status update before the callback; this exception exists only so
        a respondent is not stranded, and needs Cint's agreement (README question)."""
        rid = parse_rid(raw_rid)
        with self.db.tx() as conn:
            row = conn.execute(
                """UPDATE cint_sessions SET return_authorized = true,
                       return_authorized_by = %s, updated_at = now()
                   WHERE rid = %s AND transition_state = 'confirmed' RETURNING *""",
                (f"operator:{operator} (local policy)", rid),
            ).fetchone()
        if row is None:
            raise ValueError("only a confirmed transition can be authorized for return")
        return row

    def _resolve(self, rid: str, confirmed: bool, by: str, authorize_return: bool) -> dict[str, Any]:
        with self.db.tx() as conn:
            row = conn.execute(
                """UPDATE cint_sessions SET transition_state = %s,
                       confirmed_at = CASE WHEN %s THEN now() END,
                       confirmed_by = CASE WHEN %s THEN %s END,
                       return_authorized = %s,
                       return_authorized_by = CASE WHEN %s THEN %s END,
                       resolved_by = %s, resolved_at = now(), updated_at = now()
                   WHERE rid = %s AND transition_state IN ('unknown','failed') RETURNING *""",
                ("confirmed" if confirmed else "failed", confirmed, confirmed, by,
                 authorize_return, authorize_return, by, by, rid),
            ).fetchone()
        if row is None:
            raise ValueError("only an unknown or failed transition can be resolved")
        return row

    def session_state(self, rid: str) -> Optional[dict[str, Any]]:
        with self.db.tx() as conn:
            return conn.execute("SELECT * FROM cint_sessions WHERE rid = %s", (rid,)).fetchone()
