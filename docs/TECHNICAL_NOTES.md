# Technical note

Everything the [README](../README.md) summarizes, with its source or its label: **published contract** (Cint spec or guide, version 2025-12-18), **fault injection**, **local policy**, or **assumption to confirm**.

## Mapping in detail

| EDSL (`coop.py`) | Cint Demand API | Here |
|---|---|---|
| `create_prolific_study` | `create_project`: 202, asynchronous, `Idempotency-Key` required. Then `create_target_group`: 201, status `draft`. The target group carries `live_url` with `rid=[%RID%]` and the S2S `security_client_id` as `client_id` (`TargetGroupClientID`, an integer). The schema requires exactly one of `allocations` or `allocation_template`; the fixture allocates only to Cint's test supplier 980 with open-exchange contribution at 0%, as in the test-supplier guide. | implemented; [fixture](../cint_ref/audience_fixture.py) validated against [the extracted schema](../contracts/create_draft_target_group.json) |
| `publish_prolific_study` | `create_launch_fielding_run_from_draft_job`: 201 means the job exists, not that the group is live. Poll `get_launch_fielding_run_from_draft_job` until `Completed` (gives `created_fielding_run_id`) or `Failed`. | implemented |
| `filters` (`list_prolific_filters`) | `profiling`: questions, options, conditions, quotas. A semantic mapping, not a field rename. After launch, `manage_target_group_profiles` replaces **all** profiles; any profile left out is deleted. | illustrative fixture (question 639 from Cint's quotas guide) |
| `num_participants` | `completes_goal`. The fixture sets `filling_strategy: "completes"`: per the spec, when it is left out and no Fielding Assistant module is on, the strategy is `prescreens`, so respondents who pass screening would count toward the goal. | fixture |
| `calculate_prolific_study_cost` | `calculate_target_group_feasibility`: for a draft described in the request (including `fielding_specification`), estimated achievable completes and a suggested price; accuracy depends on incidence rate, interview length and time in field. `generate_price_prediction` is conditional: Private Exchange accounts only, otherwise 403. | documented |
| `preflight_prolific_study` | No Cint equivalent. EDSL's preflight checks the deployed survey and the credit balance and is, in its own words, not a funds reservation or spending authorization. A Cint estimate does not replace it. | documented |
| `pause` / `resume` / `stop_prolific_study` | `pause_fielding_run`, `resume_fielding_run`, `complete_fielding_run`: current ETag in `If-Match`, 204, 412 on a stale ETag. Resume only if the completes goal and end date are not reached. | pause implemented: 412 means reread, then decide again |
| `get_prolific_study_responses` | None: answers stay on the survey host. Cint only receives the outcome (`update_respondent_status`). | EDSL conversion tested |
| `approve` / `reject_prolific_study_submission` | Reconciliation, asynchronous (`processing`, `complete`, `failed`). `post_reconciliations`: per-RID positive or negative reconciliation, each line a RID and a reason code; one file can mix both. `post_reconciliations_completes`: RIDs to keep as completes for a project; per the reconciliation guide this is the **full** list, and any complete left out becomes terminated. A complete stands unless reversed. Each RID can be reconciled until the end of the 25th of the following month, Central Time. A reversal recalculates quotas and can put the target group back to live. | documented only |

Respondent-level calls have no Prolific counterpart: `get_respondent_status` and `update_respondent_status` on `s2s.cint.com`. Webhooks are managed with `create_webhook`.

## Differences that change the design

- **Two clients, two hosts.** Demand API: `api.cint.com/v1`, bearer JWT, `Cint-API-Version`. S2S: `s2s.cint.com`, the raw S2S key in `Authorization`, no version header.
- **Three code systems.** S2S transition codes (complete 5, screenout 2, quality terminate 4, quota full 3), the S2S GET status (only 1 = in survey is documented), client report codes (10, 20, 30, 40). [`outcomes.py`](../cint_ref/outcomes.py) keeps them apart and never reads a GET value as a final outcome. Pairing 5 with 10 and so on is by name (assumption), used only to read reports.
- **Launch is a job.** A 201 is not a launch.
- **Redirect security.** This reference implements Cint's recommended S2S flow and verifies webhook signatures separately; any redirect hashing follows the configuration Cint supplies.

## Decisions, attempts and returns

- `save_response` validates entries with EDSL's own parser (`ENTRIES` in `edsl/results/human_responses.py`), stores them, and decides nothing.
- `decide` asks the host hook; `record_outcome` takes a host decision directly (quality review, quota logic). First decision wins; a different later one is appended to `outcome_conflicts`. `complete_when_answered` is an **example** policy used by the tests and the example.
- Each claim sets `attempt_token`. Settlement is `UPDATE … WHERE attempt_token = … RETURNING *`; the HTTP answer comes from the committed row. A reply for an attempt that no longer owns the session goes to `late_replies` and changes nothing else (tested: expired attempt, operator decision, then the delayed 200).
- A request that never left the process (connection refused) is not counted as an attempt.
- `operator_resolve` classifies an `unknown` or `failed` transition and records `resolved_by` and `resolved_at`. It does not authorize a return. `authorize_return` does, as a **local policy not validated by Cint**. `recovery_rule` (empty) is where a Cint-confirmed recovery rule would plug in.
- Not implemented: returning a respondent refused at entry. It needs a host decision and a security-termination code Cint has not specified (question 2).

## State in PostgreSQL

[`001_cint.sql`](../migrations/001_cint.sql) and [`002_decisions_attempts.sql`](../migrations/002_decisions_attempts.sql), four tables, each tied to implemented behaviour:

- `cint_sessions`: primary key `rid` (one admission per RID, bound to one survey); `response_uuid UNIQUE` (a reuse across RIDs is a 409); no outcome without a saved response; no return authorization without a confirmed transition. `in_flight` and its `attempt_token` are committed before the S2S call, so a crash leaves a trace that `recover()` turns into `unknown`, never into success. `SELECT … FOR UPDATE` on the claim makes concurrent finishes send one transition (tested with four threads).
- `cint_webhook_inbox`: primary key on the CloudEvents `id`; insert and projection share one transaction, and the 200 is sent after the commit. `projection` is `applied`, `unmatched` (valid, no session yet: replayed at admission) or `ignored`. Session fields keep their own sequence: `observed_client_status_seq` moves only with a status change.
- `cint_quota_progress`: observed progress, updated only by a newer event time. It never turns an in-progress respondent into quota full.
- `cint_launches`: one launch per target group; the full launch request and its idempotency key are stored before sending, a retry with a different request is refused, and every local write is conditional on the state and job the caller read.

## What the fake Cint does, and on what basis

| Behaviour | Basis |
|---|---|
| GET: 404 unknown RID; 200 with `id`, `status`, `links[].href` | published contract |
| Transition: 200 on success; 404 unknown RID | published contract |
| Same-status retry is an error | published contract ("returns an error instead of HTTP 200 OK") |
| ...and that error is a 422 | assumption to confirm (the guide lists 404 and 422) |
| GET status after a transition (fake returns 99) | assumption to confirm: undocumented, never interpreted |
| Commit then drop the reply; connection refused; unrelated 422; process killed mid-call; reply held until released | fault injection |
| `href` match rule (same scheme, host, path, `rid` value) | local policy |
| Webhook signature `HMAC-SHA256(secret, "t." + body)`, secret used as given | published contract, checked against the spec's own vector |
| Ack body `{"event_id": <CloudEvents id>}` | published contract (exact key shape to confirm) |
| `response_session_id` equals the entry RID (if not a UUID, the event is stored without effect and still acknowledged) | assumption to confirm |
| Signature freshness window (off by default; when on, also rejects `t` more than 60 s in the future) | local policy |
| Draft target group validated against the schema extracted from the pinned spec (`oneOf`, nested types, `nullable`) | published contract |
| 202 project, 201 draft, 201 job with `Location`, `Processing` / `Completed` / `Failed`, ETag `W/"n"`, 412, 422, 204 | published contract |
| Project creation and launch jobs deduplicated by `Idempotency-Key`; **draft creation is not** | published (project, launch); local (draft not modelled) |
| Missing `Idempotency-Key` or invalid body returns 400 | assumption (required is published, the error code is not) |

The schema file is produced by [`scripts/extract_contract.py`](../scripts/extract_contract.py), which refuses a spec whose SHA-256 differs from the pinned one.

## Questions for Cint

1. **Ambiguous transition** (in the README).
2. **Account security configuration.** Which S2S key and `security_client_id` are provisioned, is hashed redirect also enabled, and which S2S code should a security termination use (the guides name it; the transition table lists four codes)? What should the host send for a respondent refused at entry?
3. **Respondents in progress.** What should be sent for a respondent admitted before a pause, a `complete_fielding_run` or a filled quota, who finishes afterwards?
4. **Pricing and limits.** Which pricing model and currency apply to the business unit, is Private Exchange enabled, and which limits govern recruitment spend?
5. **Webhook retry signatures.** Is `t` re-signed on each delivery attempt, and what freshness tolerance do you recommend for receivers?

**Next step once credentials exist:** the fixture's draft target group, allocated only to test supplier 980, each status exercised, and the IDs shared with Cint for verification as the S2S guide asks before going live.

## Sources (pinned)

- Cint Demand API spec, version `2025-12-18`: [demand_openapi_v1_2025-12-18.yaml](https://developer.cint.com/demand/specs/demand_openapi_v1_2025-12-18.yaml), retrieved 2026-10-08 12:11 UTC, SHA-256 `008c54ee13d77341026cb9d5cdd06d2493350da66923b243e6d70f79ee904940`.
- Cint guides (2025-12-18): [server-to-server](https://developer.cint.com/demand/docs/2025-12-18/core-workflows/how-to-server-to-server-api), [respondent journey](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/survey_respondent_management/respondent-journey-concept), [webhooks](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/understanding-webhooks-concept), [response codes](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/response_status_codes/response-codes), [reconciliations](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/reconciliations/understanding-reconciliations), [ETag](https://developer.cint.com/demand/docs/2025-12-18/getting-started/fundamentals/if-match-etag), [idempotency keys](https://developer.cint.com/demand/docs/2025-12-18/getting-started/fundamentals/idempotency-keys), [quotas](https://developer.cint.com/demand/docs/2025-12-18/core-workflows/profiles/how-to-add-quotas), [feasibility](https://developer.cint.com/demand/docs/2025-12-18/deep-dives/feasibility-pricing/how-does-feasibility-works-concept), [test supplier](https://developer.cint.com/demand/docs/2025-12-18/core-workflows/how-to-use-test-supplier).
- EDSL at `388a479896c1be520b3c9c7bdce5ac010c1911c8`: [`edsl/coop/coop.py`](https://github.com/expectedparrot/edsl/blob/388a479896c1be520b3c9c7bdce5ac010c1911c8/edsl/coop/coop.py) (Prolific methods), [`edsl/results/human_responses.py`](https://github.com/expectedparrot/edsl/blob/388a479896c1be520b3c9c7bdce5ac010c1911c8/edsl/results/human_responses.py) (`HumanResponseRow` ignores unknown top-level keys, so Cint provenance goes in `agent_traits_json_string`; `external_platform: "cint"`, `cint_rid`, `cint_intended_outcome` and `cint_transition_state` are proposals).
