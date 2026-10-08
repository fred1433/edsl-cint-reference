# Cint integration reference for EDSL human surveys

This repository is a Cint respondent flow written against EDSL's public human-survey code: it admits a Cint respondent, stores the answers in the row shape that `Results.from_human_responses` already reads, and reports the outcome to Cint server-to-server. Its tests exercise Cint completion failures on a real PostgreSQL, including the hard case where Cint applies a completion and the reply is lost.

**Scope.** Real PostgreSQL and real EDSL code pinned at [`388a479`](https://github.com/expectedparrot/edsl/tree/388a479896c1be520b3c9c7bdce5ac010c1911c8) (7 Oct 2026). Cint is simulated from its public API spec and guides (version 2025-12-18); nothing here has run against live Cint. The backend attachment points are proposed by analogy with EDSL's public client (`edsl/coop/coop.py`); the server they would attach to has not been inspected.

[Run the example](#run-it) · [Principal test: the lost acknowledgment](tests/test_lost_ack.py) · [CI runs](https://github.com/fred1433/edsl-cint-reference/actions/workflows/ci.yml)

## One respondent, end to end

EDSL's hosted survey already serves Prolific participants through the same respondent link. The Cint-specific work is respondent validation, outcome reporting, the return redirect, recruitment state and reconciliation.

1. **Entry** `GET /cint/s/{survey}/entry?rid=` calls `GET s2s.cint.com/fulfillment/respondents/{rid}` and admits only `status == 1` with a `links[].href` equal to this survey's entry link. A valid RID presented to another survey is refused.
2. **Response** is saved once, bound to the RID. The outcome (complete, screenout) is decided on the server from what was saved.
3. **Finish** sends `POST /fulfillment/respondents/transition` with the S2S code and redirects to `https://samplicio.us/s/ClientCallBack.aspx?RID=…` only after a 200. A `?status=complete` from the browser decides nothing.
4. **Webhooks** (`Cint-Signature`) are stored, then acknowledged with `{"event_id": …}`; a redelivery is acknowledged again without a second effect.

Proposed attachment points: survey entry (`admit`), response persistence (`save_response`), finalization (`finalize`), status reporting (`recover`, `GET /cint/sessions/{rid}`), all in [`cint_ref/respondent_flow.py`](cint_ref/respondent_flow.py).

## The lost acknowledgment

Cint's guide says a transition to the status a RID already has returns an error, and that the respondent must not be redirected before a 200. So if Cint applies a completion and the reply is lost, the retry fails and proves nothing. [`test_lost_ack.py`](tests/test_lost_ack.py) reproduces it: the fake commits status 5, drops the reply, the app restarts, the retry gets 422. The test checks that the answers survive, the state stays `unknown`, no confirmation is written, no redirect happens, and the response still converts to EDSL `Results`.

What closes an `unknown` row is explicit: `RespondentFlow.recovery_rule` (empty until Cint confirms which read proves a transition, question 1 below) or `operator_resolve()`, which records who decided. A request that never left the process (connection refused) stays `pending` and is retried safely.

## EDSL's Prolific surface, mapped to Cint

| EDSL (`coop.py`) | Cint Demand API, by operationId | Here |
|---|---|---|
| `create_prolific_study` | `create_project` (202, asynchronous, `Idempotency-Key` required), then `create_target_group` (201, `draft`). The target group carries `live_url` with `rid=[%RID%]` and the S2S `security_client_id` as `client_id`. | implemented, fake |
| `publish_prolific_study` | `create_launch_fielding_run_from_draft_job`: 201 means the job exists, not that the group is live. Poll `get_launch_fielding_run_from_draft_job` until `Completed` (gives `created_fielding_run_id`) or `Failed`. | implemented: never `live` before `Completed` |
| `filters` (`list_prolific_filters`) | `profiling` on the target group: questions, options, conditions, quotas. After launch, `manage_target_group_profiles` replaces **all** profiles; any profile left out is deleted. | [illustrative fixture](cint_ref/audience_fixture.py) |
| `num_participants` | `completes_goal` | fixture |
| `calculate_prolific_study_cost` | `calculate_target_group_feasibility`: estimated completes, timing, suggested CPI, sensitive to incidence rate and interview length. `generate_price_prediction` is conditional: Private Exchange accounts only, otherwise 403. | documented |
| `preflight_prolific_study` | No Cint equivalent. EDSL's preflight checks the deployed survey and the credit balance and is, in its own words, not a funds reservation or spending authorization. A Cint estimate does not replace it. | documented |
| `pause` / `resume` / `stop_prolific_study` | `pause_fielding_run`, `resume_fielding_run`, `complete_fielding_run`: each needs the current ETag in `If-Match`, returns 204, 412 on a stale ETag. Resume only works if the completes goal and end date are not reached. | pause implemented: 412 means reread, then decide again |
| `get_prolific_study_responses` | None: answers stay on the survey host. Cint only receives the outcome (`update_respondent_status`). | EDSL conversion tested |
| `approve` / `reject_prolific_study_submission` | Reconciliation, asynchronous (`processing`, `complete`, `failed`). `post_reconciliations`: rejected completes, RID plus reason code. `post_reconciliations_completes`: the **full** list of valid RIDs for a project; any complete left out becomes terminated. Each RID can be reconciled until the end of the 25th of the following month, Central Time. A reversal recalculates quotas and can put the target group back to live. | documented only |

Respondent-level calls have no Prolific counterpart: `get_respondent_status` and `update_respondent_status` on `s2s.cint.com`. Webhooks are managed with `create_webhook`.

## Differences that change the design

- **Two clients, two hosts.** Demand API: `api.cint.com/v1`, bearer JWT, `Cint-API-Version`. S2S: `s2s.cint.com`, the raw S2S key in `Authorization`, no version header. Separate classes, separate base URLs.
- **Three code systems.** S2S transition codes (complete 5, screenout 2, quality terminate 4, quota full 3), the S2S GET status (only 1 = in survey is documented), and client report codes (10, 20, 30, 40). [`outcomes.py`](cint_ref/outcomes.py) keeps them apart and never reads a GET value as a final outcome.
- **Launch is a job.** A 201 is not a launch.
- **Approval is not per submission.** The approved-completes list replaces, it does not add.
- **Redirect security.** This reference implements Cint's recommended S2S flow and verifies webhook signatures separately; any redirect hashing follows the configuration Cint supplies.

## State in PostgreSQL

[`migrations/001_cint.sql`](migrations/001_cint.sql), four tables, each tied to implemented behaviour:

- `cint_sessions`: primary key `rid` (one admission per RID, bound to one survey); `response_uuid UNIQUE` (one response); a check that no outcome exists without a saved response. `in_flight` is committed before the S2S call, so a crash leaves a trace that `recover()` turns into `unknown`, never into success. `SELECT … FOR UPDATE` on the claim makes concurrent finishes send one transition (tested with four threads).
- `cint_webhook_inbox`: primary key on the CloudEvents `id`; insert and effect share one transaction, and the 200 is sent after the commit.
- `cint_quota_progress`: observed progress, updated only by a newer event time. It never turns an in-progress respondent into quota full.
- `cint_launches`: one launch per target group; the idempotency key is stored before the request so a retry reuses it.

## What the fake Cint does, and on what basis

| Behaviour | Basis |
|---|---|
| GET: 404 unknown RID; 200 with `id`, `status`, `links[].href` | published contract |
| Transition: 200 on success; 404 unknown RID | published contract |
| Same-status retry is an error | published contract ("returns an error instead of HTTP 200 OK") |
| ...and that error is a 422 | assumption to confirm (the guide lists 404 and 422) |
| GET status after a transition (fake returns 99) | assumption to confirm: undocumented, never interpreted |
| Commit then drop the reply; connection refused; unrelated 422; process killed mid-call | fault injection |
| `href` match rule (same scheme, host, path, `rid` value) | local policy |
| Webhook signature `HMAC-SHA256(secret, "t." + body)`, secret used as given | published contract, checked against the spec's own vector |
| Ack body `{"event_id": <CloudEvents id>}` | published contract (exact key shape to confirm) |
| `response_session_id` in session webhooks equals the entry RID | assumption to confirm |
| Signature freshness window (off by default) | local policy |
| Demand: 202 project, 201 draft, 201 job with `Location`, `Processing` / `Completed` / `Failed`, ETag `W/"n"`, 412, 422, 204 | published contract |
| Missing `Idempotency-Key` returns 400 | assumption (required is published, the error code is not) |

## Five questions for Cint

1. **Ambiguous transition.** If a transition is applied but its 200 is lost, which read or retry result allows returning the respondent? Which GET values follow each final status, and is an already-applied transition distinguishable from other 422s?
2. **Account security configuration.** Which S2S key and `security_client_id` are provisioned, is hashed redirect also enabled, and which S2S code should a security termination use (the guides name it; the transition table lists four codes)?
3. **Respondents in progress.** What should be sent for a respondent admitted before a pause, a `complete_fielding_run` or a filled quota, who finishes afterwards?
4. **Pricing and limits.** Which pricing model and currency apply to the business unit, is Private Exchange enabled, and which limits govern recruitment spend?
5. **Webhook retry signatures.** Is `t` re-signed on each delivery attempt, and what freshness tolerance do you recommend for receivers?

**Next step once credentials exist:** a draft target group allocated only to Cint's test supplier (ID 980, guide "How to use the test supplier"), each status exercised, and the IDs shared with Cint for verification as the S2S guide asks before going live.

## Sources (pinned)

- Cint Demand API spec, version `2025-12-18`: [demand_openapi_v1_2025-12-18.yaml](https://developer.cint.com/demand/specs/demand_openapi_v1_2025-12-18.yaml), retrieved 2026-10-08 12:11 UTC, SHA-256 `008c54ee13d77341026cb9d5cdd06d2493350da66923b243e6d70f79ee904940`.
- Cint guides (2025-12-18): [server-to-server](https://developer.cint.com/demand/docs/2025-12-18/core-workflows/how-to-server-to-server-api), [respondent journey](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/survey_respondent_management/respondent-journey-concept), [webhooks](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/understanding-webhooks-concept), [response codes](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/response_status_codes/response-codes), [reconciliations](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/reconciliations/understanding-reconciliations), [ETag](https://developer.cint.com/demand/docs/2025-12-18/getting-started/fundamentals/if-match-etag), [idempotency keys](https://developer.cint.com/demand/docs/2025-12-18/getting-started/fundamentals/idempotency-keys), [quotas](https://developer.cint.com/demand/docs/2025-12-18/core-workflows/profiles/how-to-add-quotas), [feasibility](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/feasibility-pricing/how-does-feasibility-works-concept), [test supplier](https://developer.cint.com/demand/docs/2025-12-18/core-workflows/how-to-use-test-supplier).
- EDSL at `388a479896c1be520b3c9c7bdce5ac010c1911c8`: [`edsl/coop/coop.py`](https://github.com/expectedparrot/edsl/blob/388a479896c1be520b3c9c7bdce5ac010c1911c8/edsl/coop/coop.py) (Prolific methods), [`edsl/results/human_responses.py`](https://github.com/expectedparrot/edsl/blob/388a479896c1be520b3c9c7bdce5ac010c1911c8/edsl/results/human_responses.py) (`HumanResponseRow`: unknown top-level keys are ignored, so Cint provenance goes in `agent_traits_json_string`; the trait names `cint_rid`, `cint_outcome` and the value `external_platform: "cint"` are proposals).

## Run it

```bash
createdb edsl_cint_reference_test
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATABASE_URL=postgresql:///edsl_cint_reference_test .venv/bin/python -m pytest -v
DATABASE_URL=postgresql:///edsl_cint_reference_test .venv/bin/python examples/run_example.py
```

CI runs the same suite against a PostgreSQL 16 service container, then the example.

Built with Claude Code. No access to Cint or to any non-public server; no real credentials, no model calls.
