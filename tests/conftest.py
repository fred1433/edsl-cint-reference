import os
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cint_ref.app import create_app
from cint_ref.config import Settings
from cint_ref.db import Database
from cint_ref.respondent_flow import SurveyBinding
from fake_cint.s2s import FakeS2S

DB_URL = os.environ.get("DATABASE_URL", "postgresql:///edsl_cint_reference_test")

SURVEY = "3f1c9a52-7d1e-4c0b-9a51-2f8e6b1d0a11"        # synthetic human survey uuid
SCREENER_SURVEY = "8a2d4e61-1b3c-4f5d-8e7a-9c0b1d2e3f40"
# The example secret published in the spec's WebhookSecret schema; not a credential.
PUBLISHED_SECRET = "FP7UPZpATKBcYJj4va95PYud+a3gCidRERiLO+9MoFE="

# What the survey host records for one respondent, in EDSL's HumanResponseEntry shape:
# an answered question, a question skipped by survey logic, options shown in the
# order the respondent saw them.
ANSWERS = {
    "pet": {"answer": "Dog", "comment": None, "started_at": "2026-10-08T10:00:00Z",
            "answered_at": "2026-10-08T10:00:07Z", "question_presented": True,
            "question_options": ["Fish", "Dog", "Cat"]},
    "why_no_pet": {"answer": None, "comment": None, "started_at": None,
                   "answered_at": None, "question_presented": False, "question_options": None},
    "brand_opinion": {"answer": "Good", "comment": "bought it twice",
                      "started_at": "2026-10-08T10:00:08Z", "answered_at": "2026-10-08T10:00:15Z",
                      "question_presented": True, "question_options": ["Good", "Bad"]},
}
SCENARIO = {"brand": "Acme Kibble"}


def bindings():
    return {
        SURVEY: SurveyBinding(SURVEY),
        SCREENER_SURVEY: SurveyBinding(
            SCREENER_SURVEY,
            screenout_rule=lambda entries: (entries.get("pet") or {}).get("answer") == "None",
        ),
    }


@pytest.fixture(scope="session")
def db():
    d = Database(DB_URL)
    d.migrate()
    return d


@pytest.fixture(autouse=True)
def clean(db):
    with db.tx() as conn:
        conn.execute("TRUNCATE cint_sessions, cint_webhook_inbox, cint_quota_progress, cint_launches")


@pytest.fixture
def settings():
    return Settings(database_url=DB_URL, webhook_secret=PUBLISHED_SECRET)


class Harness:
    """The survey host as a process: start() again simulates a restart.
    The fake Cint keeps its state across restarts, like the real remote system."""

    def __init__(self, settings: Settings, fake: FakeS2S):
        self.settings, self.fake = settings, fake
        self.start()

    def start(self, **overrides):
        if overrides:
            self.settings = replace(self.settings, **overrides)
        self.app = create_app(self.settings, bindings(), self.fake.transport)
        self.client = TestClient(self.app, raise_server_exceptions=False, follow_redirects=False)
        self.flow = self.app.state.flow

    def new_respondent(self, survey=SURVEY, status=1, href_survey=None) -> str:
        rid = str(uuid.uuid4())
        href = bindings()[href_survey or survey].entry_url(self.settings.public_base_url, rid)
        self.fake.add_respondent(rid, href, status)
        return rid

    def admit(self, rid, survey=SURVEY):
        return self.client.get(f"/cint/s/{survey}/entry", params={"rid": rid})

    def save(self, rid, survey=SURVEY, entries=None, response_uuid=None):
        return self.client.post(f"/cint/s/{survey}/responses", json={
            "rid": rid, "response_uuid": response_uuid or f"resp-{rid[:8]}",
            "entries": entries or ANSWERS, "scenario": SCENARIO})

    def finish(self, rid, survey=SURVEY, **params):
        return self.client.post(f"/cint/s/{survey}/finish", params={"rid": rid, **params})

    def answered(self, survey=SURVEY, entries=None) -> str:
        rid = self.new_respondent(survey)
        assert self.admit(rid, survey).status_code == 200
        assert self.save(rid, survey, entries).status_code == 200
        return rid


@pytest.fixture
def fake_s2s(settings):
    return FakeS2S(settings.s2s_api_key)


@pytest.fixture
def h(settings, fake_s2s):
    return Harness(settings, fake_s2s)


@pytest.fixture
def edsl_survey():
    from edsl import QuestionFreeText, QuestionMultipleChoice, Survey

    return Survey([
        QuestionMultipleChoice(question_name="pet", question_text="Which pet do you have?",
                               question_options=["Cat", "Dog", "Fish", "None"]),
        QuestionFreeText(question_name="why_no_pet", question_text="Why no pet?"),
        QuestionMultipleChoice(question_name="brand_opinion",
                               question_text="What do you think of {{ scenario.brand }}?",
                               question_options=["Good", "Bad"]),
    ]).add_skip_rule("why_no_pet", "{{ pet.answer }} != 'None'")
