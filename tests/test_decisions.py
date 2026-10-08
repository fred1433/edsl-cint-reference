"""Saving stores; a trusted host decision decides; reporting sends that decision."""

import pytest

from cint_ref.outcomes import Outcome
from tests.conftest import ANSWERS, SURVEY


def post_entries(h, rid, entries):
    return h.client.post(f"/cint/s/{SURVEY}/responses",
                         json={"rid": rid, "response_uuid": f"r-{rid[:8]}", "entries": entries})


@pytest.mark.parametrize("entries,status", [
    ({}, 200),                                            # parsed by EDSL, but not finished
    ({"pet": "Dog"}, 422),                                # rejected by EDSL's parser
    ({"pet": {"answer": "Dog", "comment": 42}}, 422),     # rejected by EDSL's parser
])
def test_incomplete_or_malformed_entries_never_produce_code_5(h, fake_s2s, entries, status):
    rid = h.new_respondent()
    h.admit(rid)
    assert post_entries(h, rid, entries).status_code == status
    r = h.finish(rid)
    assert r.status_code == 409 and "location" not in r.headers
    assert fake_s2s.transitions_sent() == []


def test_save_alone_decides_nothing(h):
    rid = h.answered()
    row = h.flow.session_state(rid)
    assert row["outcome"] is None and row["transition_state"] == "not_ready"


def test_quality_decision_before_finalization_sends_code_4(h, fake_s2s):
    rid = h.answered()
    h.flow.record_outcome(rid, Outcome.QUALITY_TERMINATE, "quality_review")
    h.finish(rid)
    assert fake_s2s.transitions_sent() == [{"id": rid, "status": 4}]


def test_quota_full_decision_sends_code_3(h, fake_s2s):
    rid = h.answered()
    h.flow.record_outcome(rid, Outcome.QUOTA_FULL, "host_quota_logic")
    h.finish(rid)
    assert fake_s2s.transitions_sent() == [{"id": rid, "status": 3}]


def test_identical_decision_repeated_is_idempotent(h):
    rid = h.answered()
    first = h.flow.record_outcome(rid, Outcome.QUALITY_TERMINATE, "quality_review")
    again = h.flow.record_outcome(rid, Outcome.QUALITY_TERMINATE, "quality_review")
    assert again["outcome"] == first["outcome"] == "quality_terminate"
    assert again["outcome_conflicts"] == []


def test_same_response_uuid_on_two_rids_is_a_clean_conflict(h):
    a, b = h.new_respondent(), h.new_respondent()
    h.admit(a), h.admit(b)
    assert h.save(a, response_uuid="shared").status_code == 200
    assert h.save(b, response_uuid="shared").status_code == 409


def test_replay_with_changed_scenario_is_a_conflict(h):
    rid = h.answered()
    assert h.save(rid).status_code == 200
    r = h.save(rid, scenario={"brand": "Other Brand"})
    assert r.status_code == 409
    assert h.flow.session_state(rid)["scenario"] == {"brand": "Acme Kibble"}
    assert h.flow.session_state(rid)["response_entries"] == ANSWERS


def test_negative_resolution_keeps_its_author(h, fake_s2s):
    rid = h.answered()
    fake_s2s.inject("commit_then_drop")
    h.finish(rid)
    row = h.flow.operator_resolve(rid, confirmed=False, operator="analyst-2")
    assert row["transition_state"] == "failed"
    assert row["resolved_by"] == "operator:analyst-2" and row["resolved_at"] is not None
