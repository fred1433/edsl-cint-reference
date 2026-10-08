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
    # The response stays usable in EDSL, and so does the uncertainty.
    results = Results.from_human_responses(edsl_survey, [to_human_response_row(row)])
    out = results.select("answer.pet", "agent.cint_intended_outcome",
                         "agent.cint_transition_state").to_dicts(remove_prefix=False)[0]
    assert out == {"answer.pet": "Dog", "agent.cint_intended_outcome": "complete",
                   "agent.cint_transition_state": "unknown"}

    # An operator may classify the case; that alone does not send anyone back.
    resolved = h.flow.operator_resolve(rid, confirmed=True, operator="ops-on-call")
    assert resolved["resolved_by"] == "operator:ops-on-call" and resolved["return_authorized"] is False
    assert h.finish(rid).status_code == 202
    # Returning the respondent without a 200 is a separate, local-policy authorization.
    authorized = h.flow.authorize_return(rid, operator="ops-on-call")
    assert authorized["return_authorized_by"] == "operator:ops-on-call (local policy)"
    done = h.finish(rid)
    assert done.status_code == 303
    assert done.headers["location"] == f"https://samplicio.us/s/ClientCallBack.aspx?RID={rid}"
    assert len(fake_s2s.transitions_sent()) == 3  # nothing sent after the decision
