"""Internal outcomes and the separate code systems Cint uses for them.

Three vocabularies must not be mixed:

* S2S transition codes, sent to POST /fulfillment/respondents/transition
  (operationId update_respondent_status): Complete 5, Screenout 2,
  Quality Terminate 4, Quota Full 3.
* The status returned by GET /fulfillment/respondents/{RID}
  (get_respondent_status). The guide documents only 1 = "In Survey/Drop" and says
  these codes differ from the transition codes. No terminal mapping is published,
  so this module refuses to interpret any other value.
* Client response codes used in reports: Completed 10, Term 20, Quality Term 30,
  Overquota 40 (deep-dives/response_status_codes/response-codes).
"""

from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    COMPLETE = "complete"
    SCREENOUT = "screenout"
    QUALITY_TERMINATE = "quality_terminate"
    QUOTA_FULL = "quota_full"


_S2S_TRANSITION = {
    Outcome.COMPLETE: 5,
    Outcome.SCREENOUT: 2,
    Outcome.QUALITY_TERMINATE: 4,
    Outcome.QUOTA_FULL: 3,
}

# Pairing by name between two published tables; Cint does not publish this pairing
# as such. Used only to read reports and session webhooks, never to send anything.
_CLIENT_REPORT = {
    Outcome.COMPLETE: 10,
    Outcome.SCREENOUT: 20,
    Outcome.QUALITY_TERMINATE: 30,
    Outcome.QUOTA_FULL: 40,
}

S2S_GET_IN_SURVEY = 1


def to_s2s_transition_code(outcome: Outcome) -> int:
    return _S2S_TRANSITION[Outcome(outcome)]


def to_client_report_code(outcome: Outcome) -> int:
    return _CLIENT_REPORT[Outcome(outcome)]


def s2s_get_allows_entry(status: object) -> bool:
    """True only for the one documented GET value. Anything else is not interpreted."""
    return status == S2S_GET_IN_SURVEY
