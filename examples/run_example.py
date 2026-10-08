"""Walk one respondent through a lost completion acknowledgment, then a clean one.

    DATABASE_URL=postgresql:///edsl_cint_reference_test python examples/run_example.py

Cint is simulated in memory (fake_cint); PostgreSQL is real.
"""

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from cint_ref.app import create_app  # noqa: E402
from cint_ref.config import Settings  # noqa: E402
from cint_ref.db import Database  # noqa: E402
from cint_ref.respondent_flow import SurveyBinding  # noqa: E402
from fake_cint.s2s import FakeS2S  # noqa: E402

SURVEY = "3f1c9a52-7d1e-4c0b-9a51-2f8e6b1d0a11"
ENTRIES = {"pet": {"answer": "Dog", "question_presented": True,
                   "question_options": ["Fish", "Dog", "Cat"]}}


def main() -> None:
    settings = Settings(database_url=os.environ.get(
        "DATABASE_URL", "postgresql:///edsl_cint_reference_test"))
    Database(settings.database_url).migrate()
    bindings = {SURVEY: SurveyBinding(SURVEY)}
    cint = FakeS2S(settings.s2s_api_key)

    def boot():
        app = create_app(settings, bindings, cint.transport)
        return app, TestClient(app, follow_redirects=False)

    def step(label, response):
        loc = response.headers.get("location")
        print(f"  {label:<34} HTTP {response.status_code}"
              + (f" -> {loc}" if loc else f" {response.json()}"))

    for scenario in ("lost acknowledgment", "clean completion"):
        print(f"\n== {scenario}")
        app, client = boot()
        rid = str(uuid.uuid4())
        cint.add_respondent(rid, bindings[SURVEY].entry_url(settings.public_base_url, rid))
        step("entry ?rid=", client.get(f"/cint/s/{SURVEY}/entry", params={"rid": rid}))
        step("save response", client.post(f"/cint/s/{SURVEY}/responses", json={
            "rid": rid, "response_uuid": f"resp-{rid[:8]}", "entries": ENTRIES}))
        if scenario == "lost acknowledgment":
            cint.inject("commit_then_drop")
        step("finish (Cint applies, reply lost)" if scenario.startswith("lost") else "finish",
             client.post(f"/cint/s/{SURVEY}/finish", params={"rid": rid, "status": "complete"}))
        if scenario == "lost acknowledgment":
            print("  -- restart --")
            app, client = boot()
            app.state.flow.recover()
            state = client.get(f"/cint/sessions/{rid}").json()
            print(f"  after recovery: state={state['transition_state']} "
                  f"last_http={state['last_transition_http']} confirmed_by={state['confirmed_by']}")
            print(f"  Cint's side: transitions applied = {cint.respondents[rid].applied}")
            print("  -> response kept, outcome left 'unknown', no redirect: needs Cint's rule or an operator")


if __name__ == "__main__":
    main()
