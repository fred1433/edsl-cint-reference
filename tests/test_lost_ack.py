"""The principal failure: Cint applies the completion, the response is lost,
the survey host restarts, and the retry gets an error."""

from edsl.results import Results

from cint_ref.edsl_bridge import to_human_response_row
from tests.conftest import ANSWERS, SURVEY


def test_lost_completion_ack_keeps_response_and_uncertainty(h, fake_s2s, edsl_survey):
    rid = h.answered()

    # Cint commits status 5, then the HTTP response never arrives.
    fake_s2s.inject("commit_then_drop")
    first = h.finish(rid, status="complete")
    assert first.status_code == 202 and "location" not in first.headers
    assert fake_s2s.respondents[rid].applied == [5]  # remote side did apply it

    # Process restart; the recovery worker retries the same transition.
    h.start()
    h.flow.recover()
    row = h.flow.session_state(rid)

    # Cint refuses the same-status retry, so the error cannot prove anything.
    assert row["last_transition_http"] == 422
    assert row["transition_state"] == "unknown"
    assert row["confirmed_at"] is None and row["confirmed_by"] is None
    # The GET probe is recorded as evidence and not interpreted.
    assert row["observed_s2s_status"] == 99
    # The recorded answer survives intact.
    assert row["response_entries"] == ANSWERS
    assert len(fake_s2s.transitions_sent()) == 2

    # The respondent reloads: still no redirect to Cint.
    again = h.finish(rid)
    assert again.status_code == 202 and "location" not in again.headers

    # The response stays usable in EDSL while the outcome is unresolved.
    results = Results.from_human_responses(edsl_survey, [to_human_response_row(row)])
    assert results.select("answer.pet").to_list() == ["Dog"]

    # Only an explicit, attributed decision closes it (or a Cint-confirmed rule
    # plugged into RespondentFlow.recovery_rule).
    resolved = h.flow.operator_resolve(rid, confirmed=True, operator="ops-on-call")
    assert resolved["confirmed_by"] == "operator:ops-on-call"
    done = h.finish(rid)
    assert done.status_code == 303
    assert done.headers["location"] == f"https://samplicio.us/s/ClientCallBack.aspx?RID={rid}"
    assert len(fake_s2s.transitions_sent()) == 3  # nothing sent after the decision
