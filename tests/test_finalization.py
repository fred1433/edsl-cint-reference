"""Five finalization situations, plus concurrency on a real PostgreSQL."""

import threading

import pytest

from cint_ref.outcomes import Outcome
from fake_cint.s2s import ProcessDied


def test_confirmed_ack_is_reused_not_resent(h, fake_s2s):
    rid = h.answered()
    r1 = h.finish(rid)
    r2 = h.finish(rid)
    assert r1.status_code == r2.status_code == 303
    assert r1.headers["location"] == r2.headers["location"]
    assert fake_s2s.transitions_sent() == [{"id": rid, "status": 5}]
    assert h.flow.session_state(rid)["confirmed_by"] == "s2s_200"


def test_undetermined_outcome_stays_unknown(h, fake_s2s):
    rid = h.answered()
    fake_s2s.inject("commit_then_drop")
    r = h.finish(rid)
    assert r.status_code == 202 and r.json()["state"] == "unknown"
    assert h.flow.session_state(rid)["confirmed_at"] is None


def test_request_that_never_left_is_retried_safely(h, fake_s2s):
    rid = h.answered()
    fake_s2s.inject("not_sent")
    assert h.finish(rid).json()["state"] == "pending"
    assert h.finish(rid).status_code == 303
    assert fake_s2s.respondents[rid].applied == [5]


def test_conflicting_decisions_keep_one_and_report_the_other(h, fake_s2s):
    rid = h.answered()  # survey logic decided 'complete'
    row = h.flow.record_outcome(rid, Outcome.QUALITY_TERMINATE, "quality_check")
    assert row["outcome"] == "complete"
    assert row["outcome_conflicts"] == [{"outcome": "quality_terminate", "decided_by": "quality_check"}]
    h.finish(rid)
    assert fake_s2s.transitions_sent() == [{"id": rid, "status": 5}]


def test_crash_after_saving_is_found_and_finished_after_restart(h, fake_s2s):
    rid = h.answered()           # saved, outcome decided, nothing sent
    h.start()                    # process restart
    report = h.flow.recover()
    assert report["sent"] == [f"{rid}:confirmed"]
    assert fake_s2s.respondents[rid].applied == [5]


def test_crash_during_the_call_becomes_unknown_then_retries(h, fake_s2s):
    rid = h.answered()
    fake_s2s.inject("crash_before_send")
    with pytest.raises(ProcessDied):
        h.flow.finalize(rid)
    assert h.flow.session_state(rid)["transition_state"] == "in_flight"
    h.start(in_flight_lease_seconds=0)
    report = h.flow.recover()
    assert report["stale_in_flight"] == [rid]
    # Here the request had not reached Cint, so the retry is accepted.
    assert h.flow.session_state(rid)["transition_state"] == "confirmed"


def test_unrelated_422_is_not_a_success(h, fake_s2s):
    rid = h.answered()
    fake_s2s.inject("unrelated_422")
    r = h.finish(rid)
    assert r.status_code == 202 and r.json()["state"] == "failed"
    row = h.flow.session_state(rid)
    assert row["confirmed_at"] is None and row["last_transition_http"] == 422


def test_concurrent_finalizations_send_one_transition(settings, fake_s2s):
    from tests.conftest import Harness

    fake_s2s.latency = 0.3
    h = Harness(settings, fake_s2s)
    rid = h.answered()
    barrier = threading.Barrier(4)
    states = []

    def worker():
        barrier.wait()
        states.append(h.flow.finalize(rid).state)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fake_s2s.transitions_sent()) == 1
    assert "confirmed" in states and set(states) <= {"confirmed", "in_flight"}


def test_requests_that_never_left_do_not_exhaust_attempts(h, fake_s2s):
    rid = h.answered()
    fake_s2s.inject("not_sent", "not_sent", "not_sent", "not_sent")
    for _ in range(4):
        assert h.finish(rid).json()["state"] == "pending"
    row = h.flow.session_state(rid)
    assert row["transition_attempts"] == 0 and fake_s2s.respondents[rid].applied == []
    assert h.finish(rid).status_code == 303  # Cint reachable again: sent and confirmed


def test_recovery_can_be_scoped_to_given_rids(h, fake_s2s):
    mine, other = h.answered(), h.answered()
    report = h.flow.recover(only_rids=[mine])
    assert report["sent"] == [f"{mine}:confirmed"]
    assert h.flow.session_state(other)["transition_state"] == "pending"
