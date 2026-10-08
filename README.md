# Cint integration reference for EDSL human surveys

This repository is a Cint respondent flow written against EDSL's public human-survey code: it admits a Cint respondent, stores answers that EDSL's own parser accepts, and reports the host's decision to Cint server-to-server. Its tests exercise Cint completion failures on a real PostgreSQL, including the hard case where Cint applies a completion and the reply is lost.

**Scope.** Real PostgreSQL and real EDSL code pinned at [`388a479`](https://github.com/expectedparrot/edsl/tree/388a479896c1be520b3c9c7bdce5ac010c1911c8) (7 Oct 2026). Cint is simulated from its public API spec and guides (version 2025-12-18); nothing here has run against live Cint. The backend attachment points are proposed by analogy with EDSL's public client (`edsl/coop/coop.py`); the server they would attach to has not been inspected.

[Run the example](#run-it) · [Principal test: the lost acknowledgment](tests/test_lost_ack.py) · [CI runs](https://github.com/fred1433/edsl-cint-reference/actions/workflows/ci.yml) · [Technical note](docs/TECHNICAL_NOTES.md)

## What is guaranteed, and where it stops

- **No completion without a host decision.** Saving a response decides nothing. Only a trusted host decision (`SurveyBinding.decide` or `record_outcome`) fixes an outcome, the first one wins, and a later different one is kept as a visible conflict. Entries that EDSL's `HumanResponseEntry` parser rejects are refused; `{}` is stored but never reported as complete.
- **Intended outcome is not confirmed outcome.** The host's decision and the state of the Cint transition are stored, and exported to EDSL, separately.
- **Redirect after a 200, with one labelled exception.** The respondent goes back to Cint after a successful transition call. An operator can also authorize the return (`authorize_return`); that is a local policy Cint has not validated, recorded apart from the operator's classification.
- **Refused entries are refused, not returned.** A wrong entry link or a non-admissible status gets a 403. Sending that respondent back to Cint is a host attachment point left unimplemented: it needs a security-termination code Cint has not specified.

## One respondent, end to end

In EDSL's Prolific flow the survey is already hosted: `create_prolific_study` returns a `respondent_url` (`coop.py`). The Cint-specific work is respondent validation, outcome reporting, the return redirect, recruitment state and reconciliation.

1. **Entry** validates `?rid=` with `GET s2s.cint.com/fulfillment/respondents/{rid}`: `status == 1` and a `links[].href` equal to this survey's entry link.
2. **Response** is saved once, bound to the RID.
3. **Host decision**: complete, screenout, quality terminate or quota full, or "not ready".
4. **Finish** sends `POST /fulfillment/respondents/transition` (codes 5, 2, 4, 3). Each attempt carries a token; a reply that arrives after its attempt lost ownership is recorded and never applied.
5. **Webhooks** (`Cint-Signature`) are stored, then acknowledged; redeliveries have no second effect.

Attachment points, all in [`respondent_flow.py`](cint_ref/respondent_flow.py): survey entry (`admit`), response persistence (`save_response`), host decision (`decide`, `record_outcome`), reporting (`finalize`, `recover`).

## The lost acknowledgment

Cint's guide says a transition to the status a RID already has returns an error, and that the respondent must not be redirected before a 200. If Cint applies a completion and the reply is lost, the retry fails and proves nothing. [`test_lost_ack.py`](tests/test_lost_ack.py) reproduces it: the fake commits status 5, drops the reply, the app restarts, the retry gets 422. The answers survive, the state stays `unknown`, nothing is confirmed, nobody is redirected, and EDSL `Results` carry both the answers and `cint_transition_state = unknown`.

The question this leaves for Cint, and the reason `unknown` is deliberate: *if a transition is applied but its 200 is lost, which read or retry result allows returning the respondent? Which GET values follow each final status, and is an already-applied transition distinguishable from other 422s?* Four more questions are in the [technical note](docs/TECHNICAL_NOTES.md#questions-for-cint).

## EDSL's Prolific surface, mapped to Cint

| EDSL (`coop.py`) | Cint, by operationId | Here |
|---|---|---|
| `create_prolific_study` | `create_project` (202), then `create_target_group` (201, draft) | implemented; request validated against the pinned schema |
| `publish_prolific_study` | `create_launch_fielding_run_from_draft_job`, polled until `Completed` or `Failed` | never `live` before `Completed` |
| `filters` | `profiling`: questions, options, conditions, quotas | illustrative fixture |
| `num_participants` | `completes_goal` with `filling_strategy: "completes"` (left out, it defaults to counting prescreens) | fixture |
| `calculate_prolific_study_cost` | `calculate_target_group_feasibility`: estimated achievable completes and a suggested price for the requested fielding window | documented |
| `pause` / `resume` / `stop` | `pause_fielding_run`, `resume_fielding_run`, `complete_fielding_run`, with `If-Match` | pause implemented |
| `approve` / `reject` | `post_reconciliations`, `post_reconciliations_completes` | documented |

Details, including reconciliation semantics, are in the [technical note](docs/TECHNICAL_NOTES.md#mapping-in-detail).

## What the EDSL test proves

[`test_edsl_compat.py`](tests/test_edsl_compat.py) saves a response through the app, reads it back from PostgreSQL and passes it to the pinned `Results.from_human_responses`, next to a Prolific-shaped row. It checks the options in the order the respondent saw them, a skipped question, per-question timestamps, comments, the scenario, and provider provenance (`external_platform`, `cint_rid`). The Cint trait names are proposals.

## Run it

```bash
createdb edsl_cint_reference_test
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATABASE_URL=postgresql:///edsl_cint_reference_test .venv/bin/python -m pytest -v
DATABASE_URL=postgresql:///edsl_cint_reference_test .venv/bin/python examples/run_example.py
```

CI runs the same suite against a PostgreSQL 16 service container, then the example. MIT licensed.

Built with Claude Code. No access to Cint or to any non-public server; no real credentials, no model calls.
