"""Admission binds a RID to the right survey; the browser never decides an outcome."""

import threading

from tests.conftest import ANSWERS, SCREENER_SURVEY, SURVEY


def test_valid_rid_is_admitted_once(h, fake_s2s):
    rid = h.new_respondent()
    r = h.admit(rid)
    assert r.status_code == 200 and r.json()["admitted"] is True
    assert h.admit(rid).status_code == 200  # reload before answering resumes
    assert sum(1 for c in fake_s2s.calls if c[0] == "GET") == 1


def test_unknown_rid_is_refused(h):
    r = h.admit("6780395e-94fa-4eb7-912c-d2e19af42758")
    assert r.status_code == 403 and r.json()["reason"] == "unknown rid"


def test_valid_rid_for_another_survey_is_refused(h):
    rid = h.new_respondent(survey=SCREENER_SURVEY, href_survey=SURVEY)
    r = h.admit(rid, survey=SCREENER_SURVEY)
    assert r.status_code == 403 and "entry link" in r.json()["reason"]
    assert h.flow.session_state(rid) is None


def test_rid_already_bound_cannot_switch_survey(h):
    rid = h.new_respondent()
    h.admit(rid)
    r = h.admit(rid, survey=SCREENER_SURVEY)
    assert r.status_code == 403 and "another survey" in r.json()["reason"]


def test_rid_not_in_survey_is_refused(h):
    rid = h.new_respondent(status=3)
    assert h.admit(rid).status_code == 403


def test_completion_without_saved_response_sends_nothing(h, fake_s2s):
    rid = h.new_respondent()
    h.admit(rid)
    r = h.finish(rid, status="complete")
    assert r.status_code == 409
    assert fake_s2s.transitions_sent() == []


def test_browser_status_is_ignored(h, fake_s2s):
    no_pet = {**ANSWERS, "pet": {**ANSWERS["pet"], "answer": "None"}}
    rid = h.answered(SCREENER_SURVEY, entries=no_pet)
    h.finish(rid, survey=SCREENER_SURVEY, status="complete")
    assert fake_s2s.transitions_sent() == [{"id": rid, "status": 2}]  # screenout


def test_response_is_recorded_once(h):
    rid = h.answered()
    assert h.save(rid).status_code == 200  # same submission replayed
    other = {**ANSWERS, "pet": {**ANSWERS["pet"], "answer": "Cat"}}
    assert h.save(rid, entries=other).status_code == 409
    assert h.flow.session_state(rid)["response_entries"] == ANSWERS


def test_concurrent_admissions_create_one_session(h, db):
    rid = h.new_respondent()
    barrier = threading.Barrier(3)
    codes = []

    def enter():
        barrier.wait()
        codes.append(h.admit(rid).status_code)

    threads = [threading.Thread(target=enter) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200, 200, 200]
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) AS n FROM cint_sessions").fetchone()["n"] == 1
