"""Ordinary bad inputs get a clean 4xx, never a 500."""

from tests.conftest import ANSWERS, SURVEY

UNKNOWN_SURVEY = "00000000-0000-4000-8000-000000000000"


def test_finish_with_malformed_rid(h):
    assert h.client.post(f"/cint/s/{SURVEY}/finish", params={"rid": "abc"}).status_code == 403


def test_session_lookup_with_malformed_rid(h):
    assert h.client.get("/cint/sessions/abc").status_code == 404


def test_response_with_malformed_rid(h):
    r = h.client.post(f"/cint/s/{SURVEY}/responses",
                      json={"rid": "abc", "response_uuid": "x", "entries": ANSWERS})
    assert r.status_code == 403


def test_response_for_unknown_survey(h):
    rid = h.new_respondent()
    h.admit(rid)
    r = h.client.post(f"/cint/s/{UNKNOWN_SURVEY}/responses",
                      json={"rid": rid, "response_uuid": "x", "entries": ANSWERS})
    assert r.status_code == 403
