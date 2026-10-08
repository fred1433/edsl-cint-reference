"""Turn a stored Cint session into the row shape EDSL already reads.

EDSL's HumanResponseRow (edsl/results/human_responses.py at the pinned commit) keeps
arbitrary traits in agent_traits_json_string and ignores unknown top-level keys, so
Cint provenance goes in the traits. The trait names below are PROPOSED; only
agent.external_platform is documented by EDSL (humanize docs), with Prolific values.
"""

from __future__ import annotations

import json
from typing import Any


def to_human_response_row(session: dict[str, Any]) -> dict[str, Any]:
    traits = {
        "external_platform": "cint",           # documented trait, proposed value
        "cint_rid": str(session["rid"]),       # proposed
        "cint_outcome": session["outcome"],    # proposed
    }
    return {
        "response_uuid": session["response_uuid"],
        "response_json_string": json.dumps(session["response_entries"]),
        "agent_traits_json_string": json.dumps(traits),
        "scenario_json_string": (
            json.dumps(session["scenario"]) if session["scenario"] is not None else None
        ),
    }
