"""ILLUSTRATIVE draft target group payload.

The profiling block reproduces the example in Cint's guide "How to add and update
quotas" (2025-12-18): question 639, options "1", "2", "3", quotas 67% / 33%. What
question 639 means in a given account must be checked against that account's
question library. Identifiers below are placeholders.

Translating a Prolific filter into Cint profiling is a semantic mapping (questions,
options, conditions, quotas), not a field rename. After launch, profiles are changed
with manage_target_group_profiles, which REPLACES all profiles: any profile left out
of the request is deleted.
"""

from __future__ import annotations


def draft_target_group(live_url: str, security_client_id: str, completes_goal: int) -> dict:
    return {
        "name": "EDSL human survey (illustrative)",
        "business_unit_id": 0,                                   # placeholder
        "project_manager_id": "00000000-0000-0000-0000-000000000000",  # placeholder
        "locale": "eng_us",
        "collects_pii": False,
        "completes_goal": completes_goal,                        # Prolific num_participants
        "expected_length_of_interview_minutes": 10,
        "expected_incidence_rate": 0.5,
        "fielding_specification": {
            "start_at": "2026-11-02T00:00:00.000Z",
            "end_at": "2026-11-16T23:59:59.000Z",
        },
        "fielding_assistant_assignment": {},
        # S2S prerequisite: the target group uses security_client_id as client_id.
        "client_id": security_client_id,
        "live_url": live_url,
        "profiling": {
            "profile_adjustment_type": "percentage",
            "profiles": [{
                "object": "regular",
                "quotas_enabled": True,
                "targets": [
                    {"name": "Cat or Dog Owners",
                     "conditions": [{"object": "selection", "question_id": 639, "option": "1"},
                                    {"object": "selection", "question_id": 639, "option": "2"}],
                     "quota": {"name": "Cat or Dog Owners", "completes_goal_percentage": 67.0}},
                    {"name": "Other Pet Owners",
                     "conditions": [{"object": "selection", "question_id": 639, "option": "3"}],
                     "quota": {"name": "Other Pet Owners", "completes_goal_percentage": 33.0}},
                ],
            }],
        },
    }
