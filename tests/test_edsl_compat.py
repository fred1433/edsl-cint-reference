"""A Cint-recruited response and a Prolific-shaped one, through pinned EDSL code."""

import json

from edsl.results import Results

from cint_ref.edsl_bridge import to_human_response_row
from tests.conftest import ANSWERS, SCENARIO


def prolific_shaped_row():
    # EDSL documents agent.external_platform and agent.prolific_* (submission, study
    # and participant IDs). The exact prolific_* key names here are illustrative.
    return {
        "response_uuid": "resp-prolific-1",
        "response_json_string": json.dumps({
            "pet": {"answer": "Cat", "question_presented": True,
                    "question_options": ["Cat", "Dog", "Fish", "None"]},
            "why_no_pet": {"answer": None, "question_presented": False},
            "brand_opinion": {"answer": "Bad", "question_presented": True},
        }),
        "agent_traits_json_string": json.dumps({
            "external_platform": "prolific",
            "prolific_submission_id": "sub-1", "prolific_study_id": "study-1",
            "prolific_participant_id": "part-1",
        }),
        "scenario_json_string": json.dumps(SCENARIO),
    }


def test_cint_and_prolific_responses_become_edsl_results(h, edsl_survey):
    rid = h.answered()
    cint_row = to_human_response_row(h.flow.session_state(rid))
    results = Results.from_human_responses(edsl_survey, [cint_row, prolific_shaped_row()])

    rows = results.select(
        "agent.agent_name", "agent.external_platform", "agent.cint_rid",
        "agent.prolific_submission_id", "answer.pet", "answer.why_no_pet",
        "answer.brand_opinion", "comment.brand_opinion_comment", "scenario.brand",
        "raw_model_response.why_no_pet_question_presented",
        "raw_model_response.pet_answered_at", "question_options.pet_question_options",
    ).to_dicts(remove_prefix=False)
    cint, prolific = rows

    assert cint["agent.agent_name"] == f"resp-{rid[:8]}"
    assert cint["agent.external_platform"] == "cint"
    assert cint["agent.cint_rid"] == rid
    assert cint["answer.pet"] == "Dog"
    assert cint["answer.why_no_pet"] is None
    assert cint["raw_model_response.why_no_pet_question_presented"] is False
    assert cint["raw_model_response.pet_answered_at"] == ANSWERS["pet"]["answered_at"]
    assert cint["question_options.pet_question_options"] == ["Fish", "Dog", "Cat"]  # as shown
    assert cint["comment.brand_opinion_comment"] == "bought it twice"
    assert cint["scenario.brand"] == "Acme Kibble"

    assert prolific["agent.external_platform"] == "prolific"
    assert prolific["agent.prolific_submission_id"] == "sub-1"
    assert prolific["answer.pet"] == "Cat"
    assert prolific["raw_model_response.why_no_pet_question_presented"] is False
